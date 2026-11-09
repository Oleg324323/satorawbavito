"""Current WB cost/profile inputs and complete current-stock allocation."""
from copy import deepcopy
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import delete, func, select

from app import wb_pnl_costs, wb_pnl_advertising, wb_storage
from app import repricer_bff as bff, repricer_page_finance as page
from app.platform.catalog.orm import CatalogSkuRow, MarketplaceProductRow, MarketplaceOfferRow
from app.platform.economics import legacy_tax
from app.platform.economics.orm import CatalogCostVersionRow
from app.platform.period import Period
from app.repricer_cache import store
from app.repricer_cache.orm import WbRepricerGoodsCacheRow, WbRepricerSourceCacheRow
from app.repricer_persistence.orm import WbRepricerAlgorithmSettingsRow, WbRepricerRuntimeStateRow
from app.routers import wb_repricer_bff as router
from tests.test_legacy_finance_tax import tax_db, _cache, _fact  # noqa: F401
from tests.test_repricer_page_finance import operation

CONTEXT = dict(marketplaceAccountId=12, connectedAt="2026-01-01T00:00:00+00:00", bindingVersion=1, connection="synthetic")
ARTICLE = "Л_test"


@pytest.fixture
def inputs(tax_db, monkeypatch):
    for module in (wb_pnl_costs, store):
        monkeypatch.setattr(module, 'get_session_factory', lambda: lambda: tax_db)
    monkeypatch.setattr(store, '_redis_available', lambda: False)
    monkeypatch.setattr(wb_pnl_advertising, '_source_context', lambda org: CONTEXT if org == 1 else None)
    for model in (MarketplaceOfferRow, MarketplaceProductRow, CatalogSkuRow):
        tax_db.execute(delete(model).where(model.organization_id == 1))
    tax_db.add_all([
        WbRepricerAlgorithmSettingsRow(organization_id=1, settings_payload={'taxPct': 6}),
        WbRepricerRuntimeStateRow(organization_id=1, state_payload={'skuSettingsOverrides': {ARTICLE: {'cogsKopecks': 40000}}}),
        WbRepricerGoodsCacheRow(organization_id=1, page_offset=0, page_limit=1000,
            goods_payload=[{'nmID': 123, 'vendorCode': ARTICLE}, {'nmID': 456, 'vendorCode': 'Л_other'}],
            fetched_at=datetime(2026, 10, 31, tzinfo=timezone.utc)),
    ])
    tax_db.commit()
    store.save_source_cache(1, 'stocks', dict(sourceComplete=True, count=2,
        aggregates={'123': {'wbStockUnits': 10}, '456': {'wbStockUnits': 30}}))
    return tax_db


def resolve(source, start=date(2026, 10, 5), end=date(2026, 10, 11)):
    return legacy_tax.get_legacy_finance_taxes(1, source, Period(start, end),
        include_costs=True, use_current_settings=True)


def test_saved_400_rubles_and_profile_tax_work_for_any_period_without_catalog_or_history(inputs):
    source = _cache(_fact(1000000, 10, 2))
    original = deepcopy(source)
    for start, end in [(date(2020, 1, 1), date(2020, 1, 31)), (date(2026, 10, 5), date(2026, 10, 11))]:
        actual = resolve(source, start, end)['123']
        assert actual['settlementCogsKopecks'] == 320000
        assert actual['taxKopecks'] == 60000
        assert actual['factTaxState'] == 'configured'
    assert source == original
    assert inputs.scalar(select(func.count()).select_from(CatalogCostVersionRow)) == 0
    assert inputs.scalar(select(func.count()).select_from(CatalogSkuRow)) == 0


