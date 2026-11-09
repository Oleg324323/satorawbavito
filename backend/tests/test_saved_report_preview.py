"""A provider failure must not hide saved rows or mutate source freshness."""
from datetime import date
from types import SimpleNamespace

import pytest

from app.routers import wb_reports_bff as reports


@pytest.mark.parametrize("report_id", ["abc", "pnl", "rnp", "stock", "ads"])
@pytest.mark.parametrize("finance_allowed", [True, False])
def test_saved_preview_is_partial_scoped_and_does_not_enqueue(report_id, finance_allowed, monkeypatch):
    calls = []
    def forbidden(*args, **kwargs):
        raise AssertionError("Cache preview must not fetch providers, enqueue 1C, or rewrite sources")
    def build(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace()
    def map_payload(*args, **kwargs):
        return {"rows": [{"sku": "SAVED"}], "meta": {"freshnessState": "fresh"}}
    monkeypatch.setattr(reports, "get_cash_flow_for_period", forbidden)
    monkeypatch.setattr(reports, "save_source_cache", forbidden)
    monkeypatch.setattr(reports, "get_settings", lambda: SimpleNamespace(one_c_enabled=True))
    monkeypatch.setattr(reports, "_apply_report_rules_to_payload", lambda value, org: value)
    for name in ("build_abc_report", "build_pnl_report", "build_rnp_report", "build_cached_wb_reports_sources_snapshot", "build_cached_ads_attribution_snapshot"):
        monkeypatch.setattr(reports, name, build)
    for name in ("_map_abc_to_report_response", "_map_pnl_to_report_response", "_map_rnp_to_report_response", "_map_ads_to_report_response"):
        monkeypatch.setattr(reports, name, map_payload)
    def stock(*args, **kwargs):
        assert kwargs["read_only"] is True and kwargs["wb_token"] is None
        return map_payload()
    monkeypatch.setattr(reports, "_build_stock_report_payload", stock)
    result = reports._saved_report_preview(actor=SimpleNamespace(organization_id=987, user_id="synthetic"),
        report_id=report_id, date_from=date(2026, 9, 1), date_to=date(2026, 9, 30), group_by="sku", finance_allowed=finance_allowed)
    assert calls[0]["organization_id"] == 987
    assert calls[0]["date_from"] == date(2026, 9, 1)
    if report_id not in {"stock", "ads"}:
        assert calls[0]["finance_allowed"] is finance_allowed
    assert result["rows"] == [{"sku": "SAVED"}]
    assert result["cache"]["fresh"] is False
    assert result["meta"]["freshnessState"] == "partial"
    assert "reportJob" not in result  # A preview never completes a running job.


def test_readonly_stock_bundle_cannot_refresh_sources_even_in_mock_mode(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("Unexpected source write / provider request")
    monkeypatch.setattr(reports, "save_source_cache", forbidden)
    monkeypatch.setattr(reports, "_request_source_with_cache", forbidden)
    monkeypatch.setattr(reports, "get_source_cache", lambda org, key: None)
    snapshot = SimpleNamespace(stocks=[], orders=[], sales=[], financial_rows=[])
    result = reports._build_stock_source_bundle(organization_id=1, snapshot=snapshot,
        date_range={"from": "2026-09-01", "to": "2026-09-30"}, wb_token=None, read_only=True)
    assert result["raw_orders"]["status"] == "missing"
    assert all("fetchedAt" not in source for source in result.values())


@pytest.mark.parametrize("finance_allowed", [False, True])
def test_digest_preview_reads_requested_cache_without_marking_job_complete(monkeypatch, finance_allowed):
    from app import repricer_tasks
    def forbidden(*a, **k):
        raise AssertionError("Preview must not write cache/jobs")
    monkeypatch.setattr(reports, "save_source_cache", forbidden)
    monkeypatch.setattr(reports, "get_source_cache", lambda *a, **k: {})
    calls = []
    def source(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(orders=[{}], sales=[])
    monkeypatch.setattr(reports, "build_cached_wb_reports_sources_snapshot", source)
    monkeypatch.setattr(reports, "_build_digest_funnel_snapshot", lambda **k: {"rows": [{}]})
    monkeypatch.setattr(reports, "build_cached_ads_attribution_snapshot", lambda **k: SimpleNamespace())
    monkeypatch.setattr(repricer_tasks, "_digest_report_summary", lambda *a: {"marginKopecks": 150})
    def payload(date_range, snapshot, ads, plan, funnel, summary):
        assert summary["marginKopecks"] == (150 if finance_allowed else None)
        assert plan.rows == []
        return {"dateRange": date_range, "meta": {}, "freshness": [{"state": "fresh", "updatedAt": "now"}]}
    monkeypatch.setattr(reports, "_build_digest_payload", payload)
    result = reports._saved_digest_preview(actor=SimpleNamespace(organization_id=987),
        date_from=date(2026, 9, 1), date_to=date(2026, 9, 14), finance_allowed=finance_allowed)
    assert calls[0]["organization_id"] == 987
    assert calls[0]["date_to"] == date(2026, 9, 14)
    assert result["cache"]["fresh"] is False
    assert result["freshness"][0] == {"state": "partial", "updatedAt": None}
    assert "digestJob" not in result
