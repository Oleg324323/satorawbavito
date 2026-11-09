"""Completion regressions: synthetic ledger only, no marketplace credentials."""
from datetime import date

from app import repricer_page_finance as page, wb_reports_sprint_d as reports
from app import wb_pnl_expenses as fees
from app.routers import wb_reports_bff as router
from tests.test_repricer_page_finance import operation
from tests.test_finance_service import session  # noqa: F401 - disposable DB fixture

START, END = date(2026, 10, 5), date(2026, 10, 11)


def test_financial_readiness_does_not_inspect_finance_without_permission(monkeypatch):
    checked = []
    monkeypatch.setattr(router, "_report_missing_source_ranges", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("forbidden finance inspection")))
    monkeypatch.setattr("app.wb_abc_loading.missing_source_ranges", lambda org, source, *a, **kw: checked.append(source) or [])
    assert router._report_daily_sources_ready(2, ("finance", "ads"), date_from=START,
        date_to=END, report_id="abc", finance_allowed=False) == (True, [])
    assert checked == ["ads"]


def test_new_storage_history_invalidates_saved_report_without_finance_reload(monkeypatch):
    monkeypatch.setattr(router, "_report_payload_cache_is_fresh", lambda *a: True)
    monkeypatch.setattr(router, "list_source_cache_ranges_by_prefix", lambda org, prefix, **kw:
        [dict(dateFrom=str(START), fetchedAt="2026-10-20T02:00:00Z")] if prefix == "stock_history_" else [])
    cache = dict(dateFrom=str(START), dateTo=str(END), completedAt="2026-10-20T01:00:00Z",
                 report={"cacheVersion": router.PNL_REPORT_PAYLOAD_VERSION})
    assert router._report_payload_cache_is_usable("pnl", cache, organization_id=2) is False


def test_partial_realization_is_not_hidden_or_replaced_by_a_legacy_total(monkeypatch):
    partial = {"source": "finance_sales_reports_detailed", "sourceComplete": False,
               "missingDates": [str(END)], "aggregates": {"101": {"realizationRevenueKopecks": 123}}}
    monkeypatch.setattr(reports, "list_source_cache_ranges_by_prefix", lambda *a, **kw: [{}])
    monkeypatch.setattr("app.wb_report_finance.load_report_finance", lambda *a: partial)
    monkeypatch.setattr(reports, "get_source_cache", lambda *a, **kw: {"aggregates": {"101": {"sellerRevenueKopecks": 999}}})
    assert reports._period_cache(2, "finance", START, END) is partial


def test_legacy_schema_stamp_cannot_certify_realization():
    from app.repricer_cache.store import FINANCE_REVENUE_BASIS, FINANCE_SCHEMA_VERSION
    old = dict(revenueBasis=FINANCE_REVENUE_BASIS, financeSchemaVersion=FINANCE_SCHEMA_VERSION)
    assert reports._compatible_period_cache(old, require_current_finance_basis=True) == {}
    current = dict(old, realizationVersion=page.REALIZATION_VERSION,
                   realizationDateBasis="rrDate", realizationMoneyBasis="retailAmount")
    assert reports._compatible_period_cache(current, require_current_finance_basis=True) == current


def test_finance_planner_uses_exactly_the_reader_coverage_contract(monkeypatch):
    monkeypatch.setattr("app.cabinet.store.get_organization_wb_token_secret", lambda org: "synthetic")
    calls = []
    monkeypatch.setattr(page, "missing_page_ranges", lambda *a, **kw: calls.append((a, kw)) or [(START, END)])
    assert router._report_missing_source_ranges(2, "finance", date_from=START, date_to=END, fresh_only=True) == [(START, END)]
    assert calls == [((2, "synthetic", START, END), {"fresh_only": True})]


def test_irrelevant_zero_fee_operation_does_not_create_a_pnl_product():
    row = operation(saleDt=str(START), rrDate=str(START), sellerOperName="Возмещение издержек",
                    docTypeName="", quantity=1, retailAmount="0", retailPriceWithDisc="0", forPay="0",
                    acquiringFee="0")
    result = fees.project_pnl_operations([row], START, END)
    assert result["aggregates"] == {}


def test_expense_only_product_has_a_reconcilable_daily_basis():
    row = operation(saleDt="2026-09-01", rrDate=str(START), sellerOperName="Штраф", docTypeName="",
                    quantity=0, retailAmount="0", retailPriceWithDisc="0", forPay="0", penalty="12.34",
                    acquiringFee="0")
    result = fees.project_pnl_operations([row], START, END)
    fact = result["aggregates"]["101"]
    daily = [values["101"] for values in result["dailyAggregates"].values() if "101" in values]
    assert sum(row["salesUnits"] for row in daily) == fact["salesUnits"] == 0
    assert sum(row["realizationRevenueKopecks"] for row in daily) == fact["realizationRevenueKopecks"] == 0
    assert fact["penaltyKopecks"] == 1234


