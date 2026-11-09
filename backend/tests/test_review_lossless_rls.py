"""Runtime ACLs, exact concurrent identities and fail-closed lossless downgrade."""
# ruff: noqa: SIM117

import hashlib
from concurrent.futures import ThreadPoolExecutor
from queue import Queue
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tests import test_orders_schema_candidate as candidate
from tests import test_review_facts_schema as legacy
from tests.test_orders_schema_integration import runtime_script
from tests.test_review_lossless_migration import (
    PAIRS,
    REVISION,
    migrated,
    revise,
    scripts,
    unit,
)

cluster = candidate.cluster


@pytest.fixture(scope="module")
def latest_db(cluster):
    role = "review_lossless_" + uuid4().hex
    with migrated(cluster, "head", (role,)) as db:
        runtime = create_engine(db.owner.url.set(username=role), hide_parameters=True)
        try:
            prior_acl = None
            for _ in range(2):
                result = runtime_script(db.owner, role)
                assert result.returncode == 0, result.stderr
                with db.owner.connect() as c:
                    current_acl = acl_snapshot(c)
                if prior_acl is not None:
                    assert current_acl == prior_acl
                prior_acl = current_acl
            yield db.owner, runtime
        finally:
            runtime.dispose()


def byte_changes():
    changes = {}
    for table, column, _, _ in PAIRS:
        changes.setdefault(table, {}).update({column: None, column + "_utf8":
            b'{"streams":[{"name":"a\\u0000b"}]}' if column == "coverage"
            else (uuid4().hex + "\x00е́🚀").encode()})
    return changes


def safe_failure(error, state, constraint=None):
    assert error.value.orig.sqlstate == state
    if constraint:
        assert error.value.orig.diag.constraint_name == constraint
        assert error.value.orig.diag.message_primary == "Review identity already exists"
        assert "synthetic-secret-canary" not in str(error.value.orig)


def test_runtime_can_insert_all_bytes_but_not_expand_authority(latest_db):
    owner, runtime = latest_db
    with runtime.begin() as c:
        candidate.scope(c)
        unit(c, byte_changes())
        assert c.exec_driver_sql("SELECT public.review_strict_utf8('\\x00'::bytea)").scalar_one()
        assert c.exec_driver_sql("SELECT public.review_coverage_json_object_utf8('\\x7b7d'::bytea)").scalar_one()
    with owner.connect() as c:
        legacy.assert_acl(c, runtime.url.username)
        for column in ("source_run_id_utf8", "coverage_utf8"):
            assert c.execute(text("SELECT has_column_privilege(:role,'review_sync_runs_v2',:col,'INSERT')"),
                             {"role": runtime.url.username, "col": column}).scalar_one()
        helpers = c.exec_driver_sql("""SELECT p.proname,p.prosecdef,p.proisstrict,p.provolatile,p.proconfig,
            EXISTS(SELECT 1 FROM aclexplode(p.proacl) WHERE grantee=0 AND privilege_type='EXECUTE')
            FROM pg_proc p WHERE p.oid IN ('public.review_strict_utf8(bytea)'::regprocedure,
            'public.review_coverage_json_object_utf8(bytea)'::regprocedure)""").all()
        assert len(helpers) == 2
        for _, definer, strict, volatility, config, public in helpers:
            assert (definer, strict, volatility, public) == (False, True, "i", False)
            assert config == ["search_path=pg_catalog, public"]
    legacy.ordering_denials(runtime)
    statements = [
        "DELETE FROM review_facts", "TRUNCATE review_sync_runs_v2",
        "ALTER TABLE review_observations DISABLE TRIGGER ALL",
        "ALTER FUNCTION public.review_strict_utf8(bytea) SECURITY DEFINER",
        "DROP POLICY review_facts_org ON review_facts",
    ]
    for sql in statements:
        with pytest.raises(DBAPIError):
            with runtime.begin() as c:
                candidate.scope(c)
                c.exec_driver_sql(sql)


@pytest.mark.parametrize("scope", [None, 91002])
def test_runtime_requires_matching_org(latest_db, scope):
    with pytest.raises(DBAPIError):
        with latest_db[1].begin() as c:
            if scope:
                candidate.scope(c, scope)
            unit(c, byte_changes())


