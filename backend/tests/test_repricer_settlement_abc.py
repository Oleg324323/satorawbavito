"""Synthetic, provider-free owner settlement policy tests."""
import socket
from copy import deepcopy

import pytest

from app import repricer_bff as bff


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("No network permitted")
    monkeypatch.setattr(socket.socket, "connect", fail)


def finance(**changes):
    # All amounts in kopecks; payable already net of commission and acquiring.
    return dict(payableKopecks=85_000_000, commissionKopecks=14_000_000,
        acquiringKopecks=1_000_000, logisticsKopecks=7_000_000,
        storageKopecks=1_000_000, acceptanceKopecks=500_000, penaltyKopecks=200_000,
        deductionKopecks=0, loyaltyCostKopecks=0, adSpendKopecks=4_000_000,
        additionalPaymentKopecks=0, salesUnits=100, returnsUnits=0,
        commissionSource="buyerRevenueKopecks-payableKopecks-acquiringKopecks") | changes


def context(**changes):
    return dict(settlementCogsKopecks=40_000_000, taxKopecks=7_500_000,
                factTaxState="configured") | changes


def test_final_payout_does_not_debit_commission_acquiring_or_ads_twice():
    fact = finance()
    original = deepcopy(fact)
    result = bff.settlement_profit_metrics(fact, context())
    assert result["finalPayoutKopecks"] == 72_300_000
    assert result["settlementProfitKopecks"] == 24_800_000
    assert result["settlementBlockers"] == []
    assert fact == original
    # Parallel Ads, plan correction and company overhead do not enter the payout formula.
    assert bff.settlement_profit_metrics(finance(workReturnKopecks=999, otherExpensesKopecks=999), context()) == result


@pytest.mark.parametrize("field", ["payableKopecks", "logisticsKopecks", "storageKopecks", "adSpendKopecks", "additionalPaymentKopecks"])
def test_unknown_wb_component_is_not_zero(field):
    assert bff.settlement_profit_metrics(finance(**{field: None}), context())["settlementProfitKopecks"] is None


@pytest.mark.parametrize("change", [{"settlementCogsKopecks": None}, {"taxKopecks": None}, {"factTaxState": "missing"}])
def test_missing_cost_or_tax_blocks_profit_but_not_payout(change):
    result = bff.settlement_profit_metrics(finance(), context(**change))
    assert result["finalPayoutKopecks"] == 72_300_000
    assert result["settlementProfitKopecks"] is None


def test_signed_returns_zero_and_compensations():
    zero = {key: 0 for key, value in finance().items() if type(value) is int}
    ctx = context(settlementCogsKopecks=0, taxKopecks=0)
    assert bff.settlement_profit_metrics(finance(**zero), ctx)["settlementProfitKopecks"] == 0
    assert bff.settlement_profit_metrics(finance(**(zero | {"payableKopecks": -100, "penaltyKopecks": -2})),
        context(settlementCogsKopecks=-20, taxKopecks=-8))["settlementProfitKopecks"] == -70


def test_abc_cumulative_not_object_quotas_and_crossing_stays_prior():
    assert bff.cumulative_abc({"1": 80, "2": 15, "3": 5}) == {"1": "A", "2": "B", "3": "C"}
    assert bff.cumulative_abc({"1": 79, "2": 2, "3": 14, "4": 5}) == {"1": "A", "3": "A", "4": "B", "2": "C"}


def test_abc_ties_are_stable_and_incomplete_denominator_not_ranked():
    values = {str(i): 10 for i in range(10)}
    assert bff.cumulative_abc(values) == bff.cumulative_abc(dict(reversed(list(values.items()))))
    assert bff.cumulative_abc({"a": 100, "b": 0, "c": -5}) == {"a": "A", "b": "C", "c": "C"}
    assert bff.cumulative_abc({"a": 0, "b": -5}) == {"a": None, "b": None}
    assert bff.cumulative_abc({"a": 100, "b": None}) == {"a": None, "b": None}


def test_abc_axes_differ_and_do_not_depend_on_page_or_catalog_membership():
    facts = {"1": finance(salesUnits=80), "2": finance(salesUnits=15), "3": finance(salesUnits=5)}
    contexts = {key: context(settlementCogsKopecks=72_300_000 - 7_500_000 - profit)
                for key, profit in {"1": 5, "2": 15, "3": 80}.items()}
    rows = [{"meta": {"nmId": i}, "analytics": {}} for i in (1, 2, 3)]
    bff.apply_settlement_abc(rows, facts, contexts)
    assert [row["analytics"]["abcCode"] for row in rows] == ["AC", "BB", "CA"]
    page = [{"meta": {"nmId": 1}, "analytics": {}}]
    bff.apply_settlement_abc(page, facts, contexts)
    assert page[0]["analytics"]["abcCode"] == "AC"
    contexts["2"]["settlementCogsKopecks"] = None
    bff.apply_settlement_abc(page, facts, contexts)
    assert page[0]["analytics"]["abcCode"] == "A—"


