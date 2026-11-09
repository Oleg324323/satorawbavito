from datetime import date, datetime
from io import BytesIO
from types import SimpleNamespace
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest
from fastapi import HTTPException
from app import repricer_bff as bff
from app.routers import wb_repricer_bff as router
from app.repricer_table_export import COLUMNS, render_repricer_table


def product(nm_id=123, **kwargs):
    return bff._build_sku_row('SYNTHETIC', nm_id=nm_id, name='Full product title ' * 20,
        subject='Тест', brand=None, chrt_ids=[], current_price_kopecks=999999,
        discounted_price_kopecks=999999, buyer_price_kopecks=None,
        promotions=[], use_demo_data=False, **kwargs)


def test_reporting_keeps_gross_sales_returns_orders_and_money_distinct():
    row = product(finance_aggregate={'salesUnits':2160, 'returnsUnits':99,
        'grossSalesKopecks':540803999, 'sellerRevenueKopecks':517091440},
        baskets_aggregate=bff._sales_funnel_metrics({'orderCount':177, 'orderSum':371649,
            'buyoutCount':34, 'cancelCount':89, 'conversions':{'buyoutPercent':28}}))
    a = row['analytics']
    assert (a['grossSalesUnits'], a['grossSalesAmountKopecks']) == (2160, 540803999)
    assert (a['reportOrdersUnits'], a['ordersAmountKopecks']) == (177, 37164900)
    assert a['returnsUnits'] == 99
    assert a['buyoutPct'] == 28  # Not sales / orders.
    assert row['meta']['currentPriceKopecks'] == 999999


@pytest.mark.parametrize('value', [None, 0])
def test_missing_and_observed_zero_are_different(value):
    row = product(finance_aggregate=None if value is None else {'salesUnits':0,'grossSalesKopecks':0},
        baskets_aggregate=None if value is None else bff._sales_funnel_metrics({'orderCount':0,'orderSum':0,'conversions':{'buyoutPercent':0}}))
    for key in ('grossSalesUnits','grossSalesAmountKopecks','reportOrdersUnits','ordersAmountKopecks','buyoutPct'):
        assert row['analytics'][key] == value


def test_product_ids_map_selected_not_comparison_period():
    actual = {}
    for nm, pct in [(1025784485,28),(657172650,51),(867213853,67)]:
        bff._merge_sales_funnel_products(actual, [{'product':{'nmId':nm}, 'statistic':{
            'selected':{'orderCount':10,'orderSum':123.45,'conversions':{'buyoutPercent':pct}},
            'past':{'orderCount':999,'conversions':{'buyoutPercent':99}}}}])
    assert {nm:row['buyoutPct'] for nm,row in actual.items()} == {'1025784485':28,'657172650':51,'867213853':67}
    assert all(row['orderCount'] == 10 for row in actual.values())


def test_wb_collection_requests_exact_reporting_period_and_separate_comparison(monkeypatch):
    monkeypatch.setattr(bff,'build_wb_analytics_client',lambda *_a,**_kw:object())
    monkeypatch.setattr(bff,'RateLimitedWbApiClient',lambda **_kw:object())
    calls = []
    def request(_client, req, **_kwargs):
        calls.append(req.jsonBody)
        return {'data':{'products':[{'product':{'nmId':123},'statistic':{'selected':{
            'orderCount':2,'orderSum':123.45,'conversions':{'buyoutPercent':51}}}}]}}
    monkeypatch.setattr(bff,'_request_or_raise_sales_funnel_products',request)
    result = bff.fetch_baskets_aggregates('complete',period_days=6,date_from=datetime(2026,10,1),date_to=datetime(2026,10,6),nm_ids=[123])
    assert calls[0]['selectedPeriod'] == {'start':'2026-10-01','end':'2026-10-06'}
    assert calls[0]['pastPeriod'] == {'start':'2026-09-25','end':'2026-09-30'}
    assert result['aggregates']['123']['orderSumKopecks'] == 12345
    assert result['aggregates']['123']['buyoutPct'] == 51


def test_missing_order_amount_is_not_a_zero_even_when_other_funnel_fields_exist():
    assert bff._sales_funnel_metrics({'cartCount':1,'orderCount':2})['orderSumKopecks'] is None
    rolled = router._rollup_daily_aggregates({
        '2026-10-01':{'123':{'orderCount':2,'orderSumKopecks':None}},
        '2026-10-02':{'123':{'orderCount':3,'orderSumKopecks':10000}},
    },datetime(2026,10,1),datetime(2026,10,2))
    assert rolled['123']['orderSumKopecks'] is None
    assert rolled['123']['orderCount'] == 5


