"""Provider-free P&L fee attribution, reporting dates and cache regressions."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone

import pytest
from celery.exceptions import Retry
from fastapi import HTTPException

from app import wb_pnl_expenses as fees
from app.repricer_bff import FINANCE_REVENUE_BASIS, FINANCE_SCHEMA_VERSION
from app.repricer_page_finance import VERSION

START, END = date(2026, 10, 1), date(2026, 10, 8)


def operation(identity, nm=101, **changes):
    return dict(rrdId=identity, reportId=77, nmId=nm, quantity=0,
                vendorCode="same-article", rrDate=str(START), saleDt="2026-09-29", currency="RUB",
                paidAcceptance="0", penalty="0", ppvzSalesCommission="0", deduction="0", docTypeName="", forPay="0") | changes


def project(rows, complete=True):
    return {**fees.project_pnl_operations(rows, START, END),
            "sourceComplete": complete, "dateFrom": str(START), "dateTo": str(END),
            "missingDates": [] if complete else [str(END)],
            "revenueBasis": FINANCE_REVENUE_BASIS, "financeSchemaVersion": FINANCE_SCHEMA_VERSION,
            "fetchedAt": datetime.now(timezone.utc).isoformat()}


def test_signed_fees_match_nm_not_article_and_reconcile_unallocated():
    rows = [operation(1, paidAcceptance="12.30", penalty="10"),
            operation(2, 102, paidAcceptance="4", penalty="7"),
            operation(3, paidAcceptance="-2.30", penalty="-13"),
            operation(4, 0, paidAcceptance="5", penalty="9")]
    cache = project(rows + [deepcopy(rows[0])])
    assert fees.product_expenses(cache, 101) == dict(penaltyKopecks=-300, acceptanceKopecks=1000, state="confirmed")
    assert fees.product_expenses(cache, 102)["penaltyKopecks"] == 700
    assert fees.product_expenses(cache, None)["acceptanceKopecks"] == 500
    for field in fees.FIELDS:
        assert sum(fees.product_expenses(cache, nm)[field] for nm in (101, 102, None)) == cache["diagnostics"]["rawExpenseTotals"][field]
        assert cache["aggregates"]["101"][field] == fees.product_expenses(cache, 101)[field]


def test_posting_dates_are_inclusive_and_not_sale_dates():
    rows = [operation(1, penalty="1", paidAcceptance="2"),
            operation(2, rrDate=str(END), penalty="3", paidAcceptance="4"),
            operation(3, rrDate="2026-09-30", saleDt=str(START), penalty="100", paidAcceptance="100"),
            operation(4, rrDate="2026-10-09", saleDt=str(END), penalty="100", paidAcceptance="100")]
    assert fees.product_expenses(project(rows), 101) == dict(penaltyKopecks=400, acceptanceKopecks=600, state="confirmed")


def test_conflicting_record_is_not_silently_deduplicated():
    with pytest.raises(ValueError, match="CONFLICTING_ROWS"):
        project([operation(1, penalty="2"), operation(1, penalty="3")])


def test_zero_requires_complete_ledger_and_present_fields():
    assert fees.product_expenses(project([operation(1)]), 101)["penaltyKopecks"] == 0
    assert fees.product_expenses(project([]), 101)["penaltyKopecks"] == 0
    assert fees.product_expenses(project([operation(1)], complete=False), 101)["penaltyKopecks"] is None
    missing = operation(1)
    missing.pop("paidAcceptance")
    result = fees.product_expenses(project([missing]), 101)
    assert result == dict(penaltyKopecks=0, acceptanceKopecks=None, state="partial")
    assert fees.product_expenses({}, 101)["penaltyKopecks"] is None
    undated = operation(1, rrDate=None, penalty="2")
    assert fees.product_expenses(project([undated]), 101)["penaltyKopecks"] is None


def test_partial_nonzero_is_observed_but_not_confirmed():
    assert fees.product_expenses(project([operation(1, penalty="2")], complete=False), 101) == dict(
        penaltyKopecks=200, acceptanceKopecks=None, state="partial")


def test_stale_values_remain_visible_but_are_not_presented_as_fresh():
    cache = project([operation(1, penalty="2")])
    cache["fetchedAt"] = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    assert fees.product_expenses(cache, 101) == dict(
        penaltyKopecks=200, acceptanceKopecks=0, state="stale")
    cache["pnlExpenseObservedAt"] = [datetime.now(timezone.utc).isoformat(), cache["fetchedAt"]]
    cache["fetchedAt"] = datetime.now(timezone.utc).isoformat()
    assert fees.product_expenses(cache, 101)["state"] == "stale"


def test_pnl_loader_keeps_undated_fees_and_moscow_boundaries_without_changing_default(monkeypatch):
    from app import repricer_page_finance as page
    monkeypatch.setattr("app.cabinet.store.get_organization_wb_token_secret", lambda org: "synthetic")
    monkeypatch.setattr("app.repricer_cache.store.list_source_cache_ranges_by_prefix", lambda *a, **kw: [
        dict(sourceKey="new", dateFrom=str(START), dateTo=str(END), fetchedAt="2026-10-09T00:00:00Z")])
    rows = [operation(1, rrDate=None, penalty="3"),
            operation(2, rrDate="2026-09-30T21:00:00Z", paidAcceptance="4")]
    monkeypatch.setattr("app.repricer_cache.store.get_source_cache", lambda *a, **kw: {
        "sourceComplete": True, "operations": rows, "fetchedAt": datetime.now(timezone.utc).isoformat()})
    cache = page.load_page_finance(1, START, END, projector=fees.project_pnl_operations, include_undated=True)
    assert fees.product_expenses(cache, 101) == dict(penaltyKopecks=None, acceptanceKopecks=400, state="partial")
    selected = []
    def capture(rows, start, end):
        selected.extend(rows)
        return {"aggregates": {}}
    page.load_page_finance(1, START, END, projector=capture)
    assert selected == []  # opt-in cannot change repricer/ABC date selection


def test_projection_cache_reuses_db_and_invalidates_on_revision_and_connection(monkeypatch):
    fees._cache.clear()
    token = ["synthetic-one"]
    metas = [{"sourceKey": "raw", "fetchedAt": "one"}]
    calls = []
    monkeypatch.setattr("app.cabinet.store.get_organization_wb_token_secret", lambda org: token[0])
    monkeypatch.setattr("app.repricer_cache.store.list_source_cache_ranges_by_prefix", lambda *a, **kw: metas)
    def load(org, start, end, **kw):
        calls.append((org, start, end))
        assert kw["projector"] is fees.project_pnl_operations
        return project([operation(1, penalty="2")])
    monkeypatch.setattr("app.repricer_page_finance.load_page_finance", load)
    first = fees.load_pnl_finance(1, START, END)
    first["pnlExpenseAmounts"]["101"]["penaltyKopecks"] = 999
    assert fees.product_expenses(fees.load_pnl_finance(1, START, END), 101)["penaltyKopecks"] == 200
    assert len(calls) == 1
    metas[0]["fetchedAt"] = "two"
    fees.load_pnl_finance(1, START, END)
    token[0] = "synthetic-two"
    fees.load_pnl_finance(1, START, END)
    fees.load_pnl_finance(2, START, END)
    assert len(calls) == 4
    fees._cache.clear()


def test_pnl_builder_does_not_deduct_visible_expenses_twice(monkeypatch):
    from app import wb_reports_sprint_d as service
    from app import wb_pnl_advertising as advertising
    monkeypatch.setattr(advertising, "load_campaign_spend", lambda *a: ({}, dict(
        state="ok", aggregates={}, adSpendKopecks=0, unallocatedSpendKopecks=0,
        productCoverageComplete=True)))
    cache = project([operation(1, paidAcceptance="12", penalty="10"),
                     operation(2, 0, paidAcceptance="3", penalty="2")])
    monkeypatch.setattr(fees, "load_pnl_finance", lambda *a: cache)
    monkeypatch.setattr(service, "_period_cache", lambda org, source, *a, **kw: dict(
        source="wb_ads_fullstats", dateFrom=str(START), dateTo=str(END),
        fetchedAt=datetime.now(timezone.utc).isoformat(), aggregates={}, sourceComplete=True) if source == "ads" else {})
    monkeypatch.setattr(service, "get_legacy_finance_taxes", lambda *a, **kw: {"101": {
        "settlementCogsKopecks": 0, "settlementCogsState": "configured", "taxKopecks": 0, "factTaxState": "configured"}})
    monkeypatch.setattr(service, "get_source_cache", lambda *a, **kw: {})
    monkeypatch.setattr(service, "_goods_index", lambda *a, **kw: {})
    monkeypatch.setattr(service, "load_runtime_state", lambda *a: {})
    monkeypatch.setattr(service, "load_algorithm_settings", lambda *a: {})
    report = service._build_cached_pnl_report(organization_id=1, date_from=START, date_to=END,
        group_by="sku", requested_state="preliminary", finance_allowed=True)
    assert report is not None
    by_id = {row.rowId: row for row in report.rows}
    product = next(row for row in report.rows if row.nmId == 101)
    assert (product.penaltyKopecks, product.acceptanceKopecks) == (1000, 1200)
    assert product.netProfitKopecks == -2200
    assert by_id["account-unallocated"].netProfitKopecks == -500
    assert report.totals.netProfitKopecks == -2700


def test_pnl_worker_reuses_cached_sources_and_resumes_rate_limits(monkeypatch):
    from app import repricer_tasks as tasks, wb_abc_loading as loading, repricer_bff as bff
    from app.routers import wb_reports_bff as reports
    from app.wb_api import report_reads
    saved, built = {}, []
    monkeypatch.setattr("app.wb_catalog_loading.missing_catalog_sources", lambda org: ())
    monkeypatch.setattr(tasks, "_report_refresh_wb_token", lambda *a: "synthetic")
    monkeypatch.setattr(report_reads, "install_report_read", lambda *a: report_reads.active_report_read.set(None))
    monkeypatch.setattr(reports, "save_source_cache", lambda org, key, value: saved.__setitem__(key, value))
    monkeypatch.setattr(reports, "get_source_cache", lambda org, key, **kw: saved.get(key))
    monkeypatch.setattr(tasks.refresh_report_sources_for_org, "replace", lambda signature: built.append(signature) or {"state": "building"})
    monkeypatch.setattr(loading, "missing_source_ranges", lambda *a, **kw: [])
    monkeypatch.setattr("app.wb_pnl_advertising.missing_campaign_ranges", lambda *a, **kw: [])
    monkeypatch.setattr(bff, "fetch_finance_report_aggregates", lambda *a, **kw: pytest.fail("unexpected provider reload"))
    args = (2, "synthetic", "pnl", str(START), str(END), "sku", "financial", True, None)
    assert tasks.refresh_report_sources_for_org.run(*args) == {"state": "building"}
    assert len(built) == 1
    monkeypatch.setattr(loading, "missing_source_ranges", lambda org, source, *a, **kw: [(END, END)] if source == "finance" else [])
    def limited(*a, **kw):
        assert kw["date_from"].date() == kw["date_to"].date() == END
        kw["page_collected_callback"](2, 20)
        raise HTTPException(429, "synthetic", headers={"X-RateLimit-Retry": "125"})
    monkeypatch.setattr(bff, "fetch_finance_report_aggregates", limited)
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
    assert retries[0]["countdown"] == 126
    assert retries[0]["max_retries"] == 240
    assert retries[0]["kwargs"] == {"refresh_missing_only": True}
    assert next(reversed(saved.values()))["rowsCount"] == 20


def test_pnl_and_abc_both_reject_legacy_aggregate_only_coverage(monkeypatch):
    from app import wb_abc_loading as loading
    from app.routers import wb_reports_bff as reports
    monkeypatch.setattr("app.cabinet.store.get_organization_wb_token_secret", lambda org: "synthetic")
    monkeypatch.setattr(reports, "list_source_cache_ranges_by_prefix", lambda org, prefix, **kw: [
        dict(sourceKey="aggregate", dateFrom=str(START), dateTo=str(END), fetchedAt=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat())
    ] if prefix == "finance_" else [])
    monkeypatch.setattr(reports, "get_source_cache", lambda *a, **kw: {"sourceComplete": True})
    monkeypatch.setattr("app.repricer_cache.store.get_source_cache_completion_flags", lambda *a: {"sourceComplete": True})
    monkeypatch.setattr(reports, "finance_cache_uses_current_revenue_basis", lambda meta: True)
    assert loading.missing_source_ranges(1, "finance", START, END, fresh_only=True) == [(START, END)]
    assert loading.missing_source_ranges(1, "finance", START, END, fresh_only=True, raw_required=True) == [(START, END)]
