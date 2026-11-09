from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import event, select

from app.platform.advertising.orm import (
    WbAdvertisingFactRow,
    WbAdvertisingSpendDocumentRow,
    WbAdvertisingSyncRunRow,
)
from app.platform.advertising.service import AdvertisingService
from app.platform.period import Period
from tests.test_advertising_raw_service import (
    _map_nm,
    raw_bundle,
    session,
)  # noqa: F401

PERIOD = Period(date(2026, 9, 1), date(2026, 9, 2))
NOW = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)


def _bundle(raw, period, spend=12300):
    bundle = json.loads(
        json.dumps(raw).replace("2026-09-01", period.date_from.isoformat())
    )
    end = period.date_to.isoformat()
    bundle["period"]["dateTo"] = end
    bundle["upd"][0]["dateTo"] = end
    bundle["fullstats"][0]["dateTo"] = end
    bundle["manifest"][2]["request"]["to"] = end
    bundle["manifest"][3]["request"]["endDate"] = end
    if spend is None:
        bundle["promotion"] = {"adverts": [], "all": 0}
        bundle["adverts"] = {"adverts": []}
        bundle["fullstats"] = []
        bundle["upd"][0]["payload"] = []
        bundle["manifest"].pop()
    else:
        first, second = bundle["fullstats"][0]["payload"]
        first["sum"] = first["days"][0]["sum"] = spend / 100
        app = first["days"][0]["apps"][0]
        app["sum"] = app["nms"][0]["sum"] = (spend - 40) / 100
        second["sum"] = second["days"][0]["sum"] = 0
    return bundle


def _ingest(service, raw, period, spend=12300, observed=NOW, account=31):
    return service.ingest_raw_payload(
        account,
        period,
        _bundle(raw, period, spend),
        source_reference="test:coverage",
        observed_at=observed,
    )


def _day(number):
    return Period(date(2026, 9, number), date(2026, 9, number))


def test_daily_coverage_replaces_versions_and_has_stable_composite_evidence(
    session, raw_bundle
):
    _map_nm(session)
    service = AdvertisingService(session, 1, now=lambda: NOW)
    first = _ingest(service, raw_bundle, _day(1), observed=NOW - timedelta(hours=1))
    second = _ingest(service, raw_bundle, _day(2))
    source = service.get_pnl_source(31, PERIOD)
    assert (source.state, source.total_spend_kopecks, source.spend_by_nm) == (
        "ready",
        24600,
        {2001: 24600},
    )
    snapshot = source.snapshot
    assert snapshot.period == PERIOD
    assert snapshot.formula_version == "wb-advertising-daily-v1"
    assert snapshot.source_kind == "ads_fullstats" and snapshot.evidence_status == "raw"
    assert snapshot.sync_run_id not in {first.sync_run_id, second.sync_run_id}
    assert str(uuid.UUID(snapshot.sync_run_id)) == snapshot.sync_run_id
    assert snapshot.snapshot_checksum not in {
        first.snapshot_checksum,
        second.snapshot_checksum,
    }
    assert snapshot.captured_at == snapshot.last_observed_at == first.captured_at
    for field in (
        "fact_count",
        "campaign_count",
        "spend_document_count",
        "expected_request_count",
        "completed_request_count",
        "source_total_spend_kopecks",
        "document_total_spend_kopecks",
    ):
        assert getattr(snapshot, field) == getattr(first, field) + getattr(
            second, field
        )
    assert service.get_pnl_source(31, PERIOD).snapshot == snapshot
    assert session.get(WbAdvertisingSyncRunRow, snapshot.sync_run_id) is None
    _ingest(service, raw_bundle, _day(1), 12000, NOW + timedelta(hours=1))
    corrected = service.get_pnl_source(31, PERIOD)
    assert corrected.total_spend_kopecks == 24300
    assert corrected.spend_by_nm == {2001: 24300}
    assert corrected.snapshot.snapshot_checksum != snapshot.snapshot_checksum


