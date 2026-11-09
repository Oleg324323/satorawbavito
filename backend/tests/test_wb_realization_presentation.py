"""One report-money/date definition across WB views; no marketplace requests."""
from copy import deepcopy
from datetime import date, datetime

import pytest

from app import repricer_bff, repricer_page_finance as page
from app import wb_report_finance, wb_statistics_finance, wb_week_finance
from app.routers import wb_reports_bff, wb_repricer_bff
from app.platform.economics.legacy_tax import _revenue
from tests.test_repricer_page_finance import operation

START, END = date(2026, 10, 5), date(2026, 10, 11)


@pytest.mark.parametrize("amount", ["50.01", "30.01"])
def test_account_commission_includes_product_bridge_and_unallocated_trades(amount):
    sale = operation(1, rrDate=str(START), retailAmount=amount)
    returned = operation(2, rrDate=str(END), docTypeName="Возврат", sellerOperName="Возврат",
                         retailAmount="10.01", forPay="8.01", acquiringFee="1.00")
    unassigned = operation(3, rrDate=str(END), nmId=0, retailAmount="20.01",
                           forPay="15.01", acquiringFee="1.00")
    result = page.project_realization_operations([sale, returned, unassigned, deepcopy(sale), deepcopy(unassigned)], START, END)
    product_commission = result["aggregates"]["101"]["commissionKopecks"]
    account_commission = result["unallocatedFinancials"]["commissionKopecks"]
    assert account_commission == 400
    total = wb_repricer_bff._finance_raw_cost_total(result["diagnostics"], "commission")
    assert total == product_commission + account_commission
    summary = wb_repricer_bff._repricer_list_summary(
        [{"meta": {"nmId": 101}, "analytics": {"commissionKopecks": product_commission}}],
        finance_diagnostics=result["diagnostics"])
    assert summary["commissionKopecks"] == total
    assert summary["unassignedFinanceComponentsKopecks"]["commission"] == account_commission


def rows():
    sale = operation(1, quantity=3, retailAmount="150.03", saleDt="2026-09-30", rrDate=str(START))
    returned = operation(2, docTypeName="Возврат", sellerOperName="Возврат", rrDate=str(END))
    compensation = operation(3, quantity=99, sellerOperName="Добровольная компенсация при возврате",
        retailAmount="0", retailPriceWithDisc="0", forPay="10", acquiringFee="0", rrDate=str(END))
    logistics = operation(4, quantity=200, sellerOperName="Логистика", retailAmount="0",
        retailPriceWithDisc="0", forPay="0", acquiringFee="0", rrDate=str(END), deliveryService="7")
    excluded = operation(5, saleDt=str(START), rrDate="2026-10-12", retailAmount="999")
    return [sale, returned, compensation, logistics, excluded, deepcopy(sale)]


@pytest.mark.parametrize("project", [page.project_realization_operations,
    wb_report_finance.project_report_operations, wb_statistics_finance.project_statistics_operations])
def test_realization_is_posting_scoped_operation_amount_net_of_returns(project):
    source = rows()
    before = deepcopy(source)
    result = project(source, START, END)
    fact = result["aggregates"]["101"]
    assert fact["salesUnits"] == 3 and fact["returnsUnits"] == 1 and fact["netSalesUnits"] == 2
    assert page.report_revenue_kopecks(fact) == fact["buyerRevenueKopecks"] == 10002
    assert fact["realizationGrossSalesKopecks"] == 15003
    assert fact["sellerRevenueKopecks"] == 20004  # separate price input, not public realization
    assert fact["realizationRevenueKopecks"] - fact["commissionKopecks"] - fact["acquiringKopecks"] == fact["payableKopecks"]
    assert fact["additionalPaymentKopecks"] == 1000  # credit once, never sales
    assert result["realizationDateBasis"] == "rrDate"
    assert result["dailyAggregates"]["2026-09-30"]["101"]["realizationRevenueKopecks"] == 15003
    assert sum(day["101"]["realizationRevenueKopecks"] for day in result["dailyAggregates"].values()) == 10002
    assert source == before


@pytest.mark.parametrize("value", [0, -123, None])
def test_explicit_realization_does_not_fall_back_to_old_seller_amount(value):
    fact = dict(realizationRevenueKopecks=value, sellerRevenueKopecks=999999)
    assert page.report_revenue_kopecks(fact) == value
    assert _revenue(fact) == value
    mapped = wb_reports_bff._int_map_from_aggregates({"101": fact}, "realizationRevenueKopecks", "sellerRevenueKopecks")
    assert mapped.get(101, 0) == (value or 0)


def test_week_buyouts_use_identical_membership_and_amount_for_both_periods():
    cache = wb_week_finance.project_operations(rows(), START, END) | {"sourceComplete": True}
    assert wb_week_finance.product_buyouts(cache, 101) == dict(units=2, kopecks=10002)
    prior = wb_week_finance.project_operations(rows(), date(2026, 9, 28), date(2026, 10, 4)) | {"sourceComplete": True}
    assert wb_week_finance.product_buyouts(prior, 101) == dict(units=0, kopecks=0)


@pytest.mark.parametrize("changes,code", [
    ({"retailAmount": None}, "REALIZATION_AMOUNT_MISSING"),
    ({"retailAmount": "NaN"}, "REALIZATION_AMOUNT_MISSING"),
    ({"quantity": None}, "INVALID_TRADE_QUANTITY"),
    ({"quantity": -1}, "INVALID_TRADE_QUANTITY"),
    ({"saleDt": None}, "TRADE_DATE_MISSING"),
])
def test_incomplete_trade_cannot_be_certified_as_zero(changes, code):
    with pytest.raises(ValueError, match=code):
        page.project_realization_operations([operation(rrDate=str(START), **changes)], START, END)


