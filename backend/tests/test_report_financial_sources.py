"""Reports must reconcile sources without changing the repricer projection."""
from datetime import date, datetime, timezone
from types import SimpleNamespace

from app import wb_reports_sprint_d as service
from app.routers import wb_reports_bff as reports
from app.repricer_page_finance import VERSION
from app.repricer_bff import FINANCE_REVENUE_BASIS, FINANCE_SCHEMA_VERSION

START, END = date(2026, 9, 9), date(2026, 9, 16)


def test_report_projection_cache_is_source_scoped_and_invalidates(monkeypatch):
    from app import wb_report_finance as finance
    from app import repricer_page_finance as raw
    finance._projections.clear()
    observed = [{"sourceKey": "synthetic-raw", "fetchedAt": "one"}]
    calls = []
    monkeypatch.setattr("app.cabinet.store.get_organization_wb_token_secret", lambda org: None)
    monkeypatch.setattr("app.repricer_cache.store.list_source_cache_ranges_by_prefix", lambda *a, **kw: observed)
    def load(*args, **kwargs):
        assert raw.page_connection.get() == "synthetic"
        calls.append(args)
        return {"aggregates": {"101": {"salesKopecks": 10}}}
    monkeypatch.setattr(raw, "load_page_finance", load)
    marker = raw.page_connection.set("synthetic")
    try:
        first = finance.load_report_finance(1, START, END)
        first["aggregates"]["101"]["salesKopecks"] = 999
        assert finance.load_report_finance(1, START, END)["aggregates"]["101"]["salesKopecks"] == 10
        assert len(calls) == 1
        observed[0]["fetchedAt"] = "two"
        finance.load_report_finance(1, START, END)
        finance.load_report_finance(2, START, END)
        assert len(calls) == 3
    finally:
        raw.page_connection.reset(marker)
        finance._projections.clear()


def test_unknown_campaign_contribution_stays_unknown_per_metric():
    from app.wb_api.ads_runtime import _merge_known_metrics
    totals = {}
    _merge_known_metrics(totals, {"clicks": 5, "ad_spend_kopecks": 40})
    _merge_known_metrics(totals, {"clicks": None, "ad_spend_kopecks": 60})
    assert totals == {"clicks": None, "ad_spend_kopecks": 100}


def test_campaign_subperiod_uses_daily_root_not_whole_period():
    first, second = "2026-09-09", "2026-09-10"
    cache = dict(dateFrom=first, dateTo=second, sourceComplete=True,
                 campaignTotals={"ad_spend_kopecks": 100},
                 dailyCampaignTotals={first: {"ad_spend_kopecks": 40}, second: {"ad_spend_kopecks": 60}},
                 dailyAggregates={first: {"101": {"adSpendKopecks": 35}, "campaign-unallocated": {"adSpendKopecks": 5}},
                                  second: {"101": {"adSpendKopecks": 60}}}, aggregates={"101": {"adSpendKopecks": 95}})
    selected = service._covered_cache_from_daily(cache, prefix="ads", suffix=first,
        resolved_days=1, date_from=START, date_to=START)
    assert selected["campaignTotals"]["ad_spend_kopecks"] == 40
    assert selected["aggregates"]["campaign-unallocated"]["adSpendKopecks"] == 5
    assert selected["sourceComplete"] is True
    cache["campaignTotals"]["clicks"] = None
    cache["dailyCampaignTotals"][first]["clicks"] = None
    selected = service._covered_cache_from_daily(cache, prefix="ads", suffix=first,
        resolved_days=1, date_from=START, date_to=START)
    assert selected["campaignTotals"]["clicks"] is None
    assert selected["campaignTotals"]["ad_spend_kopecks"] == 40
    cache.pop("dailyCampaignTotals")
    legacy = service._covered_cache_from_daily(cache, prefix="ads", suffix=first,
        resolved_days=1, date_from=START, date_to=START)
    assert "campaignTotals" not in legacy
    assert legacy["sourceComplete"] is False


def test_financial_reports_reuse_current_connection_raw_projection(monkeypatch):
    monkeypatch.setattr(service, "list_source_cache_ranges_by_prefix", lambda *a, **k: [{"sourceKey": "raw"}])
    expected = {"source": "finance_sales_reports_detailed", "coveredDays": 8, "sourceComplete": True, "aggregates": {"101": {"commissionKopecks": 123}}}
    monkeypatch.setattr("app.wb_report_finance.load_report_finance", lambda *a, **kw: expected)
    assert service._period_cache(1, "finance", START, END) is expected


