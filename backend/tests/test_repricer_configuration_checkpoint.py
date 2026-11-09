"""Authenticated configuration restoration; isolated SQLite, no provider I/O."""
from copy import deepcopy

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import repricer_configuration_checkpoint as checkpoint, repricer_dispatch_store as ledger
from app.control_plane.auth import ActorContext
from app.repricer_persistence.orm import WbRepricerRuntimeStateRow, WbRepricerChangelogRow
from app.routers import wb_repricer_bff as router
from tests.test_repricer_safety_regressions import ARTICLE, Provider, healthy_row, isolated


@pytest.fixture
def api(isolated, monkeypatch):
    actor = ActorContext('synthetic-user', 'synthetic-user', 1, 'admin', frozenset({'settings:write'}))
    provider = Provider()
    monkeypatch.setattr(router, 'actor_from_request', lambda request: actor)
    monkeypatch.setattr(router, '_request_wb_token', lambda request: provider.token)
    monkeypatch.setattr(router, '_goods_lookup', lambda org: ({101: ARTICLE}, {ARTICLE: 101}))
    monkeypatch.setattr(router, '_list_repricer_skus_for_request', lambda *args, **kwargs: [healthy_row(provider)])
    app = FastAPI()
    app.include_router(router.router)
    return TestClient(app), actor, provider


def url(suffix):
    return f'/api/v1/wb-repricer/sku/{ARTICLE}/{suffix}'


def create(api):
    client, _, _ = api
    before = client.get(url('configuration-state')).json()
    response = client.post(url('configuration-checkpoints'), json={'nmId': 101, 'expectedRevision': before['pricingRevision']})
    assert response.status_code == 200, response.text
    return response.json()


def restore(api, saved):
    client, _, _ = api
    current = client.get(url('configuration-state')).json()
    return client.post(url(f"configuration-checkpoints/{saved['checkpointId']}/restore"),
        json={'nmId': 101, 'expectedRevision': current['pricingRevision']})


def test_normal_api_restores_absence_after_assignment_and_none(api, isolated):
    client, _, provider = api
    with isolated() as session:
        runtime = session.get(WbRepricerRuntimeStateRow, 1)
        runtime.state_payload = {'skuSettingsOverrides': {'other': {'automationEnabled': True}},
            'skuMetaOverrides': {'other': {'status': 'auto'}}, 'cogsHistory': {'other': [{'value': 123}]},
            'pendingPriceApprovals': {}, 'unrelated': {'keep': True}}
        session.add(WbRepricerChangelogRow(organization_id=1, entry_id='historical', article_id=ARTICLE,
            entry_payload={'priceConfirmed': True, 'newPriceKopecks': 172000}))
        session.commit()
        before = deepcopy(runtime.state_payload)
    saved = create(api)
    for strategy in ['optimal_price', 'none']:
        revision = client.get(url('configuration-state')).json()['assignmentRevision']
        response = client.post('/api/v1/wb-repricer/strategy-assignments/bulk', json={
            'articleIds': [ARTICLE], 'strategyId': strategy, 'executeAfterAssign': False,
            'expectedRevisions': {ARTICLE: revision}})
        assert response.status_code == 200, response.text
    assert client.get(url('configuration-state')).json()['present']['skuSettingsOverrides'] is True
    response = restore(api, saved)
    assert response.status_code == 200, response.text
    assert not any(client.get(url('configuration-state')).json()['present'].values())
    with isolated() as session:
        after = session.get(WbRepricerRuntimeStateRow, 1).state_payload
        for key in ['cogsHistory', 'unrelated']:
            assert after[key] == before[key]
        assert after['skuSettingsOverrides'] == before['skuSettingsOverrides']
        assert after['skuMetaOverrides'] == before['skuMetaOverrides']
        assert session.get(WbRepricerChangelogRow, 1).entry_payload['newPriceKopecks'] == 172000
    assert not provider.calls
    assert restore(api, saved).status_code == 409


