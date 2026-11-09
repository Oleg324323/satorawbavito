"""Run price ingestion/reporting against isolated SQLite, never the user's DB."""
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator

from app.cabinet.orm import LkSessionRow
from app.repricer_cache.orm import WbRepricerGoodsCacheRow, WbRepricerSourceCacheRow
from app.routers import wb_browser_prices as browser
from tests.test_local_wb_browser_prices import account, local  # noqa: F401
from tests.test_wb_browser_prices import (
    test_corrected_three_price_observation_replaces_db_cache_and_reaches_repricer_xlsx as _check_reporting,
)


class PreviewUtcDateTime(TypeDecorator):
    impl = DateTime
    cache_ok = True

    def process_result_value(self, value, dialect):
        return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


@pytest.fixture
def utc_storage(local, monkeypatch):
    # Match the launcher's UTC restoration without starting the application.
    for model in (LkSessionRow, WbRepricerGoodsCacheRow, WbRepricerSourceCacheRow):
        for column in model.__table__.columns:
            if isinstance(column.type, DateTime) and column.type.timezone:
                monkeypatch.setattr(column, 'type', PreviewUtcDateTime(timezone=True))


@pytest.fixture
def reporting_account(utc_storage, account):
    return account


def test_corrected_observation_http_database_reporting_roundtrip(reporting_account, monkeypatch):
    account = reporting_account
    from app import repricer_bff as bff
    monkeypatch.setattr(bff, 'get_settings', lambda: SimpleNamespace(repricer_preserve_local_price_overrides=False))
    now = datetime.now(timezone.utc)
    WbRepricerGoodsCacheRow.__table__.create(account.factory.kw['bind'])
    with account.factory() as session, session.begin():
        session.add(WbRepricerGoodsCacheRow(organization_id=1, page_offset=0, page_limit=1000, fetched_at=now,
            goods_payload=[{'vendorCode': 'A', 'nmID': 101, 'sizes': [{'sizeID': 1001, 'price': 4300, 'discountedPrice': 2064}]}]))
    _check_reporting(SimpleNamespace(factory=account.factory, client=account.client, now=now, module=browser), monkeypatch)
