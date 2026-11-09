"""Integration gates for the active Orders migration, never application databases."""

import ast
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from app.platform.catalog.orm import MarketplaceOfferRow, MarketplaceProductRow
from tests import test_orders_schema_candidate as candidate

cluster = candidate.cluster
MUTABLE = {"order_sync_runs", "marketplace_orders", "marketplace_order_items"}


@pytest.fixture(scope="module")
def db(cluster):
    """Actual shared grant script uses latest schema; Orders feature stays 0062."""
    role = "orders_script_" + uuid4().hex
    with candidate.disposable_database(cluster, (role,)) as database:
        result = candidate.migrate(database.url, "upgrade", "head")
        assert result.returncode == 0, result.stderr
        owner = create_engine(database.url)
        runtime = create_engine(owner.url.set(username=role))
        try:
            with owner.begin() as c:
                c.exec_driver_sql("INSERT INTO lk_organizations(organization_id,slug,name) VALUES (91001,'orders-script-one','Synthetic'),(91002,'orders-script-two','Synthetic')")
                c.exec_driver_sql("""INSERT INTO marketplace_accounts(marketplace_account_id,organization_id,marketplace,external_account_id,status)
                    VALUES (91101,91001,'avito','synthetic-a','connected'),(91102,91001,'avito','synthetic-b','connected'),(91201,91002,'wb','synthetic-c','connected')""")
            yield owner, runtime
        finally:
            runtime.dispose()
            owner.dispose()


def runtime_script(owner, role, *, fail_after_broad=False):
    # Synthetic one-use value is passed over stdin, never command line or logs.
    url = owner.url
    script = f"\\set runtime_role {role}\n\\set owner_role {url.username}\n\\set runtime_database {url.database}\n\\set runtime_password {uuid4().hex}\n"
    script += (candidate.ROOT / "ops/runtime-db-role.sql").read_text()
    if fail_after_broad:
        # Fault injection into the actual script, just after the broad grants.
        script = script.replace("-- Orders overrides", "SELECT 1/0;\n-- Orders overrides", 1)
    return candidate.subprocess.run(
        ["psql", "-X", "-w", "-v", "ON_ERROR_STOP=1", "-h", url.query["host"],
         "-p", str(url.query["port"]), "-U", url.username, "-d", url.database],
        input=script, env=candidate.isolated_environment(), capture_output=True,
        text=True, timeout=60, check=False,
    )


def assert_narrow_privileges(connection, role):
    for table in candidate.TABLES:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
            actual = connection.execute(text("SELECT has_table_privilege(:role,:table,:privilege)"),
                                        {"role": role, "table": table, "privilege": privilege}).scalar_one()
            assert actual == (privilege in {"SELECT", "INSERT"} or (privilege == "UPDATE" and table in MUTABLE)), (table, privilege)
        sequence = connection.execute(text("""SELECT pg_get_serial_sequence(:table, a.attname)
            FROM pg_attribute a WHERE a.attrelid=to_regclass(:table) AND a.attidentity<>''"""),
            {"table": table}).scalar_one()
        assert connection.execute(text("SELECT has_sequence_privilege(:role,:sequence,'USAGE')"),
                                  {"role": role, "sequence": sequence}).scalar_one()
        assert not connection.execute(text("SELECT has_sequence_privilege(:role,:sequence,'UPDATE')"),
                                      {"role": role, "sequence": sequence}).scalar_one()


def test_runtime_script_limits_orders_privileges(db):
    owner, runtime = db
    result = runtime_script(owner, runtime.url.username)
    assert result.returncode == 0, result.stderr
    with owner.connect() as connection:
        assert_narrow_privileges(connection, runtime.url.username)


def test_failed_runtime_script_cannot_leave_intermediate_broad_grants(db):
    owner, runtime = db
    assert runtime_script(owner, runtime.url.username).returncode == 0
    result = runtime_script(owner, runtime.url.username, fail_after_broad=True)
    assert result.returncode != 0 and "division by zero" in result.stderr
    with owner.connect() as c:
        assert_narrow_privileges(c, runtime.url.username)


