from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import insert, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from alembic import command
from app.cabinet.orm import LkOrganizationRow
from app.platform.finance.orm import WbFinanceOperationRow
from app.platform.finance.service import (
    FinanceService,
    normalize_operation,
    operation_values,
)
from app.platform.integrations.orm import MarketplaceAccountRow
from tests.test_empty_database_migrations import cluster, database
from tests.test_finance_pnl_rollup import NOW, PERIOD, settlement_rows

PAYABLE_TABLES = (
    "wb_finance_operations",
    "wb_finance_sync_run_sku_pnl_rollups",
    "wb_finance_sync_run_sku_daily_pnl_rollups",
)


@pytest.mark.parametrize("initial_revision", ["20260905_0060", "20260910_0085"])
def test_payable_migration_preserves_legacy_unknown_and_refuses_evidence_loss(
    database, initial_revision
):
    config, engine = database
    command.upgrade(config, initial_revision)
    operation = normalize_operation(settlement_rows()[0], 1, 31, PERIOD)
    legacy = operation_values(operation)
    legacy.pop("payable_kopecks", None)
    with Session(engine) as session, session.begin():
        session.add(
            LkOrganizationRow(organization_id=1, slug="finance", name="Finance")
        )
        session.flush()
        session.execute(
            insert(MarketplaceAccountRow.__table__).values(
                organization_id=1,
                marketplace_account_id=31,
                marketplace="wb",
                external_account_id="synthetic-finance",
                status="connected",
            )
        )
        session.flush()
        session.execute(insert(WbFinanceOperationRow.__table__).values(**legacy))
        before = session.execute(
            text("SELECT to_jsonb(operation) FROM wb_finance_operations operation")
        ).scalar_one()

    command.upgrade(config, "20260911_0086")
    with engine.begin() as connection:
        assert connection.execute(
            text("SELECT to_jsonb(operation) FROM wb_finance_operations operation")
        ).scalar_one() == {**before, "payable_kopecks": None}
        row = connection.execute(
            text(
                "SELECT payable_kopecks, commission_kopecks, payload_checksum FROM wb_finance_operations"
            )
        ).one()
        assert tuple(row) == (None, 10_000, operation.payload_checksum)
    command.downgrade(config, "20260910_0085")
    command.upgrade(config, "20260911_0086")

    observed = normalize_operation(
        settlement_rows()[0] | {"forPay": "863.00"}, 1, 31, PERIOD
    )
    with engine.begin() as connection:
        connection.execute(
            insert(WbFinanceOperationRow.__table__).values(**operation_values(observed))
        )
    with pytest.raises(
        DBAPIError, match="cannot downgrade while canonical payable evidence exists"
    ):
        command.downgrade(config, "20260910_0085")
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "20260911_0086"
        )
        assert (
            connection.scalar(
                text("SELECT sum(payable_kopecks) FROM wb_finance_operations")
            )
            == 86_300
        )


@pytest.mark.parametrize("evidence_table", PAYABLE_TABLES)
def test_payable_downgrade_rejects_hidden_evidence_and_restores_rls(
    database, evidence_table
):
    config, engine = database
    command.upgrade(config, "20260911_0086")
    revision = ScriptDirectory.from_config(config).get_revision("20260911_0086").module
    assert revision.TABLES == PAYABLE_TABLES
    with Session(engine) as session:
        session.add(
            LkOrganizationRow(organization_id=1, slug="finance", name="Finance")
        )
        session.flush()
        session.add(
            MarketplaceAccountRow(
                organization_id=1,
                marketplace_account_id=31,
                marketplace="wb",
                external_account_id="synthetic-finance",
                status="connected",
            )
        )
        session.commit()
        FinanceService(session, organization_id=1, now=lambda: NOW).ingest_snapshot(
            31,
            PERIOD,
            [settlement_rows()[0] | {"forPay": "863.00"}],
            observed_at=NOW,
        )
    with engine.begin() as connection:
        for table in PAYABLE_TABLES:
            if table != evidence_table:
                connection.exec_driver_sql(f"UPDATE {table} SET payable_kopecks = NULL")
        before = {
            table: connection.execute(
                text(
                    "SELECT relowner, relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE oid = CAST(:table AS regclass)"
                ),
                {"table": table},
            ).one()
            for table in PAYABLE_TABLES
        }
        assert all(row[1:] == (True, True) for row in before.values())
    hidden_owner = "finance_hidden_" + uuid4().hex
    with pytest.raises(
        DBAPIError, match="cannot downgrade while canonical payable evidence exists"
    ):
        with engine.begin() as connection:
            connection.exec_driver_sql(
                f"CREATE ROLE {hidden_owner} NOSUPERUSER NOBYPASSRLS"
            )
            for table in PAYABLE_TABLES:
                connection.exec_driver_sql(
                    f"ALTER TABLE {table} OWNER TO {hidden_owner}"
                )
            connection.exec_driver_sql(f"SET LOCAL ROLE {hidden_owner}")
            connection.exec_driver_sql("SET LOCAL app.organization_id = '2'")
            assert (
                connection.exec_driver_sql(
                    f"SELECT count(*) FROM {evidence_table}"
                ).scalar_one()
                == 0
            )
            with Operations.context(MigrationContext.configure(connection)):
                revision.downgrade()
            pytest.fail("downgrade must reject hidden evidence")
    with engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "20260911_0086"
        )
        for table in PAYABLE_TABLES:
            assert (
                connection.execute(
                    text(
                        "SELECT relowner, relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE oid = CAST(:table AS regclass)"
                    ),
                    {"table": table},
                ).one()
                == before[table]
            )
            assert connection.exec_driver_sql(
                f"SELECT payable_kopecks FROM {table}"
            ).scalar_one() == (86_300 if table == evidence_table else None)
        assert (
            connection.scalar(
                text("SELECT count(*) FROM pg_roles WHERE rolname = :role"),
                {"role": hidden_owner},
            )
            == 0
        )
