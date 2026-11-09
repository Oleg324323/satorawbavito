"""Provider-free Week financial attribution, source reuse and export regressions."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone

import pytest
from celery.exceptions import Retry
from fastapi import HTTPException

from app import wb_week_finance as week

START, END = date(2026, 10, 1), date(2026, 10, 8)
PRIOR_START, PRIOR_END = date(2026, 9, 23), date(2026, 9, 30)


def operation(identity, nm=101, **changes):
    return dict(rrdId=identity, nmId=nm, vendorCode="same-article", rrDate=str(START),
        currency="RUB", quantity=0, retailAmount="0", retailPriceWithDisc="0", acquiringFee="0",
        saleDt="2026-09-20", docTypeName="", sellerOperName="", bonusTypeName="",
        ppvzSalesCommission="0", deliveryService="0", paidStorage="0", penalty="0",
        paidAcceptance="0", deduction="0", forPay="0", additionalPayment="0") | changes


def projected(rows, complete=True, start=START, end=END):
    return {**week.project_operations(rows, start, end), "sourceComplete": complete,
            "dateFrom": str(start), "dateTo": str(end), "fetchedAt": datetime.now(timezone.utc).isoformat()}


def test_direct_nm_matching_signed_corrections_duplicates_and_unallocated_reconcile():
    rows = [operation(1, docTypeName="Продажа", sellerOperName="Продажа", quantity=1, retailAmount="10", retailPriceWithDisc="20", acquiringFee="0", ppvzSalesCommission="10", deliveryService="3.25", paidStorage="1", penalty="4", paidAcceptance="2"),
        operation(2, 102, docTypeName="Продажа", sellerOperName="Продажа", quantity=1, retailAmount="9", retailPriceWithDisc="18", acquiringFee="0", ppvzSalesCommission="9", deliveryService="8", paidStorage="2"),
        operation(3, docTypeName="Возврат", sellerOperName="Возврат", quantity=1, retailAmount="3", retailPriceWithDisc="6", acquiringFee="0", ppvzSalesCommission="3", penalty="-6"),
        operation(4, 0, paidStorage="50", paidAcceptance="20", penalty="11"),
        operation(5, bonusTypeName="Услуги WB Продвижение", deduction="12"),
        operation(6, sellerOperName="Компенсация за утерянный товар", forPay="30"),
        operation(7, sellerOperName="Коррекция продаж", forPay="200", additionalPayment="200")]
    cache = projected(rows + [deepcopy(rows[0])])
    values = week.product_values(cache, 101, {"taxKopecks": 123})
    assert values == dict(commissionKopecks=700, logisticsKopecks=325, storageKopecks=100,
        adSpendKopecks=1200, acceptanceKopecks=200, penaltyKopecks=-200, compensationKopecks=3000, taxKopecks=123)
    assert week.product_values(cache, 102, {})["commissionKopecks"] == 900
    assert week.product_values(cache, "0", {})["storageKopecks"] == 5000
    assert week.product_values(cache, 102, {})["penaltyKopecks"] is None
    for field in week.FIELDS:
        if field != "taxKopecks":
            assert sum(cache["amounts"][str(nm)][field] for nm in (101, 102, "0")) == sum(bucket[field] for bucket in cache["amounts"].values())
    assert rows[0]["ppvzSalesCommission"] == "10"


def test_posting_dates_inclusive_moscow_not_sale_dates_and_conflicting_ids():
    rows = [operation(1, penalty="1"), operation(2, rrDate=str(END), penalty="2"),
            operation(3, rrDate="2026-09-30T21:00:00Z", penalty="3"),
            operation(4, rrDate="2026-09-30", saleDt=str(START), penalty="100"),
            operation(5, rrDate="2026-10-09", saleDt=str(END), penalty="100")]
    assert week.product_values(projected(rows), 101, {})["penaltyKopecks"] == 600
    with pytest.raises(ValueError, match="CONFLICTING_ROWS"):
        projected([operation(1, penalty="2"), operation(1, penalty="3")])


def test_missing_zero_partial_undated_fields_and_no_tax_configuration():
    assert week.product_values(projected([]), 101, {})["storageKopecks"] == 0
    assert week.product_values({}, 101, {})["storageKopecks"] is None
    assert week.product_values(projected([operation(1)], complete=False), 101, {"taxKopecks": 99})["taxKopecks"] is None
    missing = operation(1)
    missing.pop("paidStorage")
    values = week.product_values(projected([missing]), 101, {})
    assert values["storageKopecks"] is None and values["penaltyKopecks"] == 0 and values["taxKopecks"] is None
    assert week.product_values(projected([operation(1, rrDate=None, penalty="1")]), 101, {})["penaltyKopecks"] is None
    assert week.product_values(projected([operation(1, penalty="1")], complete=False), 101, {})["penaltyKopecks"] == 100
    assert week.product_values(projected([]), 101, {"taxKopecks": 0})["taxKopecks"] == 0


def test_compensation_is_credit_not_sales_and_not_every_additional_payment():
    rows = [operation(1, sellerOperName="Компенсация товара", forPay="10", additionalPayment="10"),
            operation(2, sellerOperName="Компенсация товара", docTypeName="Возврат", forPay="3"),
            operation(3, sellerOperName="Продажа", forPay="100"),
            operation(4, sellerOperName="Другие доплаты", additionalPayment="50")]
    assert week.product_values(projected(rows), 101, {})["compensationKopecks"] == 700
    ambiguous = operation(1, sellerOperName="Компенсация товара", forPay="10", additionalPayment="20")
    assert week.product_values(projected([ambiguous]), 101, {})["compensationKopecks"] is None


def test_compensation_corrections_document_sign_currency_and_unclassified_deduction():
    cache = projected([operation(1, sellerOperName="Коррекция компенсации товара", forPay="-2"),
        operation(2, sellerOperName="Компенсация товара", docTypeName="Возврат", forPay="-1", ppvzSalesCommission="-3")])
    assert week.product_values(cache, 101, {})["compensationKopecks"] == -100
    assert week.product_values(cache, 101, {})["commissionKopecks"] is None  # incompatible adjustment definition
    assert week.product_values(projected([operation(3, currency="BYN", penalty="1")]), 101, {})["penaltyKopecks"] is None
    assert week.product_values(projected([operation(4, deduction="20")]), 101, {})["adSpendKopecks"] is None


def test_both_periods_and_financial_only_products_preserve_profit_and_tax(monkeypatch):
    current = projected([operation(1, penalty="10"), operation(2, 102, paidStorage="2"), operation(3, 0, penalty="5")])
    previous = projected([operation(4, penalty="4", rrDate=str(PRIOR_END))], start=PRIOR_START, end=PRIOR_END)
    calls = []
    def load(org, start, end):
        calls.append((org, start, end))
        return current if start == START else previous
    monkeypatch.setattr(week, "load_finance", load)
    profit = {"kopecks": 999, "deltaPct": 25}
    report = {"rows": [{"nmId": 101, "profit": deepcopy(profit)}]}
    result = week.attach(report, 7, START, END, PRIOR_START, PRIOR_END, finance_allowed=True,
        current_pnl={101: {"taxKopecks": 100}}, previous_pnl={101: {"taxKopecks": 50}},
        catalog={102: {"productName": "Financial-only product", "sku": "A102"}})
    assert calls == [(7, START, END), (7, PRIOR_START, PRIOR_END)]
    assert result["rows"][0]["penaltyKopecks"] == {"kopecks": 1000, "previousKopecks": 400, "deltaPct": 150.0}
    assert result["rows"][0]["taxKopecks"] == {"kopecks": 100, "previousKopecks": 50, "deltaPct": 100.0}
    assert result["rows"][0]["profit"] == profit
    assert result["rows"][1]["productName"] == "Financial-only product"
    assert result["unallocatedFinancials"]["selected"]["penaltyKopecks"] == 500
    assert "не распределены" in result["warning"]


def test_no_finance_permission_never_loads_raw_data(monkeypatch):
    monkeypatch.setattr(week, "load_finance", lambda *a: pytest.fail("financial read without permission"))
    result = week.attach({"rows": [{"nmId": 101}]}, 7, START, END, PRIOR_START, PRIOR_END, finance_allowed=False)
    assert all(result["rows"][0][field]["kopecks"] is None for field in week.FIELDS)


def test_unallocated_compensation_does_not_block_direct_columns_or_invent_tax(monkeypatch):
    from app.routers import wb_reports_bff as reports
    def blocked(**kwargs):
        raise ValueError("WB_FINANCE_UNALLOCATED_PAYABLE")
    monkeypatch.setattr(reports, "build_pnl_report", blocked)
    assert reports._week_pnl_values(7, START, END) == {}
    assert week.product_values(projected([]), 101, {})["taxKopecks"] is None
    def unexpected(**kwargs):
        raise ValueError("unrelated")
    monkeypatch.setattr(reports, "build_pnl_report", unexpected)
    with pytest.raises(ValueError, match="unrelated"):
        reports._week_pnl_values(7, START, END)


def test_db_projection_cache_is_revision_org_connection_and_period_scoped(monkeypatch):
    week._cache.clear()
    token, revision, calls = ["synthetic-one"], ["one"], []
    monkeypatch.setattr("app.cabinet.store.get_organization_wb_token_secret", lambda org: token[0])
    monkeypatch.setattr("app.repricer_cache.store.list_source_cache_ranges_by_prefix", lambda *a, **kw: [{"sourceKey": "raw", "fetchedAt": revision[0]}])
    def load(org, start, end, **kw):
        calls.append((org, start, end))
        assert kw == {"projector": week.project_operations, "include_undated": True}
        return projected([operation(1, penalty="1")])
    monkeypatch.setattr("app.repricer_page_finance.load_page_finance", load)
    week.load_finance(7, START, END)["amounts"]["101"]["penaltyKopecks"] = 999
    assert week.load_finance(7, START, END)["amounts"]["101"]["penaltyKopecks"] == 100
    assert len(calls) == 1
    revision[0] = "two"
    week.load_finance(7, START, END)
    week.load_finance(7, PRIOR_START, PRIOR_END)
    token[0] = "synthetic-two"
    week.load_finance(7, START, END)
    week.load_finance(8, START, END)
    assert len(calls) == 5
    week._cache.clear()


def test_partial_or_stale_week_cache_starts_existing_collection_not_false_ready():
    from app.routers import wb_reports_bff as reports
    observed = datetime.now(timezone.utc).isoformat()
    cache = {"completedAt": observed, "fetchedAt": observed, "report": {"financialVersion": week.VERSION,
        "financialCoverage": {label: {"complete": True, "fetchedAt": observed, "dateTo": str(END)} for label in ("selected", "previous")}}}
    assert reports._report_payload_cache_meta(cache, {})["fresh"] is True
    cache["report"]["financialCoverage"]["previous"]["complete"] = False
    assert reports._report_payload_cache_meta(cache, {})["fresh"] is False
    cache["report"]["financialCoverage"]["previous"].update(complete=True, fetchedAt=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat())
    assert reports._report_payload_cache_meta(cache, {})["fresh"] is False


def test_worker_reuses_db_then_collects_only_missing_finance_and_keeps_retry_header(monkeypatch):
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
    monkeypatch.setattr("app.wb_week_sources.load_funnel", lambda *a: {"complete": True, "needsCollection": False})
    monkeypatch.setattr(bff, "fetch_finance_report_aggregates", lambda *a, **kw: pytest.fail("unexpected provider reload"))
    args = (7, "synthetic", "week-over-week", str(START), str(END), "sku", "operational", True, None)
    assert tasks.refresh_report_sources_for_org.run(*args, refresh_missing_only=True) == {"state": "building"}
    assert len(built) == 1
    def missing(org, source, *a, **kw):
        assert kw["raw_required"] is True
        return [(END, END)] if source == "finance" else []
    monkeypatch.setattr(loading, "missing_source_ranges", missing)
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
            task.run(*args, refresh_missing_only=True)
    finally:
        task.pop_request()
    assert retries[0]["countdown"] == 126 and retries[0]["kwargs"] == {"refresh_missing_only": True}


def test_week_readiness_requires_raw_but_other_report_contracts_unchanged(monkeypatch):
    from app.routers import wb_reports_bff as reports
    from app import wb_abc_loading as loading
    calls = []
    monkeypatch.setattr(loading, "missing_source_ranges", lambda *a, **kw: calls.append(kw) or [])
    assert reports._report_daily_sources_ready(7, ("finance",), date_from=START, date_to=END, report_id="week-over-week")[0]
    assert calls[-1]["raw_required"] is True
    reports._report_daily_sources_ready(7, ("finance",), date_from=START, date_to=END, report_id="abc")
    assert "raw_required" not in calls[-1]


def test_corrected_raw_ledger_invalidates_fresh_week_report_without_api(monkeypatch):
    from app.routers import wb_reports_bff as reports
    monkeypatch.setattr(reports, "legacy_finance_tax_revision", lambda org: "same-tax")
    monkeypatch.setattr(week, "source_revision", lambda org: "corrected-ledger")
    cache = {"completedAt": datetime.now(timezone.utc).isoformat(), "taxRevision": "same-tax",
        "report": {"cacheVersion": reports.WEEK_OVER_WEEK_REPORT_PAYLOAD_VERSION,
            "financialVersion": week.VERSION, "financialAccess": True,
            "financialSourceRevision": "old-ledger", "rows": [{"penaltyKopecks": {"kopecks": 123}}]}}
    assert not reports._report_payload_cache_is_usable("week-over-week", cache, organization_id=7)
    cache["report"]["financialSourceRevision"] = "corrected-ledger"
    assert reports._report_payload_cache_is_usable("week-over-week", cache, organization_id=7)
