"""ABC/P&L financial changes, using disposable receipts and no WB requests."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone

import pytest

from app import wb_pnl_expenses as fees, wb_pnl_advertising as advertising
from app import wb_reports_sprint_d as reports
from app.routers import wb_reports_bff as router
from app.repricer_bff import FINANCE_REVENUE_BASIS, FINANCE_SCHEMA_VERSION
from tests.test_repricer_page_finance import operation

START, END = date(2026, 10, 12), date(2026, 10, 18)


def trade(identity=1, **changes):
    return operation(identity, **({"saleDt": str(START), "rrDate": str(START)} | changes))


def ledger(rows):
    return fees.project_pnl_operations(rows, START, END) | dict(
        sourceComplete=True, dateFrom=str(START), dateTo=str(END), missingDates=[],
        revenueBasis=FINANCE_REVENUE_BASIS, financeSchemaVersion=FINANCE_SCHEMA_VERSION,
        fetchedAt=datetime.now(timezone.utc).isoformat())


def ads(rows=None, **changes):
    values = [r.get("adSpendKopecks") for r in (rows or {}).values()]
    total = (changes.get("totals") or {}).get("adSpendKopecks",
        sum(values) if all(type(v) is int for v in values) else None)
    return dict(source="wb_ads_fullstats", dateFrom=str(START), dateTo=str(END),
                fetchedAt=datetime.now(timezone.utc).isoformat(), sourceComplete=True,
                aggregates=rows or {}, campaignTotals={"ad_spend_kopecks": total}) | changes


@pytest.fixture
def build(monkeypatch):
    inputs = {"finance": ledger([trade(ppvzSalesCommission="3")]), "ads": ads(), "tax": {
        "101": dict(settlementCogsKopecks=1000, settlementCogsState="configured",
                    taxKopecks=100, factTaxState="configured")}}
    monkeypatch.setattr(fees, "load_pnl_finance", lambda *a: inputs["finance"])
    monkeypatch.setattr(advertising, "_source_context", lambda org: dict(
        connection="fixture", marketplaceAccountId=1, bindingVersion=1, connectedAt="2026-01-01T00:00:00+00:00"))
    monkeypatch.setattr(advertising, "_candidate_caches", lambda *a: [inputs["ads"]])
    monkeypatch.setattr(reports, "_period_cache", lambda org, source, *a, **kw: inputs.get(source, {}))
    monkeypatch.setattr(reports, "get_legacy_finance_taxes", lambda *a, **kw: inputs["tax"])
    monkeypatch.setattr(reports, "get_source_cache", lambda *a, **kw: {})
    monkeypatch.setattr(reports, "_goods_index", lambda *a, **kw: {})
    monkeypatch.setattr(reports, "load_runtime_state", lambda *a: {})
    monkeypatch.setattr(reports, "load_algorithm_settings", lambda *a: {})
    def run(**options):
        return reports._build_cached_pnl_report(organization_id=2, date_from=START,
            date_to=END, group_by="sku", requested_state="preliminary", finance_allowed=True, **options)
    return inputs, run


@pytest.mark.parametrize("return_amount", ["1322.34", "-1322.34"])
def test_net_commission_includes_returns_once_without_changing_payout_bridge(return_amount):
    rows = [trade(ppvzSalesCommission="7625.80"), trade(2, docTypeName="Возврат",
             sellerOperName="Возврат", ppvzSalesCommission=return_amount)]
    before = deepcopy(rows)
    result = ledger(rows + [deepcopy(rows[0])])
    fact = result["aggregates"]["101"]
    assert reports._finance_commission_kopecks(fact) == 630346
    assert fact["commissionKopecks"] == 0  # existing other-page bridge is unchanged
    assert rows == before


def test_commission_zero_unknown_and_signed_nonreturn_adjustments():
    assert reports._finance_commission_kopecks(ledger([trade()])["aggregates"]["101"]) == 0
    missing = ledger([trade(ppvzSalesCommission=None)])["aggregates"]["101"]
    assert reports._finance_commission_kopecks(missing) is None
    assert reports._finance_commission_kopecks({"commissionKopecks": 999,
        "commissionFormulaKopecks": 888}) is None
    adjusted = ledger([trade(ppvzSalesCommission="3.11"), trade(2,
        sellerOperName="Коррекция продаж", quantity=0, retailAmount="0",
        retailPriceWithDisc="0", forPay="0", acquiringFee="0", ppvzSalesCommission="-1.01")])
    assert reports._finance_commission_kopecks(adjusted["aggregates"]["101"]) == 210


def test_pnl_uses_reported_commission_and_campaign_spend_once(build):
    inputs, run = build
    inputs["finance"] = ledger([trade(ppvzSalesCommission="3", paidAcceptance="1",
        penalty="2", deduction="7", bonusTypeName="WB Продвижение")])
    inputs["ads"] = ads({"101": {"adSpendKopecks": 1100}})
    result = run()
    product = next(row for row in result.rows if row.nmId == 101)
    assert product.commissionKopecks == 300  # not the 800 kopeck payout bridge
    assert product.adSpendKopecks == 1100  # replaces the 700 financial charge
    assert product.financialExpenseSubtotalKopecks == 1000  # 100 + 200 + 700
    # Keep the existing 5% configured other expense (250 kopecks) too.
    assert product.netProfitKopecks == 5001 - 1000 - 300 - 200 - 100 - 1100 - 100 - 200 - 250
    assert result.totals.netProfitKopecks == product.netProfitKopecks
    assert product.marginPct == round(product.netProfitKopecks / 5001 * 100, 2)


def test_account_commission_and_campaign_residual_reconcile_without_allocation(build):
    inputs, run = build
    inputs["finance"] = ledger([trade(ppvzSalesCommission="3"), trade(2, nmId=0,
        quantity=0, docTypeName="", sellerOperName="Удержание", retailAmount="0",
        retailPriceWithDisc="0", forPay="0", acquiringFee="0", ppvzSalesCommission="2",
        paidAcceptance="1", penalty="2", deduction="7", bonusTypeName="WB Продвижение")])
    inputs["ads"] = ads({"101": {"adSpendKopecks": 400},
                         "campaign-unallocated": {"nmId": 0, "adSpendKopecks": 600}})
    report = run()
    by_id = {row.rowId: row for row in report.rows}
    assert by_id["nm-101"].commissionKopecks == 300
    assert by_id["account-unallocated"].commissionKopecks == 200
    assert by_id["account-unallocated"].adSpendKopecks == 600
    assert sum(row.commissionKopecks for row in report.rows) == 500
    assert sum(row.adSpendKopecks for row in report.rows) == 1000
    assert sum(row.financialExpenseSubtotalKopecks for row in report.rows) == 1000


@pytest.mark.parametrize("campaigns", [{}, ads(sourceComplete=False),
    ads({"101": {"adSpendKopecks": None}}), ads(dateFrom="2026-10-11")])
def test_missing_incomplete_or_wrong_period_advertising_never_means_zero(build, campaigns):
    inputs, run = build
    inputs["ads"] = campaigns
    product = run().rows[0]
    assert product.adSpendKopecks is None and product.netProfitKopecks is None
    assert product.revenueKopecks == 5001 and product.commissionKopecks == 300


def test_complete_empty_campaigns_confirm_zero_and_missing_cost_still_blocks_profit(build):
    inputs, run = build
    assert run().rows[0].adSpendKopecks == 0
    inputs["tax"]["101"].update(settlementCogsKopecks=None,
        settlementCogsReason="dated_cost_missing_or_daily_basis_incomplete",
        settlementCostMissingDates=[str(START)])
    row = run().rows[0]
    assert row.cogsKopecks is None and row.netProfitKopecks is None
    assert row.costOperationDates == [str(START)]


def test_cabinet_only_campaign_spend_is_not_invented_as_zero_for_products(build):
    inputs, run = build
    inputs["ads"] = ads(totals={"adSpendKopecks": 500})
    report = run()
    assert next(row for row in report.rows if row.nmId == 101).adSpendKopecks is None
    assert next(row for row in report.rows if row.nmId is None).adSpendKopecks == 500


def test_commission_uses_posting_period_and_preserves_old_operation_dates():
    result = ledger([trade(ppvzSalesCommission="10.01", saleDt="2026-09-30"),
        trade(2, rrDate=str(END), ppvzSalesCommission="2.02"),
        trade(3, rrDate="2026-10-19", ppvzSalesCommission="1000")])
    assert reports._finance_commission_kopecks(result["aggregates"]["101"]) == 1203
    assert reports._finance_commission_kopecks(result["dailyAggregates"]["2026-09-30"]["101"]) == 1001


def test_commission_only_rows_are_added_only_in_abc_pnl_not_other_report_consumers():
    from app.wb_report_finance import project_report_operations
    from app.wb_report_commission import with_commission_products
    raw = [trade(quantity=0, sellerOperName="Коррекция продаж", retailAmount="0",
        retailPriceWithDisc="0", forPay="0", acquiringFee="0", ppvzSalesCommission="-2.01")]
    shared = project_report_operations(raw, START, END)
    assert shared["aggregates"] == {}
    abc = with_commission_products(shared, START, END)
    assert reports._finance_commission_kopecks(abc["aggregates"]["101"]) == -201
    assert shared["aggregates"] == {}  # no mutation of another view's source
    assert reports._finance_commission_kopecks(ledger(raw)["aggregates"]["101"]) == -201


def test_abc_uses_reported_commission_and_keeps_its_existing_financial_advertising(build):
    inputs, _ = build
    inputs["finance"] = ledger([trade(ppvzSalesCommission="3", deduction="7", bonusTypeName="WB Продвижение")])
    result = reports._build_abc_report_from_snapshots(organization_id=2, date_from=START,
        date_to=END, group_by="sku", filters="", finance_allowed=True)
    row = next(row for row in result.rows if row["nmId"] == 101)
    assert row["commissionKopecks"] == 300
    assert row["commissionSource"] == "ppvzSalesCommission"
    assert row["adSpendKopecks"] == 700  # campaign change is P&L-only
    assert row["netTotalKopecks"] == 5001 - 1000 - 300 - 200 - 700 - 100 - 250


def test_missing_commission_blocks_only_affected_product_and_full_total(build):
    inputs, run = build
    inputs["finance"] = ledger([trade(ppvzSalesCommission="3"), trade(2, nmId=102,
        ppvzSalesCommission=None)])
    inputs["tax"]["102"] = dict(inputs["tax"]["101"])
    report = run()
    by_nm = {row.nmId: row for row in report.rows}
    assert by_nm[101].netProfitKopecks is not None
    assert by_nm[102].commissionKopecks is None and by_nm[102].netProfitKopecks is None
    assert report.totals.netProfitKopecks is None


def test_financial_subtotal_counts_advertising_inside_deductions_once_and_keeps_signs():
    source = ledger([trade(paidAcceptance="12", penalty="10", deduction="7", bonusTypeName="WB Продвижение"),
                     trade(2, paidAcceptance="-2", penalty="-3", deduction="-1", bonusTypeName="Коррекция")])
    assert fees.financial_expense_subtotal(source, 101) == 2300
    assert fees.financial_expense_subtotal(source | {"sourceComplete": False}, 101) is None
    missing = trade(deduction=None)
    assert fees.financial_expense_subtotal(ledger([missing]), 101) is None


def test_undated_confirmed_zero_does_not_create_a_false_subtotal_gap():
    zero = trade(2, rrDate=None, saleDt=None, quantity=0, docTypeName="", sellerOperName="",
        retailAmount="0", retailPriceWithDisc="0", forPay="0", acquiringFee="0")
    assert fees.financial_expense_subtotal(ledger([trade(), zero]), 101) == 0
    assert fees.financial_expense_subtotal(ledger([trade(), zero | {"penalty": "1"}]), 101) is None


def test_pnl_rejects_a_snapshot_marked_stale_by_the_calculation_barrier():
    assert not router._report_payload_cache_is_usable("pnl", {
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "report": {"cacheVersion": router.PNL_REPORT_PAYLOAD_VERSION,
                   "meta": {"freshnessState": "stale"}}})


def test_pnl_requires_campaigns_but_abc_financial_readiness_remains_unchanged(monkeypatch):
    calls = []
    monkeypatch.setattr(advertising, "missing_campaign_ranges", lambda *a, **kw: calls.append(a) or [(START, END)])
    monkeypatch.setattr(router, "_report_missing_source_ranges", lambda *a, **kw: [])
    monkeypatch.setattr("app.wb_abc_loading.missing_source_ranges", lambda *a, **kw: [])
    assert router._report_daily_sources_ready(2, ("finance", "ads"), date_from=START,
        date_to=END, report_id="pnl") == (False, ["ads"])
    assert router._report_daily_sources_ready(2, ("finance", "ads"), date_from=START,
        date_to=END, report_id="abc") == (True, [])
    assert calls == [(2, START, END)]


def test_saved_campaigns_reused_and_incomplete_or_stale_range_requested(build):
    inputs, _ = build
    assert advertising.missing_campaign_ranges(2, START, END, fresh_only=True) == []
    inputs["ads"]["sourceComplete"] = False
    assert advertising.missing_campaign_ranges(2, START, END) == [(START, END)]
    inputs["ads"] = ads(fetchedAt=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat())
    assert advertising.missing_campaign_ranges(2, START, END, fresh_only=True) == [(START, END)]


def test_pnl_saved_calculation_reused_then_invalidated_by_input_or_cost_revision(build, monkeypatch):
    inputs, _ = build
    saved, builds = {}, []
    revision, tax = ["raw-ads-connection-v1"], ["cost-tax-v1"]
    monkeypatch.setattr(reports, "_pnl_source_revision", lambda *a: revision[0])
    monkeypatch.setattr(reports, "legacy_finance_tax_revision", lambda *a: tax[0])
    monkeypatch.setattr(reports, "get_source_cache", lambda org, key, **kw: deepcopy(saved.get((org, key))))
    monkeypatch.setattr(reports, "save_source_cache", lambda org, key, value: saved.__setitem__((org, key),
        deepcopy(value) | {"fetchedAt": datetime.now(timezone.utc).isoformat()}))
    original = reports._build_cached_pnl_report
    def calculate(**kwargs):
        builds.append(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(reports, "_build_cached_pnl_report", calculate)
    args = dict(organization_id=2, date_from=START, date_to=END, group_by="sku",
                requested_state="preliminary", finance_allowed=True)
    first = reports.build_pnl_report.__wrapped__(**args)
    second = reports.build_pnl_report.__wrapped__(**args)
    assert first == second and len(builds) == 1
    inputs["ads"] = ads({"101": {"adSpendKopecks": 500}})
    revision[0] = "raw-ads-connection-v2"
    updated = reports.build_pnl_report.__wrapped__(**args)
    assert len(builds) == 2 and updated.rows[0].netProfitKopecks == first.rows[0].netProfitKopecks - 500
    tax[0] = "cost-tax-v2"
    inputs["tax"]["101"]["settlementCogsKopecks"] += 100
    updated_cost = reports.build_pnl_report.__wrapped__(**args)
    assert len(builds) == 3 and updated_cost.rows[0].netProfitKopecks == updated.rows[0].netProfitKopecks - 100
    assert all(row["organization_id"] == 2 for row in builds)
