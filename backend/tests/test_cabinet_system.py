from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.auth_helpers import auth_headers


def client() -> TestClient:
    return TestClient(create_app())


def test_protected_endpoint_requires_bearer_token():
    response = client().get("/api/v1/wb-reports/pnl")
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "AUTH_REQUIRED"


def test_register_creates_company_owner_and_returns_token():
    api = client()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    email = f"owner-{stamp}@example.local"
    wb_token = "wb_reg_token_1234567890"
    response = api.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "OwnerPass123!",
            "fullName": "Owner User",
            "companyName": f"Demo Company {stamp}",
            "wbToken": wb_token,
        },
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["organization"]["name"].startswith("Demo Company")
    assert data["user"]["email"] == email
    assert data["user"]["permissionProfile"] == "admin"
    assert data["accessToken"]
    owner_headers = {"Authorization": f"Bearer {data['accessToken']}"}
    token_state = api.get("/api/v1/cabinet/wb-token", headers=owner_headers)
    assert token_state.status_code == 200
    token_data = token_state.json()["data"]
    assert token_data["hasToken"] is True
    assert token_data["tokenMasked"] is not None
    assert token_data["tokenMasked"].endswith("7890")


def test_cabinet_endpoints_cover_team_sessions_integrations_preferences():
    api = client()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    admin_headers = auth_headers(api, "admin")

    me = api.get("/api/v1/cabinet/me", headers=admin_headers)
    assert me.status_code == 200
    me_payload = me.json()["data"]
    assert me_payload["organization"]["organizationId"] >= 1
    assert me_payload["user"]["permissionProfile"] == "admin"

    sessions = api.get("/api/v1/cabinet/sessions", headers=admin_headers)
    assert sessions.status_code == 200
    assert sessions.json()["data"]
    current_session = api.get("/api/v1/cabinet/sessions/current", headers=admin_headers)
    assert current_session.status_code == 200
    assert current_session.json()["data"] is not None

    integrations = api.get("/api/v1/cabinet/integrations", headers=admin_headers)
    assert integrations.status_code == 200
    providers = {item["provider"] for item in integrations.json()["data"]}
    assert {"wb", "avito"} <= providers

    update_wb = api.put(
        "/api/v1/cabinet/integrations/wb",
        json={"status": "connected", "externalAccountId": "wb-main", "tokenRef": "vault://wb/main", "metadata": {"region": "ru"}},
        headers=admin_headers,
    )
    assert update_wb.status_code == 200
    assert update_wb.json()["data"]["status"] == "connected"

    prefs = api.put(
        "/api/v1/cabinet/preferences",
        json={
            "notificationSettings": {"email": {"enabled": True}},
            "exportSettings": {"defaultFormat": "csv"},
            "timezone": "Europe/Moscow",
        },
        headers=admin_headers,
    )
    assert prefs.status_code == 200
    assert prefs.json()["data"]["timezone"] == "Europe/Moscow"

    team = api.get("/api/v1/cabinet/team/users", headers=admin_headers)
    assert team.status_code == 200
    assert any(user["permissionProfile"] == "admin" for user in team.json()["data"])

    create_user = api.post(
        "/api/v1/cabinet/team/users",
        json={
            "email": f"team.user+{stamp}@example.local",
            "password": "TeamPass123!",
            "fullName": "Team User",
            "permissionProfile": "viewer",
        },
        headers=admin_headers,
    )
    assert create_user.status_code == 200
    created_user = create_user.json()["data"]
    assert created_user["permissionProfile"] == "viewer"

    promote_user = api.patch(
        f"/api/v1/cabinet/team/users/{created_user['userId']}/permission-profile",
        json={"permissionProfile": "price_sender", "reason": "expand responsibilities"},
        headers=admin_headers,
    )
    assert promote_user.status_code == 200
    assert promote_user.json()["data"]["permissionProfile"] == "price_sender"

    revoke_others = api.post("/api/v1/cabinet/sessions/revoke-others", headers=admin_headers)
    assert revoke_others.status_code == 200
    assert revoke_others.json()["data"]["revokedCount"] >= 0

    today = datetime.now(timezone.utc).date().isoformat()
    audit = api.get(
        "/api/v1/cabinet/audit/events",
        params={"actionPrefix": "team.user", "createdFrom": today, "createdTo": today},
        headers=admin_headers,
    )
    assert audit.status_code == 200
    assert audit.json()["total"] >= 1
    assert any(item["action"] == "team.user.permission_profile.update" for item in audit.json()["items"])


