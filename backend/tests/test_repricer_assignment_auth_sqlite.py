"""Real persisted authentication and separate sessions on disposable SQLite.

Run separately from repricer_offline_plugin, which permits only :memory:.
The opt-in audit runner fences all network and non-test storage before imports.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import DateTime, create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.types import TypeDecorator

from app import repricer_bff as bff, repricer_dispatch_store as ledger
from app.cabinet import store as cabinet
from app.cabinet.orm import (LkAuditEventRow, LkOrganizationRow, LkSessionRow,
    LkUserPermissionRow, LkUserRow, LkUserWbTokenRow)
from app.control_plane import auth
from app.infra import db
from app.repricer_cache import store as cache
from app.repricer_cache.orm import WbRepricerSourceCacheRow
from app.repricer_execution import StrategyExecuteReport
from app.repricer_persistence import store as persistence
from app.repricer_persistence.orm import (WbRepricerAlgorithmSettingsRow,
    WbRepricerChangelogRow, WbRepricerExecutionRunRow, WbRepricerRuntimeStateRow)
from app.routers import auth as auth_router, wb_repricer_bff as router
from tests.test_repricer_safety_regressions import ARTICLE, Provider, healthy_row


class PreviewUtcDateTime(TypeDecorator):
    """Match the existing local launcher's SQLite UTC restoration."""
    impl = DateTime
    cache_ok = True

    def process_result_value(self, value, dialect):
        return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


@pytest.fixture
def api(tmp_path, monkeypatch):
    database = tmp_path / 'synthetic-repricer.sqlite'
    monkeypatch.setenv('VELLA_ENV', 'test')
    monkeypatch.setenv('VELLA_DATABASE_URL', f'sqlite:///{database}')
    monkeypatch.setenv('VELLA_WB_API_MODE', 'fake')
    monkeypatch.setenv('VELLA_CABINET_REQUIRE_PERSISTENT_STORAGE', 'true')
    monkeypatch.setenv('VELLA_AUTH_SECRET', 'synthetic-assignment-regression-secret-only')
    engine = create_engine(f'sqlite:///{database}', connect_args={'timeout': .1})
    models = (LkOrganizationRow, LkUserRow, LkUserPermissionRow, LkSessionRow,
        LkUserWbTokenRow, LkAuditEventRow, WbRepricerRuntimeStateRow,
        WbRepricerAlgorithmSettingsRow, WbRepricerExecutionRunRow,
        WbRepricerChangelogRow, WbRepricerSourceCacheRow)
    for model in models:
        for column in model.__table__.columns:
            if isinstance(column.type, DateTime) and column.type.timezone:
                monkeypatch.setattr(column, 'type', PreviewUtcDateTime(timezone=True))
        model.__table__.create(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, 'get_engine', lambda: engine)
    monkeypatch.setattr(db, 'get_session_factory', lambda: factory)
    monkeypatch.setattr(cabinet, 'get_engine', lambda: engine)
    for module in (cabinet, ledger, persistence, cache):
        monkeypatch.setattr(module, 'get_session_factory', lambda: factory)
    monkeypatch.setattr(cache, '_redis_available', lambda: False)
    for name in ('_load_file_runtime_state', '_load_file_algorithm_settings'):
        monkeypatch.setattr(persistence, name, lambda *args: None)
    for name in ('_save_file_runtime_state', '_save_file_algorithm_settings'):
        monkeypatch.setattr(persistence, name, lambda *args: False)
    for name in ('FRONTEND_STRATEGY_ASSIGNMENTS', 'SKU_SETTINGS_OVERRIDES', 'SKU_META_OVERRIDES',
                 'LIQUIDATION_ACTIVE', 'NEGATIVE_MARGIN_CONFIRMATIONS', 'REPRICER_SKU_GROUPS',
                 'SKU_AUDIT_EVENTS', 'ALGORITHM_SETTINGS_STATE'):
        monkeypatch.setattr(bff, name, {})
    monkeypatch.setattr(bff, 'LIQUIDATION_HISTORY', [])
    monkeypatch.setattr(bff, '_HYDRATED_ORGANIZATION_ID', None, raising=False)
    persistence._HYDRATED_BASELINE.set(None)
    provider = Provider()
    rows = [healthy_row(provider), healthy_row(provider, 102)]
    # Only the marketplace/catalog boundary is synthetic. Authentication, token
    # lookup, session touches, ledger transactions and persistence remain real.
    def cached_rows(*args, **kwargs):
        result = deepcopy(rows)
        for row in result:
            article = row['meta']['articleId']
            row['meta'].update(bff.SKU_META_OVERRIDES.get(article) or {})
            row['strategy'] = bff._frontend_strategy_payload(article,
                current_status=row['meta']['status'], baskets_last_7d=row['meta']['basketsLast7d'],
                basket_norm=row['meta']['basketNorm'])
        return result
    monkeypatch.setattr(router, '_list_repricer_skus_for_request', cached_rows)
    monkeypatch.setattr(router, '_build_repricing_client', lambda *args: provider)
    initial = {'assignments': {'synthetic-102': {'strategyId': 'turnover_control', 'source': 'manual'}},
        'skuSettingsOverrides': {'synthetic-102': {'automationEnabled': True, 'cogsKopecks': 38000}},
        'pendingPriceApprovals': {'obsolete': {'articleId': ARTICLE, 'status': 'pending'}},
        'unrelated': {'keep': True}}
    with factory() as session:
        for org in (1, 2):
            session.add(LkOrganizationRow(organization_id=org, slug=f'synthetic-{org}', name='Synthetic'))
            session.add(WbRepricerRuntimeStateRow(organization_id=org, state_payload=deepcopy(initial)))
            session.add(WbRepricerAlgorithmSettingsRow(organization_id=org,
                settings_payload={**bff.DEFAULT_ALGORITHM_SETTINGS, 'nightMedianDefaultAllSkusVersion': 1}))
        session.add(LkUserRow(user_id='synthetic-user', organization_id=1, email='assignment@example.test',
            password_hash=auth.hash_password('Synthetic-only-test!'), full_name='Synthetic',
            permission_profile='admin', is_active=True))
        session.add(LkUserWbTokenRow(user_id='synthetic-user', organization_id=1,
            wb_token=provider.token, token_masked='synthetic'))
        session.commit()
    app = FastAPI()
    app.include_router(auth_router.router)
    app.include_router(router.router)
    client = TestClient(app)
    response = client.post('/api/v1/auth/login', json={
        'email': 'assignment@example.test', 'password': 'Synthetic-only-test!'})
    assert response.status_code == 200
    # Issue a real synthetic access token through the normal login endpoint.
    client.headers['Authorization'] = 'Bearer ' + response.json()['data']['accessToken']
    with factory() as session:
        login = session.scalar(select(LkSessionRow))
        login.last_seen_at = datetime.now(timezone.utc) - timedelta(hours=1)
        session.commit()
        session_id, last_seen = login.session_id, login.last_seen_at
    value = SimpleNamespace(client=client, factory=factory, provider=provider,
        initial=initial, session_id=session_id, last_seen=last_seen)
    yield value
    client.close()
    engine.dispose()
    persistence._HYDRATED_BASELINE.set(None)