def test_upgrade_narrows_inherited_defaults_without_widening_select_only_role(cluster):
    broad, reader = "orders_broad_" + uuid4().hex, "orders_reader_" + uuid4().hex
    with candidate.disposable_database(cluster, (broad, reader)) as database:
        result = candidate.migrate(database.url, "upgrade", "20260908_0061")
        assert result.returncode == 0, result.stderr
        engine = create_engine(database.url)
        try:
            with engine.begin() as c:
                c.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES GRANT ALL ON TABLES TO {broad}")
                c.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES GRANT ALL ON SEQUENCES TO {broad}")
                c.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES GRANT SELECT ON TABLES TO {reader} WITH GRANT OPTION")
                c.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES GRANT SELECT ON SEQUENCES TO {reader}")
                c.exec_driver_sql("ALTER DEFAULT PRIVILEGES GRANT SELECT ON SEQUENCES TO PUBLIC")
                before = c.exec_driver_sql("SELECT defaclobjtype,defaclacl::text FROM pg_default_acl ORDER BY oid").all()
                catalog_before = c.exec_driver_sql("SELECT relname,relacl::text FROM pg_class WHERE relname IN ('marketplace_products','marketplace_offers') ORDER BY relname").all()
            result = candidate.migrate(database.url, "upgrade", "20260909_0062")
            assert result.returncode == 0, result.stderr
            with engine.connect() as c:
                assert_narrow_privileges(c, broad)
                for table in candidate.TABLES:
                    for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "SELECT WITH GRANT OPTION"):
                        assert not c.execute(text("SELECT has_table_privilege(:role,:table,:privilege)"), {"role": reader, "table": table, "privilege": privilege}).scalar_one()
                    assert c.execute(text("SELECT has_table_privilege(:role,:table,'SELECT')"), {"role": reader, "table": table}).scalar_one()
                assert c.exec_driver_sql("SELECT defaclobjtype,defaclacl::text FROM pg_default_acl ORDER BY oid").all() == before
                assert c.exec_driver_sql("SELECT relname,relacl::text FROM pg_class WHERE relname IN ('marketplace_products','marketplace_offers') ORDER BY relname").all() == catalog_before
                assert c.execute(text("""SELECT count(*) FROM pg_class c, LATERAL aclexplode(c.relacl) a
                    WHERE c.relkind='S' AND c.oid IN (
                        SELECT d.objid FROM pg_depend d JOIN pg_class t ON t.oid=d.refobjid
                        WHERE t.relname=ANY(:tables) AND d.deptype='i' AND d.classid='pg_class'::regclass)
                    AND (a.grantee=0 OR a.grantee=(SELECT oid FROM pg_roles WHERE rolname=:reader))"""),
                    {"tables": list(candidate.TABLES), "reader": reader}).scalar_one() == 0
        finally:
            engine.dispose()


