"""Synthetic secrets and isolated in-memory DB; no application key/provider."""
from types import SimpleNamespace
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.cabinet.orm import LkOrganizationRow, OrganizationOpenAiKeyRow
from app.security import organization_openai as store
from app.security.marketplace_credentials import CredentialKeyring
from app.control_plane.auth import ActorContext


@pytest.fixture
def isolated(monkeypatch):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    LkOrganizationRow.__table__.create(engine)
    OrganizationOpenAiKeyRow.__table__.create(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session, session.begin():
        session.add_all([LkOrganizationRow(organization_id=i, slug=f'test-{i}', name='Synthetic') for i in (1, 2)])
    ring = CredentialKeyring(current_key_version=1, keys={1: bytes(range(32))})
    monkeypatch.setattr(store, 'get_session_factory', lambda: factory)
    monkeypatch.setattr(store, '_keyring', lambda: ring)
    monkeypatch.setattr(store, 'get_settings', lambda: SimpleNamespace(marketplace_credentials_enabled=True, openai_api_key=None))
    yield factory
    engine.dispose()


def test_encryption_is_org_bound_and_status_never_discloses_key(isolated):
    key = 'sk-' + 'synthetic_not_real_' * 3
    status = store.save_key(1, key)
    assert status == {'configured': True, 'storageAvailable': True, 'serverConfigured': False}
    assert key not in str(status)
    assert store.resolve_key(1) == key
    assert store.resolve_key(2) is None
    with isolated() as session, session.begin():
        row = session.get(OrganizationOpenAiKeyRow, 1)
        assert key.encode() not in row.ciphertext
        session.add(OrganizationOpenAiKeyRow(organization_id=2, nonce=row.nonce,
                                           ciphertext=row.ciphertext, key_version=row.key_version))
    with pytest.raises(store.OpenAiKeyError, match='OPENAI_KEY_UNREADABLE'):
        store.resolve_key(2)
    assert store.delete_key(1)['configured'] is False
    assert store.resolve_key(1) is None


def test_replace_rotation_and_invalid_input(isolated, monkeypatch):
    first = 'sk-' + 'synthetic_one_' * 4
    second = 'sk-' + 'synthetic_two_' * 4
    store.save_key(1, first)
    monkeypatch.setattr(store, '_keyring', lambda: CredentialKeyring(current_key_version=2,
        keys={1: bytes(range(32)), 2: bytes(reversed(range(32)))}))
    assert store.resolve_key(1) == first
    store.save_key(1, second)
    assert store.resolve_key(1) == second
    with isolated() as session:
        assert session.get(OrganizationOpenAiKeyRow, 1).key_version == 2
    for invalid in [None, 12, 'sk-short', second + ' ', 'sk-' + 'x' * 513, 'sk-' + 'я' * 50]:
        with pytest.raises(store.OpenAiKeyError, match='OPENAI_KEY_INVALID'):
            store.save_key(1, invalid)


def test_api_permission_and_validation_never_echo_secret(isolated, monkeypatch):
    from app.routers import cabinet
    actor = ActorContext('admin', 'admin', 1, 'admin', frozenset({'cabinet:read', 'integrations:write'}))
    monkeypatch.setattr(cabinet, 'actor_from_request', lambda request: actor)
    app = FastAPI(); app.include_router(cabinet.router)
    client = TestClient(app)
    key = 'sk-' + 'synthetic_not_real_' * 3
    saved = client.put('/api/v1/cabinet/openai-key', json={'apiKey': key})
    assert saved.status_code == 200 and saved.json()['data']['configured']
    assert key not in saved.text
    for payload in [{'apiKey': key, 'extra': key}, {'apiKey': key + ' '}, {'apiKey': [key]}]:
        response = client.put('/api/v1/cabinet/openai-key', json=payload)
        assert response.status_code == 400
        assert key not in response.text
    oversized = client.put('/api/v1/cabinet/openai-key', content=b'x' * 2100)
    assert oversized.status_code == 400
    actor = ActorContext('viewer', 'viewer', 2, 'viewer', frozenset({'cabinet:read'}))
    assert not client.get('/api/v1/cabinet/openai-key').json()['data']['configured']
    assert client.put('/api/v1/cabinet/openai-key', json={'apiKey': key}).status_code == 403
    assert client.delete('/api/v1/cabinet/openai-key').status_code == 403


def test_avito_ai_prefers_org_key_and_never_falls_back_after_decryption_failure(isolated, monkeypatch):
    from app.avito import orders_ai
    from tests.test_avito_orders_ai import RecordingClient, snapshot_with_two_sizes, settings
    key = 'sk-' + 'synthetic_not_real_' * 3
    store.save_key(1, key)
    monkeypatch.setattr(orders_ai, 'get_settings', lambda: settings('sk-server-synthetic'))
    client = RecordingClient({'items': []})
    snapshot = snapshot_with_two_sizes()
    snapshot.collector['options']['colorFromDescription'] = True
    snapshot.orders[0].items[0].description = 'Цвет: красный'
    orders_ai.enrich_avito_orders_snapshot_with_ai(snapshot, client=client, organization_id=1)
    assert client.posts and client.posts[0]['headers']['Authorization'] == 'Bearer ' + key
    with isolated() as session, session.begin():
        row = session.get(OrganizationOpenAiKeyRow, 1)
        row.ciphertext = b'corrupt'
    client.posts.clear()
    _, metadata = orders_ai.enrich_avito_orders_snapshot_with_ai(snapshot, client=client, organization_id=1)
    assert metadata['reason'] == 'openai_key_storage_unavailable'
    assert snapshot.orders[0].items[0].size == 'L'
    assert snapshot.orders[0].items[0].sizeEvidence['method'] == 'chat_rules'
    assert not client.posts
