"""Reading cached reports must not complete or overwrite live background work."""

from copy import deepcopy
from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import repricer_tasks
from app.main import create_app
from app.routers import wb_reports_bff as reports
from tests.auth_helpers import auth_headers

START, END = date(2026, 8, 17), date(2026, 8, 23)
PARAMS = {"preset": "custom", "from": str(START), "to": str(END)}


def test_completed_repricer_finance_job_survives_get_and_is_connection_scoped(runtime, monkeypatch):
    from app import repricer_page_finance as page
    monkeypatch.setattr(reports, "has_permission", lambda *args: True)
    token = ["synthetic-connection-one"]
    monkeypatch.setattr(reports, "get_user_wb_token_secret", lambda *args: token[0])
    monkeypatch.setattr(page, "missing_page_ranges", lambda *args, **kwargs: [])
    params = {**PARAMS, "source": "repricer-finance"}
    response = runtime.api.post("/api/wb/reports/stats/jobs", params=params)
    assert response.status_code == 200
    assert response.json()["state"] == response.json()["stage"] == "completed"
    result = runtime.api.get("/api/wb/reports/stats/jobs", params=params)
    assert result.status_code == 200 and result.json()["state"] == "completed"
    assert runtime.builds == runtime.refreshes == []
    token[0] = "synthetic-connection-two"
    assert runtime.api.get("/api/wb/reports/stats/jobs", params=params).json()["state"] == "idle"
    assert runtime.api.post("/api/wb/reports/pnl/jobs", params=params).status_code == 400
    assert runtime.api.post("/api/wb/reports/stats/jobs", params={**PARAMS, "source": "repricer-finance:forged"}).status_code == 400


def test_catalog_recovery_api_reuses_one_job_across_periods_and_rejects_forged_connections(runtime, monkeypatch):
    from app.infra import redis_client
    monkeypatch.setattr(reports, 'get_user_wb_token_secret', lambda user: 'synthetic-connection')
    monkeypatch.setattr('app.wb_catalog_loading.missing_catalog_sources', lambda org: ('goods',))
    lock = SimpleNamespace(acquire=lambda **kwargs: True, release=lambda: None)
    monkeypatch.setattr(redis_client, 'get_redis_client', lambda: SimpleNamespace(lock=lambda *a, **kw: lock))
    ids = []
    for left, right in [('2026-10-01', '2026-10-04'), ('2026-10-01', '2026-10-08'), ('2026-10-07', '2026-10-11')]:
        params = {'preset': 'custom', 'from': left, 'to': right, 'source': 'catalog'}
        result = runtime.api.post('/api/wb/reports/stats/jobs', params=params)
        assert result.status_code == 200 and result.json()['state'] == 'queued'
        ids.append(result.json()['taskId'])
        assert runtime.api.get('/api/wb/reports/stats/jobs', params=params).json()['taskId'] == ids[0]
    assert len(set(ids)) == len(runtime.refreshes) == 1 and runtime.builds == []
    assert runtime.api.post('/api/wb/reports/pnl/jobs', params={**PARAMS, 'source': 'catalog'}).status_code == 400
    assert runtime.api.post('/api/wb/reports/stats/jobs', params={**PARAMS, 'source': 'catalog:forged'}).status_code == 400
    monkeypatch.setattr(reports, 'get_user_wb_token_secret', lambda user: None)
    assert runtime.api.post('/api/wb/reports/stats/jobs', params={**PARAMS, 'source': 'catalog'}).status_code == 409


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.setattr(reports, '_report_start_guard', lambda *a: nullcontext())
    monkeypatch.setattr(repricer_tasks.build_report_for_org, 'apply_async',
        lambda args, **kw: repricer_tasks.build_report_for_org.delay(*args))
    monkeypatch.setattr('app.platform.economics.legacy_catalog.catalog_facts', lambda org, **kw: {})
    monkeypatch.setattr('app.wb_catalog_loading.missing_catalog_sources', lambda org: ())
    state = SimpleNamespace(cache={}, writes=[], builds=[], refreshes=[], ready=False, tax_revision="synthetic-confirmation")

    def save(org, key, payload):
        state.writes.append(key)
        state.cache[org, key] = deepcopy(payload)

    def enqueue(target, args):
        target.append(args)
        return SimpleNamespace(id="synthetic-task")

    monkeypatch.setattr(
        reports,
        "get_source_cache",
        lambda org, key, **kw: deepcopy(state.cache.get((org, key))),
    )
    monkeypatch.setattr(reports, "save_source_cache", save)
    monkeypatch.setattr(
        reports,
        "list_source_cache_by_prefix",
        lambda org, prefix, **kw: [
            {**deepcopy(value), "sourceKey": key}
            for (owner, key), value in state.cache.items()
            if owner == org and key.startswith(prefix)
        ],
    )
    monkeypatch.setattr(reports, "_abc_economics_version", lambda org: "cafef00d")
    monkeypatch.setattr(reports, "legacy_finance_tax_revision", lambda org: state.tax_revision)
    monkeypatch.setattr('app.wb_pnl_advertising._source_context', lambda org: None)
    monkeypatch.setattr('app.wb_reports_sprint_d._pnl_source_revision', lambda *a: 'pnl-sources')
    monkeypatch.setattr('app.wb_pnl_costs.current_cost_revision', lambda org: 'saved-costs')
    monkeypatch.setattr(reports, "_digest_source_revision", lambda *args: state.tax_revision)
    readiness = lambda *args, **kw: (state.ready, [] if state.ready else ["ads"])
    monkeypatch.setattr(reports, "_report_daily_sources_ready", readiness)
    monkeypatch.setattr(repricer_tasks, "_report_snapshot_sources_ready", readiness)
    monkeypatch.setattr(
        reports, "get_cash_flow_for_period", lambda **kw: {"status": "ready"}
    )
    monkeypatch.setattr(
        repricer_tasks.build_report_for_org,
        "delay",
        lambda *args: enqueue(state.builds, args),
    )
    monkeypatch.setattr(
        repricer_tasks.build_digest_for_org, "delay",
        lambda *args: enqueue(state.builds, args),
    )
    monkeypatch.setattr(
        repricer_tasks.refresh_report_sources_for_org,
        "delay",
        lambda *args, **kwargs: enqueue(state.refreshes, args),
    )
    monkeypatch.setattr(repricer_tasks.refresh_report_sources_for_org, "apply_async",
        lambda args=None, kwargs=None, **options: enqueue(state.refreshes, args))
    state.api = TestClient(create_app())
    state.api.headers.update(auth_headers(state.api, "viewer"))
    return state