def test_cabinet_costs_are_retained_without_revenue_weighting():
    sale = operation(saleDt=str(START), rrDate=str(START))
    charge = operation(2, nmId=0, docTypeName="", sellerOperName="Хранение", quantity=0,
                       retailAmount="0", retailPriceWithDisc="0", forPay="0", acquiringFee="0",
                       paidStorage="100", rrDate=str(START), saleDt=str(START))
    for rows in ([charge], [sale, charge]):
        result = page.project_realization_operations(rows, START, END)
        assert sum(v["storageKopecks"] for v in result["aggregates"].values()) == 0
        assert result["diagnostics"]["rawExpenseTotals"]["storageKopecks"] == 10000
        assert result["unallocatedFinancials"]["storageKopecks"] == 10000
        assert result["unallocatedDailyFinancials"][str(START)]["storageKopecks"] == 10000


def test_penalty_refund_is_not_compensation_and_credit_is_counted_once():
    values = reports._abc_financial_components(finance={"penaltyKopecks": -10000, "financeAdSpendAuthoritative": True,
        "reportedCommissionComplete": True, "reportedCommissionKopecks": 0},
        settings={}, ads={}, revenue_kopecks=0, sales_units=0, cogs_kopecks=0,
        finance_tax={"taxKopecks": 0, "factTaxState": "configured"})
    assert values["compensationKopecks"] == 0
    assert values["penaltyKopecks"] == -10000
    assert values["penaltyRefundKopecks"] == 10000
    assert values["netProfitKopecks"] == 10000


def test_rnp_financial_buyouts_preserve_operational_cohort_and_copy_rows(monkeypatch):
    from app.wb_rnp_finance import apply_financial_buyouts
    from app.wb_api.rnp_runtime import _rnp_row_from_sources
    row = _rnp_row_from_sources(dict(nmId=101, sku="SAME", openCount=100, orderCount=10,
        orderSumKopecks=10000, buyoutCount=7, buyoutSumKopecks=7000, buyoutPct=70), None, "fresh", "high")
    calls = []
    def read(org, left, right):
        calls.append((left, right))
        return dict(sourceComplete=True, aggregates={"101": dict(netSalesUnits=5 if left == START else 2,
            realizationRevenueKopecks=5000 if left == START else 2000)})
    monkeypatch.setattr("app.wb_report_finance.load_report_finance", read)
    result = apply_financial_buyouts([row], 2, START, END, finance_allowed=True)
    actual = result["rows"][0]
    assert (actual.buyoutCount, actual.buyoutSumKopecks) == (5, 5000)
    assert actual.buyoutPct == row.buyoutPct and actual.funnelBuyoutCount == row.buyoutCount == 7
    assert actual.buyoutCountDeltaPct == 150 and row.buyoutSumKopecks == 7000
    assert calls == [(START, END), (date(2026, 9, 28), date(2026, 10, 4))]


def test_rnp_partial_is_not_zero_or_final_comparison_and_no_permission_no_read(monkeypatch):
    from app.wb_rnp_finance import apply_financial_buyouts
    from app.wb_api.rnp_runtime import _rnp_row_from_sources
    row = _rnp_row_from_sources(dict(nmId=101), None, "partial", "medium")
    monkeypatch.setattr("app.wb_report_finance.load_report_finance", lambda *a: dict(sourceComplete=False, aggregates={}))
    result = apply_financial_buyouts([row], 2, START, END, finance_allowed=True)
    assert result["rows"][0].buyoutCount is result["rows"][0].buyoutCountDeltaPct is None
    monkeypatch.setattr("app.wb_report_finance.load_report_finance", lambda *a: (_ for _ in ()).throw(AssertionError("forbidden read")))
    assert apply_financial_buyouts([row], 2, START, END, finance_allowed=False)["rows"][0].financialBuyoutState == "forbidden"