@pytest.mark.parametrize("view", ["sku", "stats"])
def test_old_seller_basis_snapshot_cannot_hide_corrected_finance(monkeypatch, view):
    monkeypatch.setattr(wb_repricer_bff, "get_source_cache", lambda *a, **kw:
        dict(version=19, summary=dict(revenueKopecks=999999)))
    assert wb_repricer_bff._load_repricer_sku_snapshot(7, "complete", period_suffix="2026-10-05_2026-10-11",
        include_promotions=False, include_content=False, view=view) is None


def test_statistics_complete_old_aggregate_does_not_hide_updated_raw(monkeypatch):
    monkeypatch.setattr(wb_repricer_bff, "_load_period_source_cache_base", lambda *a, **kw:
        dict(sourceComplete=True, aggregates={"101": dict(sellerRevenueKopecks=999999)}))
    current = page.project_realization_operations(rows(), START, END) | dict(sourceComplete=True)
    monkeypatch.setattr(wb_statistics_finance, "load_statistics_finance", lambda *a: current)
    assert wb_repricer_bff._load_period_source_cache(7, "finance", "selected", 7,
        datetime(2026, 10, 5), datetime(2026, 10, 11)) is current


def test_repricer_rows_summary_and_abc_use_realization_without_changing_prices():
    finance = page.project_realization_operations(rows(), START, END)["aggregates"]["101"]
    row = repricer_bff._build_sku_row("synthetic", nm_id=101, name="Synthetic", subject="Synthetic",
        brand=None, chrt_ids=[], current_price_kopecks=20000, discounted_price_kopecks=10000,
        buyer_price_kopecks=None, promotions=[], use_demo_data=False, finance_aggregate=finance)
    fact = row["analytics"]
    assert fact["revenueKopecks"] == fact["buyoutAmountKopecks"] == 10002
    assert fact["grossSalesAmountKopecks"] == 15003
    assert fact["buyoutUnits"] == 2
    assert fact["sellerRevenueKopecks"] == 20004
    assert row["meta"]["currentPriceKopecks"] == 10000
    summary = wb_repricer_bff._repricer_list_summary([row])
    assert summary["revenueKopecks"] == summary["buyoutAmountKopecks"] == 10002
    assert summary["buyoutUnits"] == 2
    abc = wb_reports_bff._repricer_row_to_abc_row(row)
    assert abc["salesComposite"]["kopecks"] == 10002
    assert abc["salesComposite"]["units"] == 2


def test_factual_revenue_does_not_rebase_planned_price_calculations():
    original = page.project_operations(rows()[:1], date(2026, 9, 30), END)["aggregates"]["101"]
    corrected = page.project_realization_operations(rows()[:1], START, END)["aggregates"]["101"]
    def build(finance):
        return repricer_bff._build_sku_row("synthetic", nm_id=101, name="Synthetic", subject="Synthetic",
            brand=None, chrt_ids=[], current_price_kopecks=20000, discounted_price_kopecks=10000,
            buyer_price_kopecks=None, promotions=[], use_demo_data=False, finance_aggregate=finance)
    old, new = build(original), build(corrected)
    for key in ("plannedCommissionKopecks", "plannedAcquiringKopecks", "plannedTaxKopecks",
                "plannedOtherExpensesKopecks", "plannedPeriodMarginKopecks", "sellerRevenueKopecks"):
        assert new["analytics"][key] == old["analytics"][key]
    assert new["settings"] == old["settings"]
    assert new["meta"]["currentPriceKopecks"] == old["meta"]["currentPriceKopecks"]


def test_projection_rebuilds_old_cache_reuses_raw_and_rechecks_source_revision(monkeypatch):
    from app.cabinet import store as cabinet
    from app.repricer_cache import store
    source = rows()
    source_revision = ["one"]
    saved, reads = {}, []
    monkeypatch.setattr(cabinet, "get_organization_wb_token_secret", lambda org: "synthetic-credential")
    monkeypatch.setattr(store, "list_source_cache_ranges_by_prefix", lambda org, prefix, **kw:
        [dict(sourceKey="raw", dateFrom=str(START), dateTo="2026-10-12", fetchedAt=source_revision[0])] if org == 7 else [])
    def read(org, key, **kwargs):
        reads.append(key)
        if key == "raw":
            return dict(sourceComplete=True, operations=deepcopy(source), fetchedAt=source_revision[0])
        if "repricer-finance-2" in key:
            pytest.fail("old seller-price projection was reused")
        return deepcopy(saved.get((org, key), {}))
    monkeypatch.setattr(store, "get_source_cache", read)
    monkeypatch.setattr(store, "save_source_cache", lambda org, key, value, **kw: saved.update({(org, key): deepcopy(value)}))
    monkeypatch.setattr(repricer_bff, "build_wb_finance_client", lambda *a, **kw: pytest.fail("redundant WB request"))
    first = page.load_page_finance(7, START, END)
    assert first["sourceComplete"] is True and first["coveredDays"] == 7
    assert first["aggregates"]["101"]["realizationRevenueKopecks"] == 10002
    assert page.load_page_finance(7, START, END) == first and reads.count("raw") == 1
    source[0]["retailAmount"] = "160.03"
    source[-1]["retailAmount"] = "160.03"
    source_revision[0] = "two"
    assert page.load_page_finance(7, START, END)["aggregates"]["101"]["realizationRevenueKopecks"] == 11002
    assert reads.count("raw") == 2
    assert page.load_page_finance(8, START, END)["sourceComplete"] is False
