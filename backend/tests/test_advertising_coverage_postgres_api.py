"""Read full daily advertising coverage through the real PostgreSQL HTTP path."""

import json
from datetime import timedelta

from alembic import command
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.platform.advertising.orm import WbAdvertisingSyncRunRow
from app.platform.advertising.service import AdvertisingService
from app.platform.period import Period
from tests import test_abc_pnl_v2 as seeded
from tests.test_advertising_period_coverage import NOW
from tests.test_empty_database_migrations import cluster, database  # noqa: F401


def daily_bundle(day, spend=123):
    raw = json.dumps(seeded.raw_advertising_bundle())
    for original in ("2026-08-17", "2026-08-23", "2026-08-20"):
        raw = raw.replace(original, day.isoformat())
    bundle = json.loads(raw)
    campaign = bundle["fullstats"][0]["payload"][0]
    daily = campaign["days"][0]
    app = daily["apps"][0]
    for node in (campaign, daily, app, app["nms"][0]):
        node["sum"] = spend
    return bundle


def test_postgres_http_daily_ads_coverage_and_corrected_versions(database, monkeypatch):
    config, engine = database
    command.upgrade(config, "head")
    # Reuse the existing authorized API fixture and financial/catalog seed.
    monkeypatch.setattr(seeded, "create_engine", lambda *args, **kwargs: engine)
    api = seeded.api.__wrapped__()
    monkeypatch.setattr("app.routers.wb_reports_v2.utc_now", lambda: NOW)

    def no_provider(*args, **kwargs):
        raise AssertionError("HTTP report must read local evidence only")

    monkeypatch.setattr("app.repricer_bff.fetch_finance_report_aggregates", no_provider)
    monkeypatch.setattr("app.repricer_bff.fetch_ads_spend_aggregates", no_provider)

    def read():
        response = api.get(
            "/api/v2/wb/reports/abc-pnl",
            params=seeded.params(),
            headers=seeded.headers("finance-all"),
        )
        assert response.status_code == 200, response.text
        return response.json()

    try:
        with Session(engine) as session:
            # Keep original evidence, but make it unavailable as coverage.
            session.execute(
                update(WbAdvertisingSyncRunRow).values(is_materialized=False)
            )
            session.commit()
            service = AdvertisingService(session, 1, now=lambda: NOW)

            def ingest(day, *, spend=123, account=31, observed=NOW):
                return service.ingest_raw_payload(
                    account,
                    Period(day, day),
                    daily_bundle(day, spend),
                    source_reference="synthetic:postgres-coverage",
                    observed_at=observed,
                )

            first = seeded.PERIOD.date_from
            for offset in range(1, 7):
                ingest(first + timedelta(days=offset))
            ingest(first, account=33)
            missing = read()
            assert missing["summary"]["advertisingSpendKopecks"] is None
            assert missing["items"][0]["advertisingSpendKopecks"] is None
            assert missing["summary"]["profitBeforeLoyaltyKopecks"] is None
            assert "WB_PNL_ADS_NOT_CANONICAL" in missing["meta"]["blockerIds"]

            ingest(first)
            complete = read()
            assert complete["summary"]["advertisingSpendKopecks"] == 86_100
            assert complete["items"][0]["advertisingSpendKopecks"] == 86_100
            assert complete["summary"]["unattributedAdvertisingSpendKopecks"] == 0
            assert complete["summary"]["profitBeforeLoyaltyKopecks"] == 7_452
            assert complete["summary"]["profitAfterLoyaltyKopecks"] == 6_577
            assert complete["summary"]["internalExpensesKopecks"] == 0
            assert complete["meta"]["advertisingSource"] == "ads_fullstats"
            assert complete["meta"]["advertisingEvidenceStatus"] == "raw"
            assert complete["meta"]["blockerIds"] == ["WB_PNL_TAX_POLICY_NOT_CONFIRMED_750"]
            checksum = complete["meta"]["advertisingSnapshotChecksum"]
            assert len(checksum) == 64
            assert read()["meta"]["advertisingSnapshotChecksum"] == checksum

            ingest(first, spend=100, observed=NOW + timedelta(hours=1))
            corrected = read()
            assert corrected["summary"]["advertisingSpendKopecks"] == 83_800
            assert corrected["items"][0]["advertisingSpendKopecks"] == 83_800
            assert corrected["meta"]["advertisingSnapshotChecksum"] != checksum
            assert corrected["summary"]["netProfitKopecks"] is None
            assert corrected["items"][0]["profitClass"] is None
            assert corrected["items"][0]["abcCode"] is None
    finally:
        api.close()