def test_rnp_reported_article_reconciles_39_sales_minus_5_returns_without_cohort_substitution(monkeypatch):
    from app.wb_rnp_finance import apply_financial_buyouts
    from app.wb_api.rnp_runtime import _rnp_row_from_sources
    from app.wb_report_finance import project_report_operations
    start, end = date(2026, 10, 12), date(2026, 10, 18)
    trades = [operation(1, nmId=1025784485, vendorCode="Лчbt_0212", quantity=39,
        rrDate="2026-10-12", saleDt="2026-10-01", retailAmount="3900"),
        operation(2, nmId=1025784485, quantity=5, docTypeName="Возврат", sellerOperName="Возврат",
            rrDate="2026-10-18", saleDt="2026-10-01", retailAmount="500"),
        operation(3, nmId=999, quantity=20, rrDate="2026-10-13"),
        operation(4, nmId=1025784485, quantity=10, rrDate="2026-10-19"),
        operation(5, nmId=1025784485, quantity=50, rrDate="2026-10-13", sellerOperName="Доставка")]
    current = project_report_operations([*trades, trades[0]], start, end) | {"sourceComplete": True}
    previous = project_report_operations([], START, END) | {"sourceComplete": True}
    monkeypatch.setattr("app.wb_report_finance.load_report_finance", lambda org, left, right: current if left == start else previous)
    row = _rnp_row_from_sources(dict(nmId=1025784485, sku="Лчbt_0212", buyoutCount=999), None, "fresh", "high")
    result = apply_financial_buyouts([row], 2, start, end, finance_allowed=True)
    actual = next(row for row in result["rows"] if row.nmId == 1025784485)
    assert (actual.financialSalesUnits, actual.financialReturnsUnits, actual.buyoutCount) == (39, 5, 34)
    assert actual.buyoutSumKopecks == 340000
    assert actual.funnelBuyoutCount == 999
    assert next(row for row in result["rows"] if row.nmId == 999).buyoutCount == 20


def test_financial_chart_uses_posting_not_business_day_and_never_downloads(monkeypatch):
    from app.wb_finance_timeseries import financial_daily_series
    from app.wb_report_finance import project_report_operations
    sale = operation(saleDt="2026-09-30", rrDate="2026-10-05T00:00:00+03:00", retailAmount="100.02")
    returned = operation(2, docTypeName="Возврат", sellerOperName="Возврат", saleDt="2026-09-30",
                         rrDate="2026-10-06", retailAmount="25.01")
    cache = project_report_operations([sale, returned], START, END)
    cache.update(source="finance_sales_reports_detailed", sourceComplete=False, missingDates=[str(END)])
    monkeypatch.setattr("app.wb_report_finance.load_report_finance", lambda *a: cache)
    series = financial_daily_series(2, 101, START, END)
    assert sum(row["revenueKopecks"] or 0 for row in series["daily"].values()) == 7501
    assert series["daily"]["2026-10-05"]["salesUnits"] == 1
    assert series["daily"]["2026-10-06"]["salesUnits"] == -1
    assert series["daily"]["2026-10-07"]["revenueKopecks"] == 0
    assert series["daily"][str(END)]["revenueKopecks"] is None
    assert "2026-09-30" in cache["dailyAggregates"]


def test_direct_storage_is_not_reallocated_and_history_gaps_stay_unallocated(monkeypatch):
    from app.wb_report_finance import apply_storage_allocation, project_report_operations
    def charge(identity, nm, day, amount):
        return operation(identity, nmId=nm, docTypeName="", sellerOperName="Хранение", quantity=0,
            saleDt=day, rrDate=day, retailAmount="0", retailPriceWithDisc="0", forPay="0", acquiringFee="0", paidStorage=amount)
    result = project_report_operations([charge(1, 101, str(START), "10"),
        charge(2, 0, str(START), "100"), charge(3, 0, "2026-10-06", "50")], START, END)
    result["sourceComplete"] = True
    monkeypatch.setattr("app.repricer_cache.store.get_source_cache", lambda *a, **kw: {
        "sourceComplete": True, "aggregates": {"101": {"wbStockUnits": 50}, "102": {"wbStockUnits": 50}}})
    apply_storage_allocation(result, 2, START, END, [dict(sourceKey="known", dateFrom=str(START))])
    assert result["aggregates"]["101"]["storageKopecks"] == 6000
    assert result["aggregates"]["102"]["storageKopecks"] == 5000
    assert result["unallocatedFinancials"]["storageKopecks"] == 5000
    assert result["storageAllocation"]["missingDates"] == ["2026-10-06"]
    assert sum(row["storageKopecks"] for row in result["aggregates"].values()) + 5000 == 16000
    assert sum(day.get("101", {}).get("storageKopecks", 0) for day in result["dailyAggregates"].values()) == 6000


def test_zero_quantity_financial_sale_preserves_nonzero_money():
    result = page.project_realization_operations([operation(quantity=0, retailAmount="100", forPay="90",
        acquiringFee="0", saleDt=str(START), rrDate=str(START))], START, END)
    assert result["aggregates"]["101"]["realizationRevenueKopecks"] == 10000
    assert result["aggregates"]["101"]["netSalesUnits"] == 0