def test_complete_projection_recognizes_empty_days_not_operation_count():
    cache = dict(projectionVersion=VERSION, revenueBasis=FINANCE_REVENUE_BASIS,
                 financeSchemaVersion=FINANCE_SCHEMA_VERSION, sourceComplete=True,
                 dateFrom=str(START), dateTo=str(END), missingDates=[])
    from app.repricer_page_finance import REALIZATION_VERSION
    cache.update(realizationVersion=REALIZATION_VERSION, realizationMoneyBasis="retailAmount", realizationDateBasis="rrDate")
    assert service._complete_finance_ledger(cache, START, END)
    assert not service._complete_finance_ledger({**cache, "sourceComplete": False}, START, END)


def test_pnl_internal_cache_invalidates_on_finance_or_ads_update(monkeypatch):
    observations = [{"sourceKey": "finance-period", "fetchedAt": "one", "dateFrom": str(START), "dateTo": str(END)}]
    monkeypatch.setattr(service, "list_source_cache_ranges_by_prefix", lambda *a, **kw: observations)
    revision = service._pnl_source_revision(1, START, END)
    cached = {"taxRevision": "tax", "sourceRevision": revision,
              "fetchedAt": datetime.now(timezone.utc).isoformat(),
              "report": {"period": {"dateFrom": str(START), "dateTo": str(END)}}}
    monkeypatch.setattr(service, "get_source_cache", lambda *a, **kw: cached)
    monkeypatch.setattr(service.PnlReportResponse, "model_validate", lambda r: "parsed")
    assert service._get_cached_pnl_response(organization_id=1, cache_key="cache", tax_revision="tax") == "parsed"
    observations[0]["fetchedAt"] = "two"
    assert service._get_cached_pnl_response(organization_id=1, cache_key="cache", tax_revision="tax") is None


def test_projected_commission_does_not_fall_back_to_percentage():
    assert service._finance_commission_kopecks({"commissionKopecks": 123,
        "commissionFormulaKopecks": 999, "reportedCommissionRows": 0,
        "commissionSource": "sellerRevenueKopecks-tradePayableKopecks-acquiringKopecks"}) is None


def test_funnel_buyout_ratio_excludes_orders_still_in_transit():
    totals = reports._funnel_rows_totals([dict(orderCount=4301, buyoutCount=691, cancelCount=660)])
    assert totals["buyoutPct"] == 51.1


def test_rnp_missing_ads_is_not_zero(monkeypatch):
    row = SimpleNamespace(model_dump=lambda **kw: dict(nmId=101, openCount=1, adSpendKopecks=None))
    payload = SimpleNamespace(rows=[row], adSpendKopecks=0, sourceStatus="partial", groupBy="sku",
                              formulaNotes=[], blockerIds=[], diagnostics={},
                              sourceEvidence=[], adsSourceStatus="blocked")
    response = reports._map_rnp_to_report_response(payload, {"from": str(START), "to": str(END)})
    kpis = {k["id"]: k["value"] for k in response["kpis"]}
    assert kpis["ad_spend"] == kpis["tacoo"] == "—"


def test_report_credits_reconcile_without_changing_repricer_projection():
    from app.wb_report_finance import project_report_operations
    from app.repricer_page_finance import project_operations
    sale = dict(rrdId=1, nmId=101, docTypeName="Продажа", sellerOperName="Продажа",
        quantity=1, retailAmount="80", retailPriceWithDisc="100", forPay="60",
        acquiringFee="2", saleDt=str(START), rrDate=str(START))
    compensation = dict(sale, rrdId=2, quantity=0, retailAmount="0", retailPriceWithDisc="0",
        forPay="10", acquiringFee="0", sellerOperName="Добровольная компенсация при возврате")
    correction = dict(compensation, rrdId=3, forPay="5", sellerOperName="Коррекция продаж")
    rows = [sale, compensation, correction, correction]
    baseline = project_operations(rows, START, END)["aggregates"]["101"]
    result = project_report_operations(rows, START, END)
    report = result["aggregates"]["101"]
    assert baseline["sellerRevenueKopecks"] == 10000
    assert baseline["commissionKopecks"] == 3800
    assert report["sellerRevenueKopecks"] == report["grossSalesKopecks"] == 10000
    assert report["realizationRevenueKopecks"] == 8000
    assert report["commissionKopecks"] == 1800
    assert report["additionalPaymentKopecks"] == 1500
    assert report["realizationRevenueKopecks"] - report["commissionKopecks"] - report["acquiringKopecks"] + report["additionalPaymentKopecks"] == 7500
    assert result["dailyAggregates"][str(START)]["101"]["realizationRevenueKopecks"] == 8000


def test_zero_correction_does_not_create_phantom_sku():
    from app.wb_report_finance import project_report_operations
    row = dict(rrdId=1, nmId=101, docTypeName="Продажа", sellerOperName="Коррекция продаж",
               quantity=0, forPay="0", saleDt=str(START), rrDate=str(START))
    assert project_report_operations([row], START, END)["aggregates"] == {}


