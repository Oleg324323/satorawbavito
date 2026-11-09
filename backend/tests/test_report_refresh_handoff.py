"""Source refresh must hand off to a real Celery request with its own retry."""

from copy import deepcopy
from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from celery.exceptions import Ignore, Retry

from app import repricer_tasks as tasks
from app.routers import wb_reports_bff as reports


def run_as_worker(task, args, kwargs=None, **options):
    task.push_request(
        id=options.get("task_id", "refresh-task"),
        args=args,
        kwargs=kwargs or {},
        called_directly=False,
        is_eager=False,
        retries=options.get("retries", 0),
    )
    try:
        return task.run(*args, **(kwargs or {}))
    finally:
        task.pop_request()


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.setattr('app.wb_catalog_loading.missing_catalog_sources', lambda org: ())
    state = SimpleNamespace(
        cache={}, writes=[], queue=[], refreshes=[], ready=False, cash_status="pending"
    )

    def save(org, key, payload):
        state.cache[org, key] = deepcopy(payload)
        state.writes.append(deepcopy(payload))

    def refresh(**kwargs):
        state.refreshes.append(kwargs)
        return {"state": "completed", "steps": [{"source": "finance", "status": "ok"}]}

    def enqueue(args=None, kwargs=None, **options):
        state.queue.append((args, kwargs, options))
        return SimpleNamespace(id=options.get("task_id"))

    monkeypatch.setattr(tasks, "_report_refresh_wb_token", lambda *args: "synthetic")
    monkeypatch.setattr(tasks, "refresh_wb_data_sources", refresh)
    monkeypatch.setattr('app.wb_pnl_advertising.missing_campaign_ranges',
        lambda org, left, right, **kw: [] if state.ready else [(left, right)])
    monkeypatch.setattr('app.wb_pnl_advertising.collect_campaign_spend',
        lambda org, token, left, right, progress: tasks.refresh_wb_data_sources(
            organization_id=org, wb_token=token, date_from=left, date_to=right, sources=('ads',)))
    monkeypatch.setattr(tasks.repricer_bff_module, "fetch_finance_report_aggregates", lambda *a, **kw: {})
    monkeypatch.setattr('app.wb_abc_loading.missing_source_ranges', lambda *a, **kw: [] if state.ready else [(a[2], a[3])])
    monkeypatch.setattr(reports, '_report_daily_sources_ready', lambda *a, **kw: (state.ready, [] if state.ready else ['finance']))
    monkeypatch.setattr(reports, '_digest_source_revision', lambda *a: 'test-source-revision')
    monkeypatch.setattr(
        tasks,
        "_report_snapshot_sources_ready",
        lambda *args: (state.ready, ["finance"]),
    )
    monkeypatch.setattr(tasks.build_report_for_org, "apply_async", enqueue)
    monkeypatch.setattr(tasks.build_digest_for_org, "apply_async", enqueue)
    monkeypatch.setattr(
        tasks.build_digest_for_org, "AsyncResult",
        lambda task_id: SimpleNamespace(id=task_id),
    )
    monkeypatch.setattr(
        tasks.build_report_for_org,
        "AsyncResult",
        lambda task_id: SimpleNamespace(id=task_id),
    )
    monkeypatch.setattr(reports, "save_source_cache", save)
    monkeypatch.setattr(
        reports,
        "get_source_cache",
        lambda org, key, **kw: deepcopy(state.cache.get((org, key))),
    )
    monkeypatch.setattr(
        reports, "get_cash_flow_for_period", lambda **kw: {"status": state.cash_status}
    )
    monkeypatch.setattr(reports, "build_pnl_report", lambda **kw: SimpleNamespace())
    payload = {
        "cacheVersion": reports.PNL_REPORT_PAYLOAD_VERSION,
        "rows": [{"sku": "SKU-1"}],
    }
    monkeypatch.setattr(
        reports, "_map_pnl_to_report_response", lambda *args, **kwargs: deepcopy(payload)
    )
    monkeypatch.setattr(
        reports, "_build_ads_report_payload", lambda **kw: deepcopy(payload)
    )
    monkeypatch.setattr(
        reports, "_apply_report_rules_to_payload", lambda payload, org: payload
    )
    monkeypatch.setattr(reports, "build_cached_wb_reports_sources_snapshot", lambda **kw: SimpleNamespace())
    monkeypatch.setattr(reports, "_build_digest_ads_snapshot", lambda **kw: SimpleNamespace())
    monkeypatch.setattr(reports, "_build_digest_funnel_snapshot", lambda **kw: {})
    monkeypatch.setattr(reports, "build_plan_fact_report", lambda **kw: SimpleNamespace())
    monkeypatch.setattr(tasks, "_digest_report_summary", lambda *args: {})
    monkeypatch.setattr(
        reports, "_build_digest_payload",
        lambda *args: {"cacheVersion": reports.DIGEST_REPORT_PAYLOAD_VERSION, "rows": [{"sku": "SKU-1"}]},
    )
    return state