@pytest.mark.parametrize(
    "spends, state, total",
    [
        ((12300,), "missing", None),
        ((None, 12300), "ready", 12300),
        ((None, None), "empty", 0),
    ],
)
def test_missing_and_known_empty_days(session, raw_bundle, spends, state, total):
    service = AdvertisingService(session, 1, now=lambda: NOW)
    for day, spend in enumerate(spends, 1):
        _ingest(service, raw_bundle, _day(day), spend)
    source = service.get_pnl_source(31, PERIOD)
    assert (source.state, source.total_spend_kopecks) == (state, total)
    assert source.spend_by_nm == {}
    assert source.unattributed_spend_kopecks == total
    if total is None:
        assert source.blocker_ids == ("WB_PNL_ADS_NOT_CANONICAL",)


def test_intraday_requires_post_close_observation_even_on_following_day(
    session, raw_bundle
):
    clock = [datetime(2026, 9, 1, 12, tzinfo=timezone.utc)]
    service = AdvertisingService(session, 1, now=lambda: clock[0])
    first = _ingest(service, raw_bundle, _day(1), observed=clock[0])
    current = service.get_pnl_source(31, _day(1))
    assert (current.state, current.total_spend_kopecks) == ("partial", None)
    clock[0] = NOW
    stale = service.get_pnl_source(31, _day(1))
    assert (stale.state, stale.total_spend_kopecks) == ("missing", None)
    assert stale.blocker_ids == ("WB_PNL_ADS_NOT_CANONICAL",)
    replay = _ingest(service, raw_bundle, _day(1), observed=_day(1).end_exclusive_at)
    assert replay.sync_run_id == first.sync_run_id
    assert service.get_pnl_source(31, _day(1)).total_spend_kopecks == 12300


@pytest.mark.parametrize(
    "exact_offset, daily_offsets, expected",
    [
        (0, (-1, 1), 1000),
        (0, (1, 2), 24600),
        (0, (0, 1), 1000),
    ],
)
def test_exact_and_daily_choose_one_coverage_by_conservative_freshness(
    session,
    raw_bundle,
    exact_offset,
    daily_offsets,
    expected,
):
    service = AdvertisingService(session, 1, now=lambda: NOW)
    exact = _ingest(
        service, raw_bundle, PERIOD, 1000, NOW + timedelta(hours=exact_offset)
    )
    for day, offset in enumerate(daily_offsets, 1):
        _ingest(service, raw_bundle, _day(day), observed=NOW + timedelta(hours=offset))
    result = service.get_pnl_source(31, PERIOD)
    assert result.total_spend_kopecks == expected
    assert (result.snapshot.sync_run_id == exact.sync_run_id) == (expected == 1000)


@pytest.mark.parametrize(
    "incomplete", ["materialization", "requests", "zero_requests", "intraday"]
)
def test_incomplete_new_daily_version_does_not_hide_complete(
    session, raw_bundle, incomplete
):
    service = AdvertisingService(session, 1, now=lambda: NOW)
    for day in (1, 2):
        _ingest(service, raw_bundle, _day(day))
    newer = _ingest(service, raw_bundle, _day(1), 12000, NOW + timedelta(hours=1))
    row = session.get(WbAdvertisingSyncRunRow, newer.sync_run_id)
    if incomplete == "materialization":
        row.is_materialized = False
    elif incomplete == "requests":
        row.completed_request_count -= 1
    elif incomplete == "zero_requests":
        row.expected_request_count = row.completed_request_count = 0
    else:
        row.last_observed_at = _day(1).end_exclusive_at - timedelta(seconds=1)
    session.commit()
    assert service.get_pnl_source(31, PERIOD).total_spend_kopecks == 24600


def test_wider_window_and_other_tenant_cannot_fill_missing_day(session, raw_bundle):
    service = AdvertisingService(session, 1, now=lambda: NOW)
    _ingest(service, raw_bundle, _day(1))
    _ingest(service, raw_bundle, Period(date(2026, 9, 1), date(2026, 9, 3)))
    other = AdvertisingService(session, 2, now=lambda: NOW)
    _ingest(other, raw_bundle, _day(2), account=32)
    _ingest(other, raw_bundle, PERIOD, account=32)
    result = service.get_pnl_source(31, PERIOD)
    assert (result.state, result.total_spend_kopecks, result.spend_by_nm) == (
        "missing",
        None,
        {},
    )


