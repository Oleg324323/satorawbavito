"""Actual non-owner RLS, atomic ACL intersection and fail-closed downgrade."""

from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tests import test_orders_schema_candidate as candidate
from tests.test_orders_schema_integration import runtime_script
from tests.test_repricer_approvals_lifecycle import (
    A,
    E,
    T,
    claim,
    create,
    dispatch,
    outcome,
    reserve,
    scope,
)
from tests.test_repricer_approvals_schema import TABLES, scripts, seed

cluster = candidate.cluster


@pytest.fixture(scope="module")
def latest_db(cluster):
    role = "repricer_runtime_" + uuid4().hex
    with candidate.disposable_database(cluster, (role,)) as database:
        migration = candidate.migrate(database.url, "upgrade", "head")
        assert migration.returncode == 0, migration.stderr
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
                and table != E
            )
            assert (
                c.execute(
                    text("SELECT has_table_privilege(:role,:table,:privilege)"),
                    {"role": role, "table": table, "privilege": privilege},
                ).scalar_one()
                == allowed
            ), (table, privilege)
    assert (
        c.exec_driver_sql(
            "SELECT count(*) FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) x WHERE c.relname LIKE 'wb_repricer_price_%%' AND (x.grantee=0 OR x.is_grantable)"
        ).scalar_one()
        == 0
    )


def test_actual_runtime_acl_and_legal_full_transaction(latest_db):
    owner, runtime = latest_db
    with owner.begin() as c:
        assert_acl(c, runtime.url.username)
    with runtime.begin() as c:
        scope(c)
        row = claim(c, create(c))
        attempt = dispatch(c, row, reserve(c, row))
        outcome(c, row, attempt)


@pytest.mark.parametrize(
    "org,account",
    [
        (None, None),
        (7, None),
        (None, 42),
        (8, 42),
        (7, 43),
        (0, 42),
        (7, 0),
        ("07", 42),
        (7, "+42"),
        (7, 2147483648),
        (7, "hostile-canary"),
    ],
)
def test_missing_wrong_and_invalid_context_denies(latest_db, org, account):
    _, runtime = latest_db
    with runtime.begin() as c:
        if org is not None:
            c.execute(
                text("SELECT set_config('app.organization_id',:value,true)"),
                {"value": str(org)},
            )
        if account is not None:
            c.execute(
                text("SELECT set_config('app.marketplace_account_id',:value,true)"),
                {"value": str(account)},
            )
        for table in TABLES:
            assert c.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one() == 0
    with pytest.raises(DBAPIError), runtime.begin() as c:
        if org is not None and account is not None:
            scope(c, org, account)
        create(c)


@pytest.mark.parametrize(
    "operation",
    [
        "legacy",
        "audit_update",
        "delete",
        "truncate",
        "trigger",
        "policy",
        "schema",
        "role",
        "bypass",
    ],
)
def test_runtime_cannot_expand_or_erase_authority(latest_db, operation):
    _, runtime = latest_db
    statements = {
        "audit_update": f"UPDATE {E} SET event_kind='approval.imported'",
        "delete": f"DELETE FROM {A}",
        "truncate": f"TRUNCATE {A}",
        "trigger": f"ALTER TABLE {A} DISABLE TRIGGER ALL",
        "policy": f"ALTER TABLE {A} DISABLE ROW LEVEL SECURITY",
        "schema": "CREATE TABLE public.repricer_forbidden(id int)",
        "role": f"ALTER ROLE {runtime.url.username} SUPERUSER",
        "bypass": "SET LOCAL session_replication_role=replica",
    }
    with pytest.raises(DBAPIError), runtime.begin() as c:
        scope(c)
        if operation == "legacy":
            create(c, legacy=True)
        else:
            c.exec_driver_sql(statements[operation])


@pytest.mark.parametrize(
    "changes", [{"marketplace_account_id": 44}, {"organization_id": 8}]
)
def test_foreign_scope_and_catalog_fk_deny(latest_db, changes):
    _, runtime = latest_db
    with pytest.raises(DBAPIError), runtime.begin() as c:
        scope(c)
        create(c, changes=changes)


def test_actual_foreign_catalog_and_membership_fk_reject(latest_db):
    _, runtime = latest_db
    with pytest.raises(DBAPIError) as error, runtime.begin() as c:
        scope(c)
        create(c, catalog=100)
    assert error.value.orig.sqlstate == "23503"
    with pytest.raises(DBAPIError) as error, runtime.begin() as c:
        scope(c)
        row = create(c)
        c.execute(
            text(
                f"UPDATE {A} SET status='applying',version=1,claimed_by_membership_id=79,claimed_audit_id=:audit WHERE approval_row_id=:id"
            ),
            {"audit": uuid4(), "id": row["approval_row_id"]},
        )
    assert error.value.orig.sqlstate == "23503"


