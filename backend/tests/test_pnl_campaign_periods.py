"""Real P&L cache reconstruction/worker planning, without provider or DB writes."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from celery.exceptions import Ignore

from app import wb_pnl_advertising as advertising, repricer_tasks as tasks
from app.repricer_page_finance import connection_key
from app.routers import wb_reports_bff as router
from tests.test_abc_pnl_reported_economics import build, trade, ledger, ads, START, END
from tests.test_report_refresh_handoff import runtime, run_as_worker, refresh_args


@pytest.fixture
def campaigns(monkeypatch):
    state = SimpleNamespace(rows=[], context=dict(connection=connection_key("synthetic"),
        marketplaceAccountId=1, bindingVersion=1, connectedAt="2026-01-01T00:00:00+00:00"))
    monkeypatch.setattr(advertising, "_source_context", lambda org: deepcopy(state.context))
    monkeypatch.setattr(advertising, "_candidate_caches", lambda *a: deepcopy(state.rows))
    return state


def weekly(*, cabinet=True):
    daily = {(START + timedelta(days=i)).isoformat(): {"101": {"adSpendKopecks": (i+1)*100}}
             for i in range(7)}
    source = ads({"101": {"adSpendKopecks": 2800}}, dailyAggregates=daily,
        totals={"adSpendKopecks": 2800})
    if cabinet:
        source["dailyCampaignTotals"] = {day: {"ad_spend_kopecks": row["101"]["adSpendKopecks"] + 10}
            for day, row in daily.items()}
        source["campaignTotals"] = {"ad_spend_kopecks": 2870}
    else:
        source.pop("campaignTotals")
    return source


@pytest.mark.parametrize("offset,days", [(6, 1), (2, 3)])
def test_subperiod_uses_selected_campaign_totals_not_weekly_total(campaigns, offset, days):
    campaigns.rows = [weekly()]
    start = START + timedelta(days=offset)
    end = start + timedelta(days=days-1)
    cache, result = advertising.load_campaign_spend(2, start, end)
    assert result["state"] == "ok"
    expected = sum((i+1)*100 for i in range(offset, offset+days))
    assert result["adSpendKopecks"] == expected + 10*days
    assert result["unallocatedSpendKopecks"] == 10*days
    assert advertising.product_spend(result, 101) == expected
    assert cache["dateFrom"] == str(start) and cache["dateTo"] == str(end)
    assert campaigns.rows[0]["totals"]["adSpendKopecks"] == 2800


def test_legacy_daily_products_are_retained_but_do_not_invent_a_cabinet_total(campaigns):
    campaigns.rows = [weekly(cabinet=False)]
    cache, result = advertising.load_campaign_spend(2, END, END)
    assert result["state"] == "partial" and result["productCoverageComplete"]
    assert advertising.product_spend(result, 101) == 700
    assert result["adSpendKopecks"] is None and result["unallocatedSpendKopecks"] is None
    assert advertising.product_spend(result, 999) is None
    assert advertising.missing_campaign_ranges(2, END, END) == [(END, END)]


def test_stitch_saved_days_preserves_provenance_and_owns_each_day_once(campaigns):
    first, second = weekly(), weekly()
    second.update(dateFrom="2026-10-15", dateTo="2026-10-21",
        fetchedAt=(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat())
    second["dailyAggregates"] = {"2026-10-18": {"101": {"adSpendKopecks": 900}},
        "2026-10-19": {"101": {"adSpendKopecks": 1000}}}
    second["dailyCampaignTotals"] = {day: {"ad_spend_kopecks": row["101"]["adSpendKopecks"]}
        for day,row in second["dailyAggregates"].items()}
    campaigns.rows = [first, second]
    start, end = date(2026,10,17), date(2026,10,19)
    cache,result = advertising.load_campaign_spend(2,start,end)
    assert cache["source"] == "wb_ads_fullstats" and result["state"] == "ok"
    assert advertising.product_spend(result,101) == 600+900+1000
    assert result["adSpendKopecks"] == 610+900+1000
    assert len(cache["dailyAggregates"]) == 3


@pytest.mark.parametrize("change", [dict(source="foreign"), dict(marketplaceAccountId=9),
    dict(fetchedAt="2025-12-31T00:00:00+00:00"), dict(pnlAdvertisingContext={"connection":"foreign"})])
def test_foreign_or_prebinding_observations_are_not_relabelled(campaigns,change):
    campaigns.rows = [weekly() | change]
    assert advertising.load_campaign_spend(2,START,END)[1]["state"] == "no_data"


@pytest.mark.parametrize("change", [dict(sourceComplete=False),dict(coveredDays=6,requestedDays=7),
    dict(missingDates=[str(END)]),dict(coverageState="partial")])
def test_incomplete_coverage_is_never_completed_by_stitching_itself(campaigns,change):
    campaigns.rows = [weekly() | change]
    result = advertising.load_campaign_spend(2,START,END)[1]
    assert result["state"] == "partial" and not result["productCoverageComplete"]
    assert advertising.missing_campaign_ranges(2,START,END) == [(START,END)]


def test_completed_empty_campaign_response_proves_zero(campaigns):
    campaigns.rows = [ads(campaignCount=0)]
    result=advertising.load_campaign_spend(2,START,END)[1]
    assert result["state"] == "ok" and advertising.product_spend(result,101) == 0
    assert advertising.missing_campaign_ranges(2,START,END,fresh_only=True) == []
    assert advertising.load_campaign_spend(2,END,END)[1]["adSpendKopecks"] == 0


def test_completed_collection_with_unknown_attribution_does_not_reload_forever(campaigns):
    campaigns.rows=[ads({"101":{"adSpendKopecks":None}},campaignTotals={"ad_spend_kopecks":500})]
    result=advertising.load_campaign_spend(2,START,END)[1]
    assert result["state"]=="partial" and result["collectionComplete"]
    assert advertising.product_spend(result,101) is None
    assert advertising.product_spend(result,None) is None
    assert advertising.missing_campaign_ranges(2,START,END,fresh_only=True)==[]


@pytest.mark.parametrize("difference", [-1, 1])
def test_explicit_campaign_rounding_does_not_hide_unadvertised_products(campaigns,difference):
    report=dict(rows=[dict(campaignId="1",adSpendKopecks=500+difference,attributionLevel="exact_sku")],
        performanceTotals={"ad_spend_kopecks":500+difference},
        dailyPerformanceTotals={str(END):{"ad_spend_kopecks":500+difference}},
        dailyRows=[dict(campaignId="1",date=str(END),skuId="101",adSpendKopecks=500)])
    evidence=advertising._rounding_attribution(report)
    assert evidence["pnlRoundingAttributionComplete"]
    source=ads({"101":{"adSpendKopecks":500}},campaignTotals={"ad_spend_kopecks":500+difference})
    source.update(evidence,dailyAggregates={str(END):{"101":{"adSpendKopecks":500}}},
        dailyCampaignTotals=report["dailyPerformanceTotals"])
    campaigns.rows=[source]
    for start,end in [(START,END),(END,END)]:
        result=advertising.load_campaign_spend(2,start,end)[1]
        assert advertising.product_spend(result,999)==0
        assert advertising.product_spend(result,101)==500
        assert result["adSpendKopecks"]==500+difference
        assert result["unallocatedSpendKopecks"]==difference  # Never allocate or discard it.


@pytest.mark.parametrize("change", [
    dict(root=600), dict(root=400), dict(level="campaign_only"),
    dict(sku=None), dict(value=None), dict(total=900),
])
def test_rounding_proof_rejects_unattributed_spend_unknowns_and_real_mismatches(change):
    report=dict(rows=[dict(campaignId="1",adSpendKopecks=change.get("root",501),
        attributionLevel=change.get("level","exact_sku"))],
        performanceTotals={"ad_spend_kopecks":change.get("total",change.get("root",501))},
        dailyRows=[dict(campaignId="1",date=str(END),skuId=change.get("sku","101"),
            adSpendKopecks=change.get("value",500))])
    assert advertising._rounding_attribution(report)=={}


@pytest.mark.parametrize("field", ["paidAcceptance", "penalty", "deduction"])
def test_missing_one_product_expense_does_not_erase_another_profit(build,field):
    inputs,run=build
    inputs["tax"]["102"]=dict(inputs["tax"]["101"])
    inputs["finance"]=ledger([trade(ppvzSalesCommission="3"),trade(2,nmId=102,ppvzSalesCommission="3")])
    before={r.nmId:r.netProfitKopecks for r in run().rows}
    inputs["finance"]=ledger([trade(ppvzSalesCommission="3"),trade(2,nmId=102,ppvzSalesCommission="3",**{field:None})])
    report=run(); rows={r.nmId:r for r in report.rows}
    assert rows[101].netProfitKopecks == before[101]
    assert rows[102].netProfitKopecks is None and rows[102].expenseState == "partial"
    assert report.totals.netProfitKopecks is None


def test_unknown_product_advertising_is_partial_without_erasing_known_rows(build):
    inputs,run=build
    inputs["finance"]=ledger([trade(ppvzSalesCommission="3"),trade(2,nmId=102,ppvzSalesCommission="3")])
    inputs["tax"]["102"]=dict(inputs["tax"]["101"])
    inputs["ads"]=ads({"101":{"adSpendKopecks":100},"102":{"adSpendKopecks":None}})
    report=run(); rows={r.nmId:r for r in report.rows}
    assert rows[101].netProfitKopecks is not None and rows[102].netProfitKopecks is None
    assert report.totals.netProfitKopecks is None and "WB-02" in report.blockerIds
    assert rows[102].comment and report.sourceStatus == "partial"


def test_actual_pnl_worker_plan_reaches_advertising_and_reuses_saved_range(runtime,monkeypatch):
    loaded=[]
    monkeypatch.setattr(tasks,"_publish_loading_report_snapshot",lambda *a,**kw:None)
    monkeypatch.setattr(advertising,"missing_campaign_ranges",lambda org,left,right,**kw: [] if loaded else [(left,right)])
    def collect(org,token,left,right,progress):
        loaded.append((org,left,right))
    monkeypatch.setattr(advertising,"collect_campaign_spend",collect)
    runtime.ready=True
    args=list(refresh_args("pnl")); args[6]="financial"; args[7]=True
    for _ in range(2):
        with pytest.raises(Ignore):
            run_as_worker(tasks.refresh_report_sources_for_org,tuple(args))
    assert loaded == [(1,date(2026,8,17),date(2026,8,23))]
    assert runtime.refreshes == []
    assert "ads" in tasks._report_source_refresh_plan("pnl")["sources"]
    assert len(runtime.queue) == 2  # both hand off to report build, not another WB read


def test_pnl_collector_saves_exact_context_and_reuses_it(campaigns,monkeypatch):
    from app.repricer_cache import store
    saved=[]; requests=[]
    def load(**kw):
        requests.append(kw)
        return SimpleNamespace(diagnostics={})
    report=dict(performanceComplete=True,performanceTotals={"ad_spend_kopecks":450},
        dailyPerformanceTotals={str(START):{"ad_spend_kopecks":450}},dailyRows=[
            dict(date=str(START),skuId="101",adSpendKopecks=100),
            dict(date=str(START),skuId="101",adSpendKopecks=300)])
    monkeypatch.setattr(router,"build_ads_attribution_snapshot",load)
    monkeypatch.setattr(router,"_map_ads_to_report_response",lambda *a:report)
    def save(org,key,payload,**kwargs):
        assert kwargs.get("strict") is True
        payload={**payload,"fetchedAt":datetime.now(timezone.utc).isoformat()}
        saved.append((org,key)); campaigns.rows.append(payload)
    monkeypatch.setattr(store,"save_source_cache",save)
    advertising.collect_campaign_spend(2,"synthetic",START,END,lambda *a:None)
    advertising.collect_campaign_spend(2,"synthetic",START,END,lambda *a:None)
    assert len(requests)==len(saved)==1
    assert requests[0]["date_from"]==START and requests[0]["date_to"]==END
    assert requests[0]["include_budgets"] is False
    assert saved[0][1].startswith("pnl_ads_")
    result=advertising.load_campaign_spend(2,START,END)[1]
    assert advertising.product_spend(result,101)==400
    assert result["unallocatedSpendKopecks"]==50


def test_collector_reconciles_original_campaign_roots_not_expanded_ads_ui_rows(campaigns,monkeypatch):
    from app.repricer_cache import store
    from app.wb_api.ads_runtime import AdsAttributionRow, AdsAttributionSnapshot
    metrics=dict(ad_spend_kopecks=499,impressions=100,clicks=10,cart_adds=2,orders_count=1,orders_kopecks=1000)
    snapshot=AdsAttributionSnapshot(source_status="fresh",confidence="high",blocker_ids=[],source_evidence=[],
        totals=metrics,performance_totals=metrics,
        rows=[AdsAttributionRow(campaign_id="1",sku_id=None,attribution_level="exact_sku",confidence="high",**metrics)],
        daily_rows=[dict(date=str(END),campaignId="1",skuId="101",appType=1,adSpendKopecks=500,
            impressions=100,clicks=10,cartAdds=2,ordersCount=1,ordersKopecks=1000)],
        daily_performance_totals={str(END):metrics},diagnostics={"summary":{"sourceComplete":True}})
    mapped=router._map_ads_to_report_response({"from":str(START),"to":str(END)},snapshot)
    assert len(mapped["rows"])==2  # SKU plus a preserved -1 kopeck residual.
    assert advertising._rounding_attribution(mapped)=={}
    monkeypatch.setattr(router,"build_ads_attribution_snapshot",lambda **kw:snapshot)
    def save(org,key,payload,**kwargs):
        campaigns.rows.append({**payload,"fetchedAt":datetime.now(timezone.utc).isoformat()})
    monkeypatch.setattr(store,"save_source_cache",save)
    advertising.collect_campaign_spend(2,"synthetic",START,END,lambda *a:None)
    result=advertising.load_campaign_spend(2,START,END)[1]
    assert advertising.product_spend(result,999)==0
    assert result["unallocatedSpendKopecks"]==-1


@pytest.mark.parametrize("code", [429,403])
def test_incomplete_provider_response_never_saves_zero_and_only_limits_retry(campaigns,monkeypatch,code):
    from app.repricer_cache import store
    snapshot=SimpleNamespace(diagnostics={"sources":[dict(status="blocked",statusCode=code,
        rateLimit={"retryAfterSeconds":70})]})
    monkeypatch.setattr(router,"build_ads_attribution_snapshot",lambda **kw:snapshot)
    monkeypatch.setattr(router,"_map_ads_to_report_response",lambda *a:{"performanceComplete":False})
    monkeypatch.setattr(store,"save_source_cache",lambda *a:pytest.fail("Incomplete data must not be certified"))
    with pytest.raises(tasks.ReportSourceRateLimited if code==429 else RuntimeError) as failure:
        advertising.collect_campaign_spend(2,"synthetic",START,END,lambda *a:None)
    if code==429:
        assert failure.value.delay==71
    assert advertising.load_campaign_spend(2,START,END)[1]["adSpendKopecks"] is None


def test_connection_change_during_collection_does_not_publish_to_new_account(campaigns,monkeypatch):
    from app.repricer_cache import store
    def load(**kw):
        campaigns.context["marketplaceAccountId"]=9
        return SimpleNamespace(diagnostics={})
    monkeypatch.setattr(router,"build_ads_attribution_snapshot",load)
    monkeypatch.setattr(router,"_map_ads_to_report_response",lambda *a:dict(
        performanceComplete=True,performanceTotals={"ad_spend_kopecks":0}))
    monkeypatch.setattr(store,"save_source_cache",lambda *a:pytest.fail("Stale account response was published"))
    with pytest.raises(RuntimeError,match="connection changed"):
        advertising.collect_campaign_spend(2,"synthetic",START,END,lambda *a:None)


def test_performance_only_loader_proves_empty_campaigns_without_optional_reads(monkeypatch):
    from app.wb_api import ads_runtime
    from app.wb_api.client import FakeWbApiClient
    fake=FakeWbApiClient(fixtures={"/adv/v1/promotion/count":{"all":0,"adverts":[]}})
    monkeypatch.setattr(ads_runtime,"build_wb_ads_client",lambda *a,**kw:fake)
    result=ads_runtime.build_ads_attribution_snapshot(date_from=START,date_to=END,
        group_by="campaign",include_budgets=False,performance_only=True)
    assert result.performance_totals["ad_spend_kopecks"]==0
    assert result.diagnostics["summary"]["sourceComplete"] is True
    assert len(result.daily_performance_totals)==7
    assert [r.path for r in fake.requests]==["/adv/v1/promotion/count"]