def test_actual_chain_has_one_head_containing_orders_revision():
    config = Config(str(candidate.ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(candidate.ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    heads = scripts.get_heads()
    assert len(heads) == 1
    assert scripts.get_revision("20260909_0062").down_revision == "20260908_0061"
    assert "20260909_0062" in {revision.revision for revision in scripts.iterate_revisions(heads[0], "base")}


def _credential_calls(function):
    """Syntactic inventory only; setup admission separately checks block shape."""
    return [node for node in ast.walk(function) if isinstance(node, ast.Call)]


def _credential_setup_blocks(function):
    """Only the fixture's straight-line with/try setup spine is admitted."""
    def blocks(statements):
        yield statements
        for statement in statements:
            if isinstance(statement, ast.With) or (
                isinstance(statement, ast.Try) and not statement.handlers and not statement.orelse
            ):
                yield from blocks(statement.body)

    return list(blocks(function.body))


def _credential_migration_calls(function):
    # Conservative: even an extra command call in cleanup or a disconnected
    # definition is outside the approved shared sequence. Do not execute source.
    return [
        node for node in _credential_calls(function)
        if isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "command"
    ]


def _assert_credential_fixture_admission(tree):
    functions = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    fixture = functions["disposable_postgres"]
    assert not _credential_migration_calls(fixture)
    assert len(fixture.body) == 1 and isinstance(fixture.body[0], ast.If)
    dispatch = fixture.body[0]
    assert ast.dump(dispatch.test) == ast.dump(
        ast.parse("os.environ.get('ORDERS_TEST_USE_LOCAL_CLUSTER') == '1'", mode="eval").body
    )
    for branch, generator, creates_role in (
        (dispatch.body, "_local_postgres", False),
        (dispatch.orelse, "_native_postgres", True),
    ):
        assert len(branch) == 1 and isinstance(branch[0], ast.Expr)
        delegation = branch[0].value
        assert isinstance(delegation, ast.YieldFrom)
        call = delegation.value
        assert isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
        assert call.func.id == generator
        setup = functions[generator]
        assert not _credential_migration_calls(setup)
        # Fail closed on early exits, including conditional returns before the
        # setup try. This is a bounded fixture shape, not a Python CFG analyzer.
        assert not any(isinstance(node, ast.Return) for node in ast.walk(setup))
        yields = [node for node in ast.walk(setup) if isinstance(node, (ast.Yield, ast.YieldFrom))]
        assert len(yields) == 1 and isinstance(yields[0], ast.Yield)
        blocks = [
            block for block in _credential_setup_blocks(setup)
            if any(isinstance(statement, ast.Expr) and statement.value is yields[0] for statement in block)
        ]
        assert len(blocks) == 1
        block = blocks[0]
        assert [type(statement) for statement in block] == [ast.Expr, ast.Assign, ast.Expr]
        bootstrap = block[0].value
        assert isinstance(bootstrap, ast.Call) and isinstance(bootstrap.func, ast.Name)
        assert bootstrap.func.id == "_bootstrap_postgres"
        assert [
            call for call in _credential_calls(setup)
            if isinstance(call.func, ast.Name) and call.func.id == "_bootstrap_postgres"
        ] == [bootstrap]
        assert block[-1].value is yields[0]
        role_arguments = [
            keyword.value for keyword in bootstrap.keywords
            if keyword.arg == "create_runtime_role"
        ]
        assert len(role_arguments) == 1 and isinstance(role_arguments[0], ast.Constant)
        assert role_arguments[0].value is creates_role

    shared = functions["_bootstrap_postgres"]
    assert not any(isinstance(node, ast.Return) for node in ast.walk(shared))
    migrations = _credential_migration_calls(shared)
    blocks = [
        block for block in _credential_setup_blocks(shared)
        if len(block) == 4 and all(
            isinstance(statement, ast.Expr) and statement.value in migrations
            for statement in block
        )
    ]
    assert len(migrations) == 4 and len(blocks) == 1
    sequence = []
    for statement in blocks[0]:
        call = statement.value
        assert len(call.args) == 2 and isinstance(call.args[1], ast.Constant)
        sequence.append((call.func.attr, call.args[1].value))
    assert sequence == [
        ("stamp", "20260905_0060"),
        ("upgrade", "20260908_0061"),
        ("downgrade", "20260905_0060"),
        ("upgrade", "20260908_0061"),
    ]


def test_isolated_credential_fixture_pins_its_supported_revision():
    # Follow actual local/native dispatch to the shared bootstrap. This remains
    # a pure source-admission regression, not execution of either PG fixture.
    tree = ast.parse((candidate.ROOT / "tests/test_marketplace_credential_rls.py").read_text())
    _assert_credential_fixture_admission(tree)


@pytest.mark.parametrize("mutation", [
    "head", "wrong_revision", "wrong_downgrade", "bypass_local", "bypass_native",
    "bypass_local_dispatch", "bypass_native_dispatch", "disconnected_local_bootstrap",
    "early_return_local", "early_return_native",
    "external_head_local", "external_head_native",
    "external_wrong_revision_local", "external_wrong_revision_native",
])
def test_credential_fixture_admission_rejects_unsafe_reachable_changes(mutation):
    tree = ast.parse((candidate.ROOT / "tests/test_marketplace_credential_rls.py").read_text())
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    if mutation in {"head", "wrong_revision", "wrong_downgrade"}:
        action = "downgrade" if mutation == "wrong_downgrade" else "upgrade"
        call = next(
            call for call in _credential_calls(functions["_bootstrap_postgres"])
            if isinstance(call.func, ast.Attribute) and call.func.attr == action
        )
        call.args[1] = ast.Constant("head" if mutation == "head" else "20260909_0062")
    elif mutation.startswith(("early_return_", "external_")):
        generator = "_native_postgres" if mutation.endswith("_native") else "_local_postgres"
        # Mutate the real pre-yield block, leaving its original bootstrap intact.
        block = next(
            node.body for node in ast.walk(functions[generator])
            if isinstance(node, ast.Try)
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Call)
            and isinstance(node.body[0].value.func, ast.Name)
            and node.body[0].value.func.id == "_bootstrap_postgres"
        )
        if mutation.startswith("early_return_"):
            block.insert(0, ast.Return())
        else:
            target = "head" if mutation.startswith("external_head_") else "20260909_0062"
            block.insert(1, ast.parse(f"command.upgrade(config, '{target}')").body[0])
    elif mutation.endswith("_dispatch"):
        branch = functions["disposable_postgres"].body[0]
        statements = branch.body if "local" in mutation else branch.orelse
        statements[0].value.value.func.id = "_bypassed_generator"
    else:
        generator = "_native_postgres" if mutation == "bypass_native" else "_local_postgres"
        call = next(
            call for call in _credential_calls(functions[generator])
            if isinstance(call.func, ast.Name) and call.func.id == "_bootstrap_postgres"
        )
        call.func.id = "_bypassed_bootstrap"
        if mutation == "disconnected_local_bootstrap":
            # A nested function containing the correct literal call is never
            # invoked. It cannot repair the broken admitted generator path.
            functions[generator].body.extend(ast.parse(
                "def disconnected():\n    _bootstrap_postgres(engine, url, create_runtime_role=False)\n"
            ).body)
    with pytest.raises(AssertionError):
        _assert_credential_fixture_admission(tree)


@pytest.mark.parametrize("model,name,columns", [
    (MarketplaceProductRow, "uq_orders_product_account", ("organization_id", "marketplace_account_id", "marketplace_product_id")),
    (MarketplaceOfferRow, "fk_orders_offer_product_account", ("organization_id", "marketplace_account_id", "marketplace_product_id")),
    (MarketplaceOfferRow, "uq_orders_offer_product_account", ("organization_id", "marketplace_account_id", "marketplace_product_id", "marketplace_offer_id")),
])
def test_catalog_orm_has_exact_account_anchors(model, name, columns):
    constraints = {constraint.name: constraint for constraint in model.__table__.constraints}
    assert name in constraints
    assert tuple(constraints[name].columns.keys()) == columns


def test_failed_cluster_start_still_attempts_cleanup(monkeypatch, tmp_path_factory):
    monkeypatch.delenv("ORDERS_TEST_USE_LOCAL_CLUSTER", raising=False)
    monkeypatch.setattr(candidate.shutil, "which", lambda name: "/fake/" + name)
    probe = MagicMock()
    probe.__enter__.return_value.getsockname.return_value = ("127.0.0.1", 54321)
    monkeypatch.setattr(candidate.socket, "socket", lambda: probe)
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        if args[-1] == "start":
            raise RuntimeError("injected partial start failure")

    monkeypatch.setattr(candidate.subprocess, "run", execute)
    generator = candidate.cluster.__wrapped__(tmp_path_factory)
    with pytest.raises(RuntimeError, match="injected partial start failure"):
        next(generator)
    assert any(args[-1] == "stop" for args in calls), "partial start leaked cluster"


@pytest.mark.parametrize("fail_on", ["DATABASE", "ROLE", "body"])
def test_partial_database_setup_always_removes_its_exact_resources(monkeypatch, fail_on):
    # Inject an error *after* CREATE took effect: cleanup must cover uncertain setup.
    databases, roles = set(), set()
    state = {"closed": False}

    class Maintenance:
        def execute(self, query, parameters=None):
            if not isinstance(query, str):
                query = query.as_string()
            result = None
            if "inet_server_addr" in query:
                result = (True,)
            elif query.startswith("SELECT 1 FROM pg_database"):
                result = (1,) if parameters[0] in databases else None
            elif query.startswith("SELECT 1 FROM pg_roles"):
                result = (1,) if parameters[0] in roles else None
            elif query.startswith("CREATE"):
                kind, name = query.split()[1], query.split('"')[1]
                (databases if kind == "DATABASE" else roles).add(name)
                if kind == fail_on:
                    raise RuntimeError("injected setup failure")
            elif query.startswith("DROP"):
                kind, name = query.split()[1], query.split('"')[1]
                (databases if kind == "DATABASE" else roles).discard(name)
            else:
                raise AssertionError(query)
            return MagicMock(fetchone=lambda: result)

        def close(self):
            state["closed"] = True

    monkeypatch.setattr(candidate.psycopg, "connect", lambda **kwargs: Maintenance())
    with (
        pytest.raises(RuntimeError, match="injected"),
        candidate.disposable_database(({}, None, 5432, "synthetic_owner"), ("orders_fixture_" + uuid4().hex,)),
    ):
        raise RuntimeError("injected body failure")
    assert databases == roles == set()
    assert state["closed"]


@pytest.mark.parametrize("invalid", [False, True])
def test_populated_catalog_upgrade_is_atomic_and_preserves_pairing(cluster, invalid):
    with candidate.disposable_database(cluster) as database:
        result = candidate.migrate(database.url, "upgrade", "20260908_0061")
        assert result.returncode == 0, result.stderr
        engine = create_engine(database.url)
        try:
            with engine.begin() as c:
                c.exec_driver_sql("INSERT INTO lk_organizations(organization_id,slug,name) VALUES (91001,'synthetic','Synthetic')")
                c.exec_driver_sql("""INSERT INTO marketplace_accounts(marketplace_account_id,organization_id,marketplace,external_account_id,status)
                    VALUES (91101,91001,'avito','synthetic-a','connected'),(91102,91001,'avito','synthetic-b','connected')""")
                c.exec_driver_sql("INSERT INTO catalog_skus(catalog_sku_id,organization_id,code) VALUES (91301,91001,'synthetic-sku')")
                c.exec_driver_sql("""INSERT INTO marketplace_products(marketplace_product_id,organization_id,marketplace_account_id,external_product_id)
                    VALUES (91401,91001,91101,'synthetic-product')""")
                c.execute(text("""INSERT INTO marketplace_offers(marketplace_offer_id,organization_id,marketplace_account_id,marketplace_product_id,external_offer_key,catalog_sku_id)
                    VALUES (91501,91001,:account,91401,'synthetic-offer',91301)"""), {"account": 91102 if invalid else 91101})
                before = c.exec_driver_sql("SELECT * FROM marketplace_offers").all()
            result = candidate.migrate(database.url, "upgrade", "20260909_0062")
            assert result.returncode == (1 if invalid else 0), result.stderr
            if invalid:
                assert "fk_orders_offer_product_account" in result.stderr
            with engine.connect() as c:
                assert c.exec_driver_sql("SELECT * FROM marketplace_offers").all() == before
                assert c.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == ("20260908_0061" if invalid else "20260909_0062")
                for table in candidate.TABLES:
                    assert bool(c.execute(text("SELECT to_regclass(:table)"), {"table": table}).scalar_one()) == (not invalid)
                if invalid:
                    assert c.exec_driver_sql("SELECT to_regprocedure('orders_exact_text(text)')").scalar_one() is None
                    assert c.exec_driver_sql("SELECT count(*) FROM pg_constraint WHERE conname IN ('uq_orders_product_account','fk_orders_offer_product_account','uq_orders_offer_product_account')").scalar_one() == 0
        finally:
            engine.dispose()


def test_actual_runtime_dml_rls_and_ddl_denials(db):
    owner, runtime = db
    result = runtime_script(owner, runtime.url.username)
    assert result.returncode == 0, result.stderr
    with owner.connect() as c:
        assert c.execute(text("SELECT rolsuper,rolbypassrls,rolinherit,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=:role"), {"role": runtime.url.username}).one() == (False,False,False,False,False)
        assert c.execute(text("SELECT count(*) FROM pg_class WHERE relname=ANY(:tables) AND relowner=(SELECT oid FROM pg_roles WHERE rolname=:role)"), {"tables": list(candidate.TABLES), "role": runtime.url.username}).scalar_one() == 0
    with runtime.begin() as c:
        candidate.scope(c)
        oid, rid = candidate.order(c), candidate.run(c)
        iid = candidate.item(c, oid)
        obs = candidate.observation(c, oid, rid)
        assert c.execute(text("SELECT observation_id FROM order_observations WHERE observation_id=:id"), {"id": obs}).scalar_one() == obs
        assert c.execute(text("UPDATE marketplace_orders SET version=version+1 WHERE order_id=:id AND version=1"), {"id": oid}).rowcount == 1
        assert c.execute(text("UPDATE marketplace_order_items SET version=version+1 WHERE order_item_id=:id AND version=1"), {"id": iid}).rowcount == 1
        assert c.execute(text("UPDATE order_sync_runs SET page_count=page_count+1 WHERE sync_run_id=:id"), {"id": rid}).rowcount == 1
    for org in (None, 91002):
        with runtime.begin() as c:
            if org:
                candidate.scope(c, org)
            assert c.execute(text("SELECT count(*) FROM marketplace_orders WHERE order_id=:id"), {"id": oid}).scalar_one() == 0
            assert c.execute(text("UPDATE marketplace_orders SET version=version+1 WHERE order_id=:id"), {"id": oid}).rowcount == 0
        with pytest.raises(DBAPIError, match="row-level security"), runtime.begin() as c:
            if org:
                candidate.scope(c, org)
            candidate.order(c)
    for statement in (
        "UPDATE order_observations SET normalized_evidence='{}'",
        "DELETE FROM marketplace_orders", "TRUNCATE marketplace_orders CASCADE",
        "CREATE TABLE public.orders_forbidden(id int)",
        "ALTER TABLE marketplace_orders ADD COLUMN forbidden int",
        "SELECT setval(pg_get_serial_sequence('marketplace_orders','order_id'), 1)",
    ):
        with pytest.raises(DBAPIError, match="permission denied|must be owner"), runtime.begin() as c:
            candidate.scope(c)
            c.exec_driver_sql(statement)


def test_runtime_script_refuses_orders_without_forced_rls(db):
    owner, runtime = db
    try:
        with owner.begin() as c:
            c.exec_driver_sql("ALTER TABLE order_observations NO FORCE ROW LEVEL SECURITY")
        result = runtime_script(owner, runtime.url.username)
        assert result.returncode != 0
        assert "canonical tables without forced RLS: order_observations" in result.stderr
    finally:
        with owner.begin() as c:
            c.exec_driver_sql("ALTER TABLE order_observations FORCE ROW LEVEL SECURITY")
