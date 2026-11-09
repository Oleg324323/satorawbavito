"""Disposable PostgreSQL only; never migrate the working or production DB."""
from datetime import datetime, timezone

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.cabinet.orm import LkOrganizationRow
from app.platform.integrations.orm import MarketplaceAccountRow
from app.platform.finance.service import FinanceService
from app.platform.period import Period
from tests.test_empty_database_migrations import cluster, database  # noqa: F401
from tests.test_wb_finance_completion import START, END
from tests.test_repricer_page_finance import operation


def test_classified_finance_upgrade_roundtrip_and_guard_preserve_evidence(database):
    config, engine = database
    command.upgrade(config, "head")
    migration = ScriptDirectory.from_config(config).get_revision("20261020_0094").module
    for table in migration.TABLES:
        fields = {column["name"]: column for column in inspect(engine).get_columns(table)}
        assert fields["advertising_kopecks"]["nullable"]
        assert not fields["compensation_kopecks"]["nullable"]
    command.downgrade(config, "20261009_0093")
    command.upgrade(config, "head")
    now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    with Session(engine) as session:
        session.add(LkOrganizationRow(organization_id=1, slug="synthetic", name="Synthetic"))
        session.flush()
        session.add(MarketplaceAccountRow(marketplace_account_id=31, organization_id=1,
            marketplace="wb", external_account_id="synthetic", status="connected"))
        session.commit()
        FinanceService(session, 1, now=lambda: now).ingest_snapshot(31, Period(START, END), [
            operation(rrDate=str(START), saleDt=str(START), deduction="12.34", bonusTypeName="WB Продвижение")])
    with engine.connect() as connection:
        before = connection.execute(text("SELECT payload_checksum, advertising_kopecks FROM wb_finance_operations")).all()
        assert before[0][1] == 1234
    with pytest.raises(DBAPIError, match="classified finance evidence exists"):
        command.downgrade(config, "20261009_0093")
    with engine.connect() as connection:
        assert connection.execute(text("SELECT payload_checksum, advertising_kopecks FROM wb_finance_operations")).all() == before
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20261020_0094"
        assert all(row == (True, True) for row in connection.execute(text(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = ANY(:tables)"),
            {"tables": list(migration.TABLES)}).all())