def test_cross_tenant_fk_cannot_be_hidden_by_bytes(latest_db):
    with pytest.raises(DBAPIError):
        with latest_db[1].begin() as c:
            candidate.scope(c)
            unit(c, byte_changes(), account=91201)


IDENTITIES = [("review_facts", "external_review_id", "uq_review_fact_external"),
              ("review_sync_runs_v2", "source_run_id", "uq_review_run_source")]


@pytest.mark.parametrize("table,column,constraint", IDENTITIES)
def test_old_new_semantic_equality_and_long_nul_keys(latest_db, table, column, constraint):
    runtime = latest_db[1]
    key = "synthetic-secret-canary-" + uuid4().hex
    with runtime.begin() as c:
        candidate.scope(c)
        unit(c, {table: {column: key}})
    with pytest.raises(DBAPIError) as error:
        with runtime.begin() as c:
            candidate.scope(c)
            unit(c, {table: {column: None, column + "_utf8": key.encode()}})
    safe_failure(error, "23505", constraint)
    reverse = key + "-reverse"
    with runtime.begin() as c:
        candidate.scope(c)
        unit(c, {table: {column: None, column + "_utf8": reverse.encode()}})
    with pytest.raises(DBAPIError) as error:
        with runtime.begin() as c:
            candidate.scope(c)
            unit(c, {table: {column: reverse}})
    safe_failure(error, "23505", constraint)
    long_key = (key + "\x00" + "".join(hashlib.sha256(str(i).encode()).hexdigest()
                                       for i in range(100))).encode()
    with runtime.begin() as c:
        candidate.scope(c)
        for value in (long_key, (key + "é").encode(), (key + "é").encode()):
            unit(c, {table: {column: None, column + "_utf8": value}})
        unit(c, {table: {column: None, column + "_utf8": key.encode()}}, account=91102)
    with pytest.raises(DBAPIError) as error:
        with runtime.begin() as c:
            candidate.scope(c)
            unit(c, {table: {column: None, column + "_utf8": long_key}})
    safe_failure(error, "23505", constraint)


