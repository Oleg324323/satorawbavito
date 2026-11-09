from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.repricer_cache.store import FINANCE_REVENUE_BASIS, FINANCE_SCHEMA_VERSION
from app.routers import wb_repricer_bff


@pytest.mark.parametrize("known_dates", [True, None, []])
def test_daily_stitch_skips_only_known_duplicate_days(monkeypatch, known_dates):
    first, last = "2026-09-01", "2026-09-02"
    days = [first] * 50 + [last]
    payloads = {
        f"finance_synthetic_{index}": {
            "revenueBasis": FINANCE_REVENUE_BASIS, "financeSchemaVersion": FINANCE_SCHEMA_VERSION,
            "dailyAggregates": {day: {"123": {"additionalPaymentKopecks": -100}}},
        }
        for index, day in enumerate(days)
    }
    metadata = [
        {"sourceKey": "finance_empty", "dateFrom": first, "dateTo": last,
         "dailyAggregateDates": [], "dailyAggregatesDays": 0},
    ] + [
        {"sourceKey": key, "dateFrom": first, "dateTo": last,
         "dailyAggregateDates": list(payload["dailyAggregates"]) if known_dates else known_dates}
        for key, payload in payloads.items()
    ]
    reads = []

    def read(organization_id, source_key, *, slim):
        assert organization_id == 2 and slim is False
        reads.append(source_key)
        if source_key == "finance_empty":
            return {"dailyAggregates": {}}
        return payloads[source_key]

    def list_ranges(organization_id, prefix, *, limit):
        assert (organization_id, prefix, limit) == (2, "finance_", 100)
        return metadata

    monkeypatch.setattr(wb_repricer_bff, "get_source_cache", read)
    monkeypatch.setattr(wb_repricer_bff, "list_source_cache_ranges_by_prefix", list_ranges)
    result = wb_repricer_bff._stitched_period_cache_from_days(
        2, "finance", date.fromisoformat(first), date.fromisoformat(last),
    )

    assert result == {
        "aggregates": {"123": {"additionalPaymentKopecks": -200}},
        "dailyAggregates": {day: {"123": {"additionalPaymentKopecks": -100}} for day in (first, last)},
        "count": 1,
        "fetchedAt": None,
        "coverageState": "ok",
        "coveredDays": 2,
        "requestedDays": 2,
        "missingDates": [],
        "revenueBasis": FINANCE_REVENUE_BASIS,
        "financeSchemaVersion": FINANCE_SCHEMA_VERSION,
    }
    assert reads == (["finance_synthetic_0", "finance_synthetic_50"] if known_dates else list(payloads))


def test_numeric_preset_uses_covering_date_range_cache(monkeypatch) -> None:
    covering = {
        "dateFrom": "2026-08-26",
        "dateTo": "2026-09-01",
        "periodDays": 7,
        "revenueBasis": FINANCE_REVENUE_BASIS,
        "financeSchemaVersion": FINANCE_SCHEMA_VERSION,
        "dailyAggregates": {
            "2026-08-26": {"123": {"buyerRevenueKopecks": 100}},
            "2026-09-01": {"123": {"buyerRevenueKopecks": 200}},
        },
        "aggregates": {"123": {"buyerRevenueKopecks": 300}},
    }
    monkeypatch.setattr(
        wb_repricer_bff, "get_source_cache", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        wb_repricer_bff,
        "get_covering_source_cache",
        lambda *_args, **_kwargs: covering,
    )

    result = wb_repricer_bff._load_period_source_cache(
        2,
        "finance",
        "7",
        7,
        datetime(2026, 8, 26, tzinfo=timezone.utc),
        datetime(2026, 9, 1, tzinfo=timezone.utc),
        require_full_sync_coverage=False,
    )

    assert result["aggregates"]["123"]["buyerRevenueKopecks"] == 300
    assert result["coveredByCache"]["dateFrom"] == "2026-08-26"