def seed(
    runtime, report_id, *, cached=True, state="running", stage="refreshing_sources"
):
    job = {
        "reportId": report_id,
        "state": state,
        "stage": stage,
        "taskId": "live-task",
        "updatedAt": reports._utc_now_iso(),
        "percent": 37,
        "label": "Loading source",
    }
    key = reports._report_job_cache_key(
        report_id,
        START,
        END,
        "sku",
        "operational",
        finance_allowed=False,
    )
    runtime.cache[1, key] = deepcopy(job)
    if cached:
        versions = {
            "abc": reports.ABC_REPORT_PAYLOAD_VERSION,
            "pnl": reports.PNL_REPORT_PAYLOAD_VERSION,
            "week-over-week": reports.WEEK_OVER_WEEK_REPORT_PAYLOAD_VERSION,
        }
        payload_key = reports._report_cache_key(
            report_id, START, END, "sku", "operational", organization_id=1
        )
        runtime.cache[1, payload_key] = {
            "pnlAdvertisingContext": None,
            "pnlSourceRevision": "pnl-sources",
            "pnlCurrentCostRevision": "saved-costs",
            "taxRevision": runtime.tax_revision,
            "completedAt": reports._utc_now_iso(),
            "dateFrom": str(START),
            "dateTo": str(END),
            "report": {
                "cacheVersion": versions[report_id],
                "economicsVersion": "cafef00d",
                "rows": [{"sku": "SKU-1", "orders": {"units": 1}}],
            },
        }
    return key, job


@pytest.mark.parametrize("report_id", ["abc", "pnl", "week-over-week"])
@pytest.mark.parametrize("revision", ["changed-confirmation", "unavailable"])
def test_report_cache_rejects_changed_or_unavailable_tax_without_mutating_job(runtime, report_id, revision):
    key, job = seed(runtime, report_id)
    endpoint = f"/api/wb/reports/{report_id}/latest-cache"
    assert runtime.api.get(endpoint, params=PARAMS).status_code == 200
    before = deepcopy(runtime.cache)
    runtime.tax_revision = revision
    assert runtime.api.get(endpoint, params=PARAMS).status_code == 404
    assert runtime.cache == before and runtime.cache[1, key] == job
    assert runtime.writes == runtime.builds == runtime.refreshes == []


@pytest.mark.parametrize("report_id,previous", [("abc", "v17"), ("pnl", "v3"), ("week-over-week", "v2")])
@pytest.mark.parametrize("suffix", ["", "/latest-cache"])
def test_monetary_report_readers_reject_previous_payloads(runtime, report_id, previous, suffix):
    seed(runtime, report_id)
    key = reports._report_cache_key(report_id, START, END, "sku", "operational", organization_id=1)
    current = deepcopy(runtime.cache[1, key])
    current["report"]["rows"][0]["netProfitKopecks"] = 9000
    old = deepcopy(current)
    old["report"]["cacheVersion"] = previous
    old["report"]["rows"][0]["netProfitKopecks"] = 987654321
    # Seed both the previous physical key and old metadata at the current key.
    old_key = key.replace(current["report"]["cacheVersion"], previous, 1)
    runtime.cache[1, old_key] = old
    runtime.cache[1, key] = old
    response = runtime.api.get(f"/api/wb/reports/{report_id}{suffix}", params=PARAMS)
    assert response.status_code == (404 if suffix else 200)
    assert "987654321" not in response.text
    runtime.cache[1, key] = current
    response = runtime.api.get(f"/api/wb/reports/{report_id}{suffix}", params=PARAMS)
    assert response.status_code == 200
    assert response.json()["rows"][0]["netProfitKopecks"] == 9000


