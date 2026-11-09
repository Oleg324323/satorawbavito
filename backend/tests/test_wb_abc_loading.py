"""ABC reuses exact saved intervals and resumes WB limits without force reloads."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from celery.exceptions import Retry
from fastapi import HTTPException

from app import repricer_bff as bff, repricer_tasks as tasks
from app import wb_abc_loading as loading
from app.routers import wb_reports_bff as reports

START, END = date(2026, 10, 1), date(2026, 10, 8)


@pytest.fixture
def saved_sources(monkeypatch):
    from app.cabinet import store
    from app.repricer_page_finance import connection_key
    monkeypatch.setattr(store, "get_organization_wb_token_secret", lambda org: "synthetic-current-connection")
    prefix = f"repricer_finance_{connection_key('synthetic-current-connection')}_"
    state = []
    monkeypatch.setattr(reports, "list_source_cache_ranges_by_prefix",
        lambda org, key, **kw: [row for row in state if row['sourceKey'].startswith(key)])
    monkeypatch.setattr('app.repricer_cache.store.list_source_cache_ranges_by_prefix', reports.list_source_cache_ranges_by_prefix)
    monkeypatch.setattr(reports, "get_source_cache",
        lambda org, key, **kw: next((row for row in state if row['sourceKey'] == key), {}))
    monkeypatch.setattr('app.repricer_cache.store.get_source_cache', reports.get_source_cache)
    monkeypatch.setattr('app.repricer_cache.store.get_source_cache_completion_flags',
        lambda org, key: next((row for row in state if row['sourceKey'] == key), {}))

    def add(start, end, *, complete=True, age=2, owner_prefix=prefix):
        from app.wb_api.financial_reports import DETAIL_FIELDS, SOURCE_VERSION
        row = dict(sourceKey=f"{owner_prefix}{start}_{end}", dateFrom=str(start), dateTo=str(end),
            sourceComplete=complete, fetchedAt=(datetime.now(timezone.utc) - timedelta(hours=age)).isoformat(),
            requestedFields=list(DETAIL_FIELDS), operationCount=0, sourceVersion=SOURCE_VERSION,
            reportPeriod="daily", currencies=[])
        state.append(row)
        return row
    return add


def test_abc_does_not_request_comparison_finance():
    assert reports._report_source_bounds("abc", START, END) == (START, END)
    assert reports._report_source_bounds("week-over-week", START, END) == (date(2026, 9, 23), END)


def test_closed_period_reuses_completed_repricer_cursor_and_empty_days(saved_sources):
    saved_sources(date(2026, 9, 29), date(2026, 10, 9))
    assert loading.missing_source_ranges(2, "finance", START, END, fresh_only=True) == []


def test_covered_period_does_not_decode_older_overlapping_ledgers(saved_sources, monkeypatch):
    saved_sources(START, END)
    saved_sources(date(2026, 9, 29), date(2026, 10, 9))
    from app.repricer_cache import store
    original = store.get_source_cache_completion_flags
    calls = []
    def read(org, key, **kw):
        calls.append(key)
        return original(org, key, **kw)
    monkeypatch.setattr(store, "get_source_cache_completion_flags", read)
    monkeypatch.setattr(reports, "get_source_cache", lambda *a, **kw: pytest.fail("Coverage must not hydrate full ledgers"))
    assert loading.missing_source_ranges(2, "finance", START, END, fresh_only=True) == []
    assert len(calls) == 1


def test_finance_only_requests_missing_days_not_comparison_or_saved_days(saved_sources):
    saved_sources(START, date(2026, 10, 6))
    saved_sources(date(2026, 10, 8), END)
    assert loading.missing_source_ranges(2, "finance", START, END, fresh_only=True) == [(date(2026, 10, 7), date(2026, 10, 7))]


def test_incomplete_or_other_connection_is_not_coverage(saved_sources):
    saved_sources(START, END, complete=False)
    saved_sources(START, END, owner_prefix="repricer_finance_other-connection_")
    assert loading.missing_source_ranges(2, "finance", START, END) == [(START, END)]


def test_stale_finance_remains_readable_but_is_due_for_revalidation(saved_sources):
    saved_sources(START, END, age=25)
    assert loading.missing_source_ranges(2, "finance", START, END) == []
    assert loading.missing_source_ranges(2, "finance", START, END, fresh_only=True) == [(START, END)]


@pytest.fixture
def abc_task(monkeypatch):
    monkeypatch.setattr(tasks, '_publish_loading_report_snapshot', lambda *a, **kw: None)
    monkeypatch.setattr('app.wb_catalog_loading.missing_catalog_sources', lambda org: ())
    from app.wb_api import report_reads
    saved, calls = {}, []
    monkeypatch.setattr(tasks, "_report_refresh_wb_token", lambda *a: "synthetic-current-connection")
    monkeypatch.setattr(report_reads, "install_report_read", lambda *a: report_reads.active_report_read.set(None))
    monkeypatch.setattr(reports, "get_source_cache", lambda org, key, **kw: saved.get(key, {}))
    monkeypatch.setattr(reports, "save_source_cache", lambda org, key, value: saved.__setitem__(key, value))
    monkeypatch.setattr(tasks, "get_settings", lambda: SimpleNamespace(advertising_shadow_ingest_enabled=False))
    monkeypatch.setattr(tasks.refresh_report_sources_for_org, "replace", lambda signature: calls.append(signature) or {"state": "building"})
    monkeypatch.setattr(tasks, "refresh_wb_data_sources", lambda **kw: pytest.fail("Saved ABC sources must not be recollected"))
    args = (2, "synthetic-user", "abc", str(START), str(END), "sku", "operational", True, None)
    key = reports._report_job_cache_key("abc", START, END, "sku", "operational", finance_allowed=True)
    return args, key, saved, calls


def test_saved_abc_sources_go_directly_to_cache_only_builder(abc_task, monkeypatch):
    args, key, saved, calls = abc_task
    monkeypatch.setattr(loading, "missing_source_ranges", lambda *a, **kw: [])
    monkeypatch.setattr(bff, "fetch_finance_report_aggregates", lambda *a, **kw: pytest.fail("WB finance must not be requested"))
    assert tasks.refresh_report_sources_for_org.run(*args) == {"state": "building"}
    assert len(calls) == 1 and calls[0].task == "reports.build_report_for_org"
    assert calls[0].args[3:5] == (str(START), str(END))


@pytest.mark.parametrize("status", [401, 403, 500])
def test_non_limit_errors_are_not_retried_as_429(abc_task, monkeypatch, status):
    args, key, saved, calls = abc_task
    monkeypatch.setattr(loading, "missing_source_ranges", lambda org, source, *a, **kw: [(START, END)] if source == "finance" else [])
    def collect(*a, **kw):
        raise HTTPException(status, "synthetic provider failure")
    monkeypatch.setattr(bff, "fetch_finance_report_aggregates", collect)
    with pytest.raises(HTTPException):
        tasks.refresh_report_sources_for_org.run(*args)
    assert saved[key]["state"] == "failed" and not calls


def test_abc_finance_429_after_three_retries_still_resumes_and_honors_headers(abc_task, monkeypatch):
    args, key, saved, calls = abc_task
    missing = (date(2026, 10, 7), END)
    monkeypatch.setattr(loading, "missing_source_ranges", lambda org, source, *a, **kw: [missing] if source == "finance" else [])
    def collect(*a, **kw):
        assert (kw["date_from"].date(), kw["date_to"].date()) == missing
        assert kw["repricer_operations_org"] == 2
        kw["page_collected_callback"](2, 40)
        raise HTTPException(429, "synthetic throttle", headers={"X-RateLimit-Retry": "125"})
    monkeypatch.setattr(bff, "fetch_finance_report_aggregates", collect)
    retries = []
    def retry(**kw):
        retries.append(kw)
        raise Retry()
    task = tasks.refresh_report_sources_for_org
    monkeypatch.setattr(task, "retry", retry)
    task.push_request(retries=3, kwargs={})
    try:
        with pytest.raises(Retry):
            task.run(*args)
    finally:
        task.pop_request()
    assert saved[key]["state"] == "queued" and saved[key]["stage"] == "rate_limit"
    assert saved[key]["retryAfterSeconds"] == 126 and saved[key]["rowsCount"] == 40
    assert retries[0]["countdown"] == 126 and retries[0]["max_retries"] == 240
    assert retries[0]["kwargs"] == {"refresh_missing_only": True}
    assert not calls


def test_old_abc_refresh_action_uses_incremental_job_endpoint(monkeypatch):
    calls = []
    monkeypatch.setattr(reports, "start_report_job", lambda *args: calls.append(args) or {"state": "completed"})
    request = object()
    assert reports.start_report_source_refresh_job(request, "abc", "custom", str(START), str(END), "sku", "operational") == {"state": "completed"}
    assert calls == [(request, "abc", "custom", str(START), str(END), "sku", "operational")]


def test_abc_projection_slices_a_wider_saved_ledger_to_selected_trade_dates():
    from app.wb_report_finance import project_report_operations
    def sale(identity, day, rubles):
        return dict(rrdId=identity, nmId=101, sellerOperName="Продажа", docTypeName="Продажа",
            quantity=1, retailAmount=rubles, retailPriceWithDisc=rubles, forPay=rubles * .8,
            saleDt=day, rrDate=day)
    rows = [sale(1, "2026-09-30", 999), sale(2, "2026-10-01", 100),
            sale(3, "2026-10-08", 200), sale(4, "2026-10-09", 888)]
    result = project_report_operations(rows + [rows[1]], START, END)
    assert result["aggregates"]["101"]["salesUnits"] == 2
    assert result["aggregates"]["101"]["sellerRevenueKopecks"] == 30_000
    assert set(result["dailyAggregates"]) == {str(START), str(END)}
