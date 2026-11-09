"""Week buyouts must be financial merchandise, never an order cohort."""
from copy import deepcopy
from datetime import date, datetime, timezone

import pytest

from app import wb_week_finance as finance, wb_week_sources as sources

START, END = date(2026, 10, 7), date(2026, 10, 11)
PRIOR_START, PRIOR_END = date(2026, 10, 2), date(2026, 10, 6)


def trade(identity=1, **changes):
    return dict(rrdId=identity, nmId=101, vendorCode="same", sku="same", srid="same-order",
        docTypeName="Продажа", sellerOperName="Продажа", quantity=1, currency="RUB",
        saleDt=str(START), rrDate=str(START), retailPriceWithDisc="100.02", retailAmount="50.01",
        forPay="40.01", acquiringFee="2", ppvzSalesCommission="10", paidStorage="0",
        paidAcceptance="0", penalty="0", deliveryService="0", deduction="0") | changes


def project(rows, start=START, end=END, complete=True):
    return finance.project_operations(rows, start, end) | {"sourceComplete": complete}


def test_posting_day_inclusive_moscow_boundaries_not_order_or_sale_day():
    rows = [trade(1, rrDate="2026-10-06T21:00:00Z", saleDt="2026-09-01"),
        trade(2, rrDate="2026-10-11T20:59:59Z", saleDt="2026-10-12"),
        trade(3, rrDate="2026-10-06T20:59:59Z"), trade(4, rrDate="2026-10-11T21:00:00Z"),
        trade(5, rrDate=str(PRIOR_END), orderDt=str(START))]
    assert finance.product_buyouts(project(rows), 101) == dict(units=2, kopecks=10002)
    assert finance.product_buyouts(project(rows, PRIOR_START, PRIOR_END), 101) == dict(units=2, kopecks=10002)


def test_product_ids_operation_amount_net_returns_signed_reversals_and_no_srid_dedup():
    rows = [trade(1, quantity=3, retailAmount="150.03"), trade(2, docTypeName="Возврат", sellerOperName="Возврат"),
        trade(3, quantity=-1, retailAmount="-50.01"), trade(4, nmId=102, quantity=2, retailAmount="100.02"), trade(5, nmId=0, quantity=4, retailAmount="200.04")]
    cache = project(rows + [deepcopy(rows[0])])
    assert finance.product_buyouts(cache, 101) == dict(units=1, kopecks=5001)
    assert finance.product_buyouts(cache, 102) == dict(units=2, kopecks=10002)
    assert finance.product_buyouts(cache, "0") == dict(units=4, kopecks=20004)
    assert sum(row["units"] for row in cache["buyouts"].values()) == 7
    with pytest.raises(ValueError, match="CONFLICTING_ROWS"):
        project([trade(), trade(quantity=2)])


@pytest.mark.parametrize("operation", ["Доставка", "Хранение", "Добровольная компенсация при возврате",
    "Возмещение издержек по перемещению и операционной обработке товара", "Компенсация скидки по программе лояльности"])
def test_non_trade_quantities_are_not_buyouts_even_in_sale_document(operation):
    assert finance.product_buyouts(project([trade(sellerOperName=operation, quantity=500)]), 101) == dict(units=0, kopecks=0)


@pytest.mark.parametrize("changes,expected", [
    ({"quantity": None}, {"units": None, "kopecks": None}),
    ({"quantity": True}, {"units": None, "kopecks": None}),
    ({"retailAmount": None}, {"units": 1, "kopecks": None}),
    ({"retailAmount": "NaN"}, {"units": 1, "kopecks": None}),
    ({"currency": "BYN"}, {"units": 1, "kopecks": None}),
    ({"rrDate": None}, {"units": None, "kopecks": None}),
    ({"sellerOperName": "Коррекция продаж"}, {"units": None, "kopecks": None}),
    ({"sellerOperName": "Коррекция возвратов"}, {"units": None, "kopecks": None}),
])
def test_unknown_fields_or_corrections_do_not_fabricate_totals(changes, expected):
    assert finance.product_buyouts(project([trade(**changes)]), 101) == expected


def test_invalid_fractional_buyout_quantity_is_not_truncated():
    # Exercise the new projection independently of the pre-existing expense
    # commission parser, whose input contract requires an integer quantity.
    assert finance.product_buyouts(project([trade(quantity="1.5")]), 101) == dict(units=None, kopecks=None)


def test_confirmed_zero_missing_partial_unallocated_and_negative_net():
    assert finance.product_buyouts(project([]), 101) == dict(units=0, kopecks=0)
    assert finance.product_buyouts({}, 101) == dict(units=None, kopecks=None)
    assert finance.product_buyouts(project([trade()], complete=False), 101) == dict(units=1, kopecks=5001)  # known subtotal, never a confirmed period total
    assert finance.product_buyouts(project([trade(nmId=0)]), 101) == dict(units=None, kopecks=None)
    assert finance.product_buyouts(project([trade(docTypeName="Возврат", sellerOperName="Возврат")]), 101) == dict(units=-1, kopecks=-5001)