def state(api, org=1):
    with api.factory() as session:
        return deepcopy(session.get(WbRepricerRuntimeStateRow, org).state_payload)


def assign(api, strategy, *, revision=None, **options):
    before = state(api)
    return api.client.post('/api/v1/wb-repricer/strategy-assignments/bulk', json={
        'articleIds': [ARTICLE], 'strategyId': strategy, 'executeAfterAssign': False,
        'expectedRevisions': {ARTICLE: revision or ledger.assignment_revision(before, ARTICLE)}, **options})


def assert_untouched(api, before, after):
    assert after['assignments']['synthetic-102'] == before['assignments']['synthetic-102']
    assert after['skuSettingsOverrides']['synthetic-102'] == before['skuSettingsOverrides']['synthetic-102']
    assert after['unrelated'] == before['unrelated']
    assert state(api, 2) == api.initial
    assert not api.provider.calls
    with api.factory() as session:
        assert session.scalar(select(func.count()).select_from(WbRepricerExecutionRunRow)) == 0
        assert session.scalar(select(func.count()).select_from(WbRepricerChangelogRow)) == 0
        assert session.get(LkSessionRow, api.session_id).last_seen_at > api.last_seen


@pytest.mark.parametrize('remove', [False, True], ids=['assign', 'unassign'])
def test_assignment_only_with_real_session_write_does_not_deadlock(api, remove):
    if remove:
        with api.factory() as session:
            row = session.get(WbRepricerRuntimeStateRow, 1)
            row.state_payload = {**row.state_payload, 'assignments': {
                **row.state_payload['assignments'], ARTICLE: {'strategyId': 'optimal_price', 'source': 'manual'}}}
            session.commit()
    before = state(api)
    response = assign(api, 'none' if remove else 'stockout_guard')
    assert response.status_code == 200, response.text
    after = state(api)
    assert response.json()['assignmentRevisions'][ARTICLE] == ledger.assignment_revision(after, ARTICLE)
    assert response.json()['assignmentRevisions'][ARTICLE] != ledger.assignment_revision(before, ARTICLE)
    if remove:
        assert ARTICLE not in after['assignments']
        assert after['skuSettingsOverrides'][ARTICLE]['automationEnabled'] is False
        assert after['skuMetaOverrides'][ARTICLE]['status'] == 'manual'
    else:
        assert after['assignments'][ARTICLE]['strategyId'] == 'stockout_guard'
        assert after['skuSettingsOverrides'][ARTICLE]['automationEnabled'] is True
        fresh = api.client.get('/api/v1/wb-repricer/strategy-assignments')
        assert fresh.status_code == 200
        assert next(item for item in fresh.json()['items'] if item['articleId'] == ARTICLE)['strategyId'] == 'stockout_guard'
    assert after['pendingPriceApprovals']['obsolete']['status'] == 'rejected'
    assert 'execution' not in response.json()
    assert_untouched(api, before, after)


