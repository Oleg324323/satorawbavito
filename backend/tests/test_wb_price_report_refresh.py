"""Current browser prices may change without rebuilding period financials."""
from copy import deepcopy
import pytest

from app.routers import wb_reports_bff as reports


@pytest.fixture(autouse=True)
def isolated_rules(monkeypatch):
    from app.report_rules.store import _default
    monkeypatch.setattr(reports, 'load_active_profile', _default)


def test_cached_abc_overlays_exact_current_prices_but_not_historical_spp_or_profit(monkeypatch):
    monkeypatch.setattr(reports, 'get_source_cache', lambda org, *a, **kw: {'marketplaceAccountId': 7} if org == 17 else {})
    calls = []
    def goods(org):
        calls.append(org)
        return [{'nmID': 101, 'sizes': [{'sizeID': 201, 'price': 4300,
                 'discountedPrice': 2064, 'buyerPriceNoWalletKopecks': 144400}]}]
    monkeypatch.setattr(reports, 'list_cached_goods', goods)
    payload = {'meta': {'id': 'abc'}, 'filters': {'groupBy': 'sku'}, 'rows': [
        {'sku': 'same-name', 'nmId': 101, 'priceBeforeSppKopecks': 100000,
         'priceWithSppKopecks': 90000, 'sppPct': 21, 'sppObservedOn': '2026-10-16',
         'sppSource': 'supplier.orders.spp', 'netTotalKopecks': 12345},
        {'sku': 'same-name', 'nmId': 102, 'priceWithSppKopecks': 80000},
        {'sku': 'unallocated', 'isUnallocated': True, 'priceWithSppKopecks': None}]}
    original = deepcopy(payload)
    result = reports._apply_report_rules_to_payload(payload, 17)
    first, second, residual = result['rows']
    assert calls == [17]
    assert first['priceBeforeSppKopecks'] == 206400
    assert first['priceWithSppKopecks'] == 144400
    for key in ('sppPct','sppObservedOn','sppSource','netTotalKopecks'):
        assert first[key] == original['rows'][0][key]
    assert second.get('priceWithSppKopecks') is None
    assert residual.get('priceWithSppKopecks') is None
    assert payload == original
    monkeypatch.setattr(reports, 'list_cached_goods', lambda org: [
        {'nmID': 101, 'sizes': [{'price': 4300, 'discountedPrice': 2064}]}])
    assert reports._apply_report_rules_to_payload(payload, 17)['rows'][0].get('priceWithSppKopecks') is None


def test_other_reports_and_grouped_or_legacy_tenants_never_use_overlay(monkeypatch):
    monkeypatch.setattr(reports, 'get_source_cache', lambda *a, **kw: {})
    monkeypatch.setattr(reports, 'list_cached_goods', lambda *_: (_ for _ in ()).throw(AssertionError('unexpected catalog read')))
    for report, group in [('abc','sku'), ('abc','brand'), ('pnl','sku'), ('rnp','sku')]:
        payload = {'meta': {'id': report}, 'filters': {'groupBy': group},
                   'rows': [{'nmId': 101,'priceWithSppKopecks': 12345}]}
        assert reports._apply_report_rules_to_payload(payload, 18)['rows'][0]['priceWithSppKopecks'] == 12345
