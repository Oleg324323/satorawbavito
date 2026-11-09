from types import SimpleNamespace

import pytest

from app.repricer_cache.orm import WbRepricerSourceCacheRow


def test_source_cache_key_column_accepts_background_stock_report_key():
    key = "reports_payload_stock_2026-07-10_2026-07-16_warehouse_operational"

    assert len(key) == 65
    assert WbRepricerSourceCacheRow.__table__.c.source_key.type.length == 255


def test_background_report_job_fails_when_payload_is_not_persisted(monkeypatch):
    from app import repricer_tasks
    from app.routers import wb_reports_bff as reports

    saved_jobs: list[dict] = []
    snapshot = SimpleNamespace(source_status="fresh", orders=[], sales=[], stocks=[])
    monkeypatch.setattr(repricer_tasks, "_report_snapshot_sources_ready", lambda *_args, **_kwargs: (True, []))
    monkeypatch.setattr(repricer_tasks, "refresh_wb_data_sources", lambda **_kwargs: pytest.fail("stock cache build must not refresh WB"))
    monkeypatch.setattr(reports, "build_cached_wb_reports_sources_snapshot", lambda **_kwargs: snapshot)
    monkeypatch.setattr(reports, "_apply_report_rules_to_payload", lambda report, _organization_id: report)
    monkeypatch.setattr(
        reports,
        "_build_stock_report_payload",
        lambda *_args, **_kwargs: {"rows": [{"sku": "NM_1"}]},
    )

    def discard_report_payload(_organization_id, key, payload):
        if key.startswith("reports_job_stock"):
            saved_jobs.append(payload)
        return payload

    monkeypatch.setattr(reports, "save_source_cache", discard_report_payload)
    monkeypatch.setattr(reports, "get_source_cache", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="Background report payload was not persisted"):
        repricer_tasks.build_report_for_org.run(
            1,
            "viewer",
            "stock",
            "2026-07-10",
            "2026-07-16",
            "warehouse",
            "operational",
            False,
            "wb-token",
        )

    assert saved_jobs[-1]["state"] == "failed"
    assert "reports_payload_stock" in saved_jobs[-1]["error"]
