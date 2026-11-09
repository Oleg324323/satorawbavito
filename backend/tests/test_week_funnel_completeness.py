"""Week-only coverage regressions; isolated observations, no database/provider."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone

import pytest

from app import wb_week_sources as week

START, END = date(2026, 10, 12), date(2026, 10, 18)


def fact(nm='684820752', *, start=START, end=END, stale=False, **values):
    return dict(nmId=nm, dateFrom=str(start), dateTo=str(end),
        fetchedAt=(datetime.now(timezone.utc)-timedelta(days=2 if stale else 0)).isoformat(),
        visits=100, carts=297, orders=52, orderAmount=9360000,
        buyouts=None, buyoutAmount=None, cancels=None) | values


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    week._load_funnel.cache_clear()
    monkeypatch.setattr(week, '_connection', lambda org: f'connection-{org}')
    monkeypatch.setattr(week, 'source_revision', lambda *args: 'fixed')
    monkeypatch.setattr(week, '_facts', lambda *args: [])
    monkeypatch.setattr(week.store, 'get_source_cache', lambda *args, **kwargs: {})
    monkeypatch.setattr(week.store, 'list_source_cache_ranges_by_prefix', lambda *args, **kwargs: [])
    monkeypatch.setattr(week.store, 'list_cached_goods', lambda org: [])


def load(monkeypatch, facts):
    monkeypatch.setattr(week, '_facts', lambda *args: deepcopy(facts))
    return week.load_funnel(7, START, END)


def test_stale_complete_week_survives_new_partial_day(monkeypatch):
    rows = [fact(stale=True), fact(end=START, carts=48, orders=8, orderAmount=1440000)]
    original = deepcopy(rows)
    result = load(monkeypatch, rows)
    assert result['metrics']['684820752']['orderCount'] == 52
    assert result['metrics']['684820752']['cartCount'] == 297
    assert result['metrics']['684820752']['orderSumKopecks'] == 9360000
    assert result['dataComplete']
    assert not result['complete'] and result['needsCollection']
    assert rows == original


def test_complete_disjoint_replacement_is_used_without_adding_the_old_week(monkeypatch):
    result = load(monkeypatch, [fact(stale=True),
        fact(end=START, carts=48, orders=8, orderAmount=1440000),
        fact(start=START+timedelta(days=1), carts=249, orders=44, orderAmount=7920000)])
    assert result['metrics']['684820752']['orderCount'] == 52
    assert result['metrics']['684820752']['cartCount'] == 297
    assert result['complete'] and not result['needsCollection']


def test_complete_cover_wins_over_fresh_overlapping_longer_fragment(monkeypatch):
    result = load(monkeypatch, [
        fact(start=START+timedelta(days=1), end=END-timedelta(days=1), orders=999),
        fact(end=START+timedelta(days=2), orders=20),
        fact(start=START+timedelta(days=3), orders=32)])
    assert result['metrics']['684820752']['orderCount'] == 52
    assert result['complete']


def test_overlapping_partial_totals_are_not_summed_or_prorated(monkeypatch):
    result = load(monkeypatch, [fact(end=END-timedelta(days=1)), fact(start=START+timedelta(days=1))])
    assert result['metrics']['684820752']['orderCount'] is None
    assert not result['dataComplete'] and result['needsCollection']


def test_missing_metric_is_not_zero_and_explains_no_repeat_of_complete_response(monkeypatch):
    receipt = dict(sourceComplete=True,dateFrom=str(START),dateTo=str(END),
        fetchedAt=datetime.now(timezone.utc).isoformat(),aggregates={'684820752':fact(carts=None)})
    monkeypatch.setattr(week.store, 'get_source_cache', lambda *args, **kwargs: receipt)
    result = week.load_funnel(7,START,END)
    assert result['metrics']['684820752']['orderCount'] == 52
    assert result['metrics']['684820752']['cartCount'] is None
    assert not result['needsCollection']
    assert result['unresolvedReasons']['source_fields_missing'] == 1
    assert not result['dataComplete']


def test_completed_seller_population_does_not_turn_omitted_catalog_product_into_zero(monkeypatch):
    receipt = dict(sourceComplete=True,dateFrom=str(START),dateTo=str(END),
        fetchedAt=datetime.now(timezone.utc).isoformat(),aggregates={'684820752':fact(orders=0,carts=0,orderAmount=0)})
    monkeypatch.setattr(week.store, 'get_source_cache', lambda *args, **kwargs: receipt)
    monkeypatch.setattr(week.store, 'list_cached_goods', lambda org:[{'nmID':684820752},{'nmID':102}])
    result = week.load_funnel(7,START,END)
    assert result['populationComplete'] and result['dataComplete']
    assert result['metrics']['684820752']['orderCount'] == 0
    assert '102' not in result['metrics']
    assert result['unresolvedReasons']['source_product_omitted'] == 1
    assert not result['needsCollection']


def test_missing_day_requires_only_missing_interval(monkeypatch):
    result = load(monkeypatch, [fact(end=END-timedelta(days=1))])
    assert result['metrics']['684820752']['cartCount'] is None
    assert result['needsCollection']
    assert result['missingRanges'] == [(END,END)]


def test_another_period_or_connection_does_not_supply_values(monkeypatch):
    calls=[]
    monkeypatch.setattr(week, '_facts', lambda org, connection, start, end:
        calls.append((org,connection,start,end)) or [fact(start=START-timedelta(days=1))])
    result=week.load_funnel(9,START,END)
    assert calls == [(9,'connection-9',START,END)]
    assert result['metrics']['684820752']['orderCount'] is None


def test_fresh_complete_response_wins_and_shared_aggregator_is_unchanged(monkeypatch):
    from app.platform.period import Period
    from app.wb_sales_funnel import aggregate
    rows=[fact(stale=True),fact(end=START,orders=8)]
    shared,_=aggregate(Period(START,END),rows)
    assert not shared['fieldCoverage']['orders']
    assert load(monkeypatch,rows)['metrics']['684820752']['orderCount']==52
    week._load_funnel.cache_clear()
    rows.append(fact(orders=55,carts=300))
    result=load(monkeypatch,rows)
    assert result['metrics']['684820752']['orderCount']==55
    assert result['metrics']['684820752']['cartCount']==300
    assert result['complete'] and not result['needsCollection']


def test_attach_publishes_population_and_explicit_unresolved_reason(monkeypatch):
    def funnel(org,left,right):
        return dict(metrics={'684820752':dict(orderCount=52,orderSumKopecks=9360000,cartCount=None)},
            complete=False,dataComplete=False,populationComplete=True,needsCollection=False,
            missingDates=[],unresolvedReasons={'source_fields_missing':1})
    monkeypatch.setattr(week,'load_funnel',funnel)
    report=dict(rows=[dict(nmId=684820752),dict(nmId=999)],
        unallocatedFinancials={'selected':{},'previous':{}})
    result=week.attach(report,7,START,END,START-timedelta(days=7),END-timedelta(days=7),
        finance_allowed=False,catalog={})
    assert result['rows'][0]['funnelPopulation']=={'selected':True,'previous':True}
    assert result['rows'][1]['funnelPopulation']=={'selected':False,'previous':False}
    assert result['rows'][0]['orders']['units']==52
    assert result['rows'][0]['baskets']['units'] is None
    assert result['metricCoverage']['selected']['funnelUnresolvedReasons']=={'source_fields_missing':1}
    assert not result['metricCoverage']['selected']['funnelNeedsCollection']
    assert 'WB не передал обязательные поля (1)' in result['warning']


def test_old_week_derived_payload_is_rejected_without_changing_other_versions():
    from app.routers import wb_reports_bff as reports
    cache=dict(completedAt=datetime.now(timezone.utc).isoformat(),
        report=dict(cacheVersion='v15',rows=[{'orders':{'units':1}}]))
    assert not reports._report_payload_cache_is_usable('week-over-week',cache)
    cache['report']['cacheVersion']=reports.WEEK_OVER_WEEK_REPORT_PAYLOAD_VERSION
    assert reports._report_payload_cache_is_usable('week-over-week',cache)


def test_existing_completed_enumeration_reuses_population_without_another_download(monkeypatch):
    receipt=dict(complete=True,returnedNmIds=['684820752'],dateFrom=str(START),dateTo=str(END),
        fetchedAt=datetime.now(timezone.utc).isoformat())
    result=load(monkeypatch,[fact(),receipt])
    assert result['populationComplete'] and result['dataComplete']
    assert result['metrics']['684820752']['orderCount']==52
    assert not result['needsCollection'] and result['missingRanges']==[]


def test_completed_enumeration_never_fills_an_absent_fact_with_zero(monkeypatch):
    receipt=dict(complete=True,returnedNmIds=['684820752','102'],dateFrom=str(START),dateTo=str(END),
        fetchedAt=datetime.now(timezone.utc).isoformat())
    result=load(monkeypatch,[fact(),receipt])
    assert result['populationComplete'] and not result['dataComplete']
    assert result['metrics']['102']['orderCount'] is None
    assert result['unresolvedReasons']['source_fields_missing']==1
    assert not result['needsCollection']
