"""Report-local orchestration: no provider access or production state."""
from datetime import date, datetime, timedelta, timezone
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from celery.exceptions import Retry
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app import repricer_tasks as tasks, wb_abc_loading as loading, wb_week_sources as week
from app.repricer_cache import store
from app.routers import wb_reports_bff as reports

START, END = date(2026, 10, 7), date(2026, 10, 11)
PREVIOUS = (date(2026, 10, 2), date(2026, 10, 6))


@pytest.mark.parametrize("value,expected", [(True, True), (False, False), (None, False), ("true", False)])
def test_receipt_flags_do_not_hydrate_ledgers_or_cross_accounts(monkeypatch, value, expected):
    engine = create_engine("sqlite://")
    store.WbRepricerSourceCacheRow.__table__.create(engine)
    with Session(engine) as session:
        session.add(store.WbRepricerSourceCacheRow(organization_id=1, source_key="synthetic",
            payload={"sourceComplete": value, "operations": ["x" * 10_000]},
            fetched_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc)))
        session.commit()
        monkeypatch.setattr(store, "_run_db", lambda callback: callback(session))
        statements = []
        def capture(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        event.listen(engine, "before_cursor_execute", capture)
        assert store.get_source_cache_completion_flags(1, "synthetic")["sourceComplete"] is expected
        assert store.get_source_cache_completion_flags(2, "synthetic") == {}
        assert all("json_type(" in statement for statement in statements)
    engine.dispose()


@pytest.fixture
def collection(monkeypatch):
    from app.wb_api import report_reads
    state = SimpleNamespace(saved={}, calls=[], previews=[], builders=[])
    monkeypatch.setattr(tasks, "_report_refresh_wb_token", lambda *a: "synthetic")
    monkeypatch.setattr('app.wb_catalog_loading.missing_catalog_sources', lambda org: ())
    monkeypatch.setattr(report_reads, "install_report_read", lambda *a: report_reads.active_report_read.set(None))
    monkeypatch.setattr(reports, "get_source_cache", lambda org, key, **kw: state.saved.get(key, {}))
    monkeypatch.setattr(reports, "save_source_cache", lambda org, key, value: state.saved.__setitem__(key, value))
    monkeypatch.setattr(tasks, "_publish_loading_report_snapshot", lambda *a, **kw: state.previews.append((len(state.calls), kw['include_previous'])))
    monkeypatch.setattr(tasks, "get_settings", lambda: SimpleNamespace(advertising_shadow_ingest_enabled=False))
    monkeypatch.setattr(loading, "missing_source_ranges", lambda org, source, left, right, **kw: [(left, right)] if source in {"finance", "ads"} else [])
    monkeypatch.setattr(week, "load_funnel", lambda org, left, right: {"needsCollection": True, "missingRanges": [(left, right)]})
    monkeypatch.setattr(week, "collect_funnel", lambda org, token, left, right: state.calls.append(("funnel", left, right)))
    def finance(*args, **kwargs):
        state.calls.append(("finance", kwargs['date_from'].date(), kwargs['date_to'].date()))
    monkeypatch.setattr(tasks.repricer_bff_module, "fetch_finance_report_aggregates", finance)
    def collect(**kw):
        state.calls.append(("ads", kw['date_from'], kw['date_to']))
        return {"state": "completed", "steps": []}
    monkeypatch.setattr(tasks, "refresh_wb_data_sources", collect)
    monkeypatch.setattr(tasks.refresh_report_sources_for_org, "replace", lambda signature: state.builders.append(signature) or {"state": "building"})
    state.args = (1, "synthetic-user", "week-over-week", str(START), str(END), "sku", "operational", True, None)
    return state


def test_every_selected_source_precedes_every_previous_source(collection):
    assert tasks.refresh_report_sources_for_org.run(*collection.args) == {"state": "building"}
    assert [call[1:] for call in collection.calls] == [(START, END)] * 3 + [PREVIOUS] * 3
    # Collection remains current-first, but persisted comparison evidence is
    # readable before the first provider batch (including long campaign reads).
    assert collection.previews[0] == (0, True)
    assert (3, True) in collection.previews
    assert all(include_previous for _, include_previous in collection.previews)


@pytest.mark.parametrize("report_id", ["abc", "pnl"])
def test_financial_worker_never_waits_for_shadow_campaign_backfill(collection, monkeypatch, report_id):
    monkeypatch.setattr('app.wb_pnl_advertising.missing_campaign_ranges', lambda *a, **kw: [])
    economics = []
    monkeypatch.setattr(tasks, "get_settings", lambda: SimpleNamespace(
        advertising_shadow_ingest_enabled=True, advertising_shadow_ingest_organization_ids=(1,)))
    monkeypatch.setattr(loading, "missing_source_ranges", lambda org, source, left, right, **kw:
        [(left, right)] if source == "finance" else [])
    monkeypatch.setattr(tasks, "get_session_factory", lambda: lambda: nullcontext(object()))
    monkeypatch.setattr(tasks, "_connected_wb_account_id", lambda *a, **kw: 31)
    monkeypatch.setattr(tasks, "apply_cost_backfill", lambda session, **kw: economics.append(kw))
    monkeypatch.setattr(tasks, "list_cached_goods", lambda org: [])
    monkeypatch.setattr(tasks, "get_source_cache", lambda *a, **kw: {})
    monkeypatch.setattr("app.platform.advertising.raw_backfill.backfill_raw_advertising",
        lambda *a, **kw: pytest.fail("financial report must not download campaign evidence"))
    args = (*collection.args[:2], report_id, *collection.args[3:])
    assert tasks.refresh_report_sources_for_org.run(*args) == {"state": "building"}
    assert collection.calls == [("finance", START, END)]
    assert len(economics) == 1 and economics[0]["marketplace_account_id"] == 31
    assert economics[0]["include_costs"] is False


def test_complete_previous_coverage_is_reused_without_new_marketplace_reads(collection, monkeypatch):
    monkeypatch.setattr(loading, "missing_source_ranges", lambda org, source, left, right, **kw:
        [(left, right)] if left == START and source in {"finance", "ads"} else [])
    monkeypatch.setattr(week, "load_funnel", lambda org, left, right:
        {"needsCollection": left == START, "missingRanges": [(left, right)]})
    tasks.refresh_report_sources_for_org.run(*collection.args)
    assert [call[1:] for call in collection.calls] == [(START, END)] * 3
    assert len(collection.builders) == 1


def test_terminal_current_error_does_not_starve_previous_period(collection, monkeypatch):
    def finance(*args, **kw):
        left, right = kw['date_from'].date(), kw['date_to'].date()
        collection.calls.append(("finance", left, right))
        if left == START:
            raise HTTPException(500, "synthetic source unavailable")
    monkeypatch.setattr(tasks.repricer_bff_module, "fetch_finance_report_aggregates", finance)
    tasks.refresh_report_sources_for_org.run(*collection.args)
    assert ("ads", START, END) in collection.calls
    assert any(call[1:] == PREVIOUS for call in collection.calls)
    errors = collection.builders[0].kwargs['source_refresh']['sourceErrors']
    assert errors[0]['periodPhase'] == 'selected'
    assert errors[0]['sources'] == ['finance']


def test_current_throttle_resumes_before_previous_and_honors_retry_header(collection, monkeypatch):
    def limited(*args, **kw):
        raise HTTPException(429, "synthetic", headers={"Retry-After": "61"})
    monkeypatch.setattr(tasks.repricer_bff_module, "fetch_finance_report_aggregates", limited)
    retries = []
    def retry(**kw):
        retries.append(kw)
        raise Retry()
    monkeypatch.setattr(tasks.refresh_report_sources_for_org, "retry", retry)
    with pytest.raises(Retry):
        tasks.refresh_report_sources_for_org.run(*collection.args)
    assert all(call[1:] == (START, END) for call in collection.calls)
    assert retries[0]['countdown'] >= 61
    assert retries[0]['kwargs']['refresh_missing_only'] is True


@pytest.mark.parametrize('report_id', ['abc', 'pnl', 'rnp', 'week-over-week'])
@pytest.mark.parametrize('start,end', [('2026-10-01', '2026-10-06'), ('2026-10-11', '2026-10-14')])
def test_cached_endpoint_does_not_build_a_report_inside_the_gateway(monkeypatch, report_id, start, end):
    actor = SimpleNamespace(organization_id=1)
    monkeypatch.setattr(reports, 'actor_from_request', lambda request: actor)
    monkeypatch.setattr(reports, 'assert_permission_or_audit', lambda **kw: None)
    monkeypatch.setattr(reports, 'has_permission', lambda *a: True)
    monkeypatch.setattr(reports, '_latest_report_payload_cache', lambda **kw: None)
    monkeypatch.setattr(reports, '_saved_report_preview', lambda **kw: pytest.fail('HTTP must not assemble raw financial data'))
    with pytest.raises(HTTPException) as error:
        reports.get_reports_latest_cache.__wrapped__(object(), report_id, 'sku', 'operational', 'custom', start, end)
    assert error.value.status_code == 404


@pytest.mark.parametrize('report_id', ['abc', 'pnl', 'rnp', 'week-over-week'])
def test_report_waits_for_a_busy_worker_instead_of_expiring_after_30_seconds(report_id):
    stamp = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    assert reports._report_job_is_reusable({'state': 'queued', 'reportId': report_id, 'updatedAt': stamp})
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    assert not reports._report_job_is_reusable({'state': 'queued', 'reportId': report_id, 'updatedAt': old})


def test_source_changes_do_not_force_synchronous_pnl_rebuild(monkeypatch):
    monkeypatch.setattr('app.wb_pnl_advertising._source_context', lambda org: None)
    monkeypatch.setattr('app.wb_pnl_costs.current_cost_revision', lambda org: 'synthetic-cost')
    monkeypatch.setattr(reports, 'legacy_finance_tax_revision', lambda org: 'synthetic')
    monkeypatch.setattr(reports, 'get_settings', lambda: SimpleNamespace(one_c_enabled=False))
    monkeypatch.setattr(reports, 'list_source_cache_ranges_by_prefix', lambda *a, **kw: [{
        'dateFrom': '2026-10-01', 'dateTo': '2026-10-06', 'fetchedAt': datetime.now(timezone.utc).isoformat()}])
    cache = {'dateFrom': '2026-10-01', 'dateTo': '2026-10-06',
        'completedAt': (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(),
        'taxRevision': 'synthetic', 'pnlCurrentCostRevision': 'synthetic-cost',
        'report': {'cacheVersion': reports.PNL_REPORT_PAYLOAD_VERSION, 'cashFlow': {'status': 'disabled'}}}
    assert reports._report_payload_cache_is_usable('pnl', cache, organization_id=1, allow_stale=True)
    assert not reports._report_payload_cache_is_usable('pnl', cache, organization_id=1)


def test_provisional_snapshot_is_never_a_completed_source_receipt(monkeypatch):
    monkeypatch.setattr('app.wb_pnl_advertising._source_context', lambda org: None)
    monkeypatch.setattr('app.wb_pnl_costs.current_cost_revision', lambda org: 'synthetic-cost')
    monkeypatch.setattr(reports, 'legacy_finance_tax_revision', lambda org: 'synthetic')
    monkeypatch.setattr(reports, 'get_settings', lambda: SimpleNamespace(one_c_enabled=False))
    cache = {'taxRevision': 'synthetic', 'pnlCurrentCostRevision': 'synthetic-cost',
        'completedAt': datetime.now(timezone.utc).isoformat(),
        'report': {'loadingSnapshot': True, 'cacheVersion': reports.PNL_REPORT_PAYLOAD_VERSION, 'cashFlow': {'status': 'disabled'}}}
    assert reports._report_payload_cache_is_usable('pnl', cache, organization_id=1, allow_stale=True)
    assert not reports._report_payload_cache_is_usable('pnl', cache, organization_id=1)
    assert reports._report_payload_cache_meta(cache, {'from': str(START), 'to': str(END)})['fresh'] is False


def test_concurrent_report_starts_dispatch_once_and_keep_the_same_task(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, Lock
    from app.infra import redis_client
    actor = SimpleNamespace(organization_id=1, user_id='synthetic')
    state, locks, sent = {}, {}, []
    entered, release = Event(), Event()
    class Gate:
        def __init__(self, key):
            self.lock = locks.setdefault(key, Lock())
        def acquire(self, blocking=True, blocking_timeout=1):
            return self.lock.acquire(timeout=blocking_timeout)
        def release(self):
            self.lock.release()
    monkeypatch.setattr(redis_client, 'get_redis_client', lambda: SimpleNamespace(lock=lambda key, **kw: Gate(key)))
    monkeypatch.setattr(reports, 'actor_from_request', lambda request: actor)
    monkeypatch.setattr(reports, 'has_permission', lambda *a: True)
    monkeypatch.setattr(reports, 'assert_permission_or_audit', lambda **kw: None)
    monkeypatch.setattr(reports, 'touch_demand', lambda *a, **kw: None)
    monkeypatch.setattr(reports, '_report_cache_key', lambda *a, **kw: 'synthetic-report')
    monkeypatch.setattr(reports, 'get_source_cache', lambda org, key, **kw: state.get(key, {}))
    monkeypatch.setattr(reports, 'save_source_cache', lambda org, key, value: state.__setitem__(key, value))
    monkeypatch.setattr(reports, '_report_daily_sources_ready', lambda *a, **kw: (False, ['ads']))
    monkeypatch.setattr('app.wb_catalog_loading.missing_catalog_sources', lambda org: ())
    monkeypatch.setattr(reports, 'get_cash_flow_for_period', lambda **kw: {'status': 'disabled'})
    def dispatch(**kw):
        sent.append(kw)
        entered.set()
        assert release.wait(2)
    monkeypatch.setattr(tasks.refresh_report_sources_for_org, 'apply_async', dispatch)
    args = (object(), 'pnl', 'custom', str(START), str(END), 'sku', 'operational')
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(reports.start_report_job, *args)
        assert entered.wait(2)
        second = executor.submit(reports.start_report_job, *args)
        release.set()
        a, b = first.result(), second.result()
    assert len(sent) == 1
    assert a['taskId'] == b['taskId'] and b['reused'] is True


def test_fast_builder_completion_is_not_overwritten_by_http_handler(monkeypatch):
    from contextlib import nullcontext
    actor = SimpleNamespace(organization_id=1, user_id='synthetic')
    state = {}
    monkeypatch.setattr(reports, '_report_start_guard', lambda *a: nullcontext())
    monkeypatch.setattr(reports, 'actor_from_request', lambda request: actor)
    monkeypatch.setattr(reports, 'has_permission', lambda *a: True)
    monkeypatch.setattr(reports, 'assert_permission_or_audit', lambda **kw: None)
    monkeypatch.setattr(reports, 'touch_demand', lambda *a, **kw: None)
    monkeypatch.setattr(reports, '_report_cache_key', lambda *a, **kw: 'synthetic-report')
    monkeypatch.setattr(reports, 'get_source_cache', lambda org, key, **kw: state.get(key, {}))
    monkeypatch.setattr(reports, 'save_source_cache', lambda org, key, value: state.__setitem__(key, value))
    monkeypatch.setattr(reports, '_report_daily_sources_ready', lambda *a, **kw: (True, []))
    monkeypatch.setattr('app.wb_catalog_loading.missing_catalog_sources', lambda org: ())
    monkeypatch.setattr(reports, 'get_cash_flow_for_period', lambda **kw: {'status': 'disabled'})
    key = reports._report_job_cache_key('pnl', START, END, 'sku', 'operational', finance_allowed=True)
    def dispatch(**kw):
        assert state[key]['taskId'] == kw['task_id']
        state[key] = {**state[key], 'state': 'completed', 'stage': 'completed'}
    monkeypatch.setattr(tasks.build_report_for_org, 'apply_async', dispatch)
    reports.start_report_job(object(), 'pnl', 'custom', str(START), str(END), 'sku', 'operational')
    assert state[key]['state'] == 'completed'
