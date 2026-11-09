"""WB repricer read projection only; no provider calls or business-setting writes."""
from copy import deepcopy
from datetime import date

import pytest

from app import repricer_page_finance as finance
from app.routers import wb_repricer_bff as router
from app.wb_repricer_reporting import financial_totals, load_buyout_comparison, project_buyout_comparison
from tests.test_repricer_page_finance import operation

START, END = date(2026, 10, 5), date(2026, 10, 11)


@pytest.mark.parametrize("selected", [None, {"101"}, {"102"}, {"999"}])
def test_light_comparison_matches_full_trade_projection_not_service_quantities(selected):
    rows = [
        operation(1, rrDate=str(START), saleDt="2026-09-30", quantity=3, retailAmount="150.03"),
        operation(2, rrDate=str(END), quantity=1, docTypeName="Возврат", sellerOperName="Возврат"),
        operation(3, rrDate=str(END), nmId=102, quantity=0, retailAmount="-7.13"),
        operation(4, rrDate=str(START), nmId=0, quantity=2, retailAmount="40.02"),
        operation(5, rrDate=str(END), sellerOperName="Логистика", quantity=500, deliveryService="7.13"),
        operation(6, rrDate="2026-10-12", quantity=100, retailAmount="9999"),
        operation(7, rrDate=str(END), sellerOperName="Добровольная компенсация при возврате",
                  quantity=100, forPay="8.13"),
    ]
    rows.append(deepcopy(rows[0]))
    original = deepcopy(rows)
    full = finance.project_realization_operations(rows, START, END) | dict(sourceComplete=True)
    light = project_buyout_comparison(rows, START, END) | dict(sourceComplete=True)
    assert financial_totals(light, selected) == financial_totals(full, selected)
    assert "realizationVersion" not in light  # Cannot be consumed as a complete financial ledger.
    assert rows == original


@pytest.mark.parametrize("changes,code", [
    ({"retailAmount": None}, "REALIZATION_AMOUNT_MISSING"),
    ({"retailAmount": "NaN"}, "REALIZATION_AMOUNT_MISSING"),
    ({"quantity": -1}, "INVALID_TRADE_QUANTITY"),
    ({"quantity": True}, "INVALID_TRADE_QUANTITY"),
    ({"saleDt": None}, "TRADE_DATE_MISSING"),
    ({"rrdId": None}, "INVALID_ROW_ID"),
])
def test_comparison_does_not_turn_invalid_trades_into_zero(changes, code):
    with pytest.raises(ValueError, match=code):
        project_buyout_comparison([operation(rrDate=str(START), **changes)], START, END)


def test_comparison_rejects_conflicting_duplicate_operations():
    rows = [operation(rrDate=str(START)), operation(rrDate=str(START), retailAmount="99.99")]
    with pytest.raises(ValueError, match="CONFLICTING_ROWS"):
        project_buyout_comparison(rows, START, END)


def test_comparison_uses_existing_loader_and_request_memo_without_full_expense_projection(monkeypatch):
    calls = []
    def load(org, start, end, **kwargs):
        calls.append((org, start, end, kwargs))
        if kwargs.get("cached_only"):
            return None
        return dict(aggregates={}, sourceComplete=True)
    monkeypatch.setattr(finance, "load_page_finance", load)
    memo = {}
    first = load_buyout_comparison(2, START, END, memo=memo)
    assert load_buyout_comparison(2, START, END, memo=memo) is first
    load_buyout_comparison(3, START, END, memo=memo)
    load_buyout_comparison(2, START, START, memo=memo)
    assert len(calls) == 6
    assert all(call[3] == {"cached_only": True} for call in calls[::2])
    assert all(call[3] == {"projector": project_buyout_comparison} for call in calls[1::2])


def test_comparison_reuses_existing_revision_validated_full_projection(monkeypatch):
    saved = dict(aggregates={}, sourceComplete=True)
    def load(org, start, end, **kwargs):
        assert kwargs == {"cached_only": True}
        return saved
    monkeypatch.setattr(finance, "load_page_finance", load)
    assert load_buyout_comparison(2, START, END) is saved


def test_comparison_request_memo_is_isolated_by_marketplace_connection(monkeypatch):
    calls = []
    def load(org, start, end, **kwargs):
        calls.append(finance.page_connection.get())
        return dict(aggregates={}, sourceComplete=True)
    monkeypatch.setattr(finance, "load_page_finance", load)
    memo = {}
    marker = finance.page_connection.set("synthetic-account-one")
    try:
        load_buyout_comparison(2, START, END, memo=memo)
        finance.page_connection.set("synthetic-account-two")
        load_buyout_comparison(2, START, END, memo=memo)
        assert calls == ["synthetic-account-one", "synthetic-account-two"]
    finally:
        finance.page_connection.reset(marker)


def test_cached_only_probe_does_not_read_raw_operations_build_or_write(monkeypatch):
    from app.cabinet import store as cabinet
    from app.repricer_cache import store
    monkeypatch.setattr(cabinet, "get_organization_wb_token_secret", lambda *_: "synthetic")
    monkeypatch.setattr(store, "list_source_cache_ranges_by_prefix", lambda *_args, **_kwargs: [])
    reads = []
    monkeypatch.setattr(store, "get_source_cache", lambda org, key, **kwargs: reads.append(key) or {})
    def forbidden(*args, **kwargs):
        pytest.fail("cached-only lookup must not project or write")
    monkeypatch.setattr(finance, "project_realization_operations", forbidden)
    monkeypatch.setattr(store, "save_source_cache", forbidden)
    assert finance.load_page_finance(2, START, END, cached_only=True) is None
    assert len(reads) == 1 and "page_finance_projection_" in reads[0]


@pytest.mark.parametrize("start,end,expected", [
    (START, END, 13_492_300),
    (date(2026, 10, 12), date(2026, 10, 16), 0),
    (END, END, 13_492_300),
])
def test_financial_advertising_summary_does_not_wait_for_or_substitute_campaign_spend(start, end, expected):
    charge = operation(1, rrDate=str(END), nmId=0, docTypeName="", sellerOperName="Удержание",
        quantity=0, retailAmount="0", retailPriceWithDisc="0", forPay="0", acquiringFee="0",
        deduction="134923", bonusTypeName="Оказание услуг «WB Продвижение»")
    ledger = finance.project_realization_operations([charge, deepcopy(charge)], start, end)
    for campaigns in (None, dict(adSpendKopecks=999_999_999, adClicks=13)):
        summary = router._repricer_list_summary([], finance_diagnostics=ledger["diagnostics"], ads_totals=campaigns)
        assert summary["adSpendKopecks"] == expected
        assert summary["adSpendSource"] == "finance_deduction_wb_promotion"
        assert summary["deductionKopecks"] == 0
        assert summary["expensesKopecks"] == expected


def test_unclassified_financial_deductions_leave_advertising_unavailable():
    unknown = operation(rrDate=str(START), deduction="13", bonusTypeName="")
    ledger = finance.project_realization_operations([unknown], START, END)
    summary = router._repricer_list_summary([], finance_diagnostics=ledger["diagnostics"],
                                           ads_totals=dict(adSpendKopecks=12345))
    assert summary["adSpendKopecks"] is None
    assert summary["adSpendSource"] is None