@pytest.mark.parametrize("location", ["exact", "latest"])
def test_digest_reader_rejects_previous_monetary_payload(runtime, location):
    key = reports._digest_cache_key(START, END) if location == "exact" else "reports_digest_latest"
    old_key = f"reports_digest_v12_{START}_{END}" if location == "exact" else key
    old = {"completedAt": reports._utc_now_iso(), "digest": {
        "meta": {"freshnessState": "cached"},
        "cacheVersion": "v12", "kpis": [{"id": "margin_profit", "value": "987654321"}],
    }}
    runtime.cache[1, old_key] = old
    runtime.cache[1, key] = old
    response = runtime.api.get("/api/wb/reports/digest", params=PARAMS)
    assert response.status_code == 200
    assert "987654321" not in response.text
    runtime.cache[1, key] = {"completedAt": reports._utc_now_iso(), "digest": {
        "meta": {"freshnessState": "cached"},
        "cacheVersion": reports.DIGEST_REPORT_PAYLOAD_VERSION,
        "kpis": [{"id": "margin_profit", "value": "9000"}],
    }}
    response = runtime.api.get("/api/wb/reports/digest", params=PARAMS)
    assert response.status_code == 200
    assert response.json()["kpis"][0]["value"] == "9000"


@pytest.mark.parametrize("report_id", ["abc", "pnl", "week-over-week"])
@pytest.mark.parametrize(
    "suffix,params", [("", PARAMS), ("/latest-cache", {}), ("/latest-cache", PARAMS)]
)
def test_cached_report_reads_preserve_active_job_without_writes(
    runtime, report_id, suffix, params
):
    key, job = seed(runtime, report_id)
    response = runtime.api.get(f"/api/wb/reports/{report_id}{suffix}", params=params)
    assert response.status_code == 200
    assert response.json()["rows"]
    assert response.json()["reportJob"] == job
    assert runtime.cache[1, key] == job
    assert runtime.writes == []
    repeated = runtime.api.post(f"/api/wb/reports/{report_id}/jobs", params=PARAMS)
    assert repeated.status_code == 200
    assert repeated.json()["reused"] is True
    assert runtime.builds == runtime.refreshes == []


@pytest.mark.parametrize("report_id", ["abc", "pnl", "week-over-week"])
@pytest.mark.parametrize("state", ["completed", "failed", "paused"])
@pytest.mark.parametrize(
    "suffix,params",
    [("", PARAMS), ("/jobs", PARAMS), ("/latest-cache", {}), ("/latest-cache", PARAMS)],
)
def test_cached_reads_preserve_finished_source_refresh(
    runtime, report_id, state, suffix, params
):
    key, job = seed(runtime, report_id, state=state, stage=state)
    job.update(kind="report_source_refresh", sync={"state": "completed"})
    if state in {"failed", "paused"}:
        job["error"] = "synthetic build failure"
    runtime.cache[1, key] = deepcopy(job)
    response = runtime.api.get(f"/api/wb/reports/{report_id}{suffix}", params=params)
    assert response.status_code == 200
    if suffix == "/jobs":
        assert response.json() == {**job, "reused": True}
    else:
        assert response.json()["rows"][0]["sku"] == "SKU-1"
        assert response.json()["reportJob"] == job
    assert runtime.cache[1, key] == job
    assert runtime.writes == runtime.builds == runtime.refreshes == []


def test_retry_queue_does_not_show_the_previous_attempt_error(runtime):
    key, job = seed(runtime, "week-over-week", state="failed", stage="failed")
    job.update(kind="report_source_refresh", error="previous WB HTTP 429")
    runtime.cache[1, key] = deepcopy(job)
    response = runtime.api.post("/api/wb/reports/week-over-week/jobs", params=PARAMS)
    assert response.status_code == 200
    assert response.json()["state"] == "queued"
    assert response.json()["error"] is None
    assert runtime.cache[1, key]["error"] is None


@pytest.mark.parametrize("report_id", ["abc", "pnl", "week-over-week"])
@pytest.mark.parametrize("state", ["completed", "failed"])
def test_normal_start_can_use_cache_after_finished_source_refresh(runtime, report_id, state):
    runtime.ready = True
    key, job = seed(runtime, report_id, state=state, stage=state)
    job.update(kind="report_source_refresh", sync={"state": "completed"})
    runtime.cache[1, key] = deepcopy(job)
    response = runtime.api.post(f"/api/wb/reports/{report_id}/jobs", params=PARAMS)
    assert response.status_code == 200
    assert response.json()["state"] == "completed"
    assert response.json()["stage"] == "cache"
    assert response.json()["cacheFresh"] is True
    assert runtime.cache[1, key] == response.json()
    assert runtime.builds == runtime.refreshes == []


@pytest.mark.parametrize("report_id", ["abc", "pnl", "week-over-week"])
def test_explicit_refresh_retries_failed_job_once_even_with_cache(runtime, report_id):
    key, job = seed(runtime, report_id, state="failed", stage="failed")
    job["kind"] = "report_source_refresh"
    runtime.cache[1, key] = deepcopy(job)
    for reused in (False, True):
        response = runtime.api.post(
            f"/api/wb/reports/{report_id}/refresh-sources-job", params=PARAMS
        )
        assert response.status_code == 200
        assert response.json()["state"] == "queued"
        assert response.json()["reused"] is reused
    assert len(runtime.refreshes) == 1
    assert runtime.builds == []


