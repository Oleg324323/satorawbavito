import importlib
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.control_plane.auth import ActorContext
from app.infra.db import get_db_session
from app.orders.cursor import OrdersCursorCodec
from app.orders.router import get_orders_actor, get_orders_cursor_codec, router
from tests.test_orders_read_service import prepared as _prepared_fixture
from tests.test_orders_read_service import read_db as _read_db_fixture
from tests.test_orders_schema_candidate import cluster  # noqa: F401

prepared = _prepared_fixture
read_db = _read_db_fixture


def client_for(runtime, principal):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_orders_actor] = lambda: ActorContext(
        principal.user_id,
        principal.user_id,
        principal.organization_id,
        "custom",
        frozenset(),
        session_id=principal.session_id,
    )
    app.dependency_overrides[get_orders_cursor_codec] = lambda: OrdersCursorCodec(
        b"synthetic-test-key-at-least-32-bytes"
    )

    def db():
        with Session(runtime) as session:
            yield session

    app.dependency_overrides[get_db_session] = db
    return TestClient(app)


def test_http_reads_existing_snapshot_with_live_guard_and_safe_errors(prepared):
    _, runtime, principal, snapshot = prepared
    with client_for(runtime, principal) as client:
        response = client.get(
            "/api/v2/orders",
            params={
                "account_id": 91101,
                "snapshot_id": snapshot,
                "query_checksum": "a" * 64,
                "limit": 1,
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["snapshot_id"] == str(snapshot)
        assert len(body["rows"]) == 1 and body["next_cursor"]
        assert isinstance(body["rows"][0]["row_version"], str)
        next_page = client.get(
            "/api/v2/orders",
            params={
                "account_id": 91101,
                "cursor": body["next_cursor"],
                "query_checksum": "a" * 64,
                "limit": 1,
            },
        )
        assert next_page.status_code == 200
        assert next_page.json()["next_cursor"] is None
        forbidden = client.get(
            "/api/v2/orders",
            params={
                "account_id": 91102,
                "snapshot_id": snapshot,
                "query_checksum": "a" * 64,
            },
        )
        assert forbidden.status_code == 403
        invalid = client.get(
            "/api/v2/orders",
            params={
                "account_id": 91101,
                "cursor": "synthetic-sensitive-not-to-echo",
                "query_checksum": "a" * 64,
            },
        )
        assert invalid.status_code == 400
        assert "synthetic-sensitive" not in invalid.text


def test_http_requires_existing_snapshot_and_has_typed_openapi(prepared):
    _, runtime, principal, _ = prepared
    with client_for(runtime, principal) as client:
        missing = client.get(
            "/api/v2/orders", params={"account_id": 91101, "query_checksum": "b" * 64}
        )
        assert missing.status_code == 409
        spec = client.get("/openapi.json").json()
        schema = spec["paths"]["/api/v2/orders"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]
        assert "$ref" in schema
        assert (
            spec["components"]["schemas"]["OrdersReadRowResponse"]["properties"][
                "row_version"
            ]["type"]
            == "string"
        )


@pytest.mark.parametrize("change", ["revoke", "rebind"])
def test_discovery_is_not_authority_between_transactions(prepared, monkeypatch, change):
    owner, runtime, principal, snapshot = prepared
    module = importlib.import_module("app.orders.router")
    discover = module.discover_orders_bindings

    def raced(*args):
        result = discover(*args)
        with owner.begin() as connection:
            if change == "revoke":
                connection.execute(
                    text(
                        "UPDATE iam_memberships SET is_active=false WHERE membership_id=:id"
                    ),
                    {"id": principal.membership_id},
                )
            else:
                connection.execute(
                    text(
                        "UPDATE marketplace_accounts SET external_account_id='synthetic-rebound' WHERE marketplace_account_id=91101"
                    )
                )
        return result

    monkeypatch.setattr(module, "discover_orders_bindings", raced)
    try:
        with client_for(runtime, principal) as client:
            response = client.get(
                "/api/v2/orders",
                params={
                    "account_id": 91101,
                    "snapshot_id": snapshot,
                    "query_checksum": "a" * 64,
                },
            )
            assert response.status_code == 403
            assert "rows" not in response.json()
    finally:
        if change == "rebind":
            with owner.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE marketplace_accounts SET external_account_id='synthetic-a' WHERE marketplace_account_id=91101"
                    )
                )


def test_missing_bearer_is_safe_401_without_metadata_discovery(prepared):
    _, runtime, principal, _ = prepared
    with client_for(runtime, principal) as client:
        del client.app.dependency_overrides[get_orders_actor]
        response = client.get(
            "/api/v2/orders", params={"account_id": 91101, "query_checksum": "a" * 64}
        )
        assert response.status_code == 401
        assert response.json() == {"detail": {"code": "orders_authentication_required"}}


def test_real_signed_auth_path_uses_synthetic_db_session(prepared, monkeypatch):
    _, runtime, principal, snapshot = prepared
    from app.cabinet import store
    from app.control_plane import auth

    settings = SimpleNamespace(
        auth_secret="synthetic-auth-key-not-real-at-least-32",
        auth_access_ttl_seconds=120,
    )
    monkeypatch.setattr(auth, "get_settings", lambda: settings)

    def local_db(operation):
        with Session(runtime) as session:
            return operation(session)

    # Keep the actual session resolver, but pin its DB dispatcher to the disposable DB.
    monkeypatch.setattr(store, "_run_db", local_db)
    with client_for(runtime, principal) as client:
        actor = client.app.dependency_overrides[get_orders_actor]()
        token = auth._issue_access_token(actor=actor).access_token
        del client.app.dependency_overrides[get_orders_actor]
        params = {
            "account_id": 91101,
            "snapshot_id": snapshot,
            "query_checksum": "a" * 64,
        }
        response = client.get(
            "/api/v2/orders",
            params=params,
            headers={"Authorization": "Bearer " + token},
        )
        assert response.status_code == 200, response.text
        rejected = client.get(
            "/api/v2/orders",
            params=params,
            headers={"Authorization": "Bearer " + token + "tamper"},
        )
        assert rejected.status_code == 401
        assert token not in rejected.text


@pytest.mark.parametrize("selection", ["explicit", "latest", "cursor"])
def test_rebound_account_cannot_read_old_snapshot_on_a_later_request(
    prepared, selection
):
    owner, runtime, principal, snapshot = prepared
    with owner.begin() as connection:
        connection.execute(
            text(
                "UPDATE marketplace_accounts SET external_account_id='synthetic-rebound' WHERE marketplace_account_id=91101"
            )
        )
    try:
        with client_for(runtime, principal) as client:
            params = {"account_id": 91101, "query_checksum": "a" * 64}
            if selection == "explicit":
                params["snapshot_id"] = snapshot
            elif selection == "cursor":
                params["cursor"] = OrdersCursorCodec(
                    b"synthetic-test-key-at-least-32-bytes"
                ).issue(
                    principal=principal,
                    accounts=(91101,),
                    snapshot_id=snapshot,
                    after_position=1,
                    query_checksum="a" * 64,
                )
            response = client.get("/api/v2/orders", params=params)
            assert response.status_code == 409
            assert "rows" not in response.json()
    finally:
        with owner.begin() as connection:
            connection.execute(
                text(
                    "UPDATE marketplace_accounts SET external_account_id='synthetic-a' WHERE marketplace_account_id=91101"
                )
            )
