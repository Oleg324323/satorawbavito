from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select

from app import wb_sales_funnel as funnel
from app.platform.period import Period
from tests.test_wb_sales_funnel import db, catalog, observation_rows, seller_summary, wb_product


@pytest.mark.parametrize('field', [*funnel.FIELDS, 'revenueShare', 'cartConversion', 'orderConversion', 'buyoutPercent'])
def test_server_sort_is_numeric_stable_missing_last_and_before_pagination(db, field):
    period = Period(date(2026, 10, 12), date(2026, 10, 18))
    catalog(db, 61)
    scope, now = funnel.scope_key(1, 'synthetic'), datetime.now(timezone.utc)
    products = [wb_product(nm, shareOrderPercent=1) for nm in range(1, 62)]
    funnel.upsert(db, 1, observation_rows(scope, period.date_from, period.date_to, products, now))
    funnel.project(db, 1, 1, scope, period)
    seller_summary(db, scope, period, products)
    for cache in db.scalars(select(funnel.Cache).where(funnel.Cache.source_key.startswith(f'{scope}_page_'))):
        nm = int(cache.payload['nmId'])
        value = 70001 if nm == 61 else 65099 if nm == 1 else -101 if nm == 2 else None if nm == 3 else 0
        cache.payload = {**cache.payload, field: value}
    db.commit()
    baseline = funnel.read_page(db, 1, 1, scope, period)['totals']
    for direction, first in [('desc', '61'), ('asc', '2')]:
        page = funnel.read_page(db, 1, 1, scope, period, sort=field, direction=direction, page_size=50)
        assert page['items'][0]['nmId'] == first
        second = funnel.read_page(db, 1, 1, scope, period, sort=field, direction=direction, page=2, page_size=50)
        assert second['items'][-1]['nmId'] == '3'
        assert page['totals'] == second['totals'] == baseline
        all_ids = [item['nmId'] for item in page['items'] + second['items']]
        assert len(set(all_ids)) == 61


def test_product_sort_uses_first_displayed_seller_article_and_natural_text_before_pagination(db):
    period = Period(date(2026, 10, 12), date(2026, 10, 18))
    catalog(db, 4)
    products = db.scalars(select(funnel.Product).order_by(funnel.Product.external_product_id)).all()
    for row, article in zip(products, ['Товар 10', 'Товар 2', 'SKU 10', 'SKU 2']):
        row.seller_article = article
        row.title = 'Item'
    db.commit()
    scope = funnel.scope_key(1, 'synthetic')
    for direction, expected in [('asc', ['2', '1', '4', '3']), ('desc', ['3', '4', '1', '2'])]:
        result = []
        for page in (1, 2):
            result.extend(item['nmId'] for item in funnel.read_page(db, 1, 1, scope, period,
                sort='sku', direction=direction, page=page, page_size=2)['items'])
        assert result == expected
    assert [item['nmId'] for item in funnel.read_page(db, 1, 1, scope, period, sort='sku', query='Товар', direction='asc')['items']] == ['2', '1']