def test_runtime_not_owner_no_trigger_execution_or_role_escalation(latest_db):
    owner, runtime = latest_db
    with owner.begin() as c:
        role = c.execute(
            text(
                "SELECT rolsuper,rolbypassrls,rolcreaterole,rolcreatedb FROM pg_roles WHERE rolname=:name"
            ),
            {"name": runtime.url.username},
        ).one()
        assert role == (False, False, False, False)
        assert (
            c.execute(
                text(
                    "SELECT count(*) FROM pg_class WHERE relname=ANY(:tables) AND relowner=CAST(:role AS regrole)"
                ),
                {"tables": list(TABLES), "role": runtime.url.username},
            ).scalar_one()
            == 0
        )
        for name in (
            "repricer_account_lock",
            "repricer_row_guard",
            "repricer_validate",
        ):
            assert not c.execute(
                text("SELECT has_function_privilege(:role,:function,'EXECUTE')"),
                {"role": runtime.url.username, "function": name + "()"},
            ).scalar_one()
        assert (
            c.exec_driver_sql(
                "SELECT count(*) FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) x WHERE p.proname LIKE 'repricer_%%' AND (p.prosecdef OR x.grantee=0 OR x.is_grantable)"
            ).scalar_one()
            == 0
        )
        assert (
            c.execute(
                text(
                    "SELECT count(*) FROM pg_attribute WHERE attrelid=ANY(SELECT oid FROM pg_class WHERE relname=ANY(:tables)) AND attidentity<>''"
                ),
                {"tables": list(TABLES)},
            ).scalar_one()
            == 0
        )
    with pytest.raises(DBAPIError), runtime.begin() as c:
        c.exec_driver_sql(f"SET LOCAL ROLE {owner.url.username}")


def test_same_org_wrong_account_updates_match_zero_and_delete_denies(latest_db):
    _, runtime = latest_db
    with runtime.begin() as c:
        scope(c, 7, 43)
        for table in (A, T):
            assert (
                c.exec_driver_sql(
                    f"UPDATE {table} SET version=version RETURNING *"
                ).all()
                == []
            )
    for table in TABLES:
        with pytest.raises(DBAPIError), runtime.begin() as c:
            scope(c, 7, 43)
            c.exec_driver_sql(f"DELETE FROM {table}")


def test_runtime_script_failure_is_atomic(latest_db):
    owner, runtime = latest_db
    with owner.begin() as c:
        before = c.exec_driver_sql(
            "SELECT oid,relacl::text FROM pg_class WHERE relname LIKE 'wb_repricer_price_%%' ORDER BY oid"
        ).all()
    result = runtime_script(owner, runtime.url.username, fail_after_broad=True)
    assert result.returncode != 0
    with owner.begin() as c:
        assert (
            c.exec_driver_sql(
                "SELECT oid,relacl::text FROM pg_class WHERE relname LIKE 'wb_repricer_price_%%' ORDER BY oid"
            ).all()
            == before
        )
        assert_acl(c, runtime.url.username)


def old_state(c):
    return (
        c.exec_driver_sql(
            "SELECT c.oid,c.relacl::text,a.attnum,a.attacl::text FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid WHERE c.relnamespace='public'::regnamespace AND c.relkind IN ('r','S') AND c.relname NOT LIKE 'wb_repricer_price_%%' AND a.attnum>0 ORDER BY c.oid,a.attnum"
        ).all(),
        c.exec_driver_sql(
            "SELECT oid,defaclacl::text FROM pg_default_acl ORDER BY oid"
        ).all(),
        c.exec_driver_sql(
            "SELECT organization_id,slug,name FROM lk_organizations ORDER BY organization_id"
        ).all(),
    )


def test_runtime_script_clears_preexisting_new_function_grant_options(latest_db):
    owner, runtime = latest_db
    role = runtime.url.username
    with owner.begin() as c:
        c.exec_driver_sql(
            f"GRANT EXECUTE ON FUNCTION public.repricer_context_id(text) TO {role} WITH GRANT OPTION"
        )
        c.exec_driver_sql(
            "GRANT EXECUTE ON FUNCTION public.repricer_context_id(text) TO PUBLIC"
        )
        c.exec_driver_sql(
            f"GRANT UPDATE(event_kind) ON {E} TO {role} WITH GRANT OPTION"
        )
    result = runtime_script(owner, role)
    assert result.returncode == 0, result.stderr
    with owner.begin() as c:
        assert (
            c.exec_driver_sql(
                "SELECT count(*) FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) x WHERE p.proname LIKE 'repricer_%%' AND (x.grantee=0 OR x.is_grantable)"
            ).scalar_one()
            == 0
        )
        assert not c.execute(
            text("SELECT has_column_privilege(:role,:table,'event_kind','UPDATE')"),
            {"role": role, "table": E},
        ).scalar_one()


