"""Dormant role artifact admission; physical role proofs are separate gates."""
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tests import test_orders_schema_candidate as candidate

cluster = candidate.cluster


def test_dormant_projection_role_artifact_requires_explicit_custody():
    source = Path(__file__).resolve().parents[1] / "ops/wb-history-projection-role.sql"
    assert source.is_file(), "Dedicated history projection grant artifact is missing"
    sql = source.read_text()
    assert ':"projection_role"' in sql and ':"projection_helper_owner"' in sql
    assert "CREATE ROLE" not in sql and "PASSWORD" not in sql
    assert "ALTER DEFAULT PRIVILEGES" not in sql
    assert "BEGIN;" in sql and sql.rstrip().endswith("COMMIT;")


def test_owned_projection_grants_deny_direct_parent_mutation_and_secret_reads(cluster):
    """Actual grants, not an approximation using the general-purpose API role."""
    source = candidate.ROOT / "ops/wb-history-projection-role.sql"
    runtime, helper, unrelated = ("history_role_" + uuid4().hex for _ in range(3))
    psql = shutil.which("psql")
    assert psql is not None
    with candidate.disposable_database(cluster, (runtime, helper, unrelated)) as database:
        migrated = candidate.migrate(database.url, "upgrade", "20260910_0085")
        assert migrated.returncode == 0, migrated.stderr
        owner = create_engine(database.url, hide_parameters=True)
        worker = create_engine(owner.url.set(username=runtime), hide_parameters=True)
        try:
            with owner.begin() as connection:
                connection.exec_driver_sql(f'ALTER ROLE "{helper}" NOLOGIN')
            def apply_grants():
                return subprocess.run(
                    [psql, "-X", "-h", database.host, "-p", str(database.port), "-U", database.owner,
                     "-d", database.name, "-v", f"projection_role={runtime}",
                     "-v", f"projection_helper_owner={helper}", "-f", str(source)],
                    env=candidate.isolated_environment(), capture_output=True, text=True, timeout=60, check=False,
                )

            # A reused identity must be rejected, not silently converted into a
            # privileged helper caller or a second secret-reading runtime.
            excessive = (
                ("EXECUTE ON FUNCTION public.wb_history_projection_apply_parent(integer,integer,bigint,bigint)", unrelated),
                ("SELECT(ciphertext) ON public.marketplace_account_credentials", runtime),
                ("DELETE ON public.iam_memberships", runtime),
                ("REFERENCES(order_id) ON public.marketplace_orders", runtime),
                ("SELECT(ciphertext) ON public.marketplace_account_credentials", helper),
            )
            for privilege, grantee in excessive:
                with owner.begin() as connection:
                    connection.exec_driver_sql(f'GRANT {privilege} TO "{grantee}"')
                rejected = apply_grants()
                assert rejected.returncode != 0, "Overprivileged identity was accepted"
                assert "history_projection_privileges_denied" in rejected.stderr
                with owner.begin() as connection:
                    connection.exec_driver_sql(f'REVOKE {privilege} FROM "{grantee}"')

            for _ in range(2):
                granted = apply_grants()
                assert granted.returncode == 0, granted.stderr
            with owner.begin() as connection:
                connection.execute(text("INSERT INTO lk_organizations(organization_id,slug,name) "
                    "VALUES(71001,'history-role-synthetic','Synthetic')"))
                connection.execute(text("INSERT INTO marketplace_accounts(organization_id,marketplace_account_id,marketplace,external_account_id,status) "
                    "VALUES(71001,71001,'wb','synthetic-history-role','connected')"))
                role = connection.execute(text("SELECT oid,rolcanlogin,rolsuper,rolbypassrls,rolcreaterole,rolcreatedb,rolinherit "
                    "FROM pg_roles WHERE rolname=:name"), {"name": helper}).one()
                assert tuple(role)[1:] == (False,) * 6
                routine = connection.execute(text("SELECT proowner,prosecdef,proconfig FROM pg_proc WHERE oid="
                    "'public.wb_history_projection_apply_parent(integer,integer,bigint,bigint)'::regprocedure")).one()
                assert routine.proowner == role.oid and routine.prosecdef
                assert "search_path=pg_catalog, public" in routine.proconfig
                assert not connection.scalar(text("SELECT EXISTS(SELECT FROM pg_auth_members WHERE member=:role OR roleid=:role)"), {"role": role.oid})
            with worker.begin() as connection:
                _scope(connection)
                parent = connection.scalar(text("INSERT INTO marketplace_orders(organization_id,marketplace_account_id,marketplace,external_order_id) "
                    "VALUES(71001,71001,'wb','synthetic-parent') RETURNING order_id"))
                assert connection.scalar(text("SELECT order_id FROM marketplace_orders WHERE order_id=:parent FOR UPDATE"), {"parent": parent}) == parent
                # Even empty exact lookups must have the real resolver's locks;
                # these timestamp privileges do not authorize business updates.
                connection.execute(text("SELECT marketplace_product_id,updated_at FROM marketplace_products "
                    "WHERE organization_id=71001 AND marketplace_account_id=71001 FOR UPDATE")).all()
                connection.execute(text("SELECT marketplace_offer_id,external_offer_key,catalog_sku_id,updated_at FROM marketplace_offers "
                    "WHERE organization_id=71001 AND marketplace_account_id=71001 FOR SHARE")).all()
                connection.execute(text("SELECT catalog_sku_id,updated_at FROM catalog_skus "
                    "WHERE organization_id=71001 FOR SHARE")).all()
            statements = (
                ("UPDATE marketplace_orders SET version=version+1 WHERE order_id=:parent", "42501"),
                ("UPDATE marketplace_orders SET order_id=order_id WHERE order_id=:parent", "428C9"),
                ("UPDATE marketplace_orders SET order_id=order_id+1 WHERE order_id=:parent", "428C9"),
                ("UPDATE marketplace_orders SET order_id=DEFAULT WHERE order_id=:parent", "P0001"),
                ("SELECT ciphertext FROM marketplace_account_credentials", "42501"),
                ("SELECT wb_token FROM lk_user_wb_tokens", "42501"),
                ("UPDATE marketplace_products SET title='synthetic-denied' WHERE organization_id=71001", "42501"),
                ("UPDATE marketplace_offers SET catalog_sku_id=NULL WHERE organization_id=71001", "42501"),
                ("UPDATE catalog_skus SET code='synthetic-denied' WHERE organization_id=71001", "42501"),
            )
            for statement, expected_state in statements:
                with worker.connect() as connection, connection.begin():
                    _scope(connection)
                    with pytest.raises(DBAPIError) as denied:
                        connection.execute(text(statement), {"parent": parent})
                    assert getattr(denied.value.orig, "sqlstate", None) == expected_state
                    connection.rollback()
            with owner.connect() as connection:
                assert connection.scalar(text("SELECT version FROM marketplace_orders WHERE order_id=:parent"), {"parent": parent}) == 1
        finally:
            worker.dispose()
            owner.dispose()


def _scope(connection):
    connection.execute(text("SELECT set_config('app.organization_id','71001',true),"
        "set_config('app.marketplace_account_id','71001',true)"))
