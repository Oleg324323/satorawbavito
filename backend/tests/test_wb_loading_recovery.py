"""Isolated regressions for the empty-cache dependency and period reuse."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import wb_catalog_loading as catalog, repricer_sync as sync
from app.routers import wb_repricer_bff as repricer, wb_reports_bff as reports


def test_catalog_missing_is_not_confirmed_empty(monkeypatch):
    monkeypatch.setattr(catalog, 'get_source_cache', lambda *a, **kw: {'cards': []})
    monkeypatch.setattr(catalog, 'cached_goods_meta', lambda org: {'pagesCached': 0, 'totalCached': 0})
    assert catalog.missing_catalog_sources(1) == ('goods',)
    with pytest.raises(HTTPException) as error:
        catalog.require_goods_cache(1)
    assert error.value.detail['code'] == 'WB_CATALOG_CACHE_REQUIRED'
    monkeypatch.setattr(catalog, 'cached_goods_meta', lambda org: {'pagesCached': 1, 'totalCached': 0})
    assert catalog.missing_catalog_sources(1) == ()
    catalog.require_goods_cache(1)


@pytest.mark.parametrize('top', [True, False])
@pytest.mark.parametrize('screen', ['sku', 'stats'])
def test_missing_goods_guards_top_filtered_and_paginated_reads(monkeypatch, top, screen):
    monkeypatch.setattr(repricer, '_hydrate_org_repricer_state', lambda request: 42)
    monkeypatch.setattr(repricer, 'cached_goods_meta', lambda org: {'pagesCached': 0, 'totalCached': 0})
    monkeypatch.setattr(repricer, '_load_repricer_sku_snapshot_page', lambda *a, **kw: pytest.fail('must not read false-empty snapshot'))
    method = repricer.get_sku_list if screen == 'sku' else repricer.get_repricer_stats
    with pytest.raises(HTTPException) as error:
        method(object(), period_days=8, date_from=date(2026, 10, 1), date_to=date(2026, 10, 8),
            top_mode=top, page=2, page_size=25, q='known article')
    assert error.value.status_code == 409


def test_recovery_reuses_cache_and_serializes_missing_collection(monkeypatch):
    from app.infra import redis_client
    calls = []
    lock = SimpleNamespace(acquire=lambda **kw: True, release=lambda: calls.append('release'))
    monkeypatch.setattr(redis_client, 'get_redis_client', lambda: SimpleNamespace(lock=lambda *a, **kw: lock))
    missing = [True]
    monkeypatch.setattr(catalog, 'missing_catalog_sources', lambda org: ('goods', 'content') if missing[0] else ())
    def collect(**kwargs):
        assert kwargs['organization_id'] == 42
        assert kwargs['_catalog_recovery'] is True and kwargs['_parallelize'] is False
        assert kwargs['execute_lock'] is False
        calls.append(kwargs['sources'])
        missing[0] = False
        return {'state': 'completed', 'steps': []}
    monkeypatch.setattr(sync, 'refresh_wb_data_sources', collect)
    catalog.recover_catalog(42, 'synthetic')
    catalog.recover_catalog(42, 'synthetic')
    assert calls == [('goods', 'content'), 'release']
    missing[0] = True
    lock.acquire = lambda **kw: False
    from app.repricer_tasks import ReportSourceRateLimited
    with pytest.raises(ReportSourceRateLimited) as waiting:
        catalog.recover_catalog(42, 'synthetic')
    assert waiting.value.delay == 5
    assert calls == [('goods', 'content'), 'release']


def test_recovery_collects_goods_without_tariffs_price_events_or_warmup(monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail('catalog recovery must not mutate strategy, warmup or price history')
    monkeypatch.setattr(sync, 'list_cached_goods', lambda org: [])
    monkeypatch.setattr(sync, 'browser_prices_selected', lambda org: False)
    for name in ('fetch_commission_tariffs', 'fetch_external_spp_prices', 'record_wb_sync_price_change_events', '_mark_new_goods_as_warmup'):
        monkeypatch.setattr(sync, name, forbidden)
    monkeypatch.setattr(sync, 'fetch_catalog_goods_page', lambda *a, **kw: {'goods': [{'nmID': 101, 'vendorCode': 'REAL'}]})
    published = []
    monkeypatch.setattr(sync, 'replace_goods_pages', lambda **kw: published.append(kw) or {'totalCached': 1})
    result = sync.refresh_wb_data_sources(organization_id=42, wb_token='synthetic', sources=['goods'],
        execute_lock=False, _parallelize=False, _catalog_recovery=True)
    assert result['state'] == 'completed'
    assert published[0]['pages'][0]['goods'][0]['nmID'] == 101


def test_catalog_job_deduplicates_dates_and_persists_before_dispatch(monkeypatch):
    from app.infra import redis_client
    cache, dispatched = {}, []
    lock = SimpleNamespace(acquire=lambda **kw: True, release=lambda: None)
    monkeypatch.setattr(redis_client, 'get_redis_client', lambda: SimpleNamespace(lock=lambda *a, **kw: lock))
    monkeypatch.setattr(catalog, 'missing_catalog_sources', lambda org: ('goods',))
    monkeypatch.setattr(reports, 'get_source_cache', lambda org, key, **kw: cache.get((org, key)))
    monkeypatch.setattr(reports, 'save_source_cache', lambda org, key, value: cache.update({(org, key): value}))
    first = reports._report_job_cache_key('stats', date(2026, 10, 1), date(2026, 10, 4), 'sku', 'catalog:synthetic')
    second = reports._report_job_cache_key('stats', date(2026, 10, 7), date(2026, 10, 11), 'brand', 'catalog:synthetic')
    assert first == second
    assert first != reports._report_job_cache_key('stats', date(2026, 10, 1), date(2026, 10, 4), 'sku', 'catalog:other')
    def enqueue(**kwargs):
        queued = cache[42, first]
        assert queued['state'] == 'queued' and queued['taskId'] == kwargs['task_id']
        assert kwargs['kwargs'] == {'refresh_missing_only': True}
        dispatched.append(kwargs)
        cache[42, first] = {**queued, 'state': 'running'}
    catalog.queue_catalog_recovery(42, first, (42,), enqueue)
    assert cache[42, first]['state'] == 'running'  # No HTTP write after worker heartbeat.
    reused = catalog.queue_catalog_recovery(42, second, (42,), enqueue)
    assert reused['reused'] is True and reused['state'] == 'running'
    assert len(dispatched) == 1


def test_catalog_dispatch_error_is_terminal_and_keeps_cache(monkeypatch):
    from app.infra import redis_client
    cache = {}
    lock = SimpleNamespace(acquire=lambda **kw: True, release=lambda: None)
    monkeypatch.setattr(redis_client, 'get_redis_client', lambda: SimpleNamespace(lock=lambda *a, **kw: lock))
    monkeypatch.setattr(catalog, 'missing_catalog_sources', lambda org: ('goods',))
    monkeypatch.setattr(reports, 'get_source_cache', lambda org, key, **kw: cache.get((org, key)))
    monkeypatch.setattr(reports, 'save_source_cache', lambda org, key, value: cache.update({(org, key): value}))
    def unavailable(**kwargs):
        raise ConnectionError('synthetic queue outage')
    with pytest.raises(HTTPException) as error:
        catalog.queue_catalog_recovery(42, 'catalog-job', (42,), unavailable)
    assert error.value.status_code == 503 and cache[42, 'catalog-job']['state'] == 'failed'


def test_catalog_waiting_behind_rate_limited_work_is_reused_but_bounded():
    job = dict(state='queued', reportId='stats', source='catalog',
        updatedAt=(datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat())
    assert reports._report_job_is_reusable(job) is True
    job['updatedAt'] = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
    assert reports._report_job_is_reusable(job) is False


@pytest.mark.parametrize('known_ids,confirmed_empty', [([], False), ([], True), ([101, 102], False)])
def test_baskets_resolve_technical_catalog_or_fail_truthfully(monkeypatch, known_ids, confirmed_empty):
    monkeypatch.setattr(sync, 'list_cached_goods', lambda org: [])
    monkeypatch.setattr(sync, 'cached_goods_meta', lambda org: {'pagesCached': int(confirmed_empty)})
    monkeypatch.setattr('app.platform.economics.legacy_catalog.catalog_facts',
        lambda org, **kw: {nm: {} for nm in known_ids} if org == 42 else {})
    monkeypatch.setattr(sync, 'get_source_cache', lambda *a, **kw: {})
    monkeypatch.setattr(sync, '_baskets_daily_seed_from_recent_cache', lambda *a, **kw: ({}, []))
    monkeypatch.setattr(sync, '_release_warmup_goods_by_baskets', lambda *a: 0)
    observed, stored = [], []
    def collect(*a, **kw):
        observed.append(kw)
        return {'aggregates': {} if confirmed_empty else {'101': {'cartCount': 0, 'orderCount': 0}}, 'dateFrom': '2026-10-01', 'dateTo': '2026-10-08'}
    monkeypatch.setattr(sync, 'fetch_baskets_aggregates', collect)
    monkeypatch.setattr(sync, 'save_source_cache', lambda org, key, payload: stored.append(payload))
    result = sync.refresh_wb_data_sources(organization_id=42, wb_token='synthetic', sources=['baskets'],
        execute_lock=False, _parallelize=False, date_from=date(2026, 10, 1), date_to=date(2026, 10, 8))
    step = result['steps'][0]
    if known_ids or confirmed_empty:
        assert step['status'] == 'ok' and result['state'] == 'completed'
        assert observed[0]['nm_ids'] == []  # One seller-wide paginated read, not one API call per product.
        assert stored[0]['aggregateComplete'] is True
        if known_ids:
            assert stored[0]['aggregates']['101']['cartCount'] == 0
        else:
            assert stored[0]['aggregates'] == {}
    else:
        assert result['state'] == 'partial' and step['status'] == 'error'
        assert step['phase'] == 'failed' and 'WB_CATALOG_CACHE_REQUIRED' in step['error']
        assert observed == stored == []


@pytest.mark.parametrize('left,right', [(1, 4), (1, 8), (7, 11)])
def test_statistics_reuses_raw_ledger_with_legacy_math_and_inclusive_dates(monkeypatch, left, right):
    from app.repricer_cache import store
    from app.repricer_page_finance import connection_key
    from app.wb_statistics_finance import load_statistics_finance, project_statistics_operations
    from app.repricer_bff import aggregate_finance_report_rows
    monkeypatch.setattr('app.cabinet.store.get_organization_wb_token_secret', lambda org: 'synthetic')
    rows = [dict(rrdId=day, nmId=101, quantity=1, retailAmount=100, retailPriceWithDisc=120,
        ppvzSalesCommission=10, deliveryService=2, forPay=88, docTypeName='Продажа', sellerOperName='Продажа',
        saleDt=f'2026-10-{day:02d}', rrDate=f'2026-10-{day:02d}') for day in range(1, 13)]
    rows.append({**rows[3], 'rrdId': 40, 'docTypeName': 'Возврат', 'sellerOperName': 'Возврат', 'penalty': -2})
    prefix = f'repricer_finance_{connection_key("synthetic")}_'
    key = prefix + '2026-10-01_2026-10-12'
    payload = dict(operations=rows, sourceComplete=True, dateFrom='2026-10-01', dateTo='2026-10-12', fetchedAt='2026-10-13T00:00:00Z')
    monkeypatch.setattr(store, 'list_source_cache_ranges_by_prefix', lambda org, p, **kw: [dict(payload, sourceKey=key)] if org == 42 and p == prefix else [])
    monkeypatch.setattr(store, 'get_source_cache', lambda org, k, **kw: deepcopy(payload) if k == key else {})
    start, end = date(2026, 10, left), date(2026, 10, right)
    result = load_statistics_finance(42, start, end)
    expected = aggregate_finance_report_rows([row for row in rows if str(start) <= row['rrDate'] <= str(end)],
        date_from=datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc),
        date_to=datetime.combine(end, datetime.max.time(), tzinfo=timezone.utc))
    assert result['aggregates'] == expected['aggregates']
    assert result['sourceComplete'] is True and result['coveredDays'] == right - left + 1
    assert result['dateFrom'] == str(start) and result['dateTo'] == str(end)
    assert load_statistics_finance(999, start, end)['coveredDays'] == 0
    empty = project_statistics_operations([], start, end)
    assert empty['aggregates'] == {}


def test_top_movers_compare_only_proven_same_product_and_period():
    start, end = date(2026, 10, 7), date(2026, 10, 11)
    row = dict(orderCount=61, buyoutCount=11, buyoutCountProvided=True, vendorCode='ARTICLE', productName='Real title',
        previous=dict(orderCount=50, buyoutCount=15, buyoutCountProvided=True))
    cached = dict(dateFrom=str(start), dateTo=str(end), aggregateComplete=True, aggregates={'867213853': row},
        comparisonDateFrom='2026-10-02', comparisonDateTo='2026-10-06')
    result = reports._top_movers_from_cache(cached, start, end)
    assert result['comparisonFrom'] == '2026-10-02' and result['comparisonTo'] == '2026-10-06'
    assert result['rows'][0]['salesDelta'] == -4 and result['rows'][0]['ordersDelta'] == 11
    assert result['status'] == 'ready'
    row.pop('buyoutCountProvided')  # Old normalization cannot prove an observed zero.
    assert reports._top_movers_from_cache(cached, start, end)['rows'][0]['salesDelta'] is None
    assert reports._top_movers_from_cache(cached, date(2026, 10, 1), end)['rows'] == []
    cached['aggregateComplete'] = False
    assert reports._top_movers_from_cache(cached, start, end)['status'] == 'partial'


def test_statistics_reads_persisted_ledger_and_observes_corrections_without_provider(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.repricer_cache import store
    from app.repricer_cache.orm import WbRepricerSourceCacheRow
    from app.repricer_page_finance import connection_key
    from app.wb_statistics_finance import load_statistics_finance, project_statistics_operations
    engine = create_engine('sqlite+pysqlite:///:memory:')
    try:
        WbRepricerSourceCacheRow.__table__.create(engine)
        monkeypatch.setattr(store, 'get_session_factory', lambda: sessionmaker(bind=engine))
        monkeypatch.setattr(store, '_redis_available', lambda: False)
        monkeypatch.setattr('app.cabinet.store.get_organization_wb_token_secret', lambda org: 'synthetic')
        monkeypatch.setattr('app.repricer_bff.build_wb_client', lambda *a, **kw: pytest.fail('DB reuse must not call WB'))
        key = f'repricer_finance_{connection_key("synthetic")}_2026-10-01_2026-10-08'
        row = dict(rrdId=7, nmId=101, quantity=1, retailAmount=100, retailPriceWithDisc=100,
            rrDate='2026-10-07', saleDt='2026-10-07', docTypeName='Продажа', sellerOperName='Продажа')
        payload = dict(sourceComplete=True, dateFrom='2026-10-01', dateTo='2026-10-08', operations=[row, dict(row)])
        store.save_source_cache(42, key, payload, strict=True)
        revision = store.get_repricer_sources_revision(42)
        start, end = date(2026, 10, 1), date(2026, 10, 8)
        first = load_statistics_finance(42, start, end)
        assert first['sourceComplete'] is True and first['coveredDays'] == 8
        assert first['aggregates'] == project_statistics_operations([row], start, end)['aggregates']
        corrected = {**row, 'retailAmount': 200, 'retailPriceWithDisc': 200}
        store.save_source_cache(42, key, {**payload, 'operations': [corrected]}, strict=True)
        assert store.get_repricer_sources_revision(42) != revision
        second = load_statistics_finance(42, start, end)
        assert second['aggregates'] == project_statistics_operations([corrected], start, end)['aggregates']
        assert second['aggregates'] != first['aggregates']
        assert load_statistics_finance(99, start, end)['coveredDays'] == 0
    finally:
        engine.dispose()
