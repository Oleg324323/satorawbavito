"""ACL and FORCE org+account isolation are checked separately on disposable data."""
# Separate contexts expose transaction and migration boundaries.
# ruff: noqa: SIM117

import json
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tests import test_orders_schema_candidate as candidate
from tests.test_orders_schema_integration import runtime_script
from tests.test_production_assignment_lifecycle import (
    TABLES,
    H,
    R,
    W,
    assign,
    create,
    fresh,
    scope,
    seed,
)
from tests.test_production_assignment_schema import PREVIOUS, TARGET, old_acls, scripts

cluster = candidate.cluster


@pytest.fixture(scope="module")
def db(cluster):
    role = "production_script_" + uuid4().hex
    with candidate.disposable_database(cluster, (role,)) as database:
        migrated = candidate.migrate(database.url, "upgrade", "head")
        assert migrated.returncode == 0, migrated.stderr
        owner = create_engine(database.url, hide_parameters=True)
        runtime = create_engine(owner.url.set(username=role), hide_parameters=True)
        try:
            with owner.begin() as c:
                seed(c)
            result = runtime_script(owner, role)
            assert result.returncode == 0, result.stderr
            yield owner, runtime
        finally:
            runtime.dispose()
            owner.dispose()


def assert_acl(c, role):
    for table in TABLES:
        for privilege in (
            "SELECT",
            "INSERT",
            "UPDATE",
            "DELETE",
            "TRUNCATE",
            "REFERENCES",
            "TRIGGER",
        ):
            allowed = (
                privilege in ("SELECT", "INSERT")
                or privilege == "UPDATE"
                and table == W
            )
            assert (
                c.execute(
                    text("SELECT has_table_privilege(:role,:table,:p)"),
                    {"role": role, "table": table, "p": privilege},
                ).scalar_one()
                == allowed
            )
            assert not c.execute(
                text("SELECT has_table_privilege(:role,:table,:p)"),
                {"role": role, "table": table, "p": privilege + " WITH GRANT OPTION"},
            ).scalar_one()
        identity = {W: "work_item_id", R: "receipt_id", H: "assignment_event_id"}[table]
        sequence = c.execute(
            text("SELECT pg_get_serial_sequence(:table,:identity)"),
            {"table": table, "identity": identity},
        ).scalar_one()
        for privilege in ("USAGE", "SELECT", "UPDATE"):
            assert c.execute(
                text("SELECT has_sequence_privilege(:role,:seq,:p)"),
                {"role": role, "seq": sequence, "p": privilege},
            ).scalar_one() == (privilege == "USAGE")


def test_runtime_acl_positive_and_role_properties(db):
    owner, runtime = db
    with owner.connect() as c:
        assert_acl(c, runtime.url.username)
        assert c.execute(
            text(
                "SELECT rolsuper,rolbypassrls,rolinherit FROM pg_roles WHERE rolname=:r"
            ),
            {"r": runtime.url.username},
        ).one() == (False, False, False)
        assert (
            c.execute(
                text(
                    "SELECT count(*) FROM pg_class WHERE relname=ANY(:tables) AND (NOT relrowsecurity OR NOT relforcerowsecurity OR relowner=(SELECT oid FROM pg_roles WHERE rolname=:r))"
                ),
                {"tables": list(TABLES), "r": runtime.url.username},
            ).scalar_one()
            == 0
        )
    with runtime.begin() as c:
        scope(c)
        assign(c, create(c))


@pytest.mark.parametrize(
    "org,account",
    [
        (None, None),
        (91001, None),
        (None, 91101),
        (91002, 91101),
        (91001, 91102),
        (0, 91101),
        (91001, 0),
        ("091001", 91101),
        (91001, "+91101"),
        (91001, 2147483648),
        (91001, "canary"),
    ],
)
def test_context_is_default_deny_for_reads_and_mutations(db, org, account):
    owner, runtime = db
    row = fresh(db)
    with runtime.begin() as c:
        scope(c)
        assign(c, row)
    with owner.connect() as c:
        assert_acl(c, runtime.url.username)
    for operation in ("SELECT", "INSERT", "UPDATE"):
        with runtime.connect() as c:
            with c.begin():
                for setting, value in (
                    ("app.organization_id", org),
                    ("app.marketplace_account_id", account),
                ):
                    if value is not None:
                        c.execute(
                            text("SELECT set_config(:s,:v,true)"),
                            {"s": setting, "v": str(value)},
                        )
                if operation == "SELECT":
                    for table in TABLES:
                        assert (
                            c.execute(
                                text(
                                    f"SELECT count(*) FROM {table} WHERE work_item_id=:id"
                                ),
                                {"id": row["work_item_id"]},
                            ).scalar_one()
                            == 0
                        )
                else:
                    try:
                        if operation == "INSERT":
                            create(c, binding=(row["order_id"], row["order_item_id"]))
                        else:
                            changed = c.execute(
                                text(
                                    f"UPDATE {W} SET version=version+1 WHERE work_item_id=:id"
                                ),
                                {"id": row["work_item_id"]},
                            ).rowcount
                            assert changed == 0
                    except DBAPIError as error:
                        assert error.orig.sqlstate in ("23514", "42501")
                        c.rollback()
                    else:
                        assert operation == "UPDATE"