@pytest.mark.parametrize(
    "parents, expected, diagnostic",
    [
        ((149, 149), 300, "WB_ADS_HIERARCHY_TOLERANCE_APPLIED"),
        ((148, 152), None, "WB_ADS_HIERARCHY_RECONCILIATION_FAILED"),
        ((None, 150), None, "WB_ADS_SOURCE_METRIC_INCOMPLETE"),
    ],
)
def test_daily_metric_validation_cannot_cancel_errors_or_fall_back(
    session,
    raw_bundle,
    parents,
    expected,
    diagnostic,
):
    _map_nm(session)
    service = AdvertisingService(session, 1, now=lambda: NOW)
    _ingest(service, raw_bundle, PERIOD, 1000, NOW - timedelta(hours=1))
    for day, parent in enumerate(parents, 1):
        snapshot = _ingest(service, raw_bundle, _day(day), 150)
        row = session.scalar(
            select(WbAdvertisingFactRow).where(
                WbAdvertisingFactRow.sync_run_id == snapshot.sync_run_id,
                WbAdvertisingFactRow.fact_scope == "campaign",
                WbAdvertisingFactRow.grain == "period",
                WbAdvertisingFactRow.campaign_id == 1001,
            )
        )
        row.spend_kopecks = parent
    session.commit()
    result = service.get_pnl_source(31, PERIOD)
    assert result.total_spend_kopecks == expected
    assert result.state == ("ready" if expected is not None else "partial")
    if expected is None:
        assert result.spend_by_nm == {}
        assert result.blocker_ids == (diagnostic,)
    else:
        assert result.spend_by_nm == {2001: 300}
        assert diagnostic in service.get_raw_reconciliation(31, PERIOD).diagnostics


def test_signed_documents_remain_separate_from_operational_spend(session, raw_bundle):
    service = AdvertisingService(session, 1, now=lambda: NOW)
    for day in (1, 2):
        bundle = _bundle(raw_bundle, _day(day), 200)
        bundle["upd"][0]["payload"][1]["updSum"] = -2
        service.ingest_raw_payload(
            31, _day(day), bundle, source_reference="test:documents"
        )
    result = service.get_raw_reconciliation(31, PERIOD)
    assert result.document_spend_kopecks == -100
    assert service.get_pnl_source(31, PERIOD).total_spend_kopecks == 400
    assert result.unknown_spend_kopecks == 0


def test_period_reconciles_each_day_without_accumulating_rounding_as_spend(
    session, raw_bundle
):
    _map_nm(session)
    service = AdvertisingService(session, 1, now=lambda: NOW)
    for offset in (-1, 1):
        bundle = _bundle(raw_bundle, PERIOD, 150)
        campaign = bundle["fullstats"][0]["payload"][0]
        second_day = json.loads(json.dumps(campaign["days"][0]))
        second_day["date"] = "2026-09-02T00:00:00Z"
        campaign["days"].append(second_day)
        for day in campaign["days"]:
            day["sum"] = (150 + offset) / 100
        for metric in ("views", "clicks", "atbs", "orders", "sum_price", "canceled"):
            campaign[metric] *= 2
        campaign["sum"] = (300 + 3 * offset) / 100
        snapshot = service.ingest_raw_payload(
            31, PERIOD, bundle, source_reference="test:rounding",
            observed_at=NOW + timedelta(minutes=offset),
        )
        source = service.get_pnl_source(31, PERIOD)
        assert (source.total_spend_kopecks, source.spend_by_nm) == (300, {2001: 300})
        assert source.unattributed_spend_kopecks == 0
        assert "WB_ADS_HIERARCHY_TOLERANCE_APPLIED" in service.get_raw_reconciliation(
            31, PERIOD
        ).diagnostics

        period_row = session.scalar(
            select(WbAdvertisingFactRow).where(
                WbAdvertisingFactRow.sync_run_id == snapshot.sync_run_id,
                WbAdvertisingFactRow.campaign_id == 1001,
                WbAdvertisingFactRow.grain == "period",
            )
        )
        period_row.spend_kopecks = 300 + 2 * offset - 2
        session.commit()
        invalid = service.get_pnl_source(31, PERIOD)
        assert invalid.total_spend_kopecks is None
        assert invalid.blocker_ids == ("WB_ADS_HIERARCHY_RECONCILIATION_FAILED",)