def refresh_args(report_id):
    return (
        1,
        "synthetic-user",
        report_id,
        "2026-08-17",
        "2026-08-23",
        "sku",
        "operational",
        False,
        None,
    )


@pytest.mark.parametrize("report_id", ["abc", "pnl", "rnp", "week-over-week"])
def test_financial_report_refresh_publishes_completed_raw_rows_to_canonical(runtime, monkeypatch, report_id):
    payload = {"rows": [{"rrdId": 101}], "dateFrom": "2026-08-17", "dateTo": "2026-08-23"}
    snapshots = []
    monkeypatch.setattr(tasks, "_report_source_refresh_plan", lambda _report_id: {"sources": ("finance",)})
    monkeypatch.setattr(tasks.repricer_bff_module, "fetch_finance_report_aggregates", lambda *a, **kw: payload)
    monkeypatch.setattr("app.platform.finance.service.shadow_ingest_legacy_finance_payload",
        lambda org, data: snapshots.append((org, data)) or {"state": "ready"})
    args = list(refresh_args(report_id))
    args[7] = True
    with pytest.raises(Ignore):
        run_as_worker(tasks.refresh_report_sources_for_org, tuple(args))
    assert snapshots and all(org == 1 and data is payload for org, data in snapshots)


def test_failed_canonical_publication_does_not_complete_report_refresh(runtime, monkeypatch):
    monkeypatch.setattr(tasks, "_report_source_refresh_plan", lambda _report_id: {"sources": ("finance",)})
    monkeypatch.setattr("app.platform.finance.service.shadow_ingest_legacy_finance_payload",
        lambda *a, **kw: {"state": "failed"})
    args = list(refresh_args("abc"))
    args[7] = True
    with pytest.raises(RuntimeError, match="финансовый снимок"):
        run_as_worker(tasks.refresh_report_sources_for_org, tuple(args))
    assert runtime.writes[-1]["state"] == "failed"
    assert runtime.queue == []


def test_statistics_comparison_collects_only_ads_and_funnel_summary_for_exact_window(runtime):
    runtime.ready = True
    args = list(refresh_args('stats'))
    args[3:5] = ['2026-10-04', '2026-10-06']
    args[6] = 'statistics-comparison'
    result = run_as_worker(tasks.refresh_report_sources_for_org, tuple(args))
    assert result['state'] == 'completed'
    assert len(runtime.refreshes) == 1
    call = runtime.refreshes[0]
    assert call['sources'] == ('ads', 'baskets')
    assert call['baskets_include_daily_detail'] is False
    assert call['date_from'] == date(2026, 10, 4)
    assert call['date_to'] == date(2026, 10, 6)