def test_summary_uses_same_profit_without_global_ads_or_plan_double_count():
    from app.routers.wb_repricer_bff import _repricer_list_summary
    analytics = {"revenueKopecks": 100_000_000, "salesUnits": 100,
                 **bff.settlement_profit_metrics(finance(), context())}
    rows = [{"meta": {"nmId": 1}, "analytics": analytics}]
    result = _repricer_list_summary(rows, ads_totals={"adSpendKopecks": 999_999_999})
    assert result["settlementProfitKopecks"] == 24_800_000
    assert result["revenueKopecks"] == 100_000_000
    assert result["finalPayoutKopecks"] == 72_300_000


def test_unreconciled_account_costs_block_profit_class():
    rows = [{"meta": {"nmId": 1}, "analytics": {}}]
    bff.apply_settlement_abc(rows, {"1": finance()},
        {"1": context(settlementAllocationConfirmed=False)})
    assert rows[0]["analytics"]["abcCode"] == "A—"
    assert rows[0]["analytics"]["settlementProfitKopecks"] is None
    assert rows[0]["analytics"]["settlementAttributedProfitKopecks"] == 24_800_000


@pytest.mark.parametrize("patch", [{"taxKopecks": None}, {"settlementCogsKopecks": None},
                                  {"factTaxState": "missing"}])
def test_product_subtotal_never_invents_missing_economics(patch):
    result = bff.settlement_profit_metrics(finance(), context(**patch))
    assert result["settlementAttributedProfitKopecks"] is None


def test_expense_only_observation_has_zero_payout_not_missing_payout():
    row = finance(rowsCount=2, salesUnits=0, returnsUnits=0, buyerRevenueKopecks=0,
        payableKopecks=0, commissionKopecks=0, acquiringKopecks=0, commissionSource="ppvzSalesCommission")
    assert bff.settlement_payable_confirmed(row)
    assert not bff.settlement_payable_confirmed(row | {"salesUnits": 1})
    assert not bff.settlement_payable_confirmed(row | {"returnsUnits": 1})
    assert not bff.settlement_payable_confirmed(row | {"rowsCount": 0})
    assert not bff.settlement_payable_confirmed(row | {"payableKopecks": None})


def test_daily_rollup_confirms_every_sale_day_not_first_commission_label():
    from datetime import datetime
    from app.routers.wb_repricer_bff import _rollup_daily_aggregates
    expense = finance(rowsCount=1, salesUnits=0, returnsUnits=0, buyerRevenueKopecks=0,
        payableKopecks=0, commissionKopecks=0, acquiringKopecks=0, commissionSource="ppvzSalesCommission")
    days = {"2026-10-01": {"1": expense}, "2026-10-02": {"1": finance(rowsCount=1)}}
    def roll():
        return _rollup_daily_aggregates(days, datetime(2026, 10, 1), datetime(2026, 10, 2))["1"]
    assert bff.settlement_payable_confirmed(roll())
    days["2026-10-02"]["1"]["commissionSource"] = "commissionPercent"
    assert not bff.settlement_payable_confirmed(roll())
    days["2026-10-02"]["1"] = finance(rowsCount=1)
    del days["2026-10-02"]["1"]["payableKopecks"]
    assert not bff.settlement_payable_confirmed(roll())
    days["2026-10-02"]["1"] = finance(rowsCount=1)
    del days["2026-10-02"]["1"]["storageKopecks"]
    assert bff.settlement_profit_metrics(roll(), context())["settlementProfitKopecks"] is None


def test_advertising_reconciliation_survives_daily_aggregate_diagnostics():
    from app.routers.wb_repricer_bff import _finance_raw_cost_total
    diagnostic = bff.build_finance_diagnostics_from_aggregates({"1": finance(adSpendKopecks=12345)})
    assert _finance_raw_cost_total(diagnostic, "adSpend") == 12345
    diagnostic = bff.build_finance_diagnostics_from_aggregates({"1": finance(adSpendKopecks=None)})
    assert _finance_raw_cost_total(diagnostic, "adSpend") is None


def test_catalog_products_without_finance_do_not_erase_observed_summary():
    from app.routers.wb_repricer_bff import _repricer_list_summary
    observed = {"meta": {"nmId": 1}, "analytics": {"financeState": "ok", **bff.settlement_profit_metrics(finance(), context())}}
    unsold = {"meta": {"nmId": 2}, "analytics": {"financeState": "no_data", **bff.settlement_profit_metrics({}, {})}}
    assert _repricer_list_summary([observed, unsold])["settlementProfitKopecks"] == 24_800_000
    assert _repricer_list_summary([unsold])["settlementProfitKopecks"] is None


def test_filtered_summary_uses_persisted_sku_shares_not_redistribution():
    from app.routers.wb_repricer_bff import _repricer_list_summary
    rows = [{"meta": {"nmId": i}, "analytics": {
        "revenueKopecks": 100_000_000, **bff.settlement_profit_metrics(finance(), context())}}
        for i in (1, 2)]
    all_profit = _repricer_list_summary(rows)["settlementProfitKopecks"]
    assert sum(_repricer_list_summary([row])["settlementProfitKopecks"] for row in rows) == all_profit
