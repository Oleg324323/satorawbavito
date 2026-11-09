"""Actual route/DTO with synthetic dependencies; no provider or database."""
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.wb_live.router import router, live_repository, get_marketplace_credential_actor


@pytest.fixture
def http():
    calls = []
    actor = SimpleNamespace(organization_id=1)
    def create(who, account, key, *, date_from):
        calls.append((who, account, key, date_from))
        return {"state": "queued", "sources": []}
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[live_repository] = lambda: SimpleNamespace(create_history_job=create)
    app.dependency_overrides[get_marketplace_credential_actor] = lambda: actor
    return TestClient(app), calls, actor


def test_history_post_preserves_native_text_and_exact_actor(http):
    client, calls, actor = http
    cursor = "2026-09-01T12:13:14.12345+03:00"
    response = client.post("/api/v2/wb/accounts/2/history", json={"dateFrom": cursor}, headers={"Idempotency-Key": "history-key"})
    assert response.status_code == 200
    assert calls == [(actor, 2, "history-key", cursor)]


@pytest.mark.parametrize("body", [{}, {"dateFrom": None}, {"dateFrom": "yesterday"}, {"dateFrom": "2026-13-40"},
    {"dateFrom": "2026-09-01", "synthetic-secret-canary": True}, {"dateFrom": ["2026-09-01"]}])
def test_history_invalid_closed_payload_never_calls_repository(http, body):
    client, calls, _ = http
    response = client.post("/api/v2/wb/accounts/2/history", json=body, headers={"Idempotency-Key": "history-key"})
    assert response.status_code == 422 and not calls
    # The key is not used as a Pydantic error location; global handler strips input.
    assert all("synthetic-secret-canary" not in str(error["loc"]) for error in response.json()["detail"])


def test_history_requires_idempotency_header(http):
    client, calls, _ = http
    assert client.post("/api/v2/wb/accounts/2/history", json={"dateFrom": "2026-09-01"}).status_code == 422
    assert not calls