def test_exact_funnel_conversion_not_displaced_by_newer_broader_daily_cache(monkeypatch):
    exact = {'dateFrom':'2026-10-01','dateTo':'2026-10-06','fetchedAt':'2026-10-07T00:00:00Z',
        'aggregates':{'123':{'buyoutPct':28,'buyoutSource':'sales_funnel.conversions.buyoutPercent'}}}
    monkeypatch.setattr(router, 'get_source_cache', lambda *_a, **_kw: exact)
    monkeypatch.setattr(router, 'get_covering_source_cache', lambda *_a, **_kw: pytest.fail('must retain exact-period conversion'))
    actual = router._load_period_source_cache_base(1,'baskets','2026-10-01_2026-10-06',6,
        datetime(2026,10,1),datetime(2026,10,6),require_full_sync_coverage=False,prefer_freshest_covering=True)
    assert actual == exact


def test_xlsx_contains_all_products_columns_numeric_formats_and_period():
    items = [product(nm, finance_aggregate={'salesUnits':0,'grossSalesKopecks':12345},
        baskets_aggregate=bff._sales_funnel_metrics({'orderCount':0,'orderSum':0,'conversions':{'buyoutPercent':28}})) for nm in range(1, 622)]
    blob = render_repricer_table(items, date_from=date(2026,10,1), date_to=date(2026,10,6), filters='brand=test; sales desc',cache={'financeFreshnessState':'stale'})
    with ZipFile(BytesIO(blob)) as archive:
        assert len(archive.namelist()) == len(set(archive.namelist()))
        sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        ns = {'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        rows = sheet.findall('m:sheetData/m:row',ns)
        assert len(rows[1:622]) == 621
        assert all(len(row) == len(COLUMNS) for row in rows[1:622])
        values = dict(zip([c[0] for c in COLUMNS], rows[1]))
        assert values['WB-арт.'].get('t') == 'inlineStr'
        for label, val, style in [('Продажи, шт.','0','1'),('Продажи, ₽','123.45','2'),('Заказы, ₽','0.0','2'),('Выкуп','0.28','3')]:
            assert values[label].find('m:v',ns).text == val
            assert values[label].get('s') == style
        xml = archive.read('xl/worksheets/sheet1.xml').decode()
        assert '2026-10-01' in xml and '2026-10-06' in xml and 'stale' in xml
        assert 'brand=test; sales desc' in xml
        assert ('Full product title ' * 20).strip() in xml
        ET.fromstring(archive.read('xl/styles.xml'))


def test_export_endpoint_has_no_page_cap_and_preserves_requested_order(monkeypatch):
    from starlette.requests import Request
    actor = SimpleNamespace(organization_id=1, user_id="reader-without-token")
    monkeypatch.setattr(router,'actor_from_request', lambda _r:actor)
    monkeypatch.setattr(router,'_hydrate_org_repricer_state',lambda _r:1)
    monkeypatch.setattr(router,'get_user_wb_token_secret',lambda _user:None)
    monkeypatch.setattr(router,'get_organization_wb_token_secret',lambda org:"company-token" if org == 1 else pytest.fail("foreign organization"))
    def cache_meta(_organization_id, _days, *, date_from, date_to, include_content, require_full_sync_coverage):
        assert include_content is True and require_full_sync_coverage is False
        return {}
    monkeypatch.setattr(router,'_repricer_list_cache_meta',cache_meta)
    def load(_r,_scenario,**kwargs):
        from app.repricer_page_finance import connection_key, page_connection
        assert kwargs['wb_token'] == 'company-token'
        assert page_connection.get() == connection_key('company-token')
        assert kwargs['max_items'] is None
        assert kwargs['date_from'] == date(2026,10,1) and kwargs['date_to'] == date(2026,10,6)
        assert kwargs['require_full_sync_coverage'] is False
        return [product(nm) for nm in range(1, 622)]
    monkeypatch.setattr(router,'_list_repricer_skus_for_request',load)
    request = Request({'type':'http','headers':[],'query_string':b''})
    payload = router.RepricerTableExportRequest(dateFrom='2026-10-01',dateTo='2026-10-06',nmIds=list(range(621,0,-1)),sourceSignature=[None]*7)
    response = router.export_repricer_table(request,payload)
    assert response.status_code == 200
    with ZipFile(BytesIO(response.body)) as archive:
        root = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        ns = {'m':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        assert root.find('.//m:c[@r="D2"]/m:is/m:t',ns).text == '621'
        assert root.find('.//m:c[@r="D622"]/m:is/m:t',ns).text == '1'
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI(); app.include_router(router.router)
    with TestClient(app) as client:
        http = client.post('/api/v1/wb-repricer/table.xlsx',json=payload.model_dump(mode='json'))
        assert http.status_code == 200
        assert '2026-10-01-2026-10-06.xlsx' in http.headers['content-disposition']
    payload.sourceSignature = ['changed'] + [None]*6
    with pytest.raises(HTTPException) as exc:
        router.export_repricer_table(request,payload)
    assert exc.value.status_code == 409
    payload.sourceSignature = [None]*7
    observations = iter([{}, {'stocksFetchedAt':'changed-during-read'}])
    monkeypatch.setattr(router,'_repricer_list_cache_meta',lambda *_a,**_kw:next(observations))
    with pytest.raises(HTTPException) as exc:
        router.export_repricer_table(request,payload)
    assert exc.value.status_code == 409