def test_missing_source_read_preserves_refresh_and_does_not_allow_duplicate(runtime):
    key, job = seed(runtime, "ads", cached=False)
    response = runtime.api.get("/api/wb/reports/ads", params=PARAMS)
    assert response.status_code == 200
    assert response.json()["rows"] == []
    assert response.json()["reportJob"] == job
    assert runtime.cache[1, key] == job
    assert runtime.writes == []
    repeated = runtime.api.post(
        "/api/wb/reports/ads/refresh-sources-job", params=PARAMS
    )
    assert repeated.status_code == 200
    assert repeated.json()["reused"] is True
    assert runtime.refreshes == []


@pytest.mark.parametrize(
    "state,stage", [("running", "pnl"), ("waiting_1c", "waiting_1c")]
)
def test_source_refresh_reuses_its_replacement_builder(runtime, monkeypatch, state, stage):
    monkeypatch.setattr(
        reports, "get_cash_flow_for_period",
        lambda **kw: pytest.fail("reused job must not request 1C again"),
    )
    key, job = seed(runtime, "pnl", cached=False, state=state, stage=stage)
    job["kind"] = "report_source_refresh"
    runtime.cache[1, key] = job
    response = runtime.api.post(
        "/api/wb/reports/pnl/refresh-sources-job", params=PARAMS
    )
    assert response.status_code == 200
    assert response.json()["reused"] is True
    assert runtime.cache[1, key] == job
    assert runtime.writes == runtime.refreshes == []


@pytest.mark.parametrize("active", [False, True])
def test_wow_cold_cache_read_preserves_real_live_job_without_building(runtime, monkeypatch, active):
    key, job = seed(runtime, "week-over-week", cached=False)
    if not active:
        runtime.cache.pop((1, key))
    monkeypatch.setattr(
        reports,
        "_build_week_over_week_fallback_report",
        lambda **kwargs: pytest.fail("Cold HTTP GET must not build either period"),
    )
    response = runtime.api.get(
        "/api/wb/reports/week-over-week/latest-cache", params=PARAMS
    )
    assert response.status_code == 404
    if active:
        assert runtime.cache[1, key] == job
    else:
        assert (1, key) not in runtime.cache
    assert runtime.writes == runtime.builds == runtime.refreshes == []


@pytest.mark.parametrize("state", ["completed", "failed"])
def test_wow_derived_cache_preserves_finished_refresh(runtime, monkeypatch, state):
    key, job = seed(runtime, "week-over-week", cached=False, state=state, stage=state)
    job.update(kind="report_source_refresh", sync={"state": "completed"})
    runtime.cache[1, key] = deepcopy(job)
    monkeypatch.setattr(
        reports, "_build_week_over_week_fallback_report",
        lambda **kw: pytest.fail("Saved job state must not trigger synchronous assembly"),
    )
    response = runtime.api.get("/api/wb/reports/week-over-week/latest-cache", params=PARAMS)
    assert response.status_code == 404
    assert runtime.cache[1, key] == job
    assert runtime.writes == []
    assert runtime.builds == runtime.refreshes == []


@pytest.mark.parametrize("state,stage", [("running", "pnl"), ("queued", "")])
def test_missing_source_does_not_start_refresh_over_active_build(
    runtime, monkeypatch, state, stage
):
    monkeypatch.setattr(
        reports, "get_cash_flow_for_period",
        lambda **kw: pytest.fail("reused job must not request 1C again"),
    )
    key, job = seed(runtime, "pnl", cached=False, state=state, stage=stage)
    response = runtime.api.post("/api/wb/reports/pnl/jobs", params=PARAMS)
    assert response.status_code == 200
    assert response.json()["reused"] is True
    assert runtime.cache[1, key] == job
    assert runtime.writes == runtime.builds == runtime.refreshes == []


@pytest.mark.parametrize(
    "state,stage", [("running", "pnl"), ("queued", ""), ("waiting_1c", "waiting_1c")]
)
@pytest.mark.parametrize("suffix", ["", "/jobs"])
def test_cache_does_not_complete_other_active_build_stages(
    runtime, state, stage, suffix
):
    key, job = seed(runtime, "pnl", state=state, stage=stage)
    response = runtime.api.get(f"/api/wb/reports/pnl{suffix}", params=PARAMS)
    assert response.status_code == 200
    actual = response.json() if suffix else response.json()["reportJob"]
    assert actual["state"] == state
    assert actual["stage"] == stage
    assert actual["percent"] == 37
    assert runtime.cache[1, key] == job
    assert runtime.writes == []


def test_finished_daily_wait_can_enqueue_once_after_sources_arrive(runtime):
    result = repricer_tasks.build_report_for_org.run(
        1,
        "synthetic-user",
        "ads",
        str(START),
        str(END),
        "sku",
        "operational",
        False,
        None,
    )
    assert result["state"] == "waiting_daily_detail"
    runtime.ready = True
    for reused in (False, True):
        response = runtime.api.post("/api/wb/reports/ads/jobs", params=PARAMS)
        assert response.status_code == 200
        assert response.json()["state"] == "queued"
        assert response.json()["reused"] is reused
    # SKU-level totals alone cannot produce campaign budgets.
    assert runtime.builds == []
    assert len(runtime.refreshes) == 1


