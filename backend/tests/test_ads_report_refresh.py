from copy import deepcopy
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import repricer_tasks as tasks
from app.routers import wb_reports_bff as reports, wb_repricer_bff as repricer
from app.wb_ads_cache import store
from app.wb_sync_plan import WbSyncProfile


@pytest.fixture
def runtime(monkeypatch):
    day = date(2026, 7, 1)
    state = SimpleNamespace(source={}, outer={}, actor=SimpleNamespace(organization_id=77, user_id="synthetic"))
    state.args = dict(actor=state.actor, date_from=day, date_to=day,
                      date_range={"preset": "custom", "from": "2026-07-01", "to": "2026-07-01"}, wb_token=None)
    state.outer_args = dict(organization_id=77, report_id="ads", date_from=day,
                            date_to=day, group_by="campaign", source="operational")
    monkeypatch.setattr(store, "_run_db", lambda fn: None)
    monkeypatch.setattr(store, "_MEMORY_REPORT_CACHE", {})
    monkeypatch.setattr(reports, "_period_cache", lambda *a, **kw: deepcopy(state.source))
    monkeypatch.setattr(reports, "actor_from_request", lambda request: state.actor)
    monkeypatch.setattr(reports, "assert_permission_or_audit", lambda **kw: None)
    monkeypatch.setattr(reports, "record_audit_event", lambda **kw: None)
    monkeypatch.setattr(reports, "get_source_cache", lambda org, key, **kw: deepcopy(state.outer.get((org, key))))
    monkeypatch.setattr(reports, "save_source_cache", lambda org, key, value: state.outer.__setitem__((org, key), deepcopy(value)))
    monkeypatch.setattr(reports, "_apply_report_rules_to_payload", lambda payload, org: payload)
    monkeypatch.setattr(tasks, "_report_snapshot_sources_ready", lambda org, sources, *a, **kw: (tuple(sources) == ("ads",), []))
    monkeypatch.setattr(repricer, "_build_repricer_sku_snapshot", lambda *a, **kw: {})
    monkeypatch.setattr(reports, "get_cash_flow_for_period", lambda **kw: {"status": "pending"})
    monkeypatch.setattr(reports, "_map_cash_flow_to_expenses_response", lambda *a, **kw: {"rows": []})
    return state


def set_source(state, spend):
    row = {"nmId": 101, "campaignId": 42, "adSpendKopecks": spend}
    state.source = {"sourceComplete": True, "aggregates": {"101": row}, "dailyAggregates": {"2026-07-01": {"101": row}}}


def invoke_refresh(kind):
    if kind == "manual":
        return reports.refresh_ads_report_cache(SimpleNamespace(), preset="custom", from_="2026-07-01", to="2026-07-01")
    if kind == "worker":
        return tasks.build_report_for_org.run(77, "synthetic", "ads", "2026-07-01", "2026-07-01", "campaign", "operational", False, None)
    day = date(2026, 7, 1)
    profile = WbSyncProfile("synthetic-ads", "synthetic", "report", None, day, day, ("ads",))
    return tasks._materialize_report_snapshots_for_profile(77, profile, persist_progress=False)


@pytest.mark.parametrize("kind", ["manual", "worker", "profile"])
@pytest.mark.parametrize("spend", [200, 0])
def test_ads_refresh_replaces_old_report_from_current_cached_sources(runtime, monkeypatch, kind, spend):
    set_source(runtime, 100)
    old_report = reports._build_ads_report_payload(**runtime.args, refresh=True)
    reports._save_exact_report_payload_cache(**runtime.outer_args, report=old_report)
    set_source(runtime, spend)
    result = invoke_refresh(kind)
    if kind == "manual":
        assert result["cache"]["status"] == "refreshed"
    else:
        assert result["state"] == "completed"
    cached = store.get_ads_report_cache(organization_id=77, date_from=date(2026, 7, 1), date_to=date(2026, 7, 1), group_by="campaign")
    assert cached["rows"][0]["adSpendKopecks"] == spend
    assert cached["rows"][0]["nmId"] == 101
    # The read path must keep the freshly materialized report, without rebuilding.
    monkeypatch.setattr(reports, "build_cached_ads_attribution_snapshot", lambda **kw: pytest.fail("cached read rebuilt ads"))
    latest = reports._latest_report_payload_cache(**runtime.outer_args)
    assert latest is not None
    assert latest[0]["report"]["rows"][0]["adSpendKopecks"] == spend
    assert latest[0]["report"]["rows"][0]["nmId"] == 101
    reused = reports._build_ads_report_payload(**runtime.args)
    assert reused["rows"][0]["adSpendKopecks"] == spend
    assert reused["cache"]["status"] == "hit"


