"""Retry the finance cursor chain without losing pages or masking real errors."""
from datetime import date

import pytest
from celery.exceptions import Retry
from fastapi import HTTPException

from app import repricer_bff as bff, repricer_page_finance as page, repricer_tasks as tasks
from app.routers import wb_reports_bff as reports


@pytest.mark.parametrize("detail,headers,expected", [
    ({}, {"Retry-After": "120.5"}, 122),
    ({"details": {"rateLimit": "invalid"}}, {"X-Ratelimit-Retry": "nan"}, 61),
])
def test_retry_window_accepts_headers_and_rejects_malformed_numbers(detail, headers, expected):
    assert tasks._finance_rate_limit_delay(HTTPException(429, detail, headers=headers)) == expected


@pytest.fixture
def finance_job(monkeypatch):
    saved = {}
    monkeypatch.setattr(tasks, "_report_refresh_wb_token", lambda *a: "synthetic-credential")
    from app.wb_api import report_reads
    monkeypatch.setattr(report_reads, "install_report_read", lambda *a: report_reads.active_report_read.set(None))
    for module in (bff, reports):
        monkeypatch.setattr(module, "get_source_cache", lambda org, key, **kw: saved.get(key, {}))
        monkeypatch.setattr(module, "save_source_cache", lambda org, key, payload, **kw: saved.__setitem__(key, payload))
    monkeypatch.setattr(page, "missing_page_ranges", lambda *a, **kw: [(date(2026, 10, 7), date(2026, 10, 8))])
    task = tasks.refresh_report_sources_for_org
    def retry(**kwargs):
        retries.append(kwargs)
        raise Retry()
    retries = []
    monkeypatch.setattr(task, "retry", retry)
    args = (2, "synthetic", "stats", "2026-10-01", "2026-10-08", "sku",
            f"repricer-finance:{page.connection_key('synthetic-credential')}", True, None)
    key = reports._report_job_cache_key("stats", date(2026, 10, 1), date(2026, 10, 8), "sku", args[6], finance_allowed=True)
    return task, args, key, saved, retries


@pytest.mark.parametrize("retry_after,reset,expected", [(3, None, 61), (100, 180, 181), (None, None, 61)])
def test_finance_429_is_queued_instead_of_failed(finance_job, monkeypatch, retry_after, reset, expected):
    task, args, key, saved, retries = finance_job
    def collect(*a, **kw):
        kw["page_collected_callback"](1, 5557)
        raise HTTPException(429, {"details": {"rateLimit": {"retryAfterSeconds": retry_after, "resetAfterSeconds": reset}}})
    monkeypatch.setattr(bff, "fetch_finance_report_aggregates", collect)
    with pytest.raises(Retry):
        task.run(*args)
    assert saved[key]["state"] == "queued"
    assert saved[key]["stage"] == "rate_limit"
    assert saved[key]["rowsCount"] == 5557
    assert retries[0]["countdown"] == expected
    assert retries[0]["kwargs"] == {"refresh_missing_only": True}


def test_task_retry_resumes_saved_cursor_and_publishes_only_complete_source(finance_job, monkeypatch):
    task, args, key, saved, retries = finance_job
    cursors = []
    def request(client, req):
        cursors.append(req.jsonBody["rrdId"])
        if len(cursors) == 1:
            return [{"rrdId": 11, "nmId": 101, "sellerOperName": "Продажа", "docTypeName": "Продажа",
                     "quantity": 1, "currency": "RUB", "retailAmount": 100, "retailPriceWithDisc": 100, "forPay": 80,
                     "saleDt": "2026-10-07", "rrDate": "2026-10-07"}]
        if len(cursors) == 2:
            raise HTTPException(429, "synthetic rate limit")
        return []
    monkeypatch.setattr(bff, "build_wb_finance_client", lambda *a, **kw: object())
    monkeypatch.setattr(bff, "RateLimitedWbApiClient", lambda **kw: object())
    monkeypatch.setattr(bff, "_request_or_raise_finance_report", request)
    complete = []
    monkeypatch.setattr(page, "save_operations", lambda *a, **kw: complete.append(a))
    with pytest.raises(Retry):
        task.run(*args)
    assert not complete
    assert next(v for k,v in saved.items() if k.startswith("repricer_finance_progress"))["sourceComplete"] is False
    task.push_request(retries=1, kwargs={"refresh_missing_only": True})
    try:
        result = task.run(*args, refresh_missing_only=True)
    finally:
        task.pop_request()
    assert result["state"] == "completed"
    assert cursors == [0, 11, 11]
    assert len(complete) == 1 and len(complete[0][-1]) == 1


@pytest.mark.parametrize("progress,previous_count,retry_number,should_retry", [(True, 3, 8, True), (False, 2, 8, True), (False, 3, 3, False)])
def test_retry_budget_counts_stalls_not_successful_pages(finance_job, monkeypatch, progress, previous_count, retry_number, should_retry):
    task, args, key, saved, retries = finance_job
    saved[key] = {"pagesLoaded": 7, "rowsCount": 700, "rateLimitRetryCount": previous_count}
    def collect(*a, **kw):
        if progress:
            kw["page_collected_callback"](8, 800)
        raise HTTPException(429, "synthetic rate limit")
    monkeypatch.setattr(bff, "fetch_finance_report_aggregates", collect)
    task.push_request(retries=retry_number, kwargs={"refresh_missing_only": True})
    try:
        with pytest.raises(Retry if should_retry else tasks.ReportSourceRateLimited):
            task.run(*args, refresh_missing_only=True)
    finally:
        task.pop_request()
    assert saved[key]["state"] == ("queued" if should_retry else "failed")
    if should_retry:
        assert retries[0]["max_retries"] == retry_number + 1
        assert saved[key]["rateLimitRetryCount"] == (1 if progress else previous_count + 1)


@pytest.mark.parametrize("status", [401, 403, 502])
def test_other_provider_errors_are_not_retried(finance_job, monkeypatch, status):
    task, args, key, saved, retries = finance_job
    def collect(*a, **kw):
        raise HTTPException(status, "synthetic failure")
    monkeypatch.setattr(bff, "fetch_finance_report_aggregates", collect)
    with pytest.raises(HTTPException):
        task.run(*args)
    assert saved[key]["state"] == "failed"
    assert retries == []