@pytest.mark.parametrize(
    "statement",
    [
        f"DELETE FROM {W}",
        f"TRUNCATE {R}",
        f"UPDATE {H} SET reason='changed'",
        "CREATE TABLE public.production_forbidden(id int)",
        f"SELECT setval(pg_get_serial_sequence('{W}','work_item_id'),1)",
        "SELECT production_account_lock()",
        "SELECT production_row_guard()",
        "SELECT production_validate()",
    ],
)
def test_runtime_forbidden_privileges(db, statement):
    _, runtime = db
    with pytest.raises(DBAPIError) as error, runtime.begin() as c:
        scope(c)
        c.exec_driver_sql(statement)
    assert error.value.orig.sqlstate == "42501"


@pytest.mark.parametrize("table", TABLES)
@pytest.mark.parametrize("operation", ["DELETE", "TRUNCATE"])
def test_physical_rejection_even_owner(db, table, operation):
    owner, _ = db
    row = fresh(db)
    with owner.begin() as c:
        scope(c)
        assign(c, row)
    with pytest.raises(DBAPIError) as error, owner.begin() as c:
        scope(c)
        c.exec_driver_sql(
            f"{operation} {'FROM ' if operation == 'DELETE' else ''}{table}"
        )
    assert error.value.orig.sqlstate in ("23514", "0A000")


def test_failed_runtime_script_rolls_back_and_force_preflight(db):
    owner, runtime = db
    with owner.connect() as c:
        before = c.execute(
            text(
                "SELECT relname,relacl::text FROM pg_class WHERE relname=ANY(:tables) ORDER BY relname"
            ),
            {"tables": list(TABLES)},
        ).all()
    result = runtime_script(owner, runtime.url.username, fail_after_broad=True)
    assert result.returncode != 0 and "division by zero" in result.stderr
    with owner.connect() as c:
        assert (
            c.execute(
                text(
                    "SELECT relname,relacl::text FROM pg_class WHERE relname=ANY(:tables) ORDER BY relname"
                ),
                {"tables": list(TABLES)},
            ).all()
            == before
        )
        assert_acl(c, runtime.url.username)
    try:
        with owner.begin() as c:
            c.exec_driver_sql(f"ALTER TABLE {R} NO FORCE ROW LEVEL SECURITY")
        result = runtime_script(owner, runtime.url.username)
        assert result.returncode != 0 and R in result.stderr
    finally:
        with owner.begin() as c:
            c.exec_driver_sql(f"ALTER TABLE {R} FORCE ROW LEVEL SECURITY")


def test_nonempty_and_force_hidden_downgrade_refuse_before_ddl(db):
    owner, runtime = db
    fresh(db)
    migration = scripts().get_revision(TARGET).module
    for engine in (owner, runtime):
        with pytest.raises(DBAPIError), engine.begin() as c:
            with Operations.context(MigrationContext.configure(c)):
                migration.downgrade()
    with owner.connect() as c:
        assert all(
            c.execute(text("SELECT to_regclass(:t)"), {"t": table}).scalar_one()
            for table in TABLES
        )


