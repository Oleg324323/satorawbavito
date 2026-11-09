"""Offline admission tests; actual transaction and concurrency proofs live in PG tests."""
import importlib
from dataclasses import replace

import pytest
from sqlalchemy import create_engine, event

from app.control_plane.auth import ActorContext
from app.notification_preferences_read import NotificationPreferenceFlags

ACTOR = ActorContext("a", "self-user", 7, "admin", frozenset({"preferences:write"}), session_id="session")
FLAGS = NotificationPreferenceFlags(True, False, True, False)


def api():
    assert importlib.util.find_spec("app.notification_preferences_service"), "missing self preferences service"
    return importlib.import_module("app.notification_preferences_service")


def engine():
    value = create_engine("postgresql+psycopg://")
    @event.listens_for(value, "do_connect")
    def unavailable(*args, **kwargs):
        raise RuntimeError("offline database diagnostic")
    return value


def test_disabled_service_never_connects():
    module = api()
    service = module.NotificationPreferencesService(engine=engine())
    with pytest.raises(module.NotificationPreferencesError, match="^PREFERENCES_DISABLED$"):
        service.get_current(authenticated_actor=ACTOR)


def test_non_postgres_is_rejected_without_fallback():
    module = api()
    with pytest.raises(module.NotificationPreferencesError, match="^PREFERENCES_CONFIGURATION_INVALID$"):
        module.NotificationPreferencesService(engine=create_engine("sqlite://"), enabled=True)


@pytest.mark.parametrize("actor", [None, replace(ACTOR, session_id=None), replace(ACTOR, organization_id=True), replace(ACTOR, user_id="")])
def test_invalid_session_identity_denied_before_persistence(actor):
    module = api()
    service = module.NotificationPreferencesService(engine=engine(), enabled=True)
    with pytest.raises(module.NotificationPreferencesError, match="^PREFERENCES_DENIED$"):
        service.get_current(authenticated_actor=actor)


@pytest.mark.parametrize("version", [True, 1, "01", "0", "9223372036854775808"])
def test_invalid_version_denied_before_persistence(version):
    module = api()
    service = module.NotificationPreferencesService(engine=engine(), enabled=True)
    with pytest.raises(module.NotificationPreferencesError, match="^PREFERENCES_INVALID$"):
        service.replace(authenticated_actor=ACTOR, expected_version=version, flags=FLAGS)


def test_database_failure_is_fixed_and_has_no_raw_exception_context():
    module = api()
    service = module.NotificationPreferencesService(engine=engine(), enabled=True)
    with pytest.raises(module.NotificationPreferencesError) as error:
        service.replace(authenticated_actor=ACTOR, expected_version="1", flags=FLAGS)
    assert error.value.code == "PREFERENCES_UNAVAILABLE"
    assert error.value.__context__ is None
    assert "diagnostic" not in str(error.value)


def test_activated_legacy_engine_failure_is_safe_and_has_no_fallback(monkeypatch):
    from fastapi import HTTPException

    from app.cabinet import store
    from app.config import Settings

    def unavailable():
        raise RuntimeError("synthetic-engine-diagnostic")

    monkeypatch.setattr(store, "get_settings", lambda: Settings(canonical_notifications_enabled=True))
    monkeypatch.setattr(store, "get_engine", unavailable)
    with pytest.raises(HTTPException) as error:
        store.update_preferences(user_id="offline-self", organization_id=7, actor_user_id="offline-self",
                                 notification_settings={}, export_settings={}, timezone_value="UTC")
    assert error.value.status_code == 503
    assert error.value.detail == {"code": "PREFERENCES_UNAVAILABLE"}
    assert error.value.__context__ is None
    assert "offline-self" not in store._MEMORY.preferences