@pytest.mark.parametrize("kind", ["manual", "worker", "profile"])
def test_ads_refresh_preserves_last_good_report_when_source_is_blocked(runtime, kind):
    set_source(runtime, 100)
    old_report = reports._build_ads_report_payload(**runtime.args, refresh=True)
    reports._save_exact_report_payload_cache(**runtime.outer_args, report=old_report)
    before = deepcopy(store._MEMORY_REPORT_CACHE)
    before_outer = deepcopy(reports._latest_report_payload_cache(**runtime.outer_args))
    runtime.source = {}
    if kind == "profile":
        assert invoke_refresh(kind)["state"] == "failed"
    else:
        with pytest.raises(HTTPException) as failure:
            invoke_refresh(kind)
        assert failure.value.status_code == 409
        assert failure.value.detail == "WB_ADS_CACHE_EMPTY"
    assert store._MEMORY_REPORT_CACHE == before
    assert reports._latest_report_payload_cache(**runtime.outer_args) == before_outer


@pytest.mark.parametrize("status,code,delay", [("partial", 429, 137), ("blocked", 429, 137), ("partial", 403, None)])
def test_ads_worker_resumes_rate_limited_batches_and_keeps_partial_cache(runtime, monkeypatch, status, code, delay):
    from celery.exceptions import Retry
    from app import wb_report_ads
    state = SimpleNamespace(saved=[], retries=[])
    snapshot = SimpleNamespace(source_status=status, diagnostics={"sources": [{
        "sourceId": "wb-ads-fullstats", "status": "blocked", "statusCode": code,
        "rateLimit": {"retryAfterSeconds": 136},
    }]})
    monkeypatch.setattr(tasks, "_report_refresh_wb_token", lambda *a: "synthetic")
    monkeypatch.setattr(reports, "build_ads_attribution_snapshot", lambda **kw: snapshot)
    monkeypatch.setattr(reports, "_map_ads_to_report_response", lambda *a: {
        "rows": [{"nmId": 101, "campaignId": "42", "impressions": 200}],
        "performanceComplete": False, "dailyRows": [],
    })
    def saved(**kw):
        state.saved.append(deepcopy(kw["payload"]))
        return kw["payload"]
    monkeypatch.setattr(reports, "save_ads_report_cache", saved)
    monkeypatch.setattr(reports, "save_ads_history_snapshots", lambda **kw: None)
    monkeypatch.setattr(reports, "_save_exact_report_payload_cache", lambda **kw: None)
    monkeypatch.setattr(wb_report_ads, "save_campaign_performance", lambda *a: None)
    def retry(**kw):
        state.retries.append(kw)
        raise Retry()
    monkeypatch.setattr(tasks.refresh_report_sources_for_org, "retry", retry)
    args = (77, "synthetic", "ads", "2026-10-01", "2026-10-08", "campaign", "operational", False, None)
    if delay:
        with pytest.raises(Retry):
            tasks.refresh_report_sources_for_org.run(*args)
        assert state.retries[0]["countdown"] == delay
        assert state.retries[0]["max_retries"] == 80
        assert state.retries[0]["kwargs"]["refresh_missing_only"] is True
        job = next(value for value in runtime.outer.values() if value.get("stage") == "rate_limit")
        assert job["state"] == "queued"
    else:
        assert tasks.refresh_report_sources_for_org.run(*args)["state"] == "failed"
        assert state.retries == []  # Permission failures must not loop.
    assert len(state.saved) == (0 if status == "blocked" else 1)
    if state.saved:
        assert state.saved[0]["rows"][0]["impressions"] == 200
        assert state.saved[0]["performanceComplete"] is False