def test_inherited_default_intersection_preserves_readers_and_old_acls(cluster):
    broad, reader = (
        "production_" + label + "_" + uuid4().hex for label in ("broad", "reader")
    )
    with candidate.disposable_database(cluster, (broad, reader)) as database:
        migrated = candidate.migrate(database.url, "upgrade", PREVIOUS)
        assert migrated.returncode == 0, migrated.stderr
        owner = create_engine(database.url, hide_parameters=True)
        try:
            with owner.begin() as c:
                c.exec_driver_sql(
                    f"ALTER DEFAULT PRIVILEGES GRANT ALL ON TABLES TO {broad} WITH GRANT OPTION"
                )
                c.exec_driver_sql(
                    f"ALTER DEFAULT PRIVILEGES GRANT SELECT ON TABLES TO {reader} WITH GRANT OPTION"
                )
                c.exec_driver_sql(
                    f"ALTER DEFAULT PRIVILEGES GRANT ALL ON SEQUENCES TO {broad} WITH GRANT OPTION"
                )
                c.exec_driver_sql(
                    f"ALTER DEFAULT PRIVILEGES GRANT SELECT ON SEQUENCES TO {reader}"
                )
                c.exec_driver_sql(
                    "ALTER DEFAULT PRIVILEGES GRANT ALL ON TABLES TO PUBLIC"
                )
                c.exec_driver_sql(
                    "ALTER DEFAULT PRIVILEGES GRANT ALL ON SEQUENCES TO PUBLIC"
                )
                before = old_acls(c)
            migrated = candidate.migrate(database.url, "upgrade", TARGET)
            assert migrated.returncode == 0, migrated.stderr
            with owner.connect() as c:
                assert old_acls(c) == before
                assert_acl(c, broad)
                for table in TABLES:
                    assert c.execute(
                        text("SELECT has_table_privilege(:r,:t,'SELECT')"),
                        {"r": reader, "t": table},
                    ).scalar_one()
                    for p in ("INSERT", "UPDATE", "DELETE", "SELECT WITH GRANT OPTION"):
                        assert not c.execute(
                            text("SELECT has_table_privilege(:r,:t,:p)"),
                            {"r": reader, "t": table, "p": p},
                        ).scalar_one()
                assert (
                    c.exec_driver_sql(
                        "SELECT count(*) FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) a WHERE c.relname LIKE 'production_%%' AND (a.grantee=0 OR a.is_grantable)"
                    ).scalar_one()
                    == 0
                )
        finally:
            owner.dispose()


def test_column_only_grantee_distinct_owner_and_hidden_downgrade(cluster, monkeypatch):
    column_role, table_owner = (
        "production_" + label + "_" + uuid4().hex for label in ("column", "owner")
    )
    with candidate.disposable_database(cluster, (column_role, table_owner)) as database:
        migrated = candidate.migrate(database.url, "upgrade", PREVIOUS)
        assert migrated.returncode == 0, migrated.stderr
        owner = create_engine(database.url, hide_parameters=True)
        try:
            migration = scripts().get_revision(TARGET).module
            with owner.begin() as c:
                seed(c)
                c.exec_driver_sql(
                    f"GRANT USAGE ON SCHEMA public TO {column_role},{table_owner}"
                )
                before = old_acls(c)
                original = migration.op.execute

                def inject(statement, *args, **kwargs):
                    result = original(statement, *args, **kwargs)
                    if statement == migration.MODEL:
                        original(
                            f"GRANT SELECT(work_item_id),INSERT(idempotency_key),UPDATE(request_checksum),REFERENCES(receipt_id) ON {R} TO {column_role} WITH GRANT OPTION"
                        )
                        original(f"GRANT UPDATE(reason) ON {H} TO PUBLIC")
                        for table in TABLES:
                            original(f"ALTER TABLE {table} OWNER TO {table_owner}")
                    return result

                with (
                    Operations.context(MigrationContext.configure(c)),
                    monkeypatch.context() as patch,
                ):
                    patch.setattr(migration.op, "execute", inject)
                    migration.upgrade()
                assert old_acls(c) == before
                for privilege in ("SELECT", "INSERT", "UPDATE"):
                    assert not c.execute(
                        text("SELECT has_table_privilege(:role,:t,:p)"),
                        {"role": column_role, "t": R, "p": privilege},
                    ).scalar_one()
                for col, privilege, want in (
                    ("work_item_id", "SELECT", True),
                    ("idempotency_key", "INSERT", True),
                    ("request_checksum", "UPDATE", False),
                    ("receipt_id", "REFERENCES", False),
                ):
                    assert (
                        c.execute(
                            text("SELECT has_column_privilege(:role,:t,:col,:p)"),
                            {"role": column_role, "t": R, "col": col, "p": privilege},
                        ).scalar_one()
                        == want
                    )
                    assert not c.execute(
                        text("SELECT has_column_privilege(:role,:t,:col,:p)"),
                        {
                            "role": column_role,
                            "t": R,
                            "col": col,
                            "p": privilege + " WITH GRANT OPTION",
                        },
                    ).scalar_one()
                for role in (column_role, table_owner):
                    for function in (
                        "production_exact_text(text)",
                        "production_ascii_json_string(text)",
                        "production_assignment_bytes(bigint,bigint,integer,text,text)",
                    ):
                        assert c.execute(
                            text("SELECT has_function_privilege(:role,:f,'EXECUTE')"),
                            {"role": role, "f": function},
                        ).scalar_one()
                scope(c)
                row = create(c)
            # Distinct owner can lock the relations but FORCE RLS hides their data.
            with owner.begin() as c:
                c.exec_driver_sql(f"SET LOCAL ROLE {table_owner}")
                assert c.exec_driver_sql(f"SELECT count(*) FROM {W}").scalar_one() == 0
                assert c.exec_driver_sql(
                    "SELECT production_assignment_bytes(1,2,3,'key','reason') IS NOT NULL"
                ).scalar_one()
            with pytest.raises(DBAPIError) as error, owner.begin() as c:
                c.exec_driver_sql(f"SET LOCAL ROLE {table_owner}")
                with Operations.context(MigrationContext.configure(c)):
                    migration.downgrade()
            assert error.value.orig.sqlstate == "42501"
            with owner.connect() as c:
                assert (
                    c.execute(
                        text(f"SELECT work_item_id FROM {W} WHERE work_item_id=:id"),
                        {"id": row["work_item_id"]},
                    ).scalar_one()
                    == row["work_item_id"]
                )
                assert all(
                    c.execute(text("SELECT to_regclass(:t)"), {"t": table}).scalar_one()
                    for table in TABLES
                )
        finally:
            owner.dispose()