def test_rate_limited_source_retries_only_missing_sources_without_blocking_worker(runtime, monkeypatch):
    monkeypatch.setattr(tasks, 'refresh_wb_data_sources', lambda **kw: {
        'state': 'failed', 'steps': [{'source': 'finance', 'status': 'error',
        'error': 'rate limit exceeded; повтор через 125 сек'}]})
    queued = []
    monkeypatch.setattr(tasks.refresh_report_sources_for_org, 'apply_async',
                        lambda args=None, kwargs=None, **kw: queued.append({**kw, 'kwargs': kwargs, 'args': args}) or SimpleNamespace(id='retry'))
    with pytest.raises(Retry):
        run_as_worker(tasks.refresh_report_sources_for_org, refresh_args('stats'))
    assert queued[0]['countdown'] == 125
    assert queued[0]['kwargs']['refresh_missing_only'] is True
    assert runtime.writes[-1]['state'] == 'queued'
    assert runtime.writes[-1]['stage'] == 'rate_limit'


def test_rate_limited_source_stops_after_three_retries(runtime, monkeypatch):
    monkeypatch.setattr(tasks, 'refresh_wb_data_sources', lambda **kw: {
        'state': 'failed', 'steps': [{'source': 'finance', 'status': 'error', 'error': 'HTTP 429'}]})
    with pytest.raises(tasks.ReportSourceRateLimited):
        run_as_worker(tasks.refresh_report_sources_for_org, refresh_args('stats'), retries=3)
    assert runtime.writes[-1]['state'] == 'failed'


def test_authentication_failure_is_not_rate_limit_retry():
    assert tasks._report_rate_limit_delay({'steps': [{'status': 'error', 'error': 'HTTP 403'}]}) is None


def test_rate_limit_queue_is_reusable_through_retry_delay_and_normal_grace():
    now = datetime.now(timezone.utc)
    job = {'state': 'queued', 'stage': 'rate_limit', 'retryAfterSeconds': 60,
           'updatedAt': (now - timedelta(seconds=45)).isoformat()}
    assert reports._report_job_is_reusable(job)
    assert not reports._report_job_is_reusable({**job, 'stage': 'queued'})
    assert not reports._report_job_is_reusable({**job, 'updatedAt': (now - timedelta(seconds=95)).isoformat()})


@pytest.mark.parametrize("windows", [[(date(2026, 8, 17), date(2026, 8, 23))], []])
def test_repricer_finance_job_never_mutates_legacy_reports(runtime, monkeypatch, windows):
    from app import repricer_page_finance as page
    monkeypatch.setattr(page, "missing_page_ranges", lambda *args: windows)
    collected = []
    monkeypatch.setattr(tasks.repricer_bff_module, "fetch_finance_report_aggregates", lambda *args, **kw: collected.append((args, kw)))
    args = list(refresh_args("stats"))
    args[6] = "repricer-finance:" + page.connection_key("synthetic")
    result = run_as_worker(tasks.refresh_report_sources_for_org, tuple(args), {"refresh_missing_only": True})
    assert result["state"] == result["stage"] == "completed"
    assert result["kind"] == "report_source_refresh"
    assert len(collected) == len(windows)
    if windows:
        assert collected[0][0] == ("complete",)
        assert collected[0][1]["repricer_operations_org"] == 1
        assert collected[0][1]["date_from"].date() == windows[0][0]
    assert runtime.refreshes == runtime.queue == []


def test_repricer_connection_change_records_failed_job(runtime):
    args = list(refresh_args("stats")); args[6] = "repricer-finance:old-connection"
    with pytest.raises(RuntimeError, match="connection changed"):
        run_as_worker(tasks.refresh_report_sources_for_org, tuple(args))
    assert runtime.writes[-1]["state"] == "failed"
    assert runtime.refreshes == runtime.queue == []


def test_leaving_view_pauses_worker_without_losing_saved_source_batches(runtime, monkeypatch):
    from app.wb_api.report_reads import ReportDemandEnded, active_report_read
    def finish_batch_then_cancel(**kwargs):
        runtime.cache[1, 'persisted-source-batch'] = {'rows': [{'nmId': 123}]}
        raise ReportDemandEnded()
    monkeypatch.setattr(tasks, 'refresh_wb_data_sources', finish_batch_then_cancel)
    monkeypatch.setattr(tasks.repricer_bff_module, 'fetch_finance_report_aggregates', lambda *a, **kw: finish_batch_then_cancel(**kw))
    result = run_as_worker(tasks.refresh_report_sources_for_org, refresh_args('pnl'))
    assert result['state'] == 'paused'
    assert runtime.cache[1, 'persisted-source-batch']['rows'] == [{'nmId': 123}]
    assert runtime.queue == []
    assert active_report_read.get() is None