@pytest.mark.parametrize("table,column,constraint", IDENTITIES)
def test_two_sessions_wait_then_see_winning_legacy_identity(latest_db, table, column, constraint):
    owner, runtime = latest_db
    key = "synthetic-secret-canary-" + uuid4().hex
    pid = Queue()
    existing_run = None
    if table == "review_facts":
        # Prepare and COMMIT the run first: the loser's fact identity trigger,
        # not an earlier parent-run insert, must be the observed account wait.
        with runtime.begin() as c:
            candidate.scope(c)
            run_id, sequence = legacy.run(c)
            existing_run = {"sync_run_id": run_id, "run_sequence": sequence}

    def loser():
        try:
            with runtime.begin() as c:
                candidate.scope(c)
                pid.put(c.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
                unit(c, {table: {column: None, column + "_utf8": key.encode()}}, existing_run=existing_run)
        except DBAPIError as error:
            return error.orig.sqlstate, error.orig.diag.constraint_name, error.orig.diag.message_primary
        return "unexpected-success"

    with ThreadPoolExecutor(max_workers=1) as pool:
        with runtime.begin() as c:
            candidate.scope(c)
            unit(c, {table: {column: key}})
            future = pool.submit(loser)
            loser_pid = pid.get(timeout=5)
            deadline = monotonic() + 5
            waiting = False
            while monotonic() < deadline:
                with owner.connect() as probe:
                    waiting, query = probe.execute(text("SELECT wait_event_type='Lock',query FROM pg_stat_activity WHERE pid=:pid"),
                                                   {"pid": loser_pid}).one()
                if waiting:
                    break
                sleep(0.02)
            assert waiting and not future.done()
            assert f"INSERT INTO {table}" in query
        assert future.result(timeout=5) == ("23505", constraint, "Review identity already exists")


def test_new_identity_fields_observations_and_terminal_runs_are_immutable(latest_db):
    runtime = latest_db[1]
    with runtime.begin() as c:
        candidate.scope(c)
        rows = unit(c, byte_changes())
    mutations = [
        ("review_facts", "external_review_id_utf8='\\x61',version=version+1", "review_id"),
        ("review_facts", "external_review_id='a',external_review_id_utf8=NULL,version=version+1", "review_id"),
        ("review_sync_runs_v2", "source_run_id_utf8='\\x61'", "sync_run_id"),
        ("review_sync_runs_v2", "source_run_id='a',source_run_id_utf8=NULL", "sync_run_id"),
        ("review_observations", "text_utf8='\\x61'", "observation_id"),
    ]
    for table, change, key in mutations:
        with pytest.raises(DBAPIError) as error:
            with runtime.begin() as c:
                candidate.scope(c)
                c.execute(text(f"UPDATE {table} SET {change} WHERE {key}=:id"), {"id": rows[table][key]})
        safe_failure(error, "42501" if table == "review_observations" else "23514")
    with pytest.raises(DBAPIError) as error:
        with latest_db[0].begin() as c:
            c.execute(text("UPDATE review_observations SET text_utf8='\\x61' WHERE observation_id=:id"),
                      {"id": rows["review_observations"]["observation_id"]})
    safe_failure(error, "23514")
    run_id = rows["review_sync_runs_v2"]["sync_run_id"]
    with runtime.begin() as c:
        candidate.scope(c)
        c.execute(text("UPDATE review_sync_runs_v2 SET status='failed',completed_at=now() WHERE sync_run_id=:id"),
                  {"id": run_id})
    with pytest.raises(DBAPIError) as error:
        with runtime.begin() as c:
            candidate.scope(c)
            c.execute(text("UPDATE review_sync_runs_v2 SET coverage_utf8='\\x7b7d' WHERE sync_run_id=:id"), {"id": run_id})
    safe_failure(error, "23514")


def definitions(c):
    return c.exec_driver_sql("""SELECT proname,pg_get_functiondef(oid) FROM pg_proc WHERE oid IN
        ('public.review_exact_identity_guard()'::regprocedure,'public.review_fact_guard()'::regprocedure,
         'public.review_run_guard()'::regprocedure) ORDER BY proname""").all()


def acl_snapshot(c):
    return c.exec_driver_sql("""SELECT c.relname,c.relacl::text,a.attname,a.attacl::text
        FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
        WHERE c.relnamespace='public'::regnamespace AND c.relkind IN ('r','S')
          AND a.attnum>0 AND NOT a.attisdropped ORDER BY c.relname,a.attnum""").all()


def test_table_locks_do_not_fence_privileged_acl_changes(cluster, monkeypatch):
    """Characterize why exclusive operator DDL/ACL maintenance is required."""
    with migrated(cluster, "20260909_0064") as db:
        migration = scripts().get_revision(REVISION).module
        original_execute = migration.execute
        preflight_done, resume = Event(), Event()
        grant_pid = Queue()

        def pause_before_first_ddl(sql):
            if sql == migration.HELPERS_SQL:
                preflight_done.set()
                assert resume.wait(5)
            original_execute(sql)

        monkeypatch.setattr(migration, "execute", pause_before_first_ddl)

        def upgrade():
            with db.owner.begin() as c:
                with Operations.context(MigrationContext.configure(c)):
                    migration.upgrade()

        def grant():
            with db.owner.begin() as c:
                grant_pid.put(c.exec_driver_sql("SELECT pg_backend_pid()").scalar_one())
                c.exec_driver_sql("GRANT INSERT ON review_observations TO PUBLIC")

        with ThreadPoolExecutor(max_workers=2) as pool:
            upgrading = pool.submit(upgrade)
            try:
                assert preflight_done.wait(5)
                granting = pool.submit(grant)
                pid = grant_pid.get(timeout=5)
                waiting = False
                deadline = monotonic() + 2
                while monotonic() < deadline and not granting.done():
                    with db.owner.connect() as c:
                        waiting = c.execute(text("SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=:pid"),
                                            {"pid": pid}).scalar_one()
                    if waiting:
                        break
                    sleep(0.02)
                crossed_boundary = granting.done()
            finally:
                resume.set()
            upgrading.result(timeout=5)
            granting.result(timeout=5)
        assert crossed_boundary and not waiting
        # An owner can cross preflight even while relation locks are held. This
        # deliberately violates the operator prerequisite, not a supported run.
        with db.owner.connect() as c:
            assert c.exec_driver_sql("""SELECT EXISTS (SELECT 1 FROM pg_class c,
                LATERAL aclexplode(c.relacl) acl WHERE c.oid='review_observations'::regclass
                AND acl.grantee=0 AND acl.privilege_type='INSERT')""").scalar_one()
            assert c.exec_driver_sql("SELECT to_regprocedure('public.review_strict_utf8(bytea)') IS NOT NULL").scalar_one()


@pytest.mark.parametrize("table,column,required,nonempty", PAIRS)
@pytest.mark.parametrize("privilege", ["INSERT", "UPDATE"])
@pytest.mark.parametrize("column_grant", [False, True])
def test_public_writers_refused_atomically(cluster, table, column, required, nonempty, privilege, column_grant):
    with migrated(cluster, "20260909_0064") as db:
        with db.owner.begin() as c:
            unit(c)
            right = f"{privilege} ({column})" if column_grant else privilege
            c.exec_driver_sql(f"GRANT {right} ON {table} TO PUBLIC")
            before = acl_snapshot(c)
            funcs = definitions(c)
            before_data = c.exec_driver_sql(f"SELECT row_to_json(t) FROM {table} t").all()
        with db.owner.begin() as c:
            with pytest.raises(DBAPIError) as error:
                with c.begin_nested():
                    revise(c, "upgrade")
            safe_failure(error, "55000")
            assert error.value.orig.diag.message_primary == "review_lossless_acl_unsupported"
            assert acl_snapshot(c) == before
            assert definitions(c) == funcs
            assert c.exec_driver_sql(f"SELECT row_to_json(t) FROM {table} t").all() == before_data
            assert c.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "20260909_0064"
            assert c.exec_driver_sql("SELECT to_regprocedure('public.review_strict_utf8(bytea)')").scalar_one() is None


def test_named_column_grants_inheritance_and_reader_are_preserved(cluster):
    writer, member, reader, table_owner = ["review_acl_" + uuid4().hex for _ in range(4)]
    with migrated(cluster, "20260909_0064", (writer, member, reader, table_owner)) as db:
        with db.owner.begin() as c:
            c.exec_driver_sql(f"ALTER ROLE {member} INHERIT")
            c.exec_driver_sql(f"GRANT {writer} TO {member}")
            c.exec_driver_sql(f"GRANT INSERT (source_run_id) ON review_sync_runs_v2 TO {writer} WITH GRANT OPTION")
            c.exec_driver_sql(f"GRANT UPDATE (coverage) ON review_sync_runs_v2 TO {writer}")
            c.exec_driver_sql(f"GRANT SELECT ON review_sync_runs_v2 TO {reader}, PUBLIC")
            c.exec_driver_sql(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO {reader}")
            c.exec_driver_sql(f"ALTER TABLE review_observations OWNER TO {table_owner}")
            before = acl_snapshot(c)
            defaults = c.exec_driver_sql("SELECT oid,defaclacl::text FROM pg_default_acl ORDER BY oid").all()
            functions = c.exec_driver_sql("SELECT oid,proacl::text FROM pg_proc WHERE pronamespace='public'::regnamespace ORDER BY oid").all()
        result = candidate.migrate(db.database.url, "upgrade", REVISION)
        assert result.returncode == 0, result.stderr
        with db.owner.connect() as c:
            after = acl_snapshot(c)
            assert [r for r in after if not r.attname.endswith("_utf8")] == before
            assert c.exec_driver_sql("SELECT oid,defaclacl::text FROM pg_default_acl ORDER BY oid").all() == defaults
            assert c.exec_driver_sql("""SELECT oid,proacl::text FROM pg_proc WHERE pronamespace='public'::regnamespace
                AND proname NOT IN ('review_strict_utf8','review_coverage_json_object_utf8') ORDER BY oid""").all() == functions
            for role in (writer, member):
                for column, insert, update, grant_option in (
                    ("source_run_id_utf8", True, False, True),
                    ("coverage_utf8", False, True, False),
                    ("run_sequence", False, False, False),
                ):
                    for privilege, expected in (("INSERT", insert), ("UPDATE", update),
                                                ("INSERT WITH GRANT OPTION", grant_option)):
                        assert c.execute(text("SELECT has_column_privilege(:r,'review_sync_runs_v2',:c,:p)"),
                                         {"r": role, "c": column, "p": privilege}).scalar_one() is expected
                for helper in ("review_strict_utf8", "review_coverage_json_object_utf8"):
                    assert c.execute(text("SELECT has_function_privilege(:r,:f,'EXECUTE')"),
                                     {"r": role, "f": f"public.{helper}(bytea)"}).scalar_one()
            for column in ("source_run_id_utf8", "coverage_utf8"):
                assert not c.execute(text("SELECT has_column_privilege(:r,'review_sync_runs_v2',:c,'INSERT')"),
                                     {"r": reader, "c": column}).scalar_one()
            assert not c.execute(text("SELECT has_function_privilege(:r,'public.review_strict_utf8(bytea)','EXECUTE')"),
                                 {"r": reader}).scalar_one()
            assert c.execute(text("SELECT has_function_privilege(:r,'public.review_strict_utf8(bytea)','EXECUTE')"),
                             {"r": table_owner}).scalar_one()


def test_old_rows_upgrade_downgrade_restore_exact_functions_and_values(cluster):
    with migrated(cluster, "20260909_0064") as db:
        with db.owner.begin() as c:
            rows = unit(c)
            before = definitions(c)
            original = {table: c.execute(text(f"SELECT row_to_json(t) FROM {table} t")).all()
                        for table in ("review_facts", "review_observations", "review_sync_runs_v2")}
            physical = {table: c.exec_driver_sql(f"SELECT ctid::text,xmin::text FROM {table}").all()
                        for table in original}
        result = candidate.migrate(db.database.url, "upgrade", REVISION)
        assert result.returncode == 0, result.stderr
        with db.owner.connect() as c:
            for table, expected in original.items():
                new_columns = [column + "_utf8" for relation, column, _, _ in PAIRS if relation == table]
                assert c.execute(text(f"SELECT to_jsonb(t)-CAST(:cols AS text[]) FROM {table} t"),
                                 {"cols": new_columns}).all() == expected
                assert c.exec_driver_sql(f"SELECT ctid::text,xmin::text FROM {table}").all() == physical[table]
        result = candidate.migrate(db.database.url, "downgrade", "20260909_0064")
        assert result.returncode == 0, result.stderr
        with db.owner.connect() as c:
            assert definitions(c) == before
            for table, expected in original.items():
                assert c.execute(text(f"SELECT row_to_json(t) FROM {table} t")).all() == expected
        result = candidate.migrate(db.database.url, "upgrade", REVISION)
        assert result.returncode == 0, result.stderr
        with pytest.raises(DBAPIError) as error:
            with db.owner.begin() as c:
                unit(c, {"review_facts": {"external_review_id": None,
                    "external_review_id_utf8": rows["review_facts"]["external_review_id"].encode()}})
        safe_failure(error, "23505", "uq_review_fact_external")


@pytest.mark.parametrize("table,column,required,nonempty", PAIRS)
def test_any_byte_column_refuses_downgrade_before_ddl(cluster, table, column, required, nonempty):
    with migrated(cluster, REVISION) as db:
        with db.owner.begin() as c:
            unit(c, {table: {column: None, column + "_utf8": b"{}" if column == "coverage" else b"x"}})
        result = candidate.migrate(db.database.url, "downgrade", "20260909_0064")
        assert result.returncode != 0
        assert "Review lossless downgrade blocked" in result.stderr
        with db.owner.connect() as c:
            assert c.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == REVISION
            assert c.execute(text(f"SELECT count(*) FROM {table} WHERE {column}_utf8 IS NOT NULL")).scalar_one() == 1


def test_force_rls_owner_cannot_mistake_hidden_bytes_for_empty(cluster):
    role = "review_hidden_" + uuid4().hex
    with migrated(cluster, REVISION, (role,)) as db:
        with db.owner.begin() as c:
            unit(c, byte_changes())
        with db.owner.begin() as c:
            for table in ("review_sync_runs_v2", "review_facts", "review_observations"):
                c.exec_driver_sql(f"ALTER TABLE {table} OWNER TO {role}")
            c.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {role}")
        with db.owner.begin() as c:
            c.exec_driver_sql(f"SET LOCAL ROLE {role}")
            assert c.exec_driver_sql("SELECT count(*) FROM review_facts").scalar_one() == 0
            with pytest.raises(DBAPIError) as error:
                with c.begin_nested():
                    revise(c, "downgrade")
            safe_failure(error, "42501")
        with db.owner.connect() as c:
            assert c.exec_driver_sql("SELECT count(*) FROM review_facts WHERE external_review_id_utf8 IS NOT NULL").scalar_one() == 1
