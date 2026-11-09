import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.platform.integrations import account_discovery_http as http
from app.platform.integrations.account_discovery import AccountDiscoveryError
from app.platform.integrations.account_discovery_http import (
    make_marketplace_account_discovery_router,
)


class Service:
    def list_accounts(self, actor, *, provider=None):
        assert actor == "trusted-server-actor"
        self.provider = provider
        return [
            {
                "marketplaceAccountId": 2,
                "provider": "avito",
                "externalAccountId": "external",
                "displayName": None,
                "status": "disconnected",
            }
        ]


def client(monkeypatch, service):
    monkeypatch.setattr(
        http, "get_marketplace_credential_actor", lambda request: "trusted-server-actor"
    )
    app = FastAPI()
    app.include_router(
        make_marketplace_account_discovery_router(service_dependency=lambda: service)
    )
    return TestClient(app)


@pytest.mark.parametrize("provider", [None, "wb", "avito"])
def test_fixed_metadata_wire_and_no_store(monkeypatch, provider):
    service = Service()
    response = client(monkeypatch, service).get(
        "/api/v2/cabinet/marketplace-accounts",
        params={} if provider is None else {"provider": provider},
    )
    assert (
        response.status_code == 200 and response.headers["cache-control"] == "no-store"
    )
    assert service.provider == provider
    assert set(response.json()["data"][0]) == {
        "marketplaceAccountId",
        "provider",
        "externalAccountId",
        "displayName",
        "status",
    }


@pytest.mark.parametrize(
    "query",
    [
        "provider=other",
        "provider=WB",
        "provider=",
        "provider=wb&provider=avito",
        "organizationId=9",
    ],
)
def test_invalid_query_fixed_error_no_echo(monkeypatch, query):
    response = client(monkeypatch, Service()).get(
        "/api/v2/cabinet/marketplace-accounts?" + query
    )
    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "ACCOUNT_DISCOVERY_INVALID_REQUEST"}}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "code,status",
    [("ACCOUNT_DISCOVERY_DISABLED", 503), ("ACCOUNT_DISCOVERY_ACCESS_DENIED", 403)],
)
def test_safe_domain_error(monkeypatch, code, status):
    class Denied:
        def list_accounts(self, *args, **kwargs):
            raise AccountDiscoveryError(code)

    response = client(monkeypatch, Denied()).get("/api/v2/cabinet/marketplace-accounts")
    assert response.status_code == status and response.json() == {
        "detail": {"code": code}
    }


def test_raw_failure_not_disclosed(monkeypatch):
    class Broken:
        def list_accounts(self, *args, **kwargs):
            raise RuntimeError("synthetic secret SQL detail")

    response = client(monkeypatch, Broken()).get("/api/v2/cabinet/marketplace-accounts")
    assert response.status_code == 503 and "secret" not in response.text


def test_unauthenticated_keeps_401_with_safe_body(monkeypatch):
    browser = client(monkeypatch, Service())

    def unauthenticated(request):
        raise HTTPException(401, detail="synthetic internal auth detail")

    monkeypatch.setattr(http, "get_marketplace_credential_actor", unauthenticated)
    response = browser.get("/api/v2/cabinet/marketplace-accounts")
    assert response.status_code == 401
    assert response.json() == {"detail": {"code": "ACCOUNT_DISCOVERY_ACCESS_DENIED"}}
    assert response.headers["cache-control"] == "no-store"