def test_stale_revision_keeps_saved_state_and_returns_409(api):
    initial_revision = ledger.assignment_revision(state(api), ARTICLE)
    assert assign(api, 'optimal_price').status_code == 200
    saved = state(api)
    response = assign(api, 'turnover_control', revision=initial_revision)
    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'STRATEGY_ASSIGNMENT_CHANGED'
    assert state(api) == saved


@pytest.mark.parametrize('invalid', ['malformed', 'expired_session', 'expired_access_token', 'revoked'])
def test_invalid_authentication_never_mutates_assignments(api, monkeypatch, invalid):
    before = state(api)
    if invalid == 'malformed':
        api.client.headers['Authorization'] = 'Bearer synthetic-invalid'
    elif invalid == 'expired_access_token':
        future = datetime.now(timezone.utc) + timedelta(days=365)
        monkeypatch.setattr(auth, '_utc_now', lambda: future)
    else:
        with api.factory() as session:
            row = session.get(LkSessionRow, api.session_id)
            if invalid == 'expired_session':
                row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            else:
                row.revoked_at = datetime.now(timezone.utc)
            session.commit()
    assert assign(api, 'optimal_price').status_code == 401
    assert state(api) == before
    assert not api.provider.calls


@pytest.mark.parametrize('strategy,config,canonical', [
    ('baskets_orders_4599', None, 'baskets_orders'),
    ('revenue_dynamics_4600', None, 'metric_dynamics'),
    ('schedule', {'scheduleRules': [{'mode': 'hold', 'timeFrom': '07:00', 'timeTo': '08:00'}]}, 'schedule'),
    ('plan_fact_group', {'groupArticleIds': [ARTICLE], 'planOrders': 9}, 'plan_fact_group'),
])
def test_existing_aliases_and_configuration_survive(api, strategy, config, canonical):
    response = assign(api, strategy, config=config)
    assert response.status_code == 200, response.text
    saved = state(api)['assignments'][ARTICLE]
    assert saved['strategyId'] == canonical
    if canonical == 'schedule':
        assert saved['config']['scheduleRules'][0]['timeFrom'] == '07:00'
    if canonical == 'plan_fact_group':
        assert saved['config']['planOrders'] == 9
        assert saved['config']['groupArticleIds'] == [ARTICLE]


def test_implicit_execution_keeps_authenticated_actor_token_and_target_scope(api, monkeypatch):
    calls = []
    def execute(articles, **kwargs):
        calls.append((articles, kwargs))
        return StrategyExecuteReport(runId='synthetic-execution', executedCount=0,
            skippedCount=0, blockedCount=0, items=[])
    monkeypatch.setattr(router, 'execute_repricer_strategies', execute)
    response = assign(api, 'optimal_price', executeAfterAssign=True)
    assert response.status_code == 200, response.text
    assert len(calls) == 1
    articles, options = calls[0]
    assert articles == [ARTICLE]
    assert options['organization_id'] == 1
    assert options['actor_id'] == 'synthetic-user' and options['actor_role'] == 'admin'
    assert options['wb_token'] == api.provider.token
    assert options['client'] is api.provider and options['trigger'] == 'bulk_assign'
    assert options['options'].force is False
    assert response.json()['execution']['runId'] == 'synthetic-execution'
    assert not api.provider.calls