def test_gated_v2_matches_posting_membership_without_changing_business_dates(session):
    from app.platform.finance.service import FinanceService
    from app.platform.period import Period
    from datetime import datetime, timezone
    rows = [operation(saleDt="2026-09-30", rrDate="2026-10-05T21:00:00Z", retailAmount="100", forPay="90", acquiringFee="0"),
            operation(2, docTypeName="Продажа", sellerOperName="Добровольная компенсация", quantity=9,
                retailAmount="999", forPay="10", saleDt="2026-10-06", rrDate="2026-10-06", acquiringFee="0"),
            operation(3, docTypeName="", sellerOperName="Штраф", quantity=9, retailAmount="0", forPay="0",
                penalty="-100", saleDt="2026-10-06", rrDate="2026-10-06", acquiringFee="0")]
    service = FinanceService(session, 1, now=lambda: datetime(2026, 10, 20, tzinfo=timezone.utc))
    service.ingest_snapshot(31, Period(START, END), rows)
    selected = Period(date(2026, 10, 6), date(2026, 10, 6))
    source = service.get_pnl_source(31, selected)
    fact = source.facts[0]
    assert fact.net_units == 1 and fact.revenue_kopecks == 10000
    assert fact.compensation_kopecks == 1000 and fact.penalty_kopecks == -10000
    assert fact.commission_kopecks == 1000
    assert (101, date(2026, 9, 30)) in source.daily_net_units
    assert service.get_page(31, Period(START, START), limit=10, offset=0).summary.net_units == 0


def test_abc_financial_readiness_does_not_wait_for_campaigns(monkeypatch):
    monkeypatch.setattr(router, "_report_missing_source_ranges", lambda org, source, **kw: [] if source == "finance" else [(START, END)])
    assert router._report_daily_sources_ready(2, ("finance", "ads"), date_from=START, date_to=END, report_id="abc") == (True, [])


def test_unallocated_storage_is_a_visible_account_row_even_without_sales():
    row = reports._abc_unallocated_row(dict(sourceComplete=True, unallocatedFinancials={"storageKopecks": 10000}))
    assert row["isUnallocated"] and row["nmId"] is None
    assert row["storageKopecks"] == 10000 and row["netTotalKopecks"] is None
    assert row["taxKopecks"] is None  # no new tax regime/rate is invented for an unallocated entry


def test_legacy_price_planning_projection_retains_its_existing_default_allocation():
    sale = operation(rrDate=str(START), saleDt=str(START))
    fee = operation(2, nmId=0, sellerOperName="Хранение", docTypeName="", quantity=0,
        rrDate=str(START), saleDt=str(START), paidStorage="100", forPay="0", retailAmount="0", acquiringFee="0")
    planning = page.project_operations([sale, fee], START, END)
    public = page.project_realization_operations([sale, fee], START, END)
    assert planning["aggregates"]["101"]["storageKopecks"] == 10000
    assert public["aggregates"]["101"]["storageKopecks"] == 0
    assert public["unallocatedFinancials"]["storageKopecks"] == 10000
    assert planning["aggregates"]["101"]["sellerRevenueKopecks"] == public["aggregates"]["101"]["sellerRevenueKopecks"]


def test_missing_product_realization_is_retained_as_account_evidence():
    result = page.project_realization_operations([operation(nmId=0, rrDate=str(START), saleDt=str(START),
        quantity=1, retailAmount="100", retailPriceWithDisc="120", forPay="90", acquiringFee="0")], START, END)
    assert result["aggregates"] == {}
    assert result["unallocatedRealization"]["revenueKopecks"] == 10000
    assert result["unallocatedRealization"]["netSalesUnits"] == 1
    assert result["unallocatedFinancials"]["commissionKopecks"] == 1000
    assert reports._abc_unallocated_row(result)["salesKopecks"] == 10000


def test_gated_v2_retains_unattributed_financial_advertising_not_service_units(session):
    from app.platform.finance.service import FinanceService
    from app.platform.period import Period
    from datetime import datetime, timezone
    service = FinanceService(session, 1, now=lambda: datetime(2026, 10, 20, tzinfo=timezone.utc))
    service.ingest_snapshot(31, Period(START, END), [operation(nmId=0, rrDate=str(START), saleDt=str(START),
        sellerOperName="Удержание", docTypeName="Продажа", quantity=9, retailAmount="999", forPay="0",
        deduction="12.34", bonusTypeName="WB Продвижение")])
    source = service.get_pnl_source(31, Period(START, END))
    assert len(source.facts) == 1
    assert source.facts[0].nm_id is None and source.facts[0].advertising_kopecks == 1234
    assert source.facts[0].net_units == source.facts[0].revenue_kopecks == 0