def test_cost_and_profile_tax_edits_change_old_period_and_cache_revision(inputs):
    source = _cache(_fact(1000000, 10, 2))
    before = legacy_tax.legacy_finance_tax_revision(1)
    settings = inputs.get(WbRepricerAlgorithmSettingsRow, 1)
    settings.settings_payload = {'taxPct': 8, 'wbQuarterlyTax': {'2020': {'basisPoints': [5000] * 4, 'configured': [True] * 4}}}
    runtime = inputs.get(WbRepricerRuntimeStateRow, 1)
    runtime.state_payload = {'skuSettingsOverrides': {ARTICLE: {'cogsKopecks': 50000}}}
    inputs.commit()
    actual = resolve(source, date(2020, 1, 1), date(2020, 1, 31))['123']
    assert actual['taxKopecks'] == 80000
    assert actual['settlementCogsKopecks'] == 400000
    assert legacy_tax.legacy_finance_tax_revision(1) != before


@pytest.mark.parametrize('unit,rate,expected_cost,expected_tax', [(0, 0, 0, 0), (None, 6, None, 6000), (40000, None, 80000, None)])
def test_explicit_zero_and_missing_inputs_remain_distinct(inputs, unit, rate, expected_cost, expected_tax):
    inputs.get(WbRepricerAlgorithmSettingsRow, 1).settings_payload = {'taxPct': rate}
    inputs.get(WbRepricerRuntimeStateRow, 1).state_payload = {'skuSettingsOverrides': {ARTICLE: {'cogsKopecks': unit}}}
    inputs.commit()
    actual = resolve(_cache(_fact(100000, 3, 1)))['123']
    assert actual['settlementCogsKopecks'] == expected_cost
    assert actual['taxKopecks'] == expected_tax


def test_return_only_costs_are_signed_and_foreign_account_is_rejected(inputs):
    source = _cache(_fact(-100000, 0, 2))
    actual = resolve(source)['123']
    assert actual['settlementCogsKopecks'] == -80000
    assert actual['taxKopecks'] == -6000
    source['marketplaceAccountId'] = 99
    foreign = resolve(source)['123']
    assert foreign['settlementCogsKopecks'] is None and foreign['taxKopecks'] is None


@pytest.mark.parametrize('total', [100000, 100001, -100001])
def test_storage_10_30_uses_entire_snapshot_and_exact_signed_kopecks(total):
    snapshot = {'aggregates': {'123': {'wbStockUnits': 10}, '456': {'wbStockUnits': 30}}}
    result = wb_storage.allocate(total, snapshot)
    assert result['complete']
    assert sum(result['amounts'].values()) == total
    assert result['unallocatedKopecks'] == 0
    if total == 100000:
        assert result['amounts'] == {'123': 25000, '456': 75000}
    # Reordering display/catalogue inputs cannot alter tie-breaking.
    assert wb_storage.allocate(total, {'aggregates': dict(reversed(list(snapshot['aggregates'].items())))})['amounts'] == result['amounts']


@pytest.mark.parametrize('snapshot', [{}, {'sourceComplete': False, 'aggregates': {'123': {'wbStockUnits': 10}}},
    {'aggregates': {'123': {'wbStockUnits': None}}}, {'aggregates': {'123': {'wbStockUnits': 0}}}])
def test_incomplete_or_zero_stock_keeps_expense_at_account_level(snapshot):
    result = wb_storage.allocate(100000, snapshot)
    assert result['complete'] is False
    assert result['amounts'] == {}
    assert result['unallocatedKopecks'] == 100000


def financial_source():
    start, end = date(2026, 10, 5), date(2026, 10, 11)
    rows = [operation(1, nmId=123, vendorCode=ARTICLE, saleDt=str(start), rrDate=str(start),
                      quantity=10, retailAmount='10000', forPay='8000', paidStorage='1000'),
            operation(2, nmId=0, docTypeName='', sellerOperName='Хранение', quantity=0,
                      saleDt=str(end), rrDate=str(end), retailAmount='0', forPay='0', acquiringFee='0', paidStorage='2000')]
    source = page.project_realization_operations(rows, start, end)
    source.update(sourceComplete=True, revenueBasis=bff.FINANCE_REVENUE_BASIS, financeSchemaVersion=bff.FINANCE_SCHEMA_VERSION)
    return start, end, rows, source


