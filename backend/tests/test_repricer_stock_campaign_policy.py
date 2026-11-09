"""One owner-defined profit basis for WB repricer margin and ABC."""
from copy import deepcopy
import pytest
from app import repricer_bff as bff
from app.routers import wb_repricer_bff as router
from app.wb_repricer_reporting import apply_campaign_advertising, apply_repricer_economics
from app.wb_report_finance import apply_storage_allocation
from tests.test_wb_current_economics import inputs, financial_source  # noqa: F401
from tests.test_legacy_finance_tax import tax_db  # noqa: F401
from tests.test_repricer_settlement_abc import finance, context


def test_storage_stock_shares_ads_and_ignored_deduction_unblock_second_letter(inputs):
    start, end, operations, ledger = financial_source()
    original = deepcopy(operations)
    apply_storage_allocation(ledger, 1, start, end)
    contexts = router._repricer_finance_taxes(1, ledger, start, end)
    # The cash report still has unapportioned ad charges and deductions.
    ledger['unallocatedFinancials'].update(adSpendKopecks=90000, deductionKopecks=4400)
    ledger['diagnostics']['rawExpenseTotals'].update(adSpendKopecks=90000, deductionKopecks=4400)
    contexts = router._repricer_finance_taxes(1, ledger, start, end)
    ads = dict(state='ok', adSpendKopecks=3000, aggregates={
        '123': {'adSpendKopecks': 1000}, '456': {'adSpendKopecks': 2000}})
    items = [{'meta': {'nmId': nm}, 'analytics': {'financeState': 'ok', 'revenueKopecks': fact.get('sellerRevenueKopecks', 0),
        **bff.settlement_profit_metrics(fact, contexts[nm])}}
        for nm, fact in ledger['aggregates'].items()]
    before = deepcopy(ledger)
    apply_campaign_advertising(items, ledger, ads)
    apply_repricer_economics(items, ledger, ads, contexts)
    facts = {str(row['meta']['nmId']): row['analytics'] for row in items}
    assert ledger['aggregates']['123']['storageKopecks'] == 75000  # 10 / 40
    assert ledger['aggregates']['456']['storageKopecks'] == 225000  # 30 / 40
    assert facts['123']['campaignProfitKopecks'] == ledger['aggregates']['123']['payableKopecks'] - 75000 - 400000 - 60000 - 1000
    assert facts['456']['campaignProfitKopecks'] == -225000 - 2000
    assert facts['123']['abcCode'] == 'AA'
    assert facts['456']['profitClass'] == 'C'
    summary = router._repricer_list_summary(items, campaign_ads=ads,
        finance_diagnostics=router._limited_finance_diagnostics(ledger, limit=1))
    assert summary['campaignProfitKopecks'] == sum(fact['campaignProfitKopecks'] for fact in facts.values())
    assert ledger == before and operations == original


def scenario(*, allocated=True, spend=2500, missing=False):
    source = finance(deductionKopecks=None, adSpendKopecks=None)
    source['rowsCount'] = 1
    contexts = {'101': context(campaignAllocationConfirmed=allocated)}
    if missing: contexts['101']['settlementCogsKopecks'] = None
    ledger = {'sourceComplete': True, 'aggregates': {'101': source}}
    ads = {'state': 'ok', 'adSpendKopecks': spend, 'aggregates': {'101': {'adSpendKopecks': spend}}}
    items = [{'meta': {'nmId': 101}, 'analytics': {'financeState': 'ok', 'revenueKopecks': 100000000,
        'expensesKopecks': 7000, **bff.settlement_profit_metrics(source, contexts['101'])}}]
    return items, ledger, ads, contexts


def test_financial_advertising_and_deduction_unknown_do_not_block_known_product_spend():
    items, ledger, ads, contexts = scenario()
    original = deepcopy(ledger)
    apply_repricer_economics(items, ledger, ads, contexts)
    a = items[0]['analytics']
    expected = 85000000 - 7000000 - 1000000 - 500000 - 200000 - 40000000 - 7500000 - 2500
    assert a['campaignProfitKopecks'] == expected
    assert a['abcCode'] == 'AA' and a['campaignBlockers'] == []
    apply_repricer_economics(items, ledger, ads, contexts)
    assert a['campaignProfitKopecks'] == expected
    assert ledger == original


@pytest.mark.parametrize('reason', ['storage', 'cogs', 'ads', 'finance'])
def test_real_missing_inputs_remain_unknown(reason):
    items, ledger, ads, contexts = scenario(allocated=reason != 'storage', missing=reason == 'cogs')
    if reason == 'ads': ads['state'] = 'partial'
    if reason == 'finance': ledger['sourceComplete'] = False
    apply_repricer_economics(items, ledger, ads, contexts)
    assert items[0]['analytics']['campaignProfitKopecks'] is None
    assert items[0]['analytics']['abcCode'] == 'A—'


def test_classes_use_the_whole_financial_universe_not_the_displayed_page():
    items, ledger, ads, contexts = scenario()
    ledger['aggregates']['202'] = finance(salesUnits=1000)
    contexts['202'] = context(campaignAllocationConfirmed=True, settlementCogsKopecks=0)
    one = deepcopy(items)
    all_rows = deepcopy(items) + [{'meta': {'nmId':202}, 'analytics':{}}]
    apply_repricer_economics(one, ledger, ads, contexts)
    apply_repricer_economics(all_rows, ledger, ads, contexts)
    assert one[0]['analytics']['abcCode'] == all_rows[0]['analytics']['abcCode']


def test_ads_only_product_has_known_zero_cogs_tax_and_negative_profit():
    items, ledger, ads, contexts = scenario()
    ads['aggregates']['202'] = {'adSpendKopecks': 1000}
    ads['adSpendKopecks'] += 1000
    items.append({'meta': {'nmId':202}, 'analytics': {'financeState':'no_data','revenueKopecks':0}})
    apply_repricer_economics(items, ledger, ads, contexts)
    a=items[1]['analytics']
    assert a['campaignProfitKopecks'] == -1000
    assert a['settlementCogsKopecks'] == a['taxKopecks'] == 0
    assert a['factTaxState'] == 'configured'
