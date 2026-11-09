"""Assembled rollout/auth contract; storage behavior has its own PG gates."""

from types import SimpleNamespace

import pytest
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient

from app import main
from app.config import Settings


def configured(monkeypatch, enabled):
    settings = SimpleNamespace(**vars(Settings()))
    settings.canonical_notifications_enabled = enabled
    settings.canonical_notification_accounts = ()
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    return main.create_app()


@pytest.mark.parametrize("enabled", [False, True])
def test_notification_read_and_receipt_routes_share_explicit_rollout(monkeypatch, enabled):
    # Missing receipt wiring would otherwise give a readable but unusable inbox.
    app = configured(monkeypatch, enabled)
    paths = [route.path for route in iter_route_contexts(app.routes)]
    for path in (
        "/api/v2/reviews/notifications",
        "/api/v2/reviews/notifications/visible",
        "/api/v2/reviews/notifications/capabilities",
        "/api/v2/reviews/notifications/receipts",
    ):
        assert paths.count(path) == (1 if enabled else 0)
    assert paths.count("/api/v2/notifications/preferences") == (2 if enabled else 0)


@pytest.mark.parametrize("method", ["get", "put"])
def test_enabled_preferences_require_login_before_database(monkeypatch, method):
    app = configured(monkeypatch, True)
    with TestClient(app) as client:
        result = getattr(client, method)("/api/v2/notifications/preferences")
    assert result.status_code == 401
    assert result.json()["error"]["code"] == "PREFERENCES_AUTHENTICATION_REQUIRED"
    assert result.headers["cache-control"] == "no-store"


def test_enabled_inbox_requires_login_without_touching_database(monkeypatch):
    app = configured(monkeypatch, True)
    with TestClient(app) as client:
        result = client.get("/api/v2/reviews/notifications?marketplace_account_id=1&marketplace=wb")
    assert result.status_code == 401
    assert result.json()["error"]["code"] == "NOTIFICATION_AUTHENTICATION_REQUIRED"
    assert result.headers["cache-control"] == "no-store"


def test_empty_rollout_denies_list_and_receipts_before_database_access(monkeypatch):
    from sqlalchemy import create_engine

    from app import notification_bootstrap as bootstrap
    from app.control_plane.auth import ActorContext
    from app.notification_service import NotificationServiceError

    engine = create_engine("postgresql+psycopg://unused@127.0.0.1:1/unused")
    settings = SimpleNamespace(canonical_notifications_enabled=True,
        canonical_notification_accounts=(), auth_secret="synthetic-cursor-key-" * 3)
    monkeypatch.setattr(bootstrap, "get_settings", lambda: settings)
    monkeypatch.setattr(bootstrap, "get_engine", lambda: engine)
    actor = ActorContext("user", "user", 1, "test", frozenset(), session_id="session")
    service = bootstrap.notification_service()
    try:
        with pytest.raises(NotificationServiceError, match="NOTIFICATION_DISABLED"):
            service.list_visible(authenticated_actor=actor, marketplace_account_id=1,
                marketplace="wb", limit=50, cursor=None, codec=bootstrap.notification_cursor_codec())
        with pytest.raises(NotificationServiceError, match="NOTIFICATION_DISABLED"):
            service.mark_visible(authenticated_actor=actor, marketplace_account_id=1,
                marketplace="wb", event_ids=[], action="read")
    finally:
        engine.dispose()


def test_runtime_disable_and_configuration_failures_do_not_expose_details(monkeypatch):
    from fastapi import HTTPException

    from app import notification_bootstrap as bootstrap

    settings = SimpleNamespace(canonical_notifications_enabled=False,
        canonical_notification_accounts=(), auth_secret="short-synthetic-key")
    monkeypatch.setattr(bootstrap, "get_settings", lambda: settings)
    for dependency in (bootstrap.notification_service, bootstrap.notification_cursor_codec):
        with pytest.raises(HTTPException) as caught:
            dependency()
        assert caught.value.status_code == 503
        assert caught.value.detail == {"code": "NOTIFICATION_UNAVAILABLE"}
    settings.canonical_notifications_enabled = True
    with pytest.raises(HTTPException) as caught:
        bootstrap.notification_cursor_codec()
    assert caught.value.detail == {"code": "NOTIFICATION_UNAVAILABLE"}
    assert caught.value.__context__ is None