def test_full_storage_replaces_direct_cost_once_and_includes_stocked_product_without_sales(inputs):
    from app.wb_report_finance import apply_storage_allocation
    start, end, operations, source = financial_source()
    before = deepcopy(operations)
    apply_storage_allocation(source, 1, start, end)
    a, b = source['aggregates']['123'], source['aggregates']['456']
    assert a['storageKopecks'] == 75000
    assert b['storageKopecks'] == 225000
    assert b['salesUnits'] == b['returnsUnits'] == 0
    assert source['unallocatedFinancials']['storageKopecks'] == 0
    assert sum(row['storageKopecks'] for row in source['aggregates'].values()) == 300000
    for nm, fact in source['aggregates'].items():
        assert sum(products.get(nm, {}).get('storageKopecks', 0) for products in source['dailyAggregates'].values()) == fact['storageKopecks']
    for day, products in source['dailyAggregates'].items():
        assert sum(row['storageKopecks'] for row in products.values()) == (100000 if day == str(start) else 200000)
    contexts = router._repricer_finance_taxes(1, source, start, end)
    metrics = {nm: bff.settlement_profit_metrics(fact, contexts[nm]) for nm, fact in source['aggregates'].items()}
    assert metrics['123']['settlementCogsKopecks'] == 400000
    assert metrics['456']['settlementCogsKopecks'] == 0
    assert metrics['456']['settlementProfitKopecks'] == -225000
    assert metrics['123']['settlementProfitKopecks'] == a['payableKopecks'] - 75000 - 400000 - 60000
    assert operations == before


def test_missing_stock_retains_known_account_profit_inputs_and_revision_changes(inputs):
    from app.wb_report_finance import apply_storage_allocation
    start, end, _, source = financial_source()
    before = wb_storage.revision(1)
    store.save_source_cache(1, 'stocks', {'sourceComplete': False, 'aggregates': {'123': {'wbStockUnits': 10}}})
    assert wb_storage.revision(1) != before
    apply_storage_allocation(source, 1, start, end)
    assert source['unallocatedFinancials']['storageKopecks'] == 300000
    assert source['aggregates']['123']['storageKopecks'] == 0
    assert resolve(source)['123']['settlementCogsKopecks'] == 400000
    assert resolve(source)['123']['taxKopecks'] == 60000


def test_wb_lowercase_article_resolves_saved_cost_but_conflicting_overrides_do_not(inputs):
    source = _cache({**_fact(100000, 3, 1), 'vendorCode': ARTICLE.lower()})
    inputs.get(WbRepricerRuntimeStateRow, 1).state_payload = {
        'skuSettingsOverrides': {ARTICLE.lower(): {'cogsKopecks': 40000}}}
    inputs.commit()
    assert resolve(source)['123']['settlementCogsKopecks'] == 80000
    inputs.get(WbRepricerRuntimeStateRow, 1).state_payload = {
        'skuSettingsOverrides': {ARTICLE: {'cogsKopecks': 40000}, ARTICLE.lower(): {'cogsKopecks': 50000}}}
    inputs.commit()
    assert resolve(source)['123']['settlementCogsKopecks'] is None


def test_stock_replacement_invalidates_derived_finance_revision(inputs):
    before = legacy_tax.legacy_finance_tax_revision(1)
    store.save_source_cache(1, 'stocks', {'sourceComplete': True,
        'aggregates': {'123': {'wbStockUnits': 30}, '456': {'wbStockUnits': 10}}})
    assert legacy_tax.legacy_finance_tax_revision(1) != before
    assert wb_storage.allocate(100000, wb_storage.load_stock(1))['amounts'] == {'123': 75000, '456': 25000}
