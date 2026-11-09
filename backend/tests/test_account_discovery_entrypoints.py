"""Assembled discovery boundary, independent of WB-only synchronization rollout."""

from dataclasses import replace

import pytest
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient

from app import main
from app.config import Settings


@pytest.mark.parametrize("enabled", [False, True])
def test_account_discovery_registration_is_separate_and_default_off(monkeypatch, enabled):
    settings = replace(Settings(), canonical_account_discovery_enabled=enabled, wb_live_sync_enabled=False)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    paths = [route.path for route in iter_route_contexts(main.create_app().routes)]
    assert paths.count("/api/v2/cabinet/marketplace-accounts") == (1 if enabled else 0)
    assert "/api/v1/cabinet/marketplace-accounts" not in paths


def test_account_discovery_requires_login_before_engine_or_service(monkeypatch):
    monkeypatch.setattr(main, "get_settings", lambda: replace(Settings(), canonical_account_discovery_enabled=True))
    with TestClient(main.create_app()) as client:
        response = client.get("/api/v2/cabinet/marketplace-accounts")
    assert response.status_code == 401
    assert response.json() == {"detail": {"code": "ACCOUNT_DISCOVERY_ACCESS_DENIED"}}
    assert response.headers["cache-control"] == "no-store"
