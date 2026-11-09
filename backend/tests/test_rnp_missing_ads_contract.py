from datetime import date

import pytest

from app.wb_reports_sprint_d import build_rnp_report


def test_missing_ads_cache_returns_blocked_report_and_cached_replay(monkeypatch):
    saved = {}

    def read(organization_id, key, **_kwargs):
        assert organization_id == 7
        return saved.get(key)

    def save(organization_id, key, payload):
        assert organization_id == 7
        saved[key] = payload

    monkeypatch.setattr("app.wb_api.rnp_runtime.get_source_cache", read)
    monkeypatch.setattr("app.wb_api.rnp_runtime.save_source_cache", save)
    monkeypatch.setattr("app.wb_api.rnp_runtime.list_source_cache_ranges_by_prefix", lambda *_args, **_kwargs: [])
    for _ in range(2):
        report = build_rnp_report(
            date(2026, 6, 1), date(2026, 6, 7), "sku", True, organization_id=7,
        )
        assert report.sourceStatus == "blocked"
        assert report.adsSourceStatus == "blocked"
        assert {"WB-02", "WB_ADS_CACHE_EMPTY", "WB-11"} <= set(report.blockerIds)
        assert report.rows == []
        assert report.drrPct is None


@pytest.mark.parametrize("ads_status", ["blocked", "unknown"])
def test_legacy_cached_missing_ads_response_keeps_required_blocker(monkeypatch, ads_status):
    cached = {
        "sourceRevision": {"baskets_": None, "ads_": None},
        "rows": [], "sourceStatus": "blocked", "confidence": "blocked",
        "adsSourceStatus": ads_status, "blockerIds": ["WB_ADS_CACHE_EMPTY"],
    }
    monkeypatch.setattr("app.wb_api.rnp_runtime.get_source_cache", lambda *_args, **_kwargs: cached)
    monkeypatch.setattr("app.wb_api.rnp_runtime.list_source_cache_ranges_by_prefix", lambda *args, **kwargs: [])
    report = build_rnp_report(date(2026, 6, 1), date(2026, 6, 7), "sku", True, organization_id=7)
    assert {"WB-02", "WB_ADS_CACHE_EMPTY", "WB-11"} <= set(report.blockerIds)
    assert report.adsSourceStatus == ads_status
    assert report.drrPct is None
    assert cached["blockerIds"] == ["WB_ADS_CACHE_EMPTY"]