@pytest.mark.parametrize("path", ["exact", "covering", "daily_union"])
@pytest.mark.parametrize("previous", [True, False])
@pytest.mark.parametrize("prefix", ["finance", "ads"])
def test_period_reader_rejects_previous_withdrawal_semantics(monkeypatch, path, previous, prefix):
    schema = "v3" if previous else FINANCE_SCHEMA_VERSION
    row = {"sellerRevenueKopecks": 10000, "paymentScheduleKopecks": 1000,
           "additionalPaymentKopecks": 1000 if previous else -1000}
    cache = {"dateFrom": "2026-08-26", "dateTo": "2026-09-01",
             "revenueBasis": FINANCE_REVENUE_BASIS, "financeSchemaVersion": schema,
             "aggregates": {"123": row}, "dailyAggregates": {"2026-08-26": {"123": row}}}
    key = f"{prefix}_2026-08-26_2026-09-01"
    if path == "daily_union":
        key = f"{prefix}_2026-08-26_2026-08-26"
        cache["dateTo"] = "2026-08-26"
    monkeypatch.setattr(wb_repricer_bff, "get_source_cache", lambda org, requested, **kw: cache if requested == key and path != "covering" else None)
    monkeypatch.setattr(wb_repricer_bff, "get_covering_source_cache", lambda *a, **kw: cache if path == "covering" else None)
    monkeypatch.setattr(wb_repricer_bff, "get_wb_sync_status", lambda org: {})
    monkeypatch.setattr(wb_repricer_bff, "list_source_cache_ranges_by_prefix", lambda *a, **kw: [{**cache, "sourceKey": key}] if path == "daily_union" else [])
    result = wb_repricer_bff._period_source_cache(
        2, prefix, "2026-08-26_2026-09-01", 7,
        datetime(2026, 8, 26, tzinfo=timezone.utc), datetime(2026, 9, 1, tzinfo=timezone.utc),
        require_full_sync_coverage=False,
    )
    if previous and prefix == "finance":
        assert result == {}
    else:
        assert result["aggregates"]["123"]["additionalPaymentKopecks"] == row["additionalPaymentKopecks"]


@pytest.mark.parametrize("chunked", [False, True])
@pytest.mark.parametrize("previous_version", [6, 7, 8])
def test_sku_snapshot_reader_rejects_previous_monetary_payload(monkeypatch, chunked, previous_version):
    monkeypatch.setattr(wb_repricer_bff, "legacy_finance_tax_revision", lambda _org: "tax")
    options = dict(period_suffix="2026-08-26_2026-09-01", include_promotions=False, include_content=False)
    key = wb_repricer_bff._repricer_sku_snapshot_key("complete", **options)
    old_key = wb_repricer_bff._repricer_sku_snapshot_key("complete", version=previous_version, **options)
    old = {"version": previous_version, "items": [{"analytics": {"netProfitKopecks": 987654321}}],
           "summary": {"marginKopecks": 987654321}}
    if chunked:
        old.update(storage="chunked", chunkSize=150)
    entries = {(2, old_key): old, (2, key): old}
    monkeypatch.setattr(wb_repricer_bff, "get_source_cache", lambda org, requested, **kw: entries.get((org, requested)))
    assert wb_repricer_bff._load_repricer_sku_snapshot_page(2, "complete", page=1, page_size=10, **options) is None
    current = {"version": wb_repricer_bff.SKU_LIST_SNAPSHOT_VERSION,
               "stateRevision": wb_repricer_bff._repricer_view_state_revision(), "taxRevision": "tax", "total": 1,
               "items": [{"analytics": {"netProfitKopecks": 9000}}], "summary": {"marginKopecks": 9000}}
    if chunked:
        current.update(storage="chunked", chunkSize=150)
        entries[2, wb_repricer_bff._repricer_sku_snapshot_chunk_key(key, 0)] = {"items": current["items"]}
    entries[2, key] = current
    loaded = wb_repricer_bff._load_repricer_sku_snapshot_page(2, "complete", page=1, page_size=10, **options)
    assert loaded[0]["summary"]["marginKopecks"] == loaded[1][0]["analytics"]["netProfitKopecks"] == 9000
    assert wb_repricer_bff._load_repricer_sku_snapshot(3, "complete", **options) is None
    assert wb_repricer_bff._load_repricer_sku_snapshot(2, "complete", **{**options, "period_suffix": "2026-09-02_2026-09-08"}) is None
