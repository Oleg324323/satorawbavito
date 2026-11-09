"""Manual mode through real authentication and disposable persisted SQLite."""
from copy import deepcopy

import pytest
from sqlalchemy import create_engine, select

from app import repricer_bff as bff, repricer_dispatch_store as ledger, repricer_execution as execution
from app.cabinet.orm import LkUserRow
from app.repricer_persistence.orm import WbRepricerRuntimeStateRow, WbRepricerExecutionRunRow
from app.routers import wb_repricer_bff as router
from tests.test_repricer_assignment_auth_sqlite import api, assign, state
from tests.test_repricer_dispatch_concurrency import intent, race
from tests.test_repricer_safety_regressions import ARTICLE


@pytest.fixture
def manual_api(api, monkeypatch):
    original_rows = router._list_repricer_skus_for_request

    def cached_rows(*args, **kwargs):
        rows = original_rows(*args, **kwargs)
        for row in rows:
            row['settings'].update(deepcopy(bff.SKU_SETTINGS_OVERRIDES.get(row['meta']['articleId']) or {}))
        return rows

    monkeypatch.setattr(router, '_list_repricer_skus_for_request', cached_rows)
    monkeypatch.setattr(bff, 'list_repricer_skus', cached_rows)
    assert assign(api, 'plan_fact_group', config={
        'groupName': 'Synthetic group', 'groupArticleIds': [ARTICLE, 'synthetic-102'], 'planOrders': 9}).status_code == 200
    return api


def pause(api, expected=True, enabled=False):
    body = {'automationEnabled': enabled}
    if expected:
        body['expectedRevision'] = ledger.assignment_revision(state(api), ARTICLE)
    return api.client.patch(f'/api/v1/wb-repricer/sku/{ARTICLE}/automation', json=body)


def test_pause_persists_without_changing_strategy_or_inherited_economics(manual_api):
    api = manual_api
    before, other_org = state(api), state(api, 2)
    response = pause(api)
    assert response.status_code == 200, response.text
    after = state(api)
    assert after['assignments'] == before['assignments']
    assert after['skuSettingsOverrides'][ARTICLE] == {**before['skuSettingsOverrides'][ARTICLE], 'automationEnabled': False}
    assert after['skuMetaOverrides'][ARTICLE] == {**before['skuMetaOverrides'][ARTICLE], 'status': 'manual'}
    assert after['skuSettingsOverrides']['synthetic-102'] == before['skuSettingsOverrides']['synthetic-102']
    assert state(api, 2) == other_org
    assert after['unrelated'] == before['unrelated']
    assert response.json()['assignmentRevisions'][ARTICLE] == ledger.assignment_revision(after, ARTICLE)
    # A separate engine and connection prove the committed state is not a
    # module-global illusion. The runner permits only this disposable database.
    reopened = create_engine(api.factory.kw['bind'].url)
    with reopened.connect() as connection:
        payload = connection.execute(select(WbRepricerRuntimeStateRow.state_payload)
            .where(WbRepricerRuntimeStateRow.organization_id == 1)).scalar_one()
    reopened.dispose()
    assert payload == after
    bff.FRONTEND_STRATEGY_ASSIGNMENTS.clear()
    bff.SKU_SETTINGS_OVERRIDES.clear()
    bff.SKU_META_OVERRIDES.clear()
    fresh = api.client.get(f'/api/v1/wb-repricer/sku/{ARTICLE}/settings')
    assert fresh.status_code == 200
    assert fresh.json()['settings']['automationEnabled'] is False
    assert fresh.json()['meta']['status'] == 'manual'
    assert fresh.json()['strategy']['config']['planOrders'] == 9
    assert not api.provider.calls


def test_legacy_boolean_payload_and_explicit_resume_remain_supported(manual_api):
    assert pause(manual_api, expected=False).status_code == 200
    saved = deepcopy(state(manual_api)['assignments'][ARTICLE])
    assert pause(manual_api, enabled=True).status_code == 200
    assert state(manual_api)['skuSettingsOverrides'][ARTICLE]['automationEnabled'] is True
    assert state(manual_api)['assignments'][ARTICLE] == saved


def test_stale_manual_request_is_rejected_without_losing_newer_assignment(manual_api):
    api = manual_api
    old = ledger.assignment_revision(state(api), ARTICLE)
    assert assign(api, 'optimal_price').status_code == 200
    before = state(api)
    response = api.client.patch(f'/api/v1/wb-repricer/sku/{ARTICLE}/automation',
        json={'automationEnabled': False, 'expectedRevision': old})
    assert response.status_code == 409
    assert state(api) == before


@pytest.mark.parametrize('mode,expected', [('warmup', 'warmup'), ('liquidation', 'liquidation')])
def test_pause_preserves_lifecycle_and_unrelated_overrides(manual_api, mode, expected):
    api = manual_api
    with api.factory() as session:
        runtime = session.get(WbRepricerRuntimeStateRow, 1)
        value = deepcopy(runtime.state_payload)
        value['skuMetaOverrides'][ARTICLE]['status'] = mode
        value['skuSettingsOverrides'][ARTICLE].update(cogsKopecks=38000, taxPct=22, custom='keep')
        if mode == 'liquidation':
            value['liquidationActive'] = {ARTICLE: {'stepPct': 3, 'targetPriceKopecks': 100000}}
        runtime.state_payload = value
        session.commit()
    before = state(api)
    assert pause(api).status_code == 200
    after = state(api)
    assert after['skuMetaOverrides'][ARTICLE]['status'] == expected
    assert after['skuSettingsOverrides'][ARTICLE] == {**before['skuSettingsOverrides'][ARTICLE], 'automationEnabled': False}
    assert after.get('liquidationActive') == before.get('liquidationActive')
    assert after['assignments'] == before['assignments']