def test_stale_restore_does_not_overwrite_a_concurrent_change(api, isolated):
    client, _, _ = api
    saved = create(api)
    stale = client.get(url('configuration-state')).json()['pricingRevision']
    with isolated() as session:
        runtime = session.get(WbRepricerRuntimeStateRow, 1)
        runtime.state_payload = {'skuMetaOverrides': {ARTICLE: {'status': 'manual'}}}
        session.commit()
    response = client.post(url(f"configuration-checkpoints/{saved['checkpointId']}/restore"),
        json={'nmId': 101, 'expectedRevision': stale})
    assert response.status_code == 409
    with isolated() as session:
        assert session.get(WbRepricerRuntimeStateRow, 1).state_payload['skuMetaOverrides'][ARTICLE]['status'] == 'manual'


@pytest.mark.parametrize('state', ['reserved', 'dispatched', 'submitted', 'processing', 'needs_attention'])
@pytest.mark.parametrize('receipt_kind', ['i', 'p'])
def test_restore_rejects_unresolved_intent_or_product_receipt(api, isolated, state, receipt_kind):
    saved = create(api)
    with ledger.transaction(org=1) as session:
        ledger._put(session, ledger._key(receipt_kind, 'synthetic'), 1, {'nmId': 101, 'state': state})
    assert restore(api, saved).status_code == 409


@pytest.mark.parametrize('status', ['pending', 'applying', 'sent', 'needs_attention'])
def test_restore_rejects_pending_work(api, isolated, status):
    saved = create(api)
    with isolated() as session:
        row = session.get(WbRepricerRuntimeStateRow, 1)
        row.state_payload = {'pendingPriceApprovals': {'synthetic': {'articleId': ARTICLE, 'status': status}}}
        session.commit()
    assert restore(api, saved).status_code == 409


@pytest.mark.parametrize('field,value', [('binding', 'different'), ('actor', 'other'), ('nmId', 102), ('articleId', 'other')])
def test_checkpoint_cannot_cross_binding_actor_or_product(api, field, value):
    saved = create(api)
    with ledger.transaction(org=1) as session:
        row = ledger._row(session, saved['checkpointId'], 1)
        row.report_payload = {**row.report_payload, field: value}
    assert restore(api, saved).status_code == 409


def test_checkpoint_cannot_cross_tenants(api):
    saved = create(api)
    with ledger.transaction(org=1) as session:
        receipt = deepcopy(ledger._row(session, saved['checkpointId'], 1).report_payload)
    with pytest.raises(Exception) as error:
        checkpoint.restore_checkpoint(org=2, nm=101, article=ARTICLE, binding=receipt['binding'],
            actor=receipt['actor'], checkpoint_id=saved['checkpointId'],
            expected_revision=checkpoint.configuration_state(2, ARTICLE)['pricingRevision'])
    assert error.value.status_code == 404


def test_identity_permission_and_original_presence_are_enforced(api, isolated, monkeypatch):
    client, actor, _ = api
    revision = client.get(url('configuration-state')).json()['pricingRevision']
    assert client.post(url('configuration-checkpoints'), json={'nmId': 102, 'expectedRevision': revision}).status_code == 409
    monkeypatch.setattr(router, 'actor_from_request', lambda request: ActorContext('v', 'v', 1, 'viewer', frozenset()))
    assert client.get(url('configuration-state')).status_code == 403
    monkeypatch.setattr(router, 'actor_from_request', lambda request: actor)
    with isolated() as session:
        row = session.get(WbRepricerRuntimeStateRow, 1)
        row.state_payload = {'skuSettingsOverrides': {ARTICLE: {'automationEnabled': False}}}
        session.commit()
    revision = client.get(url('configuration-state')).json()['pricingRevision']
    assert client.post(url('configuration-checkpoints'), json={'nmId': 101, 'expectedRevision': revision}).status_code == 409


def test_missing_auth_is_rejected(isolated):
    app = FastAPI()
    app.include_router(router.router)
    client = TestClient(app)
    assert client.get(url('configuration-state')).status_code == 401


def test_scope_keeps_catalog_article_case(api, monkeypatch):
    client, _, _ = api
    monkeypatch.setattr(router, '_goods_lookup', lambda org: ({101: 'Фбbt_3036'}, {'фбbt_3036': 101}))
    response = client.get('/api/v1/wb-repricer/sku/фбbt_3036/configuration-state')
    assert response.status_code == 200, response.text
    assert response.json()['articleId'] == 'Фбbt_3036'
    assert response.json()['nmId'] == 101