def test_ads_retry_is_bounded_and_respects_provider_delay(monkeypatch):
    from app.wb_api import ads_runtime
    from app.wb_api.report_reads import active_report_read
    responses = iter([SimpleNamespace(ok=False, statusCode=429, rateLimit=SimpleNamespace(retryAfterSeconds=3)),
                      SimpleNamespace(ok=True, statusCode=200)])
    client = SimpleNamespace(request=lambda request: next(responses))
    waits, checks = [], []
    marker = active_report_read.set(SimpleNamespace(check=lambda: checks.append(True)))
    monkeypatch.setattr(ads_runtime.time, "sleep", waits.append)
    try:
        assert ads_runtime._request_with_retry(client, object(), min_interval_s=5, max_attempts=2).ok
    finally:
        active_report_read.reset(marker)
    assert sum(waits) == 5
    assert len(checks) == 5


def test_partial_current_realization_replaces_incompatible_legacy_cache(monkeypatch):
    monkeypatch.setattr(service, "list_source_cache_ranges_by_prefix", lambda *a, **kw: [{"sourceKey": "raw"}])
    partial = dict(source="finance_sales_reports_detailed", sourceComplete=False, coveredDays=1,
                   aggregates={"101": {"realizationRevenueKopecks": 100}})
    monkeypatch.setattr("app.wb_report_finance.load_report_finance", lambda *a, **kw: partial)
    cached = dict(revenueBasis=FINANCE_REVENUE_BASIS, financeSchemaVersion=FINANCE_SCHEMA_VERSION,
                  sourceComplete=True, aggregates={"101": {"sellerRevenueKopecks": 1000}})
    monkeypatch.setattr(service, "get_source_cache", lambda *a, **kw: cached)
    assert service._period_cache(1, "finance", START, END) is partial


def test_campaign_bridge_preserves_unallocated_spend_and_true_zero(monkeypatch):
    from app import wb_report_ads
    saved = []
    monkeypatch.setattr(wb_report_ads, "save_source_cache", lambda org, key, value: saved.append(value))
    report = dict(performanceComplete=True, performanceTotals={"ad_spend_kopecks": 150},
                  dailyRows=[dict(date=str(START), skuId="101", adSpendKopecks=100, clicks=None),
                             dict(date=str(START), skuId=None, adSpendKopecks=50)])
    assert wb_report_ads.save_campaign_performance(1, START, END, report)
    assert saved[0]["aggregates"]["101"]["clicks"] is None
    assert saved[0]["aggregates"]["campaign-unallocated"]["adSpendKopecks"] == 50
    assert saved[0]["campaignTotals"]["ad_spend_kopecks"] == 150
    assert len(saved[0]["dailyAggregates"]) == 8
    assert not wb_report_ads.save_campaign_performance(1, START, END, {**report, "performanceComplete": False})
    assert wb_report_ads.save_campaign_performance(1, START, END, dict(performanceComplete=True,
        performanceTotals={"ad_spend_kopecks": 0}, dailyRows=[]))
    monkeypatch.setattr(reports, "_period_cache", lambda *a, **k: saved[-1])
    snapshot = reports.build_cached_ads_attribution_snapshot(organization_id=1, date_from=START, date_to=END, group_by="campaign")
    assert snapshot.source_status == "cached" and snapshot.totals["ad_spend_kopecks"] == 0


def test_cached_ads_keeps_unknown_metrics_and_campaign_total(monkeypatch):
    monkeypatch.setattr(reports, "_period_cache", lambda *a, **k: dict(sourceComplete=True,
        campaignTotals={"ad_spend_kopecks": 150}, aggregates={"101": dict(nmId=101, adSpendKopecks=100, clicks=None)}))
    snapshot = reports.build_cached_ads_attribution_snapshot(organization_id=1, date_from=START, date_to=END, group_by="campaign")
    assert snapshot.totals["ad_spend_kopecks"] == 150
    assert snapshot.rows[0].clicks is None


def test_expenses_missing_source_is_neutral_not_zero():
    result = reports._map_cash_flow_to_expenses_response({"status": "not_loaded"},
        {"from": str(START), "to": str(END)}, "sku")
    assert {item["id"]: item["value"] for item in result["kpis"]}["operational_expenses"] == "—"


def test_funnel_daily_buyouts_do_not_use_financial_buyer_payments():
    day = str(START)
    result = reports._build_funnel_weekly_balance({"from": day, "to": day},
        dict(dailyRows={day: [dict(orderCount=4, buyoutCount=1, cancelCount=1, buyoutSumKopecks=100)]},
             financeDailyRows={day: {"101": dict(salesUnits=99, buyerRevenueKopecks=999999)}}))
    point = result["points"][0]
    assert point["salesUnits"] == 1 and point["salesKopecks"] == 100
    assert point["buyoutPct"] == 50
