"""The WB table persists an explicit edit even after the internal catalog was cleared."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from threading import Barrier

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session

from app.infra.models import Base
from app.infra.db import get_db_session
from app.control_plane.auth import ActorContext
from app.cabinet.orm import LkOrganizationRow, LkUserRow
from app.platform.identity.orm import IamMembershipRow
from app.platform.integrations.orm import MarketplaceAccountRow
from app.platform.catalog.orm import CatalogSkuRow, MarketplaceProductRow, MarketplaceOfferRow
from app.platform.economics.orm import CatalogCostVersionRow
from app.platform.economics.costs import CostsService
from app.repricer_cache.orm import WbRepricerGoodsCacheRow
from app.repricer_persistence.orm import WbRepricerAlgorithmSettingsRow
from app.routers import catalog_v2
from app import wb_pnl_costs, wb_pnl_advertising, repricer_bff


@pytest.fixture
def editor(tmp_path, monkeypatch):
    engine = create_engine(f'sqlite:///{tmp_path / "editor.db"}', connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all([LkOrganizationRow(organization_id=1, slug='editor', name='Editor'),
            LkUserRow(user_id='editor', organization_id=1, email='editor@example.invalid', password_hash='unusable', full_name='Test', permission_profile='owner'),
            IamMembershipRow(membership_id=1, organization_id=1, user_id='editor', role='owner'),
            MarketplaceAccountRow(marketplace_account_id=12, organization_id=1, marketplace='wb', external_account_id='test', status='connected'),
            WbRepricerAlgorithmSettingsRow(organization_id=1, settings_payload={'cogsByGarmentRub': {'hoodie': 720}}),
            WbRepricerGoodsCacheRow(organization_id=1, page_offset=0, page_limit=1000,
                goods_payload=[{'nmID': 123, 'vendorCode': 'TW55'}, {'nmID': 456, 'vendorCode': 'Х_test'}],
                fetched_at=datetime(2026, 11, 1, tzinfo=timezone.utc))])
        db.commit()
    context = {'marketplaceAccountId': 12, 'connectedAt': '2026-10-01T00:00:00+00:00'}
    monkeypatch.setattr(wb_pnl_advertising, '_source_context', lambda org: context if org == 1 else None)
    monkeypatch.setattr(wb_pnl_costs, 'get_session_factory', lambda: lambda: Session(engine))
    actor = ActorContext('editor', 'editor', 1, 'owner', frozenset({'catalog:read', 'costs:read', 'costs:write'}))
    app = FastAPI(); app.include_router(catalog_v2.router)
    app.dependency_overrides[catalog_v2.get_catalog_actor] = lambda: actor
    def session():
        with Session(engine) as db:
            yield db
    app.dependency_overrides[get_db_session] = session
    return TestClient(app), engine, app


def save(client, nm=123, amount=45000, version=None, key='first'):
    return client.post(f'/api/v2/wb/products/{nm}/current-cost', json={
        'amountKopecks': amount, 'expectedCostVersionId': version, 'sourceReference': key})


def test_missing_mapping_read_is_pure_then_edit_survives_new_session_and_reuse(editor, monkeypatch):
    client, engine, _ = editor
    before = client.get('/api/v2/wb/products/123/current-cost')
    assert before.status_code == 200, before.text
    assert before.json()['data']['catalogSkuId'] is None
    assert before.json()['data']['currentCost']['amountKopecks'] is None
    category = client.get('/api/v2/wb/products/456/current-cost').json()['data']
    assert category['currentCost']['amountKopecks'] == 72000
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(CatalogSkuRow)) == 0
        assert db.scalar(select(func.count()).select_from(CatalogCostVersionRow)) == 0
    response = save(client)
    assert response.status_code == 200, response.text
    saved = response.json()['data']
    assert saved['amountKopecks'] == 45000
    assert save(client).json()['data']['costVersionId'] == saved['costVersionId']
    again = client.get('/api/v2/wb/products/123/current-cost').json()['data']
    assert again['currentCost']['amountKopecks'] == 45000
    with Session(engine) as db:
        assert CostsService(db, 1).get_current_cost(again['catalogSkuId']).amount_kopecks == 45000
        assert db.scalar(select(func.count()).select_from(CatalogCostVersionRow)) == 1
    assert wb_pnl_costs.saved_current_costs(1, {'aggregates': {'123': {'vendorCode': 'tw55'}}}) == {123: 45000}
    # The actual row sent to the editor uses the same saved value, not its
    # unknown-article price-planning default. No WB API calls are permitted.
    monkeypatch.setattr(repricer_bff, '_demo_repricer_data_enabled', lambda token: False)
    monkeypatch.setattr(repricer_bff, 'cached_commission_tariffs', lambda *a, **k: {})
    from app.platform.economics import legacy_catalog
    monkeypatch.setattr(legacy_catalog, 'catalog_facts', lambda org: {})
    rows = repricer_bff.list_repricer_skus(organization_id=1, cached_goods=[
        {'nmID': 123, 'vendorCode': 'TW55', 'sizes': []},
        {'nmID': 456, 'vendorCode': 'Х_test', 'sizes': []},
        {'nmID': 789, 'vendorCode': 'TB_unknown', 'sizes': []}],
        include_promotions=False, include_content=False, cached_content_cards=[], allow_commission_tariff_fetch=False)
    assert {r['meta']['nmId']: r['settings']['savedCogsKopecks'] for r in rows} == {123: 45000, 456: 72000, 789: None}


def test_zero_conflict_and_unknown_product_do_not_create_fake_costs(editor):
    client, engine, _ = editor
    version = save(client).json()['data']['costVersionId']
    assert save(client, amount=50000, key='stale').status_code == 409
    zero = save(client, amount=0, version=version, key='zero')
    assert zero.status_code == 200, zero.text
    assert client.get('/api/v2/wb/products/123/current-cost').json()['data']['currentCost']['amountKopecks'] == 0
    assert save(client, nm=999).status_code == 404
    assert save(client, nm=-1).status_code == 422
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(CatalogSkuRow)) == 1
        assert db.scalar(select(func.count()).select_from(CatalogCostVersionRow)) == 2


def test_saved_edit_recalculates_net_cogs_for_any_selected_period(editor, monkeypatch):
    from app.platform.economics import legacy_tax
    from app.platform.period import Period
    from tests.test_legacy_finance_tax import _cache, _fact
    client, engine, _ = editor
    monkeypatch.setattr(legacy_tax, 'get_session_factory', lambda: lambda: Session(engine))
    source = _cache(_fact(1000000, sales=4, returns=1))
    def calculated(year):
        return legacy_tax.get_legacy_finance_taxes(1, source,
            Period(date(year, 10, 5), date(year, 10, 11)),
            include_costs=True, use_current_settings=True)['123']['settlementCogsKopecks']
    assert calculated(2020) is None
    first = save(client).json()['data']
    assert calculated(2020) == calculated(2026) == 135000
    response = save(client, amount=50000, version=first['costVersionId'], key='updated')
    assert response.status_code == 200, response.text
    assert calculated(2020) == calculated(2026) == 150000


def test_first_save_rolls_back_binding_when_version_is_stale(editor):
    client, engine, _ = editor
    assert save(client, version=999).status_code == 409
    with Session(engine) as db:
        for model in (CatalogSkuRow, MarketplaceProductRow, MarketplaceOfferRow, CatalogCostVersionRow):
            assert db.scalar(select(func.count()).select_from(model)) == 0


def test_concurrent_first_save_has_one_winner_and_one_cost(editor):
    client, engine, _ = editor
    barrier = Barrier(2)
    def edit(index):
        barrier.wait()
        return save(client, amount=40000 + index, key=f'concurrent-{index}').status_code
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(edit, (1, 2))) == [200, 409]
    with Session(engine) as db:
        for model in (CatalogSkuRow, MarketplaceProductRow, MarketplaceOfferRow, CatalogCostVersionRow):
            assert db.scalar(select(func.count()).select_from(model)) == 1


def test_scope_stale_cache_and_ambiguous_mapping(editor):
    client, engine, _ = editor
    with Session(engine) as db:
        member = db.get(IamMembershipRow, 1)
        member.scope_mode = 'accounts'; member.allowed_account_ids = [99]; db.commit()
    assert save(client).status_code == 403
    with Session(engine) as db:
        member = db.get(IamMembershipRow, 1); member.scope_mode = 'all'
        goods = db.scalar(select(WbRepricerGoodsCacheRow)); goods.fetched_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        db.commit()
    assert save(client).status_code == 404
    with Session(engine) as db:
        db.add_all([CatalogSkuRow(catalog_sku_id=1, organization_id=1, code='one'), CatalogSkuRow(catalog_sku_id=2, organization_id=1, code='two'),
            MarketplaceProductRow(marketplace_product_id=1, organization_id=1, marketplace_account_id=12, external_product_id='123')]); db.flush()
        for i in (1, 2):
            db.add(MarketplaceOfferRow(organization_id=1, marketplace_account_id=12, marketplace_product_id=1, catalog_sku_id=i, external_offer_key=str(i)))
        db.commit()
    assert save(client).status_code == 409
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(CatalogCostVersionRow)) == 0