def test_user_can_update_and_delete_own_wb_token_in_cabinet():
    api = client()
    viewer_headers = auth_headers(api, "viewer")

    api.delete("/api/v1/cabinet/wb-token", headers=viewer_headers)
    initial = api.get("/api/v1/cabinet/wb-token", headers=viewer_headers)
    assert initial.status_code == 200
    assert initial.json()["data"]["hasToken"] is False

    put_token = api.put(
        "/api/v1/cabinet/wb-token",
        json={"wbToken": "wb_live_token_0987654321"},
        headers=viewer_headers,
    )
    assert put_token.status_code == 200
    put_data = put_token.json()["data"]
    assert put_data["hasToken"] is True
    assert put_data["tokenMasked"].endswith("4321")

    delete_token = api.delete("/api/v1/cabinet/wb-token", headers=viewer_headers)
    assert delete_token.status_code == 200
    delete_data = delete_token.json()["data"]
    assert delete_data["hasToken"] is False
    assert delete_data["tokenMasked"] is None


def test_wb_token_update_does_not_call_wb_api(monkeypatch):
    from app.routers import cabinet as cabinet_router
    from app.cabinet.schemas import UserWbTokenUpsertRequest

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("WB API must not be called while saving user token")

    monkeypatch.setattr("app.repricer_bff.fetch_catalog_goods_page", fail_if_called)
    monkeypatch.setattr(
        cabinet_router,
        "_require_permission",
        lambda _permission, _request: SimpleNamespace(user_id="viewer", organization_id=10),
    )
    monkeypatch.setattr(
        cabinet_router,
        "upsert_user_wb_token",
        lambda **_kwargs: SimpleNamespace(hasToken=True, tokenMasked="***4321"),
    )
    monkeypatch.setattr(
        cabinet_router,
        "get_settings",
        lambda: Settings(wb_api_mode="fake", repricer_wb_sync_enabled=True),
    )
    monkeypatch.setattr(cabinet_router, "_enqueue_wb_onboarding_sync", lambda _organization_id: None)

    response = cabinet_router.put_my_wb_token(
        SimpleNamespace(client=None, headers={}),
        UserWbTokenUpsertRequest(wbToken="wb_local_token_0987654321"),
    )

    assert response.data.hasToken is True


def test_wb_token_update_enqueues_onboarding_sync_in_real_mode(monkeypatch):
    from app.routers import cabinet as cabinet_router
    from app.cabinet.schemas import UserWbTokenUpsertRequest

    enqueued: list[int] = []
    monkeypatch.setattr(
        cabinet_router,
        "get_settings",
        lambda: Settings(wb_api_mode="real", repricer_wb_sync_enabled=True),
    )
    monkeypatch.setattr(
        cabinet_router,
        "_require_permission",
        lambda _permission, _request: SimpleNamespace(user_id="viewer", organization_id=10),
    )
    monkeypatch.setattr(
        cabinet_router,
        "upsert_user_wb_token",
        lambda **_kwargs: SimpleNamespace(hasToken=True, tokenMasked="***4321"),
    )
    monkeypatch.setattr(cabinet_router, "_enqueue_wb_onboarding_sync", lambda organization_id: enqueued.append(organization_id))

    response = cabinet_router.put_my_wb_token(
        SimpleNamespace(client=None, headers={}),
        UserWbTokenUpsertRequest(wbToken="wb_real_token_0987654321"),
    )

    assert response.data.hasToken is True
    assert enqueued == [10]


def test_wb_token_update_rejects_whitespace_locally():
    api = client()
    viewer_headers = auth_headers(api, "viewer")
    response = api.put(
        "/api/v1/cabinet/wb-token",
        json={"wbToken": "wb token with spaces"},
        headers=viewer_headers,
    )

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "WB_TOKEN_HAS_WHITESPACE"


def test_user_can_update_and_delete_own_avito_credentials_in_cabinet():
    api = client()
    viewer_headers = auth_headers(api, "viewer")

    api.delete("/api/v1/cabinet/avito-credentials", headers=viewer_headers)
    initial = api.get("/api/v1/cabinet/avito-credentials", headers=viewer_headers)
    assert initial.status_code == 200
    assert initial.json()["data"]["hasCredentials"] is False

    put_credentials = api.put(
        "/api/v1/cabinet/avito-credentials",
        json={"clientId": "avito_client_0987654321", "clientSecret": "avito_secret_1234567890"},
        headers=viewer_headers,
    )
    assert put_credentials.status_code == 200
    put_data = put_credentials.json()["data"]
    assert put_data["hasCredentials"] is True
    assert put_data["clientIdMasked"].endswith("4321")
    assert put_data["clientSecretMasked"].endswith("7890")
    assert "avito_secret_1234567890" not in put_credentials.text

    delete_credentials = api.delete("/api/v1/cabinet/avito-credentials", headers=viewer_headers)
    assert delete_credentials.status_code == 200
    delete_data = delete_credentials.json()["data"]
    assert delete_data["hasCredentials"] is False
    assert delete_data["clientIdMasked"] is None
    assert delete_data["clientSecretMasked"] is None


def test_avito_credentials_update_rejects_whitespace_locally():
    api = client()
    viewer_headers = auth_headers(api, "viewer")
    response = api.put(
        "/api/v1/cabinet/avito-credentials",
        json={"clientId": "avito client", "clientSecret": "avito_secret_1234567890"},
        headers=viewer_headers,
    )

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "AVITO_CREDENTIALS_HAVE_WHITESPACE"
