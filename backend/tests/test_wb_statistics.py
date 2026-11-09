from copy import deepcopy

import pytest

from app.routers import wb_repricer_bff as router
from app.wb_statistics import account_ad_totals, apply_account_totals, project_row, ratio
from tests.test_repricer_stats_baskets import DATE_FROM, DATE_TO, SUFFIX, _good, _source_payload, stats_runtime


@pytest.mark.parametrize('spend,revenue,expected', [
    (554, 2343400, .02), (600, 2343400, .03), (0, 100, 0),
    (None, 100, None), (100, None, None), (100, 0, None), (0, 0, None),
])
def test_raw_amounts_and_zero_denominators(spend, revenue, expected):
    row = {'analytics': {'adSpendKopecks': spend, 'adRevenueKopecks': revenue, 'revenueKopecks': 999999999}}
    assert router._repricer_stats_metrics(row)['drrPct'] == expected
    assert ratio(spend, revenue, 2) == expected


def test_projection_uses_only_product_ads_and_preserves_financial_input():
    row = {'meta': {'nmId': 101}, 'analytics': {'adSpendKopecks': 554100, 'totalImpressions': 113741}}
    original = deepcopy(row)
    cache = {'aggregates': {'101': {'adSpendKopecks': 554, 'adRevenueKopecks': 2343400, 'adImpressions': 15, 'adClicks': 0}}}
    metric = router._repricer_stats_metrics(project_row(row, cache))
    assert metric['totalImpressions'] == 113741
    assert metric['impressions'] == 15
    assert metric['clicks'] == 0
    assert metric['adCartAdds'] is None
    assert metric['adSpendKopecks'] == 554
    assert metric['drrPct'] == .02
    assert router._repricer_stats_metrics(project_row(row, {}))['adSpendKopecks'] is None
    assert project_row(row, {**cache, 'sourceComplete': False})['analytics']['adsState'] == 'partial'
    assert row == original


def test_summary_weights_ad_revenue_not_financial_revenue_and_never_averages_ratios():
    items = [{'metrics': {'adSpendKopecks': spend, 'adRevenueKopecks': revenue,
                          'revenueKopecks': 999999, 'clicks': clicks, 'impressions': views,
                          'orders': orders, 'baskets': carts}}
             for spend, revenue, clicks, views, orders, carts in [(100, 1000, 1, 10, 2, 4), (900, 3000, 9, 90, 3, 6)]]
    totals = router._repricer_stats_summary(items)['metricTotals']
    assert totals['drrPct']['value'] == 25
    assert totals['ctrPct']['value'] == 10
    assert totals['cartToOrderCrPct']['value'] == 50


def test_account_totals_own_scope_not_catalog_subset_or_nested_campaign_double_count():
    fields = dict(adSpendKopecks=100, adImpressions=10, adClicks=1, adCartAdds=2, adOrders=1, adRevenueKopecks=500)
    cache = {'totals': fields, 'aggregates': {'1': {**fields, 'adSpendKopecks': 30}},
             'campaignTotals': {'ad_spend_kopecks': 99999}}
    assert account_ad_totals(cache) == fields
    summary = router._repricer_stats_summary([])
    apply_account_totals(summary, cache)
    assert summary['adSpendKopecks'] == 100
    assert summary['drrPct'] == 20
    assert summary['metricTotals']['adSpendKopecks']['scope'] == 'account'
    cache['totals']['adSpendKopecks'] = 0
    assert account_ad_totals(cache)['adSpendKopecks'] == 0
    cache['totals']['adSpendKopecks'] = None
    assert account_ad_totals(cache)['adSpendKopecks'] is None


def test_complete_summary_is_identical_across_pagination_and_filters_do_not_use_account_totals(stats_runtime, monkeypatch):
    goods, storage, request = stats_runtime
    goods[:] = [_good(i) for i in range(1, 52)]
    raw = _source_payload({str(g['nmID']): dict(adSpendKopecks=100, adRevenueKopecks=10000, adImpressions=10) for g in goods},
                          totals=dict(adSpendKopecks=6000, adRevenueKopecks=600000, adImpressions=600))
    read = router.get_source_cache
    monkeypatch.setattr(router, 'get_source_cache', lambda org, key, **kw:
                        deepcopy(raw) if key == f'ads_{SUFFIX}' else read(org, key, **kw))
    pages = [request(baskets=_source_payload({}), params={'page': p, 'pageSize': 25}) for p in (1, 2, 3)]
    assert len({item['nmId'] for p in pages for item in p['items']}) == 51
    assert all(p['summary']['adSpendKopecks'] == 6000 for p in pages)
    assert all(p['summary']['drrPct'] == 1 for p in pages)
    filtered = request(baskets=None, params={'q': 'STATS_01'})
    assert filtered['summary']['metricTotals']['adSpendKopecks']['value'] == 100
    assert filtered['summary']['metricTotals']['adSpendKopecks'].get('scope') != 'account'
    assert (filtered['dateFrom'], filtered['dateTo']) == (DATE_FROM, DATE_TO)


def test_materialized_cache_does_not_copy_daily_rows_and_skips_unchanged_source(stats_runtime, monkeypatch):
    _, storage, request = stats_runtime
    request(baskets=_source_payload({'10001': {'cartCount': 7, 'impressions': 123}}, dailyAggregates={DATE_FROM: {}}, chunks=[{}]))
    materialized = storage[1, f'repricer_stats_baskets_{SUFFIX}']
    assert not {'dailyAggregates', 'chunks'} & materialized.keys()
    assert materialized['aggregates']['10001']['impressions'] == 123
    read = router._period_source_cache
    def no_basket_reread(org, prefix, *args, **kwargs):
        assert prefix != 'baskets'
        return read(org, prefix, *args, **kwargs)
    monkeypatch.setattr(router, '_period_source_cache', no_basket_reread)
    assert request(baskets=None)['items'][0]['metrics']['totalImpressions'] == 123


def test_partial_period_is_not_presented_as_complete():
    summary = router._repricer_stats_summary([])
    apply_account_totals(summary, {'totals': {'adSpendKopecks': 1}, 'missingDates': ['2026-10-09']})
    assert summary['metricTotals']['adSpendKopecks']['partialPeriod'] is True
    assert summary['metricTotals']['clicks']['value'] is None


def test_statistics_jobs_use_period_summary_not_expensive_daily_funnel():
    from app.repricer_tasks import _report_source_refresh_plan
    from app.routers.wb_reports_bff import REPORT_DAILY_SOURCES_BY_ID
    assert _report_source_refresh_plan('stats')['baskets_include_daily_detail'] is False
    assert 'baskets-summary' in REPORT_DAILY_SOURCES_BY_ID['stats']
    assert _report_source_refresh_plan('digest')['baskets_include_daily_detail'] is True
