"""One real-fetch regression exercised through both catalog refresh callers."""
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from app import repricer_sync as sync
from app.routers import wb_repricer_bff as router


@pytest.mark.parametrize('caller', ['sync', 'manual', 'sync_then_other_step_retry'])
def test_rate_limited_catalog_keeps_observed_prices_and_stops_provider_calls(monkeypatch, caller):
    requests, saved, sleeps = [], [], []
    page = [{'vendorCode': f'SKU-{nm}', 'nmID': nm, 'sizes': [{'sizeID': nm, 'discountedPrice': 500}]}
            for nm in range(1, 1002)]

    class Client:
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def get(self, url, *, params, headers):
            assert url == 'https://prices.wbcon.su/get'
            assert headers == {'accept': 'application/json'}
            requests.append(params['articles'])
            return httpx.Response(200 if len(requests) == 1 else 429,
                                  request=httpx.Request('GET', url),
                                  json=[{'id': nm, 'salePriceU': 40000} for nm in range(1, 101)])

    monkeypatch.setattr(sync.httpx, 'Client', lambda **kwargs: Client())
    monkeypatch.setattr(sync.time, 'sleep', sleeps.append)
    monkeypatch.setattr(sync, 'get_settings', lambda: SimpleNamespace(
        spp_api_base_url='https://prices.wbcon.su', spp_api_token='must-not-leave-process',
        spp_api_timeout_seconds=15, spp_api_verify_ssl=True))
    monkeypatch.setattr(router, '_request_actor_and_wb_token', lambda _: (SimpleNamespace(organization_id=2), 'local-WB-token'))
    monkeypatch.setattr(router, '_ensure_wb_sync_not_running', lambda _: None)
    monkeypatch.setattr(router.repricer_bff_module, 'fetch_commission_tariffs', lambda *args, **kwargs: {})
    monkeypatch.setattr(sync, 'fetch_commission_tariffs', lambda *args, **kwargs: {})
    for module in (sync, router):
        monkeypatch.setattr(module, 'browser_prices_selected', lambda _: False)
        monkeypatch.setattr(module, 'list_cached_goods', lambda _: [])
        monkeypatch.setattr(module, 'fetch_catalog_goods_page',
                            lambda *args, **kwargs: {'goods': deepcopy(page[kwargs['offset']:kwargs['offset'] + kwargs['limit']])})
        monkeypatch.setattr(module, 'record_wb_sync_price_change_events', lambda *args, **kwargs: 0)
        monkeypatch.setattr(module, 'save_goods_page',
                            lambda **kwargs: saved.extend(deepcopy(kwargs['goods'])) or {'totalCached': len(saved)})
    if caller != 'manual':
        result = sync.refresh_wb_data_sources(organization_id=2, wb_token='local-WB-token',
                                             sources=['goods'], execute_lock=False)
        if caller == 'sync_then_other_step_retry':
            status = deepcopy(result)
            status['steps'].append({'source': 'stocks', 'status': 'error'})
            monkeypatch.setattr(router, 'get_source_cache', lambda *args, **kwargs: deepcopy(status))
            monkeypatch.setattr(router, 'save_source_cache', lambda org, key, value: status.update(deepcopy(value)) or deepcopy(status))
            monkeypatch.setattr(router, 'fetch_stock_aggregates', lambda *args, **kwargs: {})
            router._retry_failed_wb_sync_step(2, 'stocks', 'complete', 'local-WB-token')
            assert status['running'] is False and status['currentSource'] is None and status['finishedAt']
            result = status
        partial = result['state'] == result['steps'][0]['status'] == 'partial'
        state = result['steps'][0]
        assert state['count'] == 1001 and state['cache']['totalCached'] == 1001
        assert state['phase'] == 'partial' and state['progressPercent'] == 100 and state['finishedAt']
    else:
        result = router.refresh_sku_list_page(None, scenario='complete', limit=1000, offset=0, all=True)
        partial = result.get('partial') is True
        state = result
    assert len(saved) == 1001
    known = [row for row in saved if row['sizes'][0].get('buyerPriceNoWalletKopecks') is not None]
    assert len(known) == 100
    assert len(requests) == 2 and sleeps == [10.0]
    assert state['externalSppMatchedCount'] == 100
    assert partial and state['externalSppStatus'] == 'rate_limited' and state['externalSppHttpStatus'] == 429
    assert all(row['sizes'][0]['buyerPriceNoWalletKopecks'] == 40000 for row in known)
