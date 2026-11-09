from datetime import date, datetime, timezone
from types import SimpleNamespace

from app.routers import wb_reports_bff as reports
from app import repricer_sync as sync


def test_week_reuses_dated_pnl_with_permission_and_separates_caches(monkeypatch):
    calls = []
    def pnl(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(rows=[SimpleNamespace(nmId=101, netProfitKopecks=-12500, marginPct=-12.5, taxKopecks=0)])
    monkeypatch.setattr(reports, "build_pnl_report", pnl)
    monkeypatch.setattr(reports, "_catalog_meta_by_nm", lambda _org: {})
    monkeypatch.setattr(reports, "_week_funnel_metrics_by_nm", lambda **kw: {})
    monkeypatch.setattr(reports, "ensure_daily_stock_history", lambda *a, **kw: {})
    snapshot = SimpleNamespace(source_status="cached", orders=[], sales=[{"nmId": 101, "finishedPrice": 1000, "forPay": 700}], stocks=[], revenue_by_nm_kopecks={101: 100000})
    period = {"preset": "custom", "from": "2026-09-30", "to": "2026-10-06"}
    payload = reports._build_week_over_week_payload(period, snapshot, snapshot, organization_id=2, finance_allowed=True)
    assert payload["rows"][0]["profit"]["kopecks"] == -12500
    assert payload["rows"][0]["marginPct"]["percent"] == -12.5
    assert [(c["date_from"], c["date_to"]) for c in calls] == [(date(2026, 9, 30), date(2026, 10, 6)), (date(2026, 9, 23), date(2026, 9, 29))]
    calls.clear()
    denied = reports._build_week_over_week_payload(period, snapshot, snapshot, organization_id=2, finance_allowed=False)
    assert denied["rows"][0]["profit"]["kopecks"] is None
    assert calls == []
    args = ("week-over-week", date(2026, 9, 30), date(2026, 10, 6), "sku", "operational")
    assert reports._report_cache_key(*args, finance_allowed=True) != reports._report_cache_key(*args, finance_allowed=False)
    assert reports._report_job_cache_key(*args, finance_allowed=True) != reports._report_job_cache_key(*args, finance_allowed=False)


def test_funnel_partial_exact_cache_does_not_hide_other_cached_days(monkeypatch):
    now = datetime.now(timezone.utc).isoformat()
    def cache(day, sku, index=0):
        return {"dateFrom": day, "dateTo": day, "fetchedAt": now, "dailyAggregates": {day: {sku: {"cartCount": 5}}}, "chunks": [{"type": "daily", "date": day, "chunkIndex": index, "status": "done"}]}
    exact = cache("2026-09-30", "101")
    other = cache("2026-10-01", "102")
    competing = cache("2026-09-30", "103", 1)
    monkeypatch.setattr(sync, "get_source_cache", lambda _org, key, **kw: other if key == "other" else competing if key == "competing" else exact)
    monkeypatch.setattr(sync, "list_source_cache_ranges_by_prefix", lambda *a, **kw: [{**other, "sourceKey": "other"}, {**competing, "sourceKey": "competing"}])
    monkeypatch.setattr("app.wb_api.source_freshness.source_is_fresh", lambda p: True)
    daily, chunks = sync._baskets_daily_seed_from_recent_cache(2, range_start=datetime(2026, 9, 30), range_end=datetime(2026, 10, 6))
    assert set(daily) == {"2026-09-30", "2026-10-01"}
    assert daily["2026-09-30"] == exact["dailyAggregates"]["2026-09-30"]
    assert [c["chunkIndex"] for c in chunks if c["date"] == "2026-09-30"] == [0]
    merged_daily, merged_chunks = sync._merge_baskets_daily_seed(
        competing["dailyAggregates"], competing["chunks"], daily, chunks)
    assert merged_daily["2026-09-30"] == exact["dailyAggregates"]["2026-09-30"]
    assert [c["chunkIndex"] for c in merged_chunks if c["date"] == "2026-09-30"] == [0]