@pytest.mark.parametrize("cache_state", ["partial", "expired", "invalidated"])
def test_partial_campaign_report_retries_campaign_collector_not_sku_builder(runtime, monkeypatch, cache_state):
    runtime.ready = True
    cache_key = reports._report_cache_key(
        "ads", START, END, "sku", "operational", organization_id=1, finance_allowed=False,
    )
    runtime.cache[1, cache_key] = {
        "completedAt": (datetime.now(timezone.utc) - timedelta(days=3)).isoformat() if cache_state == "expired" else reports._utc_now_iso(),
        "report": {
            "campaignDetailsCollected": True,
            "meta": {"freshnessState": "partial" if cache_state == "partial" else "fresh"},
            "rows": [{"campaignId": 1, "budget": None if cache_state == "partial" else 1000}],
        },
    }
    if cache_state == "invalidated":
        # The old campaign snapshot can be rejected by a newer source revision
        # even though SKU sources themselves are fresh. Keep its collector.
        monkeypatch.setattr(reports, "_report_payload_cache_is_usable", lambda *a, **kw: False)
    for reused in (False, True):
        response = runtime.api.post("/api/wb/reports/ads/jobs", params=PARAMS)
        assert response.status_code == 200
        assert response.json()["state"] == "queued"
        assert response.json()["reused"] is reused
    assert runtime.builds == []
    assert len(runtime.refreshes) == 1


@pytest.mark.parametrize("waiting", ["waiting_daily_detail", "waiting_baskets_detail"])
def test_external_data_wait_is_not_a_live_worker(waiting):
    assert not reports._report_job_is_reusable(
        {"state": waiting, "updatedAt": reports._utc_now_iso()}
    )