def test_broad_defaults_intersect_before_script_preserve_old_acl_and_data(
    cluster, monkeypatch
):
    role = "repricer_defaults_" + uuid4().hex
    column_role = "repricer_column_" + uuid4().hex
    with candidate.disposable_database(cluster, (role, column_role)) as database:
        result = candidate.migrate(database.url, "upgrade", "20260909_0065")
        assert result.returncode == 0, result.stderr
        owner = create_engine(database.url, hide_parameters=True)
        runtime = create_engine(owner.url.set(username=role), hide_parameters=True)
        try:
            with owner.begin() as c:
                seed(c)
                c.exec_driver_sql(
                    f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO {role} WITH GRANT OPTION"
                )
                c.exec_driver_sql(
                    "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO PUBLIC"
                )
                c.exec_driver_sql(
                    f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO {role} WITH GRANT OPTION"
                )
                c.exec_driver_sql(
                    f"GRANT USAGE ON SCHEMA public TO {role},{column_role}"
                )
                c.exec_driver_sql(
                    f"GRANT SELECT,UPDATE ON marketplace_accounts TO {role}"
                )
                before = old_state(c)
            module = scripts().get_revision("20260909_0066").module
            original = module.op.execute

            def inject(statement, *args, **kwargs):
                value = original(statement, *args, **kwargs)
                if statement == module.RELATIONS:
                    original(
                        f"GRANT UPDATE(event_kind),REFERENCES(audit_event_id) ON {E} TO {column_role} WITH GRANT OPTION"
                    )
                    original(
                        f"GRANT SELECT(approval_id),UPDATE(status) ON {A} TO {column_role} WITH GRANT OPTION"
                    )
                    original(f"GRANT UPDATE(event_kind) ON {E} TO PUBLIC")
                return value

            with owner.begin() as c:
                with monkeypatch.context() as patch:
                    patch.setattr(module.op, "execute", inject)
                    with Operations.context(MigrationContext.configure(c)):
                        module.upgrade()
                assert old_state(c) == before
                assert_acl(c, role)
                assert not c.execute(
                    text(
                        "SELECT has_column_privilege(:role,:table,'event_kind','UPDATE')"
                    ),
                    {"role": column_role, "table": E},
                ).scalar_one()
                assert c.execute(
                    text("SELECT has_column_privilege(:role,:table,'status','UPDATE')"),
                    {"role": column_role, "table": A},
                ).scalar_one()
                assert (
                    c.exec_driver_sql(
                        "SELECT count(*) FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid CROSS JOIN LATERAL aclexplode(a.attacl) x WHERE c.relname LIKE 'wb_repricer_price_%%' AND (x.is_grantable OR x.grantee=0)"
                    ).scalar_one()
                    == 0
                )
            with runtime.begin() as c:
                scope(c)
                row = claim(c, create(c))
                outcome(
                    c,
                    row,
                    reserve(c, row),
                    status="failed",
                    error="INTERNAL_APPLY_ERROR",
                )
            with owner.begin() as c:
                assert old_state(c) == before
                c.exec_driver_sql(f"SET LOCAL ROLE {column_role}")
                scope(c)
                assert len(c.exec_driver_sql(f"SELECT approval_id FROM {A}").all()) == 1
        finally:
            runtime.dispose()
            owner.dispose()


@pytest.mark.parametrize("hidden", [False, True])
def test_downgrade_nonempty_or_hidden_refuses_atomically(cluster, hidden):
    role = "repricer_owner_" + uuid4().hex
    with candidate.disposable_database(cluster, (role,)) as database:
        result = candidate.migrate(database.url, "upgrade", "20260909_0066")
        assert result.returncode == 0, result.stderr
        owner = create_engine(database.url, hide_parameters=True)
        try:
            with owner.begin() as c:
                seed(c)
                scope(c)
                row = create(c)
            with owner.begin() as c:
                if hidden:
                    for table in TABLES:
                        c.exec_driver_sql(f"ALTER TABLE {table} OWNER TO {role}")
                    c.exec_driver_sql(
                        f"GRANT EXECUTE ON FUNCTION public.repricer_context_id(text) TO {role}"
                    )
            with pytest.raises(DBAPIError) as error, owner.begin() as c:
                if hidden:
                    c.exec_driver_sql(f"SET LOCAL ROLE {role}")
                    scope(c, 7, 43)
                    assert (
                        c.exec_driver_sql(f"SELECT count(*) FROM {A}").scalar_one() == 0
                    )
                with Operations.context(MigrationContext.configure(c)):
                    scripts().get_revision("20260909_0066").module.downgrade()
            assert error.value.orig.sqlstate == ("42501" if hidden else "P0001")
            with owner.begin() as c:
                assert (
                    c.execute(
                        text(
                            f"SELECT approval_row_id FROM {A} WHERE approval_row_id=:id"
                        ),
                        {"id": row["approval_row_id"]},
                    ).scalar_one()
                    == row["approval_row_id"]
                )
                assert (
                    c.exec_driver_sql(
                        "SELECT version_num FROM alembic_version"
                    ).scalar_one()
                    == "20260909_0066"
                )
                assert (
                    c.execute(
                        text(
                            "SELECT count(*) FROM pg_class WHERE relname=ANY(:tables)"
                        ),
                        {"tables": list(TABLES)},
                    ).scalar_one()
                    == 3
                )
        finally:
            owner.dispose()
