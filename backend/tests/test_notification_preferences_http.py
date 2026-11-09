"""Public self-preferences boundary: no identities or destinations in either direction."""
import importlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.control_plane.auth import ActorContext

ACTOR = ActorContext("actor", "self-user", 7, "viewer", frozenset(), session_id="live-session")
WIRE = {"schemaVersion": "notification-preferences-v1", "version": "9007199254740993",
        "email": {"enabled": True, "dailyDigest": False, "criticalAlerts": True}, "telegram": {"enabled": False}}
UPDATE = {**WIRE, "schemaVersion": "notification-preferences-update-v1", "expectedVersion": WIRE["version"]}
UPDATE.pop("version")


@pytest.fixture
def boundary(monkeypatch):
    # Import after collection: a missing implementation is the intended RED failure.
    http = importlib.import_module("app.notification_preferences_http")
    service_mod = importlib.import_module("app.notification_preferences_service")
    service = service_mod.NotificationPreferencesService(engine=create_engine("postgresql+psycopg://"), enabled=True)
    calls = []

    def get_current(*, authenticated_actor):
        assert authenticated_actor is ACTOR
        return WIRE

    def replace(*, authenticated_actor, expected_version, flags):
        assert authenticated_actor is ACTOR
        calls.append((expected_version, flags))
        return WIRE

    monkeypatch.setattr(service, "get_current", get_current)
    monkeypatch.setattr(service, "replace", replace)
    app = FastAPI()
    app.include_router(http.make_notification_preferences_router(service_dependency=lambda: service, max_request_bytes=1024))
    app.dependency_overrides[http._actor] = lambda: ACTOR
    return TestClient(app), calls, service, service_mod


def test_get_and_put_exact_self_wire(boundary):
    client, calls, _, _ = boundary
    response = client.get("/api/v2/notifications/preferences")
    assert response.status_code == 200
    assert response.json() == WIRE
    assert response.headers["cache-control"] == "no-store"
    response = client.put("/api/v2/notifications/preferences", json=UPDATE)
    assert response.status_code == 200
    assert response.json() == WIRE
    assert calls[0][0] == "9007199254740993"
    assert calls[0][1].email_enabled is True
    assert calls[0][1].daily_digest is False
    assert calls[0][1].critical_alerts is True
    assert calls[0][1].telegram_enabled is False


@pytest.mark.parametrize("version", [True, 1, "0", "01", "-1", "1.0", "9223372036854775808", "١", ""])
def test_noncanonical_versions_never_reach_replacement(boundary, version):
    client, calls, _, _ = boundary
    response = client.put("/api/v2/notifications/preferences", json={**UPDATE, "expectedVersion": version})
    assert response.status_code == 400
    assert response.json() == {"detail": {"code": "PREFERENCES_INVALID"}}
    assert calls == []


@pytest.mark.parametrize("fragment", [{"userId": "another"}, {"organizationId": 9},
    {"telegram": {"enabled": True, "chatId": "legacy-destination"}},
    {"email": {"enabled": 1, "dailyDigest": False, "criticalAlerts": True}}])
def test_identity_destination_and_coerced_flags_are_rejected(boundary, fragment):
    client, calls, _, _ = boundary
    response = client.put("/api/v2/notifications/preferences", json={**UPDATE, **fragment})
    assert response.status_code == 400
    assert calls == []


@pytest.mark.parametrize("raw", ['{"schemaVersion":"x","schemaVersion":"y"}', '{"email":{"enabled":true,"enabled":false}}',
                                 '{"x":NaN}', '[]', 'invalid', ' ' * 1025])
def test_bad_or_oversized_body_returns_fixed_error(boundary, raw):
    client, calls, _, _ = boundary
    response = client.put("/api/v2/notifications/preferences", content=raw, headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert response.json() == {"detail": {"code": "PREFERENCES_INVALID"}}
    assert calls == []


def test_unknown_post_commit_result_requires_readback(boundary, monkeypatch):
    client, _, service, _ = boundary
    monkeypatch.setattr(service, "replace", lambda **kwargs: {"unexpected": object()})
    response = client.put("/api/v2/notifications/preferences", json=UPDATE)
    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "PREFERENCES_READBACK_REQUIRED"}}


def test_output_is_validated_and_redacted(boundary, monkeypatch):
    client, _, service, _ = boundary
    monkeypatch.setattr(service, "get_current", lambda **kwargs: {**WIRE, "userId": "private"})
    response = client.get("/api/v2/notifications/preferences")
    assert response.status_code == 503
    assert "private" not in response.text


def test_conflict_is_fixed_409_readback(boundary, monkeypatch):
    client, _, service, module = boundary
    def stale(**kwargs):
        raise module.NotificationPreferencesError("PREFERENCES_CONFLICT")
    monkeypatch.setattr(service, "replace", stale)
    response = client.put("/api/v2/notifications/preferences", json=UPDATE)
    assert response.status_code == 409
    assert response.json() == {"detail": {"code": "PREFERENCES_CONFLICT"}}