def test_advertising_refresh_publishes_statistics_before_optional_budgets(runtime, monkeypatch):
    calls = []
    def collect(**kw):
        assert kw['include_budgets'] is False and kw['group_by'] == 'campaign'
        calls.append('collect')
        return SimpleNamespace(source_status='fresh')
    monkeypatch.setattr(reports, 'build_ads_attribution_snapshot', collect)
    monkeypatch.setattr(reports, '_map_ads_to_report_response', lambda *a: {'rows': [{'campaignId': '123', 'budgetTotalKopecks': 12000}]})
    def save(**kw):
        calls.append('save')
        assert kw['payload']['rows'][0]['budgetTotalKopecks'] == 12000
        return kw['payload']
    monkeypatch.setattr(reports, 'save_ads_report_cache', save)
    monkeypatch.setattr(reports, 'save_ads_history_snapshots', lambda **kw: calls.append('history'))
    monkeypatch.setattr(reports, '_save_exact_report_payload_cache', lambda **kw: calls.append('exact-cache'))
    from app import wb_report_budgets
    def optional_start(*args):
        assert runtime.writes[-1]['state'] == 'completed'
        calls.append('budgets')
        raise RuntimeError('optional queue unavailable')
    monkeypatch.setattr(wb_report_budgets, 'start', optional_start)
    result = run_as_worker(tasks.refresh_report_sources_for_org, refresh_args('ads'))
    assert result['state'] == 'completed'
    assert calls == ['collect', 'save', 'history', 'exact-cache', 'budgets']
    assert runtime.refreshes == runtime.queue == []  # No duplicate SKU-only API collection.


@pytest.mark.parametrize("report_id", ["abc", "pnl"])
def test_refresh_hands_off_without_completing_a_waiting_report(runtime, report_id):
    with pytest.raises(Ignore):
        run_as_worker(tasks.refresh_report_sources_for_org, refresh_args(report_id))
    assert len(runtime.queue) == len(runtime.refreshes) == 1
    args, kwargs, options = runtime.queue[0]
    assert args == refresh_args(report_id)
    assert options["task_id"] == "refresh-task"
    assert runtime.writes[-1]["state"] == "running"
    assert runtime.writes[-1]["stage"] == "building_report"
    result = run_as_worker(tasks.build_report_for_org, args, kwargs, **options)
    assert result["state"] == "failed"
    assert result["taskId"] == "refresh-task"
    assert result["kind"] == "report_source_refresh"
    assert result["sync"]["state"] == "completed"
    assert not any(row.get("state") == "completed" for row in runtime.writes)


def test_1c_retry_requeues_only_build_with_same_identity_and_context(runtime):
    runtime.ready = True
    with pytest.raises(Ignore):
        run_as_worker(tasks.refresh_report_sources_for_org, refresh_args("pnl"))
    args, kwargs, options = runtime.queue[0]
    with pytest.raises(Retry):
        run_as_worker(tasks.build_report_for_org, args, kwargs, **options)
    assert runtime.writes[-1]["state"] == "waiting_1c"
    assert runtime.writes[-1]["kind"] == "report_source_refresh"
    assert len(runtime.queue) == 2
    retry_args, retry_kwargs, retry_options = runtime.queue[1]
    assert retry_args == args and retry_kwargs == kwargs
    assert retry_options["task_id"] == "refresh-task"
    assert retry_options["countdown"] == 3
    assert retry_options["retries"] == 1
    runtime.cash_status = "ready"
    result = run_as_worker(
        tasks.build_report_for_org, retry_args, retry_kwargs, **retry_options
    )
    assert result["state"] == "completed"
    assert result["taskId"] == "refresh-task"
    assert result["kind"] == "report_source_refresh"
    assert result["sync"] == kwargs["source_refresh"]
    assert runtime.refreshes == []  # Covered sources must not be collected again.
    assert not any(row.get("state") == "failed" for row in runtime.writes)


