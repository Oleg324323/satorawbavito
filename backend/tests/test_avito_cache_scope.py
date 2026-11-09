from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.avito.auth import scoped_avito_cache_key
from app.cabinet.store import AvitoCredentialsSecret
from app.main import create_app


@pytest.mark.parametrize("module,path,method", [
    ("avito_chats", "/api/v1/avito/chats", "get"),
    ("avito_notifications", "/api/v1/avito/notifications", "get"),
    ("avito_notifications", "/api/v1/avito/notifications/n-1/read", "post"),
    ("avito_notifications", "/api/v1/avito/notifications/read-all", "post"),
    ("avito_overview", "/api/v1/avito/overview", "get"),
    ("avito_listings", "/api/v1/avito/listings", "get"),
    ("avito_stats", "/api/v1/avito/stats", "get"),
    ("avito_reviews", "/api/v1/avito/reviews", "get"),
    ("avito_orders", "/api/v1/avito/orders", "get"),
    ("avito_orders", "/api/v1/avito/orders/picking-list.xlsx", "get"),
    ("avito_repricer", "/api/v1/avito/repricer", "get"),
])
def test_cached_avito_views_require_current_credentials(monkeypatch, module, path, method):
    router = f"app.routers.{module}"
    monkeypatch.setattr(f"{router}.actor_from_request", lambda _request: SimpleNamespace(user_id="viewer", organization_id=10))
    monkeypatch.setattr(f"{router}.has_permission", lambda _actor, _permission: True)
    monkeypatch.setattr(f"{router}.get_user_avito_credentials_secret", lambda _user: None)
    monkeypatch.setattr(f"{router}.get_organization_avito_credentials_secret", lambda _org: None)
    monkeypatch.setattr(f"{router}.get_source_cache", lambda *_args, **_kwargs: pytest.fail("cache read before Avito credentials"))

    response = getattr(TestClient(create_app()), method)(path)

    assert response.status_code == 409


def test_cache_scope_changes_with_token_and_query_and_fits_database_key():
    source = "avito_stats:2026-07-01:2026-07-28:" + ",".join(str(index) * 20 for index in range(1, 16))
    first = scoped_avito_cache_key(source, "first-token")

    assert len(first) <= 255
    assert "first-token" not in first
    assert first != scoped_avito_cache_key(source, "second-token")
    assert first != scoped_avito_cache_key(source + "x", "first-token")


def test_notification_read_marks_survive_token_refresh_but_not_credential_change(monkeypatch):
    token = ["first-token"]
    credentials = [AvitoCredentialsSecret(
        client_id="first-client", client_secret="first-secret", cached_access_token=None, access_token_expires_at=None,
    )]
    cache: dict[str, dict] = {}
    router = "app.routers.avito_notifications"
    monkeypatch.setattr(f"{router}.actor_from_request", lambda _request: SimpleNamespace(user_id="viewer", organization_id=10))
    monkeypatch.setattr(f"{router}.has_permission", lambda _actor, _permission: True)
    monkeypatch.setattr(f"{router}.get_user_avito_credentials_secret", lambda _user: credentials[0])
    monkeypatch.setattr(f"{router}.resolve_user_avito_access_token", lambda **_kwargs: token[0])
    monkeypatch.setattr(f"{router}.get_source_cache", lambda _org, key, **_kwargs: cache.get(key))
    monkeypatch.setattr(f"{router}.save_source_cache", lambda _org, key, payload: cache.update({key: payload}))
    api = TestClient(create_app())

    assert api.post("/api/v1/avito/notifications/n-1/read").status_code == 200
    token[0] = "refreshed-token"
    assert api.post("/api/v1/avito/notifications/n-2/read").status_code == 200
    assert len(cache) == 1
    assert set(next(iter(cache.values()))["marks"]) == {"n-1", "n-2"}

    credentials[0] = AvitoCredentialsSecret(
        client_id="second-client", client_secret="second-secret", cached_access_token=None, access_token_expires_at=None,
    )
    token[0] = "second-token"
    assert api.post("/api/v1/avito/notifications/n-3/read").status_code == 200
    assert sorted(sorted(row["marks"]) for row in cache.values()) == [["n-1", "n-2"], ["n-3"]]