@pytest.mark.parametrize("scope", ["facts", "documents"])
def test_campaign_coverage_requires_same_run_and_interval(session, raw_bundle, scope):
    service = AdvertisingService(session, 1, now=lambda: NOW)
    first = _ingest(service, raw_bundle, _day(1), 150)
    _ingest(service, raw_bundle, _day(2), 150)
    if scope == "facts":
        campaign = session.scalar(
            select(WbAdvertisingFactRow).where(
                WbAdvertisingFactRow.sync_run_id == first.sync_run_id,
                WbAdvertisingFactRow.fact_scope == "campaign",
                WbAdvertisingFactRow.grain == "period",
                WbAdvertisingFactRow.campaign_id == 1001,
            )
        )
        campaign.date_to = PERIOD.date_to
    else:
        document = session.scalar(
            select(WbAdvertisingSpendDocumentRow).where(
                WbAdvertisingSpendDocumentRow.sync_run_id == first.sync_run_id,
                WbAdvertisingSpendDocumentRow.campaign_id == 1001,
            )
        )
        document.business_date = PERIOD.date_to
    session.commit()
    result = service.get_raw_reconciliation(31, PERIOD)
    assert service.get_pnl_source(31, PERIOD).total_spend_kopecks == 300
    assert result.unknown_spend_kopecks == (150 if scope == "documents" else 0)


@pytest.mark.parametrize("tie_breaker", ["captured_at", "sync_run_id"])
@pytest.mark.parametrize("period", [_day(1), PERIOD])
def test_version_ties_choose_latest_capture_then_run_id(
    session, raw_bundle, tie_breaker, period
):
    service = AdvertisingService(session, 1, now=lambda: NOW)
    first = _ingest(service, raw_bundle, period, 1000)
    second = _ingest(service, raw_bundle, period, 2000, NOW + timedelta(hours=1))
    row = session.get(WbAdvertisingSyncRunRow, second.sync_run_id)
    row.last_observed_at = NOW
    if tie_breaker == "sync_run_id":
        row.captured_at = NOW
    session.commit()
    expected = (
        2000
        if tie_breaker == "captured_at" or second.sync_run_id > first.sync_run_id
        else 1000
    )
    assert service.get_pnl_source(31, period).total_spend_kopecks == expected


def test_new_invalid_exact_coverage_does_not_fall_back_to_daily(session, raw_bundle):
    service = AdvertisingService(session, 1, now=lambda: NOW)
    for day in (1, 2):
        _ingest(service, raw_bundle, _day(day))
    latest = _ingest(service, raw_bundle, PERIOD, 2000, NOW + timedelta(hours=1))
    row = session.scalar(
        select(WbAdvertisingFactRow).where(
            WbAdvertisingFactRow.sync_run_id == latest.sync_run_id,
            WbAdvertisingFactRow.fact_scope == "source_sku",
        )
    )
    row.spend_kopecks = None
    session.commit()
    result = service.get_pnl_source(31, PERIOD)
    assert (result.state, result.total_spend_kopecks) == ("partial", None)
    assert result.snapshot.sync_run_id == latest.sync_run_id
    assert result.blocker_ids == ("WB_ADS_SOURCE_METRIC_INCOMPLETE",)


def test_sql_read_count_is_constant_for_daily_coverage(session, raw_bundle):
    _map_nm(session)
    service = AdvertisingService(session, 1, now=lambda: NOW + timedelta(days=40))
    for day in range(1, 16):
        _ingest(service, raw_bundle, _day(day), observed=NOW + timedelta(days=40))
    statements = []

    def record(_conn, _cursor, statement, _params, _context, _executemany):
        statements.append(statement)

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        counts = []
        for days in (2, 15):
            statements.clear()
            result = service.get_pnl_source(
                31, Period(date(2026, 9, 1), date(2026, 9, days))
            )
            assert result.total_spend_kopecks == days * 12300
            counts.append(len(statements))
            assert sum("FROM wb_advertising_facts" in sql for sql in statements) == 1
            assert (
                sum("FROM wb_advertising_spend_documents" in sql for sql in statements)
                == 1
            )
        assert counts[0] == counts[1]
    finally:
        event.remove(engine, "before_cursor_execute", record)