def test_financial_pnl_does_not_wait_for_optional_1c(runtime):
    runtime.ready = True
    args = list(refresh_args("pnl"))
    args[6] = "financial"
    result = run_as_worker(tasks.build_report_for_org, tuple(args))
    assert result["state"] == "completed"
    assert not any(row.get("state") == "waiting_1c" for row in runtime.writes)


def test_failed_replacement_remains_a_failed_refresh(runtime):
    runtime.ready = True
    runtime.cash_status = "error"
    with pytest.raises(Ignore):
        run_as_worker(tasks.refresh_report_sources_for_org, refresh_args("pnl"))
    args, kwargs, options = runtime.queue[0]
    with pytest.raises(RuntimeError, match="1С"):
        run_as_worker(tasks.build_report_for_org, args, kwargs, **options)
    failed = runtime.writes[-1]
    assert failed["state"] == failed["stage"] == "failed"
    assert failed["taskId"] == "refresh-task"
    assert failed["sync"] == kwargs["source_refresh"]
    assert reports._report_job_is_finished_refresh(failed)


def test_abc_refresh_collects_raw_advertising_before_report_handoff(runtime, monkeypatch):
    monkeypatch.setattr(tasks, "get_settings", lambda: SimpleNamespace(
        advertising_shadow_ingest_enabled=True,
        advertising_shadow_ingest_organization_ids=[1],
    ))
    calls = []

    def collect(org, period, *, wb_token):
        calls.append((org, period.date_from, period.date_to, wb_token))
        return {"state": "ready", "factCount": 1, "marketplaceAccountId": 31}

    monkeypatch.setattr(tasks, "backfill_raw_advertising", collect)
    bindings = []
    monkeypatch.setattr(tasks, "get_session_factory", lambda: lambda: nullcontext(None))
    monkeypatch.setattr(tasks, "list_cached_goods", lambda org: [{"nmID": 123, "vendorCode": "SKU"}])
    monkeypatch.setattr(tasks, "apply_cost_backfill", lambda session, **kwargs: bindings.append(kwargs))
    args = (*refresh_args("abc")[:7], True, None)
    with pytest.raises(Ignore):
        run_as_worker(tasks.refresh_report_sources_for_org, args)
    assert calls == [(1, date(2026, 8, 17), date(2026, 8, 23), "synthetic")]
    assert runtime.queue[0][1]["source_refresh"]["canonicalAdvertising"]["state"] == "ready"
    assert bindings[0]["marketplace_account_id"] == 31
    assert bindings[0]["snapshot"].goods == [{"nmID": 123, "vendorCode": "SKU"}]
    assert bindings[0]["include_costs"] is False


def test_standalone_build_keeps_existing_contract(runtime):
    runtime.ready = True
    result = run_as_worker(tasks.build_report_for_org, refresh_args("ads"))
    assert result["state"] == "completed"
    assert "kind" not in result and "sync" not in result
    assert runtime.refreshes == runtime.queue == []


def test_digest_refresh_replaces_inline_build_with_same_task(runtime):
    runtime.ready = True
    with pytest.raises(Ignore):
        run_as_worker(tasks.refresh_report_sources_for_org, refresh_args("digest"))
    assert len(runtime.queue) == len(runtime.refreshes) == 1
    args, kwargs, options = runtime.queue[0]
    assert args == (1, "2026-08-17", "2026-08-23", False, None)
    assert kwargs == {"source_refresh": {"state": "completed", "steps": [{"source": "finance", "status": "ok"}]}}
    assert options["task_id"] == "refresh-task"
    assert runtime.writes[-1]["state"] == "running"
    assert runtime.writes[-1]["stage"] == "building_report"
    assert not any("digest" in row or row["state"] in {"completed", "failed"} for row in runtime.writes)
    result = run_as_worker(tasks.build_digest_for_org, args, kwargs, **options)
    terminal = [row for row in runtime.writes if row.get("state") in {"completed", "failed"}]
    assert terminal == [result]
    assert result["state"] == "completed"
    assert result["taskId"] == "refresh-task"