def test_repeated_pause_is_idempotent_and_does_not_materialize_default_settings(manual_api):
    api = manual_api
    with api.factory() as session:
        runtime = session.get(WbRepricerRuntimeStateRow, 1)
        value = deepcopy(runtime.state_payload)
        value['skuSettingsOverrides'].pop(ARTICLE)
        runtime.state_payload = value
        session.commit()
    assert pause(api).status_code == 200
    saved = state(api)
    assert saved['skuSettingsOverrides'][ARTICLE] == {'automationEnabled': False}
    assert pause(api).status_code == 200
    assert state(api) == saved


@pytest.mark.parametrize('invalid,expected', [('missing_auth', 401), ('viewer', 403), ('foreign_product', 404)])
def test_manual_mutation_requires_authorization_and_catalog_scope(manual_api, invalid, expected):
    api = manual_api
    before, other = state(api), state(api, 2)
    article = ARTICLE
    if invalid == 'missing_auth':
        del api.client.headers['Authorization']
    elif invalid == 'viewer':
        with api.factory() as session:
            user = session.get(LkUserRow, 'synthetic-user')
            user.permission_profile = 'viewer'
            session.commit()
    else:
        article = 'only-in-another-account'
    response = api.client.patch(f'/api/v1/wb-repricer/sku/{article}/automation', json={'automationEnabled': False})
    assert response.status_code == expected, response.text
    assert state(api) == before and state(api, 2) == other
    assert not api.provider.calls


@pytest.mark.parametrize('night', [False, True])
def test_pause_blocks_normal_and_night_execution_before_any_provider_call(manual_api, night):
    api = manual_api
    assert pause(api).status_code == 200
    row = api.client.get(f'/api/v1/wb-repricer/sku/{ARTICLE}/settings').json()
    if night:
        row['strategy'].update(id='night_price_mode', typedStrategyId='night_price_mode')
    result = execution._execute_single_sku(row, client=api.provider, actor_id='synthetic-user',
        actor_role='admin', organization_id=1, all_rows=[row], options=execution.StrategyExecuteOptions())
    assert result.status == 'skipped' and result.skipReason == 'automation_disabled'
    assert not result.draftId and not result.jobId and not api.provider.calls


@pytest.mark.parametrize('reserved', [False, True])
def test_pause_revokes_previously_eligible_unsent_approval_and_retry(manual_api, reserved):
    api = manual_api
    proposal = intent(1)
    keys = ledger.claim(1, 'synthetic-binding', [proposal])
    assert keys, 'The unpaused proposal must be eligible, independent of provider write flags'
    if not reserved:
        ledger.transition(1, keys, 'dispatched')
        # Control case: an already dispatched upload remains reconcilable.
    assert pause(api).status_code == 200
    if reserved:
        with pytest.raises(ledger.DispatchPersistenceError, match='pricing_settings_changed'):
            ledger.transition(1, keys, 'dispatched')
        assert ledger.load_receipt(1, keys[0])['state'] == 'reserved'
    else:
        ledger.transition(1, keys, 'processing', result={'wbMutationSent': True, 'wbUploadId': 17})
        assert ledger.load_receipt(1, keys[0])['state'] == 'processing'
    assert ledger.claim(1, 'synthetic-binding', [proposal]) is None
    assert not api.provider.uploads


def test_pause_rejects_an_approved_but_unclaimed_draft_and_only_its_pending_approval(manual_api):
    api = manual_api
    proposal = intent(1)
    with api.factory() as session:
        runtime = session.get(WbRepricerRuntimeStateRow, 1)
        algorithm = ledger._lock_pricing_algorithm(session, 1)
        ledger._validate_dispatch_revision(session, 1, runtime, proposal, algorithm=algorithm)
        value = deepcopy(runtime.state_payload)
        value['pendingPriceApprovals'] = {
            'target': {'articleId': ARTICLE, 'status': 'pending'},
            'other': {'articleId': 'synthetic-102', 'status': 'pending'},
        }
        runtime.state_payload = value
        session.commit()
    assert pause(api).status_code == 200
    saved = state(api)
    assert saved['pendingPriceApprovals']['target']['status'] == 'rejected'
    assert saved['pendingPriceApprovals']['other']['status'] == 'pending'
    assert ledger.claim(1, 'synthetic-binding', [proposal]) is None
    assert not api.provider.uploads


def test_concurrent_pause_and_strategy_change_cannot_both_overwrite_the_same_revision(manual_api):
    api = manual_api
    old = ledger.assignment_revision(state(api), ARTICLE)
    results = race([
        lambda: api.client.patch(f'/api/v1/wb-repricer/sku/{ARTICLE}/automation',
            json={'automationEnabled': False, 'expectedRevision': old}),
        lambda: assign(api, 'stockout_guard', revision=old),
    ])
    assert sorted(response.status_code for response in results) == [200, 409]
    saved = state(api)
    if results[0].status_code == 200:
        assert saved['skuSettingsOverrides'][ARTICLE]['automationEnabled'] is False
        assert saved['assignments'][ARTICLE]['strategyId'] == 'plan_fact_group'
    else:
        assert saved['skuSettingsOverrides'][ARTICLE]['automationEnabled'] is True
        assert saved['assignments'][ARTICLE]['strategyId'] == 'stockout_guard'
    assert state(api, 2) == api.initial
    assert not api.provider.calls
