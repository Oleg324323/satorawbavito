"""Confirmed-price receipts and warmed DB/Redis views; no provider I/O."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatch
import socket

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import repricer_bff as bff, repricer_dispatch_store as ledger
from app.cabinet.orm import LkOrganizationRow  # register FK target
from app.repricer_cache import store
from app.repricer_cache.orm import WbRepricerGoodsCacheRow as Goods, WbRepricerSourceCacheRow as Source
from app.repricer_persistence.orm import WbRepricerExecutionRunRow, WbRepricerRuntimeStateRow, WbRepricerChangelogRow
from app.routers import wb_repricer_bff as router

START, END = '2026-10-05', '2026-10-11'
AT = datetime.now(timezone.utc) - timedelta(hours=1)


class Redis:
    def __init__(self):
        self.values = {}
        self.down = False

    def check(self):
        if self.down:
            raise ConnectionError('Synthetic Redis outage')

    def get(self, key):
        self.check()
        return self.values.get(key)

    def setex(self, key, ttl, value):
        self.check()
        self.values[key] = value

    def delete(self, *keys):
        self.check()
        for key in keys:
            self.values.pop(key, None)

    def scan_iter(self, *, match, count):
        self.check()
        return [key for key in self.values if fnmatch(key, match)]


def good(nm=101, price=1000):
    return dict(nmID=nm, vendorCode=f'synthetic-{nm}', discount=0, editableSizePrice=True,
                sizes=[dict(sizeID=nm * 10, price=price, discountedPrice=price),
                       dict(sizeID=nm * 10 + 1, price=1500, discountedPrice=1500)])


@pytest.fixture
def db(monkeypatch):
    def denied(*a, **kw):
        raise AssertionError('Price regression must not access the network')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket, 'getaddrinfo', denied)
    engine = create_engine('sqlite:///:memory:', poolclass=StaticPool, connect_args={'check_same_thread': False})
    for model in (Goods, Source, WbRepricerExecutionRunRow, WbRepricerRuntimeStateRow, WbRepricerChangelogRow):
        model.__table__.create(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    for module in (store, ledger):
        monkeypatch.setattr(module, 'get_session_factory', lambda: factory)
    redis = Redis()
    monkeypatch.setattr(store, 'get_redis_client', lambda: redis)
    monkeypatch.setattr(store, '_redis_available', lambda: True)
    monkeypatch.setattr(router, '_page_finance_active', lambda: False)
    monkeypatch.setattr(router, '_repricer_view_state_revision', lambda: 'state')
    monkeypatch.setattr(router, 'legacy_finance_tax_revision', lambda *a: 'tax')
    monkeypatch.setattr(router, '_snapshot_source_revision', lambda *a: 'source')
    with factory() as session:
        for org in (1, 2):
            session.add(Goods(organization_id=org, page_offset=0, page_limit=1000,
                              goods_payload=[good(), good(102)], fetched_at=AT, updated_at=AT))
        session.commit()
    yield factory, redis
    engine.dispose()


def save_snapshot(org, view, rows=None, metadata=None):
    key = router._repricer_sku_snapshot_key('complete', period_suffix=f'{START}_{END}',
                                           include_promotions=False, include_content=False, view=view)
    data = dict(dateFrom=START, dateTo=END, cache={}, stateRevision='state', taxRevision='tax',
                sourceRevision='source', goodsRevision=store.cached_goods_meta(org)['latestFetchedAt'],
                goodsPriceRevision=store.get_goods_price_revision(org))
    if view == 'stats':
        data['response'] = dict(dateFrom=START, dateTo=END, cache={})
    data.update(metadata or {})
    router._save_repricer_list_snapshot(org, key, rows or [dict(meta=dict(nmId=101, currentPriceKopecks=100000))], data)
    return key, data


def load_snapshot(org, view):
    return router._load_repricer_sku_snapshot_page(org, 'complete', period_suffix=f'{START}_{END}',
        include_promotions=False, include_content=False, view=view, page=1, page_size=25)


def dispatched():
    draft = dict(draftId='synthetic-price', state='approved', organizationId=1, bindingKey='synthetic-binding',
                 nmId=101, articleId='synthetic-101', approvalRef='synthetic-approval',
                 normalized=dict(currentSellerPriceKopecks=100000, candidateSellerPriceKopecks=120000))
    ledger.save_draft(1, draft)
    keys = ledger.claim(1, draft['bindingKey'], [draft])
    ledger.transition(1, keys, 'dispatched', result=dict(priceConfirmed=False))
    return keys


def test_confirmation_invalidates_both_views_and_chunks_but_preserves_history_and_scope(db):
    factory, redis = db
    old_goods = deepcopy(store.list_cached_goods(1))
    old_meta = store.cached_goods_meta(1)
    for org in (1, 2):
        for view in ('sku', 'stats'):
            save_snapshot(org, view)
            assert load_snapshot(org, view) is not None
    history = dict(aggregates={'101': dict(revenueKopecks=900000, netProfitKopecks=128000)})
    for key in ('finance_synthetic', 'reports_payload_synthetic'):
        store.save_source_cache(1, key, history)
    stored_history = {key: store.get_source_cache(1, key) for key in ('finance_synthetic', 'reports_payload_synthetic')}
    keys = dispatched()
    assert store.list_cached_goods(1) == old_goods
    assert load_snapshot(1, 'stats') is not None
    actual = good(price=1200)  # the second size is deliberately unchanged
    ledger.transition(1, keys, 'confirmed', result=dict(priceConfirmed=True, confirmedGoods=[actual]))
    for view in ('sku', 'stats'):
        assert load_snapshot(1, view) is None
        assert load_snapshot(2, view) is not None
    with factory() as session:
        assert not session.scalars(select(Source).where(Source.organization_id == 1,
            Source.source_key.startswith(('stats_list_snapshot_')))).all()
    updated = store.list_cached_goods(1)
    assert updated[0]['sizes'][0]['discountedPrice'] == 1200
    with factory() as session:
        saved = session.scalar(select(Goods).where(Goods.organization_id == 1)).goods_payload
        assert saved[0]['sizes'][1] == good()['sizes'][1]
    assert updated[1] == old_goods[1]
    assert store.list_cached_goods(2)[0]['sizes'][0]['discountedPrice'] == 1000
    assert store.cached_goods_meta(1) == old_meta
    for key in ('finance_synthetic', 'reports_payload_synthetic'):
        assert store.get_source_cache(1, key) == stored_history[key]
        assert store.get_source_cache(1, key)['aggregates'] == history['aggregates']
    # Duplicate confirmation is idempotent, including the changelog.
    ledger.transition(1, keys, 'confirmed', result=dict(priceConfirmed=True, confirmedGoods=[actual]))
    with factory() as session:
        assert len(session.scalars(select(WbRepricerChangelogRow)).all()) == 1


@pytest.mark.parametrize('view', ['sku', 'stats'])
def test_missed_redis_eviction_and_late_snapshot_writer_cannot_restore_old_prices(db, view):
    factory, redis = db
    store.list_cached_goods(1)
    key, old = save_snapshot(1, view)
    assert load_snapshot(1, view) is not None  # warm the Redis snapshot too
    keys = dispatched()
    redis.down = True
    ledger.transition(1, keys, 'confirmed', result=dict(priceConfirmed=True, confirmedGoods=[good(price=1200)]))
    assert ledger.load_receipt(1, keys[0])['state'] == 'confirmed'
    assert store.list_cached_goods(1)[0]['sizes'][0]['discountedPrice'] == 1200
    redis.down = False  # stale Redis entries still exist after reconnection
    assert store.list_cached_goods(1)[0]['sizes'][0]['discountedPrice'] == 1200
    assert load_snapshot(1, view) is None
    # A read that started before the confirmation finishes saving afterwards.
    router._save_repricer_list_snapshot(1, key, [dict(meta=dict(currentPriceKopecks=100000))], old)
    assert load_snapshot(1, view) is None


@pytest.mark.parametrize('state', ['dispatched', 'rejected', 'failed'])
def test_unconfirmed_receipts_preserve_prices_and_statistics(db, state):
    original = deepcopy(store.list_cached_goods(1))
    save_snapshot(1, 'stats')
    keys = dispatched()
    if state != 'dispatched':
        ledger.transition(1, keys, state, result=dict(priceConfirmed=False))
    assert store.list_cached_goods(1) == original
    assert load_snapshot(1, 'stats') is not None


def test_confirmation_without_verification_rolls_back_the_receipt_and_price(db):
    original = deepcopy(store.list_cached_goods(1))
    save_snapshot(1, 'stats')
    keys = dispatched()
    with pytest.raises(ledger.DispatchPersistenceError, match='WB_PRICE_NOT_CONFIRMED'):
        ledger.transition(1, keys, 'confirmed', result=dict(priceConfirmed=False, confirmedGoods=[good(price=1200)]))
    assert ledger.load_receipt(1, keys[0])['state'] == 'dispatched'
    assert store.list_cached_goods(1) == original
    assert load_snapshot(1, 'stats') is not None


def test_unit_estimates_follow_price_while_historical_finance_is_preserved(monkeypatch):
    settings = bff._sku_cost_settings('synthetic-price', use_demo_data=False)
    settings.update(cogsKopecks=40000, logisticsKopecks=8000, returnLogisticsKopecks=6000,
        deliveryToClientKopecks=8000, deliveryFromClientKopecks=6000, taxPct=6,
        otherExpensePerSaleKopecks=0, otherExpensePricePct=0, workReturnPerSaleKopecks=0)
    monkeypatch.setattr(bff, '_sku_cost_settings', lambda *a, **kw: deepcopy(settings))
    monkeypatch.setitem(bff.ALGORITHM_SETTINGS_STATE, 'acquiringPct', 2)
    tariff = bff._commission_tariffs_index_from_rows([dict(subjectName='Synthetic', kgvpMarketplace=18)])
    history = dict(salesUnits=10, returnsUnits=0, sellerRevenueKopecks=1000000,
                   buyerRevenueKopecks=900000, revenueGrossKopecks=900000)
    def calculate(price, finance):
        return bff._build_sku_row('synthetic-price', nm_id=101, name='Synthetic', subject='Synthetic',
            brand=None, chrt_ids=[], current_price_kopecks=price, discounted_price_kopecks=price,
            buyer_price_kopecks=round(price * .9), spp_pct=10, promotions=[], use_demo_data=False,
            list_view=True, commission_tariffs_index=tariff,
            period_aggregate=dict(salesUnits=10, ordersUnits=10, sppPct=10),
            baskets_aggregate=dict(buyoutPct=80, buyoutSource='sales_funnel.conversions.buyoutPercent',
                orderCount=12, cartCount=20, orderSumKopecks=1000000), finance_aggregate=finance)['analytics']
    for finance in (None, deepcopy(history)):
        old, new = calculate(100000, finance), calculate(120000, finance)
        assert (old['plannedMarginKopecks'], new['plannedMarginKopecks']) == (12800, 25600)
        assert old['revenueKopecks'] == new['revenueKopecks'] == (900000 if finance else 0)
        if finance is None:
            assert (old['plannedPeriodMarginKopecks'], new['plannedPeriodMarginKopecks']) == (128000, 256000)
        else:
            # Existing historical basis is intentionally retained pending a product definition.
            assert old['plannedPeriodMarginKopecks'] == new['plannedPeriodMarginKopecks'] == 128000
            assert finance == history


def test_statistics_api_rebuilds_warm_price_view_without_collecting_wb(db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.platform.economics import legacy_catalog

    monkeypatch.setattr(router, '_hydrate_org_repricer_state', lambda request: 1)
    monkeypatch.setattr(legacy_catalog, 'catalog_facts', lambda *a: {})
    monkeypatch.setattr(bff, '_demo_repricer_data_enabled', lambda *a: False)
    settings = bff._sku_cost_settings('synthetic-101', use_demo_data=False)
    settings.update(cogsKopecks=40000, deliveryToClientKopecks=8000, deliveryFromClientKopecks=6000, taxPct=6)
    monkeypatch.setattr(bff, '_sku_cost_settings', lambda *a, **kw: deepcopy(settings))
    tariff = bff._commission_tariffs_index_from_rows([dict(subjectName='Synthetic', kgvpMarketplace=18)])
    monkeypatch.setattr(bff, 'cached_commission_tariffs', lambda *a, **kw: tariff)
    store.save_source_cache(1, 'content_cards', dict(cards=[dict(nmID=101, vendorCode='synthetic-101', subjectName='Synthetic')]))
    caches = {name: dict(dateFrom=START, dateTo=END, fetchedAt=AT.isoformat(), aggregates={})
              for name in ('period_stats', 'finance', 'baskets', 'ads')}
    caches['period_stats']['aggregates']['101'] = dict(salesUnits=10, ordersUnits=10, sppPct=10)
    caches['baskets']['aggregates']['101'] = dict(buyoutPct=80, buyoutSource='sales_funnel.conversions.buyoutPercent',
                                               orderCount=12, cartCount=20)
    monkeypatch.setattr(router, '_ensure_repricer_stats_period_caches', lambda *a, **kw:
        dict(caches=caches, missingSources=[], onDemandFetchedSources=[]))
    monkeypatch.setattr(router, '_period_source_cache', lambda *a, **kw: {})
    monkeypatch.setattr(router, '_repricer_finance_taxes', lambda *a, **kw: {})
    monkeypatch.setattr(router, '_repricer_list_cache_meta', lambda *a, **kw: store.cached_goods_meta(1))
    app = FastAPI()
    app.get('/api/v1/wb-repricer/stats')(router.get_repricer_stats)
    client = TestClient(app)
    query = dict(dateFrom=START, dateTo=END, pageSize=500, topMode='true')

    def read():
        response = client.get('/api/v1/wb-repricer/stats', params=query)
        assert response.status_code == 200
        return next(row for row in response.json()['items'] if row['nmId'] == 101)

    before = read()
    assert load_snapshot(1, 'stats') is not None
    assert read() == before
    keys = dispatched()
    assert read() == before
    ledger.transition(1, keys, 'confirmed', result=dict(priceConfirmed=True, confirmedGoods=[good(price=1200)]))
    after = read()
    assert before['metrics']['currentPriceKopecks'] == 100000
    assert after['metrics']['currentPriceKopecks'] == 120000
    assert before['metrics']['marginPct'] != after['metrics']['marginPct']
    assert read() == after