@pytest.mark.parametrize("fail", [False, True])
def test_digest_builder_preserves_refresh_context_at_every_stage(runtime, monkeypatch, fail):
    runtime.ready = True
    sync = {"state": "completed", "steps": [{"source": "finance", "status": "ok"}]}

    def funnel(**kwargs):
        kwargs["progress_callback"]("funnel-page", "Page loaded", 90)
        if fail:
            raise RuntimeError("digest build failed")
        return {}

    monkeypatch.setattr(reports, "_build_digest_funnel_snapshot", funnel)
    args = (1, "2026-08-17", "2026-08-23", False, None)
    if fail:
        with pytest.raises(RuntimeError, match="digest build failed"):
            run_as_worker(tasks.build_digest_for_org, args, {"source_refresh": sync})
    else:
        run_as_worker(tasks.build_digest_for_org, args, {"source_refresh": sync})
    jobs = [row for row in runtime.writes if "state" in row]
    for job in jobs:
        assert job["taskId"] == "refresh-task"
        assert job["kind"] == "report_source_refresh"
        assert job["sync"] == sync
        assert job["dateFrom"] == "2026-08-17" and job["dateTo"] == "2026-08-23"
        if job["state"] == "running":
            assert reports._report_job_is_active_refresh(job)
    assert jobs[-1]["state"] == jobs[-1]["stage"] == ("failed" if fail else "completed")
    assert reports._report_job_is_finished_refresh(jobs[-1])
    assert len([job for job in jobs if job["state"] in {"completed", "failed"}]) == 1
    key = reports._digest_cache_key(date(2026, 8, 17), date(2026, 8, 23))
    if fail:
        assert (1, key) not in runtime.cache
        assert (1, "reports_digest_latest") not in runtime.cache
    else:
        assert runtime.cache[1, key] == runtime.cache[1, "reports_digest_latest"]
        assert runtime.cache[1, key]["digest"]["rows"] == [{"sku": "SKU-1"}]
    assert runtime.refreshes == runtime.queue == []


def test_standalone_digest_build_keeps_five_positional_arguments(runtime):
    runtime.ready = True
    result = run_as_worker(tasks.build_digest_for_org, (1, "2026-08-17", "2026-08-23", False, None))
    assert result["state"] == "completed"
    for job in runtime.writes:
        assert "kind" not in job and "sync" not in job
    assert runtime.refreshes == runtime.queue == []


def test_page_demand_refresh_only_fetches_missing_source(runtime, monkeypatch):
    monkeypatch.setattr(reports, '_report_missing_source_ranges', lambda org, source, **kwargs: [] if source == 'finance' else [(kwargs['date_from'], kwargs['date_to'])])
    with pytest.raises(Ignore):
        run_as_worker(tasks.refresh_report_sources_for_org, refresh_args('pnl'), {'refresh_missing_only': True})
    assert runtime.refreshes[0]['sources'] == ('ads',)


def test_page_demand_refresh_reuses_all_saved_sources(runtime, monkeypatch):
    monkeypatch.setattr('app.wb_pnl_advertising.missing_campaign_ranges', lambda *a, **kw: [])
    monkeypatch.setattr('app.wb_abc_loading.missing_source_ranges', lambda *args, **kwargs: [])
    monkeypatch.setattr(reports, '_report_missing_source_ranges', lambda *args, **kwargs: [])
    with pytest.raises(Ignore):
        run_as_worker(tasks.refresh_report_sources_for_org, refresh_args('pnl'), {'refresh_missing_only': True})
    assert runtime.refreshes == []
    assert len(runtime.queue) == 1
