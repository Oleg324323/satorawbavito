"""Week-only semantic regressions; no provider calls or live database required."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone

import pytest

from app import wb_week_sources as week, wb_week_finance as finance

START, END = date(2026, 10, 7), date(2026, 10, 11)
PRIOR_START, PRIOR_END = date(2026, 10, 2), date(2026, 10, 6)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    week._load_funnel.cache_clear()
    monkeypatch.setattr(week, "_connection", lambda org: "synthetic")
    monkeypatch.setattr(week, "_facts", lambda *a: [])
    monkeypatch.setattr(week, "source_revision", lambda *a: "synthetic-revision")
    monkeypatch.setattr(week.store, "get_source_cache", lambda *a, **kw: {})
    monkeypatch.setattr(week.store, "list_cached_goods", lambda org: [])
    monkeypatch.setattr(week.store, "cached_goods_meta", lambda org: {})
    monkeypatch.setattr(week.store, "list_source_cache_ranges_by_prefix", lambda *a, **kw: [])


def fact(nm="101", **changes):
    return dict(nmId=nm, visits=100, carts=20, orders=10, orderAmount=50000,
        buyouts=3, buyoutAmount=15000, cancels=1, dateFrom=str(START), dateTo=str(END),
        fetchedAt=datetime.now(timezone.utc).isoformat()) | changes


def snapshot(**units):
    return {"aggregates": {nm: {"wbStockUnits": quantity, "inWayToClient": 1000, "inWayFromClient": 500}
                           for nm, quantity in units.items()}}


def ledger(daily, **extra):
    return dict(sourceComplete=True, amounts={"0": {"storageKopecks": sum(daily.values())}},
                storageDailyKopecks=daily, unknown={}) | extra


def test_order_cohort_mapping_amounts_missing_zero_and_disjoint_cache_reuse(monkeypatch):
    rows = [fact(orders=10, buyouts=3), fact(orders=12, buyouts=4),
        fact("102", carts=0, orders=0, orderAmount=0, buyouts=None, buyoutAmount=None)]
    calls = []
    monkeypatch.setattr(week, "_facts", lambda org, conn, start, end: calls.append((org, start, end)) or rows)
    result = week.load_funnel(7, START, END)
    assert calls == [(7, START, END)]
    assert result["metrics"]["101"] == dict(cartCount=20, orderCount=12, orderSumKopecks=50000,
        buyoutCount=4, buyoutSumKopecks=15000)  # latest exact observation, not both totals
    assert result["metrics"]["102"]["cartCount"] == 0
    assert result["metrics"]["102"]["buyoutCount"] is None
    assert result["complete"] and not result["needsCollection"]  # buyouts no longer depend on this source


def test_funnel_never_prorates_another_period_or_substitutes_financial_sales(monkeypatch):
    monkeypatch.setattr(week, "_facts", lambda *a: [fact(dateFrom="2026-10-01", orders=999)])
    result = week.load_funnel(7, START, END)
    assert result["metrics"]["101"]["orderCount"] is None
    assert not result["complete"]


def test_funnel_reuses_projection_and_invalidates_corrected_observations(monkeypatch):
    calls, revision = [], ["first"]
    rows = [fact()]
    monkeypatch.setattr(week, "source_revision", lambda *a: revision[0])
    monkeypatch.setattr(week, "_facts", lambda *a: calls.append(a) or rows)
    first = week.load_funnel(7, START, END)
    first["metrics"]["101"]["orderCount"] = 999  # caller cannot mutate cached evidence
    assert week.load_funnel(7, START, END)["metrics"]["101"]["orderCount"] == 10
    assert len(calls) == 1
    rows[:] = [fact(orders=12)]
    revision[0] = "corrected"
    assert week.load_funnel(7, START, END)["metrics"]["101"]["orderCount"] == 12
    assert len(calls) == 2


def test_funnel_collects_only_missing_dates_and_reuses_contained_receipts(monkeypatch):
    rows = [fact(dateTo="2026-10-09")]
    monkeypatch.setattr(week, "_facts", lambda *a: rows)
    assert week.load_funnel(7, START, END)["missingRanges"] == [(date(2026, 10, 10), END)]
    receipt = dict(sourceComplete=True, dateFrom="2026-10-10", dateTo=str(END),
        fetchedAt=datetime.now(timezone.utc).isoformat(), aggregates={"101": fact(orders=2)})
    key = "week_funnel_synthetic_2026-10-10_2026-10-11"
    monkeypatch.setattr(week.store, "list_source_cache_ranges_by_prefix", lambda *a, **kw:
        [{"sourceKey": key, "dateFrom": "2026-10-10", "dateTo": str(END)}])
    monkeypatch.setattr(week.store, "get_source_cache", lambda org, source, **kw: receipt if source == key else {})
    week._load_funnel.cache_clear()
    result = week.load_funnel(7, START, END)
    assert result["metrics"]["101"]["orderCount"] == 12
    assert result["complete"] and not result["needsCollection"] and result["missingRanges"] == []


def test_fresh_completed_receipt_does_not_loop_for_wb_omitted_products(monkeypatch):
    cache = dict(sourceComplete=True, aggregates={"101": fact()}, dateFrom=str(START), dateTo=str(END),
                 fetchedAt=datetime.now(timezone.utc).isoformat())
    monkeypatch.setattr(week.store, "get_source_cache", lambda org, key, **kw: cache if key.startswith("week_funnel_") else {})
    monkeypatch.setattr(week.store, "list_cached_goods", lambda org: [{"nmID": 101}, {"nmID": 102}])
    result = week.load_funnel(7, START, END)
    assert not result["complete"] and not result["needsCollection"]
    assert "102" not in result["metrics"]  # not a fabricated zero


def test_collector_paginates_preserves_null_and_checks_period_and_duplicates(monkeypatch):
    from app import repricer_bff as bff
    requests, saved = [], []
    def product(nm, **changes):
        return {"product": {"nmId": nm}, "statistic": {"selected": dict(
            period={"start": str(START), "end": str(END)}, cartCount=0, orderCount=2,
            orderSum="10.23", buyoutCount=None) | changes}}
    replies = [{"currency": "RUB", "products": [product(nm) for nm in range(1, 1001)]},
               {"currency": "RUB", "products": [product(1001)]}]
    monkeypatch.setattr(bff, "_request_or_raise_sales_funnel_products", lambda client, request: requests.append(request) or replies.pop(0))
    monkeypatch.setattr(week.store, "save_source_cache", lambda org, key, value, **kw: saved.append(value) or value)
    result = week.collect_funnel(7, "synthetic", START, END)
    assert [r.jsonBody["offset"] for r in requests] == [0, 1000]
    assert all(r.jsonBody["nmIds"] == [] for r in requests)
    assert len(result["aggregates"]) == 1001
    assert result["aggregates"]["1"]["buyouts"] is None
    assert result["aggregates"]["1"]["orderAmount"] == 1023
    replies[:] = [{"currency": "RUB", "products": [product(1), product(1)]}]
    with pytest.raises(ValueError, match="PAGINATION_INVALID"):
        week.collect_funnel(7, "synthetic", START, END)
    assert len(saved) == 1  # failed pages never certify completeness
    replies[:] = [{"currency": "RUB", "products": [product(1, period={"start": "2026-10-01", "end": str(END)})]}]
    with pytest.raises(ValueError, match="PERIOD_MISMATCH"):
        week.collect_funnel(7, "synthetic", START, END)


def test_commission_uses_consistent_retail_settlement_basis_not_reported_pre_agent_value():
    operation = dict(rrdId=1, nmId=101, rrDate=str(START), docTypeName="Продажа", sellerOperName="Продажа",
        quantity=2, retailAmount="180", retailPriceWithDisc="100", forPay="150", acquiringFee="5", ppvzSalesCommission="999",
        deliveryService="12.34", paidStorage="0", paidAcceptance="0", penalty="0")
    refund = operation | dict(rrdId=2, docTypeName="Возврат", sellerOperName="Возврат",
        quantity=1, retailAmount="90", forPay="75", acquiringFee="2.5", deliveryService="-3")
    cache = finance.project_operations([operation, deepcopy(operation), refund], START, END)
    assert cache["amounts"]["101"]["commissionKopecks"] == 1250
    assert cache["amounts"]["101"]["logisticsKopecks"] == 934  # unchanged deliveryService/sign semantics
    del operation["forPay"]
    assert "commissionKopecks" in finance.project_operations([operation], START, END)["unknown"]["101"]


def test_daily_storage_all_account_inventory_changes_signed_corrections_and_kopeck_reconciliation():
    days = {str(START): 101, str(START + timedelta(days=1)): -11}
    histories = {str(START): snapshot(**{"101": 1, "102": 1, "103": 1}),
                 str(START + timedelta(days=1)): snapshot(**{"101": 0, "102": 1, "103": 1})}
    result = week.storage_allocation(ledger(days), histories, START, END)
    assert result["amounts"] == {"101": 34, "102": 28, "103": 28}
    assert sum(result["amounts"].values()) + result["unallocatedKopecks"] == 90
    assert result["complete"]
    assert week.allocate_kopecks(-101, {"103": 1, "102": 1, "101": 1}) == ({"103": -33, "102": -34, "101": -34}, 0)


def test_storage_known_days_only_with_exact_dates_and_unallocated_missing_history():
    daily = {str(START): 10000, str(END): 3000}
    result = week.storage_allocation(ledger(daily), {str(START): snapshot(**{"101": 50, "102": 50})}, START, END)
    assert result["amounts"] == {"101": 5000, "102": 5000}
    assert result["unallocatedKopecks"] == 3000
    assert result["observedDates"] == [str(START)] and result["missingDates"] == [str(END)]
    assert not result["complete"]


@pytest.mark.parametrize("history", [{}, snapshot(**{"101": 0}), snapshot(**{"101": None})])
def test_storage_missing_or_zero_denominator_never_uses_in_way_goods(history):
    result = week.storage_allocation(ledger({str(START): 100}), {str(START): history}, START, END)
    assert result["amounts"] == {} and result["unallocatedKopecks"] == 100 and not result["complete"]


def test_period_storage_unit_days_complete_history_and_missing_finance():
    end = START + timedelta(days=1)
    cache = dict(sourceComplete=True, amounts={"0": {"storageKopecks": 100}}, unknown={})
    histories = {str(START): snapshot(**{"101": 1, "102": 3}), str(end): snapshot(**{"101": 3, "102": 1})}
    assert week.storage_allocation(cache, histories, START, end)["amounts"] == {"101": 50, "102": 50}
    assert week.storage_allocation(cache, {}, START, end)["unallocatedKopecks"] == 100
    assert week.storage_allocation(cache | {"sourceComplete": False}, histories, START, end)["amounts"] == {}


def test_product_ads_multiple_campaigns_and_unallocated_without_settlement_double_count(monkeypatch):
    from app.wb_report_ads import save_campaign_performance
    from app import wb_reports_sprint_d as reports
    saved = []
    monkeypatch.setattr("app.wb_report_ads.save_source_cache", lambda org, key, value: saved.append(value))
    rows = [dict(date=str(START), skuId=101, campaignId=1, adSpendKopecks=100),
            dict(date=str(START), skuId=102, campaignId=1, adSpendKopecks=200),
            dict(date=str(END), skuId=101, campaignId=2, adSpendKopecks=300)]
    assert save_campaign_performance(7, START, END, dict(performanceComplete=True, dailyRows=rows,
        performanceTotals={"ad_spend_kopecks": 650}))
    monkeypatch.setattr(reports, "_period_cache", lambda *a: saved[0])
    assert week.load_ads(7, START, END) == ({"101": 400, "102": 200}, True, 50)
    assert week.load_ads(7, PRIOR_START, PRIOR_END) == ({}, False, None)
    saved[0]["aggregates"]["101"]["adSpendKopecks"] = None
    assert week.load_ads(7, START, END)[0]["101"] is None


def test_attach_corrects_both_periods_adds_inventory_only_products_without_touching_logistics_profit(monkeypatch):
    monkeypatch.setattr('app.wb_storage.load_stock', lambda org: snapshot(**{'101': 50, '102': 50}) | {'fetchedAt': '2026-11-01T00:00:00Z'})
    by_period = {START: {"101": dict(cartCount=20, orderCount=12, orderSumKopecks=60000, buyoutCount=3, buyoutSumKopecks=15000)},
                 PRIOR_START: {"101": dict(cartCount=10, orderCount=6, orderSumKopecks=30000, buyoutCount=2, buyoutSumKopecks=10000)}}
    monkeypatch.setattr(week, "load_funnel", lambda org, left, right: dict(metrics=by_period[left], complete=True, missingDates=[]))
    monkeypatch.setattr(week, "load_ads", lambda org, left, right: ({"101": 500 if left == START else 250}, True, 0))
    monkeypatch.setattr(finance, "load_finance", lambda org, left, right: ledger({str(left): 10000},
        buyouts={"101": {"units": 11 if left == START else 5, "kopecks": 55000 if left == START else 25000}}))
    monkeypatch.setattr(week.store, "get_source_cache", lambda org, key, **kw: snapshot(**{"101": 50, "102": 50}) if key.endswith((str(START), str(PRIOR_START))) else {})
    report = dict(rows=[dict(nmId=101, orders={"units": 999}, sales={"units": 34},
        logisticsKopecks={"kopecks": 123}, adSpendKopecks={"kopecks": 789}, profit={"kopecks": 456})], unallocatedFinancials={"selected": {}, "previous": {}})
    result = week.attach(report, 7, START, END, PRIOR_START, PRIOR_END, finance_allowed=True,
                         catalog={102: {"productName": "No sales, real inventory"}})
    row = result["rows"][0]
    assert row["orders"]["units"] == 12 and row["orders"]["previousUnits"] == 6 and row["orders"]["deltaPct"] == 100
    assert row["sales"] == dict(units=11, previousUnits=5, deltaPct=120.0, kopecks=55000, previousKopecks=25000)
    assert row["baskets"]["units"] == 20 and row["baskets"]["deltaPct"] == 100
    assert row["storageKopecks"]["kopecks"] == 5000 and row["campaignAdSpendKopecks"]["kopecks"] == 500
    assert row["adSpendKopecks"]["kopecks"] == 789  # Finance must not be overwritten by campaign activity.
    assert row["logisticsKopecks"] == {"kopecks": 123} and row["profit"] == {"kopecks": 456}
    assert result["rows"][1]["storageKopecks"]["kopecks"] == 5000
    assert result["rows"][1]["orders"]["units"] is None
    assert result["storageAllocation"]["selected"]["basis"] == "current_wb_stock_units"
    assert result["storageAllocation"]["previous"]["complete"] is True


def test_readiness_checks_the_two_exact_weeks_not_one_combined_funnel_interval(monkeypatch):
    from app.routers import wb_reports_bff as reports
    calls = []
    monkeypatch.setattr(week, "load_funnel", lambda org, left, right: calls.append((left, right)) or {"needsCollection": False})
    assert reports._report_daily_sources_ready(7, ("baskets-summary",), date_from=PRIOR_START,
        date_to=END, selected_date_from=START, report_id="week-over-week")[0]
    assert calls == [(PRIOR_START, PRIOR_END), (START, END)]


def test_operational_corrections_invalidate_week_report_cache(monkeypatch):
    from app.routers import wb_reports_bff as reports
    monkeypatch.setattr(reports, "legacy_finance_tax_revision", lambda org: "tax")
    cache = dict(taxRevision="tax", completedAt=datetime.now(timezone.utc).isoformat(), report=dict(cacheVersion=reports.WEEK_OVER_WEEK_REPORT_PAYLOAD_VERSION,
        operationalSourceRevision="old", rows=[{"orders": {"units": 1}}]))
    assert not reports._report_payload_cache_is_usable("week-over-week", cache, organization_id=7)
    cache["report"]["operationalSourceRevision"] = "synthetic-revision"
    assert reports._report_payload_cache_is_usable("week-over-week", cache, organization_id=7)