def test_both_periods_finance_only_product_chart_and_other_calculations_preserved(monkeypatch):
    monkeypatch.setattr(sources, "source_revision", lambda *a: "revision")
    monkeypatch.setattr(sources, "load_funnel", lambda *a: dict(metrics={"101": dict(
        orderCount=61, orderSumKopecks=29890000, cartCount=99, buyoutCount=999, buyoutSumKopecks=99999)},
        complete=True, missingDates=[]))
    monkeypatch.setattr(sources, "load_ads", lambda *a: ({}, True, 0))
    monkeypatch.setattr(sources.store, "get_source_cache", lambda *a, **kw: {})
    monkeypatch.setattr(finance, "load_finance", lambda org, left, right:
        project([trade(quantity=11, retailAmount="550.11"), trade(2, nmId=102)], complete=True) if left == START else
        project([trade(rrDate=str(PRIOR_END), quantity=5, retailAmount="250.05")], PRIOR_START, PRIOR_END))
    original = dict(nmId=101, sales={"units": 999}, logisticsKopecks={"kopecks": 123},
        profit={"kopecks": 456}, taxKopecks={"kopecks": 77})
    report = dict(rows=[deepcopy(original)], chart={"points": [{"sales": {"units": 999}}]},
        unallocatedFinancials={"selected": {}, "previous": {}})
    result = sources.attach(report, 7, START, END, PRIOR_START, PRIOR_END, finance_allowed=True, catalog={})
    row = result["rows"][0]
    assert row["sales"] == dict(units=11, previousUnits=5, kopecks=55011, previousKopecks=25005, deltaPct=120.0)
    assert row["orders"]["units"] == 61 and row["baskets"]["units"] == 99
    for key in ("logisticsKopecks", "profit", "taxKopecks"):
        assert row[key] == original[key]
    assert result["chart"]["points"][0]["sales"] == row["sales"]
    assert result["rows"][1]["nmId"] == 102 and result["rows"][1]["sales"]["units"] == 1


def test_finance_denied_does_not_fall_back_to_funnel(monkeypatch):
    monkeypatch.setattr(finance, "load_finance", lambda *a: pytest.fail("finance read without permission"))
    monkeypatch.setattr(sources, "source_revision", lambda *a: "revision")
    monkeypatch.setattr(sources, "load_funnel", lambda *a: dict(metrics={"101": dict(buyoutCount=99)}, complete=True, missingDates=[]))
    report = dict(rows=[{"nmId": 101}], unallocatedFinancials={"selected": {}, "previous": {}})
    result = sources.attach(report, 7, START, END, PRIOR_START, PRIOR_END, finance_allowed=False, catalog={})
    assert result["rows"][0]["sales"]["units"] is None


def test_real_raw_reader_reuses_complete_cursors_and_latest_correction_without_provider(monkeypatch):
    from app import repricer_page_finance as raw
    from app.repricer_cache import store
    finance._cache.clear()
    monkeypatch.setattr("app.cabinet.store.get_organization_wb_token_secret", lambda org: "synthetic")
    now = datetime.now(timezone.utc).isoformat()
    entries = [dict(sourceKey="new", dateFrom=str(START), dateTo=str(END), fetchedAt=now),
        dict(sourceKey="old", dateFrom=str(START), dateTo=str(END), fetchedAt="2026-10-01T00:00:00Z"),
        dict(sourceKey="later", dateFrom="2026-10-12", dateTo="2026-10-12", fetchedAt=now)]
    calls = []
    monkeypatch.setattr(store, "list_source_cache_ranges_by_prefix", lambda org, prefix, **kw: entries if org == 7 else [])
    def read(org, key, **kw):
        calls.append(key)
        meta = next((row for row in entries if row["sourceKey"] == key), None)
        if meta is None:
            return {}  # no persisted derived projection in this fixture
        from tests.test_repricer_page_finance import operation
        row = operation(saleDt=str(START), rrDate=str(START), quantity=2 if key == "new" else 100, retailAmount="100.02")
        if key == "later":
            row = trade(2, rrDate="2026-10-12")
        return dict(sourceComplete=True, operations=[row], dateFrom=meta["dateFrom"], dateTo=meta["dateTo"], fetchedAt=now)
    monkeypatch.setattr(store, "get_source_cache", read)
    marker = raw.page_connection.set(None)
    try:
        first = finance.load_finance(7, START, END)
        assert finance.product_buyouts(first, 101) == dict(units=2, kopecks=10002)
        first["buyouts"]["101"]["units"] = 999
        before = len(calls)
        assert finance.product_buyouts(finance.load_finance(7, START, END), 101)["units"] == 2
        assert len(calls) == before
        assert finance.product_buyouts(finance.load_finance(8, START, END), 101)["units"] is None
    finally:
        raw.page_connection.reset(marker)
        finance._cache.clear()


def test_old_funnel_based_report_cache_is_not_reused(monkeypatch):
    from app.routers import wb_reports_bff as reports
    monkeypatch.setattr(reports, "legacy_finance_tax_revision", lambda org: "same-tax")
    old = dict(taxRevision="same-tax", completedAt=datetime.now(timezone.utc).isoformat(),
        report=dict(cacheVersion="v10", rows=[dict(nmId=101, sales={"units": 999})]))
    assert not reports._report_payload_cache_is_usable("week-over-week", old, organization_id=7)
