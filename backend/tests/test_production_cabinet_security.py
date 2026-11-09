from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError
from app.cabinet import store


def test_production_never_creates_demo_users(monkeypatch):
    monkeypatch.setattr(store, 'get_settings', lambda: SimpleNamespace(environment='production', wb_live_sync_enabled=False))
    class NoWrites:
        def __getattr__(self, name):
            pytest.fail('Production demo bootstrap accessed the database')
    store._ensure_defaults(NoWrites())
    with pytest.raises(HTTPException) as error:
        store._ensure_defaults(None)
    assert error.value.status_code == 503


def test_production_auth_never_falls_back_to_memory_on_db_failure(monkeypatch):
    monkeypatch.setattr(store, 'get_settings', lambda: SimpleNamespace(environment='production', wb_live_sync_enabled=False))
    monkeypatch.setattr(store, 'get_engine', lambda: (_ for _ in ()).throw(SQLAlchemyError('synthetic storage outage')))
    with pytest.raises(HTTPException) as error:
        store._run_db(lambda session: pytest.fail('Unexpected DB success'))
    assert error.value.status_code == 503
    assert error.value.detail['code'] == 'CABINET_STORAGE_UNAVAILABLE'


@pytest.mark.parametrize('getter,identity', [
    (store.get_user_wb_token_secret, 'absent-user'),
    (store.get_organization_wb_token_secret, 123),
    (store.get_user_avito_credentials_secret, 'absent-user'),
    (store.get_organization_avito_credentials_secret, 123),
])
def test_missing_production_credentials_are_not_a_storage_outage(monkeypatch, getter, identity):
    monkeypatch.setattr(store, 'get_settings', lambda: SimpleNamespace(environment='production', wb_live_sync_enabled=False))
    monkeypatch.setattr(store, '_run_db', lambda _: None)
    monkeypatch.setattr(store, '_ensure_defaults', lambda *_: pytest.fail('Unexpected memory fallback'))
    assert getter(identity) is None
