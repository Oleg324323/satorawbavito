from datetime import datetime, timezone

import pytest

from app.repricer_bff import aggregate_finance_report_rows, _finance_seller_revenue_kopecks
from app.routers import wb_repricer_bff as router


def dt(day):
    return datetime.fromisoformat(day).replace(tzinfo=timezone.utc)


def trade(identity, sale_day, posting_day, price, document="Продажа", **kwargs):
    return dict(rrdId=identity, nmId=101, saleDt=sale_day, rrDate=posting_day,
                docTypeName=document, sellerOperName=document, quantity=1,
                retailPriceWithDisc=price, retailAmount="50", **kwargs)


def net(result):
    return sum(row["sellerRevenueKopecks"] for row in result["aggregates"].values())


def test_sale_price_is_not_buyer_payment_or_payout():
    row = trade(1, "2026-09-22", "2026-09-22", "123.455", forPay="40")
    assert _finance_seller_revenue_kopecks(row, 2) == 24691
    assert _finance_seller_revenue_kopecks({"retailAmount": "500"}, 1) == 0
    assert _finance_seller_revenue_kopecks({"retailPriceWithDisc": "0"}, 1) == 0


@pytest.mark.parametrize("end,expected", [("2026-10-04", 80000), ("2026-10-05", 110000)])
def test_inclusive_operation_dates_sales_minus_returns(end, expected):
    rows = [trade(1, "2026-09-21", "2026-09-22", "9000"),
            trade(2, "2026-09-22", "2026-09-22", "1000"),
            trade(3, "2026-10-04", "2026-10-04", "200", "Возврат"),
            trade(4, "2026-10-05", "2026-10-05", "300")]
    result = aggregate_finance_report_rows(rows, date_from=dt("2026-09-22"), date_to=dt(end))
    assert net(result) == expected
    assert sum(r["sellerRevenueKopecks"] for day in result["dailyAggregates"].values() for r in day.values()) == expected
    assert result["rows"] == rows  # Retain evidence for other date selections.


def cache(monkeypatch, observations):
    monkeypatch.setattr(router, "list_source_cache_ranges_by_prefix", lambda *a, **kw: [
        dict(sourceKey=key, **{k: value[k] for k in ("dateFrom", "dateTo", "fetchedAt")})
        for key, value in observations.items()])
    monkeypatch.setattr(router, "get_source_cache", lambda org, key, **kw: observations.get(key))


def snapshot(start, end, fetched, rows):
    return dict(dateFrom=start, dateTo=end, fetchedAt=fetched, rows=rows, sourceComplete=True)


def test_adjacent_reports_preserve_late_sale_and_overlap_is_not_doubled(monkeypatch):
    first = trade(1, "2026-09-22", "2026-09-22", "100")
    late = trade(2, "2026-09-22", "2026-09-23", "50")
    cache(monkeypatch, {
        "finance_a": snapshot("2026-09-22", "2026-09-22", "2026-10-01", [first]),
        "finance_b": snapshot("2026-09-23", "2026-09-23", "2026-10-01", [late]),
        "finance_c": snapshot("2026-09-22", "2026-09-23", "2026-09-30", [first, late]),
    })
    result = router._finance_period_from_operations(2, dt("2026-09-22"), dt("2026-09-22"))
    assert net(result) == 15000
    assert result["coverageState"] == "ok"


def test_new_complete_empty_observation_removes_old_rows(monkeypatch):
    cache(monkeypatch, {
        "finance_old": snapshot("2026-09-22", "2026-09-22", "2026-09-30", [trade(1, "2026-09-22", "2026-09-22", "100")]),
        "finance_new": snapshot("2026-09-22", "2026-09-22", "2026-10-01", []),
    })
    result = router._finance_period_from_operations(2, dt("2026-09-22"), dt("2026-09-23"))
    assert net(result) == 0
    assert result["coverageState"] == "partial"
    assert result["missingDates"] == ["2026-09-23"]


def test_missing_seller_price_is_not_a_complete_zero(monkeypatch):
    cache(monkeypatch, {"finance_bad": snapshot("2026-09-22", "2026-09-22", "2026-10-01", [trade(1, "2026-09-22", "2026-09-22", None)])})
    assert router._finance_period_from_operations(2, dt("2026-09-22"), dt("2026-09-22")) == {}


def test_revenue_date_filter_preserves_original_payout_and_fees():
    row = trade(1, "2026-09-21", "2026-09-22", "1000", forPay="40", deliveryService="10", commissionPercent=10)
    result = aggregate_finance_report_rows([row], date_from=dt("2026-09-22"), date_to=dt("2026-09-22"))
    actual = result["aggregates"]["101"]
    assert actual["sellerRevenueKopecks"] == 0
    assert actual["buyerRevenueKopecks"] == 5000
    assert actual["payableKopecks"] == 4000
    assert actual["logisticsKopecks"] == 1000
    assert actual["commissionFormulaKopecks"] == 500


def test_empty_ledger_clears_stale_daily_revenue_and_preserves_fees(monkeypatch):
    base = dict(aggregates={"101": {"sellerRevenueKopecks": 10000, "logisticsKopecks": 500}},
                dailyAggregates={"2026-09-22": {"101": {"sellerRevenueKopecks": 10000, "logisticsKopecks": 500}}})
    monkeypatch.setattr(router, "_load_period_source_cache_base", lambda *a, **kw: base)
    cache(monkeypatch, {"finance_empty": snapshot("2026-09-22", "2026-09-22", "2026-10-01", [])})
    result = router._load_period_source_cache(2, "finance", "custom", 1, dt("2026-09-22"), dt("2026-09-22"))
    for row in [result["aggregates"]["101"], result["dailyAggregates"]["2026-09-22"]["101"]]:
        assert row["sellerRevenueKopecks"] == 0
        assert row["logisticsKopecks"] == 500


def test_partial_ledger_does_not_erase_other_observed_days(monkeypatch):
    base = dict(aggregates={"101": {"sellerRevenueKopecks": 15000}}, dailyAggregates={})
    monkeypatch.setattr(router, "_load_period_source_cache_base", lambda *a, **kw: base)
    cache(monkeypatch, {"finance_one_day": snapshot("2026-09-22", "2026-09-22", "2026-10-01", [trade(1, "2026-09-22", "2026-09-22", "100")])})
    result = router._load_period_source_cache(2, "finance", "custom", 2, dt("2026-09-22"), dt("2026-09-23"))
    assert result["aggregates"]["101"]["sellerRevenueKopecks"] == 15000
    assert result["revenueCoverage"]["coverageState"] == "partial"
