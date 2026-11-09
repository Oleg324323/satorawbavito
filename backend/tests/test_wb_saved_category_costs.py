"""Use saved WB categories for arbitrary seller articles, without inventing COGS."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import wb_pnl_costs
from app.platform.economics.costs import CostsService
from app.platform.economics import legacy_tax
from app.platform.period import Period
from datetime import date
from app.repricer_cache.orm import WbRepricerSourceCacheRow
from app.repricer_persistence.orm import WbRepricerAlgorithmSettingsRow
from tests.test_wb_product_current_cost import editor, save  # noqa: F401
from tests.test_legacy_finance_tax import _cache, _fact


def cards(engine, *, subject='Футболки', article='TW55', observed=None, extra=None):
    with Session(engine) as db:
        settings = db.get(WbRepricerAlgorithmSettingsRow, 1)
        settings.settings_payload = {'taxPct': 6, 'cogsByGarmentRub': {'tshirt': 380, 'hoodie': 720, 'longsleeve': 440}}
        db.add(WbRepricerSourceCacheRow(organization_id=1, source_key='content_cards',
            fetched_at=observed or datetime(2026, 11, 1, tzinfo=timezone.utc),
            payload={'cards': [{'nmID': 123, 'vendorCode': article, 'subjectName': subject}], **(extra or {})}))
        db.commit()


def unit():
    return wb_pnl_costs.saved_current_costs(1, {'aggregates': {'123': {'vendorCode': 'tw55'}}}).get(123)


def test_unknown_prefix_uses_saved_category_and_net_cogs_for_the_selected_period(editor, monkeypatch):
    client, engine, _ = editor
    before = wb_pnl_costs.current_cost_revision(1)
    assert unit() is None
    cards(engine)
    assert unit() == 38000
    assert wb_pnl_costs.current_cost_revision(1) != before
    assert client.get('/api/v2/wb/products/123/current-cost').json()['data']['currentCost']['amountKopecks'] == 38000
    monkeypatch.setattr(legacy_tax, 'get_session_factory', lambda: lambda: Session(engine))
    fact = legacy_tax.get_legacy_finance_taxes(1, _cache(_fact(1000000, sales=4, returns=1)),
        Period(date(2026, 10, 5), date(2026, 10, 11)), include_costs=True, use_current_settings=True)['123']
    assert fact['settlementCogsKopecks'] == 114000
    assert fact['taxKopecks'] == 60000
    # An explicit product cost still wins, including a configured zero.
    assert save(client, amount=45000).status_code == 200
    assert unit() == 45000
    current = client.get('/api/v2/wb/products/123/current-cost').json()['data']['currentCost']['costVersionId']
    assert save(client, amount=0, version=current, key='zero').status_code == 200
    assert unit() == 0


@pytest.mark.parametrize('subject,expected', [('Футболки',38000), ('Худи',72000), ('Лонгсливы',44000), ('Неизвестная категория',None)])
def test_only_recognized_wb_categories_use_the_saved_cost(editor, subject, expected):
    _, engine, _ = editor
    cards(engine, subject=subject)
    assert unit() == expected


@pytest.mark.parametrize('options', [
    {'article': 'DIFFERENT'},
    {'observed': datetime(2020, 1, 1, tzinfo=timezone.utc)},
    {'extra': {'marketplaceAccountId': 99}},
    {'extra': {'organizationId': 99}},
    {'extra': {'cards': [{'nmID': 123, 'vendorCode': 'TW55', 'subjectName': 'Футболки'},
                         {'nmID': 123, 'vendorCode': 'TW55', 'subjectName': 'Худи'}]}},
])
def test_stale_foreign_conflicting_or_wrong_article_category_cannot_supply_cost(editor, options):
    _, engine, _ = editor
    cards(engine, **options)
    assert unit() is None


def test_card_does_not_manufacture_unconfigured_category_cost(editor):
    _, engine, _ = editor
    cards(engine)
    with Session(engine) as db:
        db.get(WbRepricerAlgorithmSettingsRow, 1).settings_payload = {'cogsByGarmentRub': {}}
        db.commit()
    assert unit() is None