@pytest.mark.parametrize('source', ['operational', 'statistics-comparison'])
def test_statistics_waits_for_busy_queue_but_keeps_abandoned_job_timeout(runtime, source):
    key = reports._report_job_cache_key('stats', START, END, 'sku', source)
    job = {'reportId': 'stats', 'source': source, 'state': 'queued', 'stage': 'queued',
           'kind': 'report_source_refresh', 'taskId': 'waiting-task',
           'updatedAt': (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()}
    runtime.cache[1, key] = job
    assert runtime.api.get('/api/wb/reports/stats/jobs', params={**PARAMS, 'source': source}).json()['state'] == 'queued'
    response = runtime.api.post('/api/wb/reports/stats/jobs', params={**PARAMS, 'source': source})
    assert response.json()['reused'] is True
    assert runtime.refreshes == []
    job['updatedAt'] = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
    assert runtime.api.get('/api/wb/reports/stats/jobs', params={**PARAMS, 'source': source}).json()['state'] == 'stale'
    # Unrelated sources keep their existing queue timeout; report tabs now
    # share the worker-aware wait covered in test_wb_report_loading.
    for report, other_source in [('ads', 'operational'), ('stats', 'repricer-finance:synthetic')]:
        assert not reports._report_job_is_reusable({**job, 'reportId': report, 'source': other_source,
            'updatedAt': (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()})


def test_statistics_comparison_job_does_not_overwrite_main_period_job(runtime):
    main = reports._report_job_cache_key('stats', START, END, 'sku', 'operational')
    comparison = reports._report_job_cache_key('stats', START, END, 'sku', 'statistics-comparison')
    assert main != comparison
    runtime.api.post('/api/wb/reports/stats/jobs', params=PARAMS)
    runtime.api.post('/api/wb/reports/stats/jobs', params={**PARAMS, 'source': 'statistics-comparison'})
    assert runtime.cache[1, main]['taskId'] != runtime.cache[1, comparison]['taskId']
    assert len(runtime.refreshes) == 2


def test_statistics_persists_queued_record_before_fast_worker_and_does_not_overwrite_completion(runtime, monkeypatch):
    key = reports._report_job_cache_key('stats', START, END, 'sku', 'operational')
    def dispatch(args, kwargs, task_id):
        assert runtime.cache[1, key]['taskId'] == task_id
        runtime.cache[1, key] = {**runtime.cache[1, key], 'state': 'completed', 'stage': 'completed'}
    monkeypatch.setattr(repricer_tasks.refresh_report_sources_for_org, 'apply_async', dispatch)
    assert runtime.api.post('/api/wb/reports/stats/jobs', params=PARAMS).status_code == 200
    assert runtime.api.get('/api/wb/reports/stats/jobs', params=PARAMS).json()['state'] == 'completed'


def test_statistics_queue_failure_is_visible_and_can_be_retried(runtime, monkeypatch):
    def dispatch(**kwargs):
        raise RuntimeError('synthetic broker failure')
    monkeypatch.setattr(repricer_tasks.refresh_report_sources_for_org, 'apply_async', dispatch)
    assert runtime.api.post('/api/wb/reports/stats/jobs', params=PARAMS).status_code == 503
    status = runtime.api.get('/api/wb/reports/stats/jobs', params=PARAMS).json()
    assert status['state'] == 'failed'
    assert 'очередь' in status['label']


@pytest.mark.parametrize("heartbeat", ["expired", "missing"])
def test_abandoned_1c_wait_is_stale_and_can_restart_once(runtime, heartbeat):
    key, job = seed(runtime, "pnl", cached=False, state="waiting_1c", stage="waiting_1c")
    if heartbeat == "expired":
        job["updatedAt"] = (
            datetime.now(timezone.utc) - reports.BACKGROUND_REPORT_JOB_STALE_AFTER
            - timedelta(seconds=1)
        ).isoformat()
    else:
        job.pop("updatedAt")
    runtime.cache[1, key] = deepcopy(job)
    runtime.ready = True
    for suffix in ("", "/jobs"):
        response = runtime.api.get(f"/api/wb/reports/pnl{suffix}", params=PARAMS)
        assert response.status_code == 200
        status = response.json() if suffix else response.json()["reportJob"]
        assert status["state"] == "stale"
        assert status["previousState"] == "waiting_1c"
    assert runtime.cache[1, key] == job
    assert runtime.writes == runtime.builds == runtime.refreshes == []
    for reused in (False, True):
        response = runtime.api.post("/api/wb/reports/pnl/jobs", params=PARAMS)
        assert response.status_code == 200
        assert response.json()["state"] == "queued"
        assert response.json()["reused"] is reused
    assert len(runtime.builds) == 1
    assert runtime.refreshes == []


def test_explicit_refresh_can_replace_abandoned_1c_wait_once(runtime):
    key, job = seed(runtime, "pnl", cached=False, state="waiting_1c", stage="waiting_1c")
    job["kind"] = "report_source_refresh"
    job["updatedAt"] = (
        datetime.now(timezone.utc) - reports.BACKGROUND_REPORT_JOB_STALE_AFTER
        - timedelta(seconds=1)
    ).isoformat()
    runtime.cache[1, key] = job
    for reused in (False, True):
        response = runtime.api.post("/api/wb/reports/pnl/refresh-sources-job", params=PARAMS)
        assert response.status_code == 200
        assert response.json()["state"] == "queued"
        assert response.json()["reused"] is reused
    assert len(runtime.refreshes) == 1
    assert runtime.builds == []


def test_expenses_pending_source_without_worker_is_not_a_stale_task(runtime, monkeypatch):
    monkeypatch.setattr(
        reports, "get_cash_flow_for_period", lambda **kw: {"status": "pending"}
    )
    response = runtime.api.get("/api/wb/reports/expenses", params=PARAMS, headers=auth_headers(runtime.api, "finance_viewer"))
    assert response.status_code == 200
    assert response.json()["reportJob"]["state"] == "waiting_1c"
    assert not response.json()["reportJob"].get("taskId")
    assert runtime.writes == runtime.builds == runtime.refreshes == []


def test_stale_worker_can_be_completed_from_valid_cache(runtime):
    key, job = seed(runtime, "pnl")
    job["updatedAt"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    runtime.cache[1, key] = job
    response = runtime.api.get("/api/wb/reports/pnl", params=PARAMS)
    assert response.status_code == 200
    assert response.json()["reportJob"]["state"] == "completed"
    assert runtime.cache[1, key]["state"] == "completed"


@pytest.mark.parametrize("endpoint", ["jobs", "refresh-sources-job"])
@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.parametrize("finance_allowed", [False, True])
def test_pnl_prepares_1c_before_either_dispatch(
    runtime, monkeypatch, endpoint, ready, finance_allowed
):
    runtime.ready = ready
    requested = []
    dispatched = []
    cash_flow = {
        "status": "ready",
        "data": {"totals": {"operationalExpenseKopecks": 164_000}},
    }

    def prepare(**kwargs):
        requested.append(kwargs)
        return cash_flow

    def enqueue(kind, args):
        assert len(requested) == 1, "1C must be prepared before any Celery dispatch"
        assert requested[0]["organization_id"] == 1
        assert requested[0]["period_from"] == START
        assert requested[0]["period_to"] == END
        assert requested[0]["requested_by"] == args[1]
        dispatched.append((kind, args))
        return SimpleNamespace(id="synthetic-task")

    monkeypatch.setattr(reports, "get_cash_flow_for_period", prepare)
    monkeypatch.setattr(
        repricer_tasks.build_report_for_org, "delay",
        lambda *args: enqueue("build", args),
    )
    monkeypatch.setattr(
        repricer_tasks.refresh_report_sources_for_org, "delay",
        lambda *args, **kwargs: enqueue("refresh", args),
    )
    monkeypatch.setattr(
        repricer_tasks.refresh_report_sources_for_org, "apply_async",
        lambda *, args, **kwargs: enqueue("refresh", args),
    )
    headers = auth_headers(runtime.api, "finance_viewer" if finance_allowed else "viewer")
    key = reports._report_job_cache_key(
        "pnl",
        START,
        END,
        "sku",
        "operational",
        finance_allowed=finance_allowed,
    )
    for reused in (False, True):
        response = runtime.api.post(
            f"/api/wb/reports/pnl/{endpoint}", params=PARAMS, headers=headers
        )
        assert response.status_code == 200
        assert response.json()["state"] == "queued"
        assert response.json()["reused"] is reused
        if not reused and endpoint == "jobs":
            assert response.json()["cashFlow"] == (cash_flow if finance_allowed else None)
        elif reused:
            assert "cashFlow" not in response.json()
        assert "cashFlow" not in runtime.cache[1, key]
    assert len(requested) == len(dispatched) == 1
    assert dispatched[0][0] == ("build" if ready else "refresh")
    assert dispatched[0][1][-2:] == (finance_allowed, None)


def test_pnl_completed_cache_does_not_request_1c(runtime, monkeypatch):
    runtime.ready = True
    key, _ = seed(runtime, "pnl", state="completed", stage="completed")
    monkeypatch.setattr(
        reports, "get_cash_flow_for_period",
        lambda **kw: pytest.fail("usable cache needs no 1C request"),
    )
    response = runtime.api.post("/api/wb/reports/pnl/jobs", params=PARAMS)
    assert response.status_code == 200
    assert response.json()["state"] == runtime.cache[1, key]["state"] == "completed"
    assert runtime.builds == runtime.refreshes == []


def seed_digest(runtime, *, cached=True, state="running", stage="funnel"):
    job = {
        "state": state, "stage": stage, "taskId": "digest-task",
        "dateFrom": str(START), "dateTo": str(END),
        "updatedAt": reports._utc_now_iso(), "percent": 82,
        "kind": "report_source_refresh", "sync": {"state": "completed"},
    }
    key = reports._digest_job_cache_key(START, END)
    runtime.cache[1, key] = deepcopy(job)
    if cached:
        runtime.cache[1, reports._digest_cache_key(START, END)] = {
            "dateFrom": str(START), "dateTo": str(END),
            "completedAt": reports._utc_now_iso(),
            "sourceComplete": True, "sourceRevision": runtime.tax_revision,
            "digest": {
                "cacheVersion": reports.DIGEST_REPORT_PAYLOAD_VERSION,
                "meta": {"freshnessState": "cached"}, "rows": [{"sku": "SKU-1"}],
            },
        }
    return key, job


@pytest.mark.parametrize('ready', [False, True])
def test_statistics_on_open_queues_only_missing_sources(runtime, ready):
    runtime.ready = ready
    response = runtime.api.post('/api/wb/reports/stats/jobs', params=PARAMS)
    assert response.status_code == 200
    assert response.json()['state'] == ('completed' if ready else 'queued')
    assert runtime.builds == []
    assert len(runtime.refreshes) == (0 if ready else 1)
    if not ready:
        assert runtime.refreshes[0][2] == 'stats'
        again = runtime.api.post('/api/wb/reports/stats/jobs', params=PARAMS)
        assert again.json()['reused'] is True
        assert len(runtime.refreshes) == 1


@pytest.mark.parametrize('changed', ['coverage', 'revision'])
def test_partial_or_outdated_digest_does_not_block_source_refresh(runtime, changed):
    seed_digest(runtime, state='completed', stage='completed')
    cached = runtime.cache[1, reports._digest_cache_key(START, END)]
    if changed == 'coverage':
        cached['sourceComplete'] = False
    else:
        cached['sourceRevision'] = 'older-source'
    runtime.ready = False
    response = runtime.api.post('/api/wb/reports/digest/refresh', params=PARAMS)
    assert response.status_code == 200
    assert response.json()['state'] == 'queued'
    assert len(runtime.refreshes) == 1 and runtime.builds == []


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("state,stage", [("queued", "queued"), ("running", "funnel")])
@pytest.mark.parametrize("endpoint", ["", "/refresh", "/refresh-sources-job"])
def test_digest_read_and_refresh_preserve_live_job(runtime, cached, state, stage, endpoint):
    key, job = seed_digest(runtime, cached=cached, state=state, stage=stage)
    method = runtime.api.post if endpoint else runtime.api.get
    response = method(f"/api/wb/reports/digest{endpoint}", params=PARAMS)
    assert response.status_code == 200
    if endpoint:
        assert response.json() == {**job, "reused": True}
    else:
        assert response.json()["digestJob"] == job
    assert runtime.cache[1, key] == job
    assert runtime.writes == runtime.builds == runtime.refreshes == []


@pytest.mark.parametrize("params", [{}, PARAMS])
@pytest.mark.parametrize("state,stage", [("running", "funnel"), ("failed", "failed")])
def test_digest_latest_reads_selected_period_job(runtime, params, state, stage):
    key, job = seed_digest(runtime, state=state, stage=stage)
    # Another period must not supply the status for the selected cached payload.
    runtime.cache[1, reports._digest_job_cache_key(END, END)] = {"state": "completed"}
    response = runtime.api.get("/api/wb/reports/digest/latest-cache", params=params)
    assert response.status_code == 200
    assert response.json()["rows"] == [{"sku": "SKU-1"}]
    assert response.json()["digestJob"] == job
    assert runtime.cache[1, key] == job
    assert runtime.writes == []


@pytest.mark.parametrize("suffix", ["", "/status"])
def test_digest_failed_refresh_remains_visible_with_fresh_cache(runtime, suffix):
    key, job = seed_digest(runtime, state="failed", stage="failed")
    response = runtime.api.get(f"/api/wb/reports/digest{suffix}", params=PARAMS)
    assert response.status_code == 200
    assert (response.json() if suffix else response.json()["digestJob"]) == job
    assert runtime.cache[1, key] == job
    assert runtime.writes == []


@pytest.mark.parametrize("state", ["queued", "running"])
@pytest.mark.parametrize("heartbeat", ["missing", "expired"])
@pytest.mark.parametrize("suffix", ["", "/status", "/refresh"])
@pytest.mark.parametrize("sources_ready", [False, True])
def test_digest_abandoned_job_is_stale_and_restarts_once(runtime, state, heartbeat, suffix, sources_ready):
    runtime.ready = sources_ready
    key, job = seed_digest(runtime, cached=False, state=state)
    if heartbeat == "missing":
        job.pop("updatedAt")
    else:
        limit = (reports.BACKGROUND_REPORT_QUEUED_STALE_AFTER if state == "queued"
                 else reports.BACKGROUND_REPORT_JOB_STALE_AFTER)
        job["updatedAt"] = (datetime.now(timezone.utc) - limit - timedelta(seconds=1)).isoformat()
    runtime.cache[1, key] = deepcopy(job)
    if suffix != "/refresh":
        response = runtime.api.get(f"/api/wb/reports/digest{suffix}", params=PARAMS)
        assert response.status_code == 200
        actual = response.json() if suffix else response.json()["digestJob"]
        assert actual["state"] == "stale"
        assert actual["previousState"] == state
        assert actual["taskId"] == "digest-task"
    assert runtime.cache[1, key] == job
    assert runtime.writes == []
    for reused in (False, True):
        response = runtime.api.post("/api/wb/reports/digest/refresh", params=PARAMS)
        assert response.status_code == 200
        assert response.json()["state"] == "queued"
        assert response.json()["reused"] is reused
    if sources_ready:
        assert runtime.builds == [(1, str(START), str(END), False, None)]
        assert runtime.refreshes == []
    else:
        assert runtime.builds == []
        assert len(runtime.refreshes) == 1
        assert runtime.refreshes[0][2:] == ('digest', str(START), str(END), 'sku', 'operational', False, None)


def test_digest_latest_exposes_stale_job_with_fresh_cache(runtime):
    key, job = seed_digest(runtime)
    job.pop("updatedAt")
    runtime.cache[1, key] = deepcopy(job)
    response = runtime.api.get("/api/wb/reports/digest/latest-cache")
    assert response.status_code == 200
    assert response.json()["digestJob"]["state"] == "stale"
    assert response.json()["digestJob"]["taskId"] == "digest-task"
    assert runtime.cache[1, key] == job
    assert runtime.writes == []


@pytest.mark.parametrize("state", ["missing", "completed", "stale", "failed"])
def test_digest_refresh_keeps_fresh_cache_fast_path(runtime, state):
    runtime.ready = True
    key, job = seed_digest(runtime, state=state, stage=state)
    if state == "missing":
        runtime.cache.pop((1, key))
    response = runtime.api.post("/api/wb/reports/digest/refresh", params=PARAMS)
    assert response.status_code == 200
    assert response.json()["state"] == "completed"
    assert response.json()["cacheFresh"] is True
    assert runtime.cache[1, key]["state"] == "completed"
    assert runtime.builds == runtime.refreshes == []


def test_digest_latest_without_job_keeps_completed_cache_response(runtime):
    key, _ = seed_digest(runtime)
    runtime.cache.pop((1, key))
    response = runtime.api.get("/api/wb/reports/digest/latest-cache")
    assert response.status_code == 200
    assert response.json()["digestJob"]["state"] == "completed"
    assert response.json()["digestJob"]["dateFrom"] == str(START)
    assert runtime.writes == []


@pytest.mark.parametrize("endpoint", ["", "/refresh"])
def test_digest_cache_preserves_standalone_builder_without_refresh_kind(runtime, endpoint):
    key, job = seed_digest(runtime)
    job.pop("kind")
    job.pop("sync")
    runtime.cache[1, key] = deepcopy(job)
    method = runtime.api.post if endpoint else runtime.api.get
    response = method(f"/api/wb/reports/digest{endpoint}", params=PARAMS)
    assert response.status_code == 200
    actual = response.json() if endpoint else response.json()["digestJob"]
    assert actual["state"] == "running" and actual["taskId"] == "digest-task"
    assert runtime.cache[1, key] == job
    assert runtime.writes == runtime.builds == runtime.refreshes == []


@pytest.mark.parametrize("missing", [False, True])
def test_digest_read_can_complete_abandoned_job_from_fresh_cache(runtime, missing):
    key, job = seed_digest(runtime)
    job.pop("updatedAt")
    if missing:
        runtime.cache.pop((1, key))
    else:
        runtime.cache[1, key] = job
    response = runtime.api.get("/api/wb/reports/digest", params=PARAMS)
    assert response.status_code == 200
    assert response.json()["digestJob"]["state"] == "completed"
    assert response.json()["rows"] == [{"sku": "SKU-1"}]
    assert runtime.cache[1, key]["state"] == "completed"
    assert runtime.builds == runtime.refreshes == []