@pytest.mark.parametrize(
    "org,account", [(None, None), (91002, 91201), (91001, 91102), ("091001", 91101)]
)
def test_rls_policy_independent_of_statement_guard_and_acl(db, org, account):
    owner, runtime = db
    row = fresh(db)
    with runtime.begin() as c:
        scope(c)
        assign(c, row)
    with owner.connect() as c:
        snapshots = {
            table: dict(
                c.execute(
                    text(f"SELECT * FROM {table} WHERE work_item_id=:id"),
                    {"id": row["work_item_id"]},
                )
                .mappings()
                .one()
            )
            for table in TABLES
        }
    for target in TABLES:
        # Local DDL/grants are rolled back: this isolates policy from earlier ACL
        # and immutable/account triggers instead of mistaking their denial for RLS.
        with owner.connect() as c:
            transaction = c.begin()
            try:
                for table in TABLES:
                    c.exec_driver_sql(
                        f"GRANT SELECT,INSERT,UPDATE,DELETE ON {table} TO {runtime.url.username}"
                    )
                    c.exec_driver_sql(
                        f"ALTER TABLE {table} DISABLE TRIGGER production_account_first"
                    )
                    c.exec_driver_sql(
                        f"ALTER TABLE {table} DISABLE TRIGGER production_guard"
                    )
                    c.exec_driver_sql(
                        f"ALTER TABLE {table} DISABLE TRIGGER production_witness"
                    )
                c.exec_driver_sql(f"SET LOCAL ROLE {runtime.url.username}")
                if org is not None:
                    scope(c, org, account)
                for table in TABLES:
                    assert (
                        c.execute(
                            text(
                                f"SELECT count(*) FROM {table} WHERE work_item_id=:id"
                            ),
                            {"id": row["work_item_id"]},
                        ).scalar_one()
                        == 0
                    )
                    assert (
                        c.execute(
                            text(
                                f"UPDATE {table} SET organization_id=organization_id WHERE work_item_id=:id"
                            ),
                            {"id": row["work_item_id"]},
                        ).rowcount
                        == 0
                    )
                    assert (
                        c.execute(
                            text(f"DELETE FROM {table} WHERE work_item_id=:id"),
                            {"id": row["work_item_id"]},
                        ).rowcount
                        == 0
                    )
                values = dict(snapshots[target])
                del values[
                    {W: "work_item_id", R: "receipt_id", H: "assignment_event_id"}[
                        target
                    ]
                ]
                if target == W:
                    del values["remaining_quantity"]
                args = {
                    key: json.dumps(value) if key.endswith("_payload") else value
                    for key, value in values.items()
                }
                placeholders = [
                    f"CAST(:{key} AS jsonb)" if key.endswith("_payload") else ":" + key
                    for key in values
                ]
                with pytest.raises(DBAPIError) as error:
                    c.execute(
                        text(
                            f"INSERT INTO {target} ({','.join(values)}) VALUES ({','.join(placeholders)})"
                        ),
                        args,
                    )
                assert error.value.orig.sqlstate == "42501"
                assert "row-level security" in error.value.orig.diag.message_primary
            finally:
                transaction.rollback()
