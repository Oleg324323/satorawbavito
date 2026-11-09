"""Actual 0079 catalog acceptance in the existing owned disposable harness."""

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tests import test_orders_schema_candidate as candidate

cluster = candidate.cluster
REVISION = "20260909_0079"


@pytest.fixture(scope="module")
def pg_database(cluster):
    """Reuse the allocator, upgrading beyond the old fetch fixture's 0061 pin."""
    role = "foundation_acceptance_" + uuid4().hex
    with candidate.disposable_database(cluster, (role,)) as database:
        migration = candidate.migrate(database.url, "upgrade", REVISION)
        assert migration.returncode == 0, migration.stderr
        options = {"options": "-c statement_timeout=10000 -c lock_timeout=5000"}
        owner = create_engine(database.url, hide_parameters=True, connect_args=options)
        runtime = create_engine(owner.url.set(username=role), hide_parameters=True,
                                connect_args=options)
        try:
            with owner.begin() as connection:
                # Existing synthetic store fixtures need public DML. The private
                # maintenance schema receives no grants or provisioning.
                connection.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {role}")
                connection.exec_driver_sql(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}")
                connection.exec_driver_sql(
                    f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}")
            yield owner, runtime
        finally:
            runtime.dispose()
            owner.dispose()


def test_0079_catalog_is_private_forced_rls_and_inert(pg_database):
    owner, runtime = pg_database
    with owner.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == REVISION
        connection.execute(text("SELECT credential_maintenance.assert_inert()"))
        tables = connection.execute(text("""
            SELECT relname, relrowsecurity, relforcerowsecurity
            FROM pg_class WHERE relnamespace='credential_maintenance'::regnamespace
              AND relkind='r' ORDER BY relname
        """)).all()
        assert tables == [("authorizations", True, True), ("targets", True, True)]
        assert connection.scalar(text("""
            SELECT count(*) FROM pg_class
            WHERE relnamespace='credential_maintenance'::regnamespace
              AND relkind IN ('v','m','f')
        """)) == 0
        functions = connection.execute(text("""
            SELECT p.proname, p.prosecdef, p.proconfig,
                   has_function_privilege(:role, p.oid, 'EXECUTE')
            FROM pg_proc p WHERE p.pronamespace='credential_maintenance'::regnamespace
        """), {"role": runtime.url.username}).all()
        assert len(functions) == 10
        assert all(not definer and config == ["search_path=pg_catalog, pg_temp"]
                   and not executable for _, definer, config, executable in functions)
        assert connection.scalar(text("""
            SELECT count(*) FROM pg_trigger t JOIN pg_proc p ON p.oid=t.tgfoid
            WHERE p.pronamespace='credential_maintenance'::regnamespace
              AND t.tgrelid IN ('public.lk_user_wb_tokens'::regclass,
                               'public.lk_user_avito_credentials'::regclass)
        """)) == 0
        assert not connection.scalar(text("""
            SELECT has_schema_privilege(:role, 'credential_maintenance', 'USAGE')
        """), {"role": runtime.url.username})
        assert connection.scalar(text("""
            SELECT count(*) FROM (
              SELECT n.nspowner AS owner, n.nspacl AS acl FROM pg_namespace n
              WHERE n.nspname='credential_maintenance'
              UNION ALL SELECT c.relowner, c.relacl FROM pg_class c
              WHERE c.relnamespace='credential_maintenance'::regnamespace
              UNION ALL SELECT p.proowner, coalesce(p.proacl, acldefault('f',p.proowner))
              FROM pg_proc p WHERE p.pronamespace='credential_maintenance'::regnamespace
            ) objects CROSS JOIN LATERAL aclexplode(objects.acl) a
            WHERE a.grantee <> objects.owner
        """)) == 0


@pytest.mark.parametrize("statement", [
    "SELECT * FROM credential_maintenance.authorizations",
    "SELECT * FROM credential_maintenance.targets",
    "SELECT credential_maintenance.assert_inert()",
])
def test_0079_runtime_has_no_maintenance_access(pg_database, statement):
    _, runtime = pg_database
    with runtime.connect() as connection:
        with pytest.raises(DBAPIError) as denied:
            connection.execute(text(statement))
        assert denied.value.orig.sqlstate == "42501"
        connection.rollback()


def test_0079_downgrade_refuses_nonempty_targets_without_dropping_schema(cluster):
    # Separate owned database keeps the intentionally retained history out of
    # all other scenarios. No authorizations or operational roles are created.
    with candidate.disposable_database(cluster) as database:
        upgraded = candidate.migrate(database.url, "upgrade", REVISION)
        assert upgraded.returncode == 0, upgraded.stderr
        engine = create_engine(database.url, hide_parameters=True)
        target = uuid4()
        try:
            with engine.begin() as connection:
                connection.execute(text("""
                    INSERT INTO credential_maintenance.targets
                      (target_credential_id, organization_id, marketplace_account_id,
                       provider, credential_kind)
                    VALUES (:target, 1, 1, 'wb', 'wb_api')
                """), {"target": target})
            result = candidate.migrate(database.url, "downgrade", "20260909_0078")
            assert result.returncode != 0
            assert "maintenance_not_empty" in result.stderr
            with engine.connect() as connection:
                assert connection.scalar(text("SELECT version_num FROM alembic_version")) == REVISION
                assert connection.scalar(text(
                    "SELECT target_credential_id FROM credential_maintenance.targets")) == target
                assert connection.scalar(text(
                    "SELECT count(*) FROM credential_maintenance.authorizations")) == 0
        finally:
            engine.dispose()
