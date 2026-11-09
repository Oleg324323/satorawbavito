from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import sessionmaker

from app.cabinet import store as cabinet_store
from app.cabinet.orm import (
    LkAuditEventRow,
    LkOrganizationRow,
    LkSessionRow,
    LkUserRow,
    LkUserWbTokenRow,
)
from app.cabinet.permissions import permissions_from_profile
from app.control_plane.auth import ActorContext
from app.infra.db import get_db_session
from app.main import create_app
from app.platform.identity.orm import IamMembershipRow
from app.platform.integrations import credential_store
from app.platform.integrations.access import get_marketplace_credential_actor
from app.platform.integrations.credential_store import (
    MarketplaceAccountCredentialOwner,
    resolve_marketplace_credential,
)
from app.platform.integrations.orm import (
    MarketplaceAccountCredentialRow,
    MarketplaceAccountRow,
)
from app.security.marketplace_credentials import CredentialKeyring
from tests.test_credential_maintenance_inert_postgres import (
    cluster,  # noqa: F401
    pg_database as current_pg_database,
)


CANARY = "synthetic-marketplace-api-secret-72f1"
LEGACY_CANARY = "synthetic-legacy-secret-must-not-win-05d4"
WB_SELLER_ID = "64f8d3e5-3d25-4b5c-9f23-0fd3937af451"
OTHER_WB_SELLER_ID = "5e2e5772-c32a-44ec-897a-485497d67585"
NOW = datetime(2026, 9, 8, 18, 0, tzinfo=timezone.utc)


def _headers(user_id: str) -> dict[str, str]:
    return {"X-Test-User": user_id}


@pytest.fixture
def credential_pg_database(cluster):
    # Reuse the current 0079 allocator, runtime role and verified cleanup with
    # function scope. Each API case owns its committed history; no row deletion
    # or externally owned transaction/savepoint is needed between cases.
    yield from current_pg_database.__wrapped__(cluster)


@pytest.fixture
def credential_api(credential_pg_database, monkeypatch: pytest.MonkeyPatch):
    owner_engine, runtime_engine = credential_pg_database
    factory = sessionmaker(bind=runtime_engine, expire_on_commit=False)
    inspection_factory = sessionmaker(bind=owner_engine, expire_on_commit=False)

    actors = {
        "admin-all": (1, "admin"),
        "admin-scoped": (1, "admin"),
        "read-only": (1, "viewer"),
        "revoked-admin": (1, "admin"),
        "stale-admin": (1, "admin"),
        "other-org-admin": (2, "admin"),
    }
    memberships = {
        "admin-all": ("all", [], True, "admin"),
        # JSON rows created by older writers can contain decimal string IDs.
        "admin-scoped": ("selected", ["101", "201"], True, "admin"),
        "read-only": ("all", [], True, "viewer"),
        "revoked-admin": ("all", [], False, "admin"),
        # Simulates an access token minted before the membership was downgraded.
        "stale-admin": ("all", [], True, "viewer"),
        "other-org-admin": ("all", [], True, "admin"),
    }
    now = datetime.now(timezone.utc)
    with inspection_factory() as session:
        session.add_all([
            LkOrganizationRow(organization_id=1, slug="one", name="One"),
            LkOrganizationRow(organization_id=2, slug="two", name="Two"),
        ])
        session.flush()
        session.add_all(
            [
                LkUserRow(
                    user_id=user_id,
                    organization_id=organization_id,
                    email=f"{user_id}@example.invalid",
                    password_hash="unused-synthetic",
                    full_name=user_id,
                    permission_profile=profile,
                    is_active=True,
                )
                for user_id, (organization_id, profile) in actors.items()
            ]
        )
        session.flush()
        session.add_all(
            [
                *[
                    LkSessionRow(
                        session_id=f"synthetic-login-{user_id}",
                        user_id=user_id,
                        issued_at=now,
                        last_seen_at=now,
                        expires_at=now + timedelta(hours=1),
                        refresh_token_hash="synthetic-unused-refresh-hash",
                        user_agent="synthetic-credential-api-test",
                        ip_address="192.0.2.1",
                    )
                    for user_id in actors
                ],
                *[
                    IamMembershipRow(
                        membership_id=index,
                        organization_id=actors[user_id][0],
                        user_id=user_id,
                        role=membership_profile,
                        permissions=sorted(
                            permissions_from_profile(membership_profile)
                        ),
                        scope_mode=scope,
                        allowed_account_ids=account_ids,
                        is_active=is_active,
                    )
                    for index, (
                        user_id,
                        (scope, account_ids, is_active, membership_profile),
                    ) in enumerate(memberships.items(), 1)
                ],
                MarketplaceAccountRow(
                    marketplace_account_id=101,
                    organization_id=1,
                    marketplace="wb",
                    external_account_id=WB_SELLER_ID,
                    status="connected",
                ),
                MarketplaceAccountRow(
                    marketplace_account_id=102,
                    organization_id=1,
                    marketplace="wb",
                    external_account_id=OTHER_WB_SELLER_ID,
                    status="connected",
                ),
                MarketplaceAccountRow(
                    marketplace_account_id=201,
                    organization_id=1,
                    marketplace="avito",
                    external_account_id="explicit-avito-account-one",
                    status="connected",
                ),
                MarketplaceAccountRow(
                    marketplace_account_id=301,
                    organization_id=2,
                    marketplace="avito",
                    external_account_id="explicit-avito-account-two",
                    status="connected",
                ),
                LkUserWbTokenRow(
                    token_id=1,
                    user_id="admin-all",
                    organization_id=1,
                    wb_token=LEGACY_CANARY,
                    token_masked="***",
                ),
            ]
        )
        session.commit()

    monkeypatch.setattr(credential_store, "get_session_factory", lambda: factory)
    monkeypatch.setattr(
        credential_store,
        "_load_keyring",
        lambda: CredentialKeyring(current_key_version=7, keys={7: b"a" * 32}),
    )
    monkeypatch.setattr(credential_store, "_utc_now", lambda: NOW)

    def override_session():
        with factory() as session:
            yield session

    def override_actor(request: Request) -> ActorContext:
        user_id = request.headers.get("x-test-user")
        if user_id not in actors:
            raise HTTPException(status_code=401, detail="AUTH_REQUIRED")
        organization_id, profile = actors[user_id]
        return ActorContext(
            actor_id=user_id,
            user_id=user_id,
            organization_id=organization_id,
            permission_profile=profile,
            permissions=permissions_from_profile(profile),
            session_id=f"synthetic-login-{user_id}",
        )

    from app.routers import cabinet as cabinet_router

    def unexpected_wb_verification(_token: str) -> str:
        raise AssertionError("WB verification must be explicitly faked by the case")

    monkeypatch.setattr(cabinet_router, "actor_from_request", override_actor)
    monkeypatch.setattr(cabinet_router, "get_session_factory", lambda: factory)
    monkeypatch.setattr(cabinet_router, "fetch_wb_seller_id", unexpected_wb_verification)
    # The real route dependency imports its own symbols. Forward the key loader
    # so per-case key-readiness failures still reach the real management service.
    monkeypatch.setattr(cabinet_router, "_load_keyring", lambda: credential_store._load_keyring())
    monkeypatch.setattr(cabinet_store, "get_engine", lambda: owner_engine)
    monkeypatch.setattr(cabinet_store, "get_session_factory", lambda: inspection_factory)
    app = create_app()
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[get_marketplace_credential_actor] = override_actor
    client = TestClient(app)
    try:
        yield SimpleNamespace(client=client, factory=inspection_factory)
    finally:
        client.close()
        app.dependency_overrides.clear()


def _path(account_id: int, provider: str, kind: str) -> str:
    return (
        f"/api/v1/cabinet/marketplace-accounts/{account_id}"
        f"/credentials/{provider}/{kind}"
    )


def test_credential_mutations_require_write_permission_and_active_membership(
    credential_api,
) -> None:
    path = _path(201, "avito", "avito_oauth_client")
    payload = {"clientId": "synthetic-client", "clientSecret": CANARY}

    read_only = credential_api.client.put(
        path,
        json=payload,
        headers=_headers("read-only"),
    )
    revoked = credential_api.client.put(
        path,
        json=payload,
        headers=_headers("revoked-admin"),
    )
    stale_permission = credential_api.client.put(
        path,
        json=payload,
        headers=_headers("stale-admin"),
    )

    assert read_only.status_code == 403
    assert revoked.status_code == 403
    assert stale_permission.status_code == 403
    assert CANARY not in read_only.text
    assert CANARY not in revoked.text
    assert CANARY not in stale_permission.text
    with credential_api.factory() as session:
        assert session.scalars(select(MarketplaceAccountCredentialRow)).all() == []


def test_credential_access_is_tenant_account_and_provider_scoped(
    credential_api,
) -> None:
    cross_tenant = credential_api.client.get(
        _path(301, "avito", "avito_oauth_client"),
        headers=_headers("admin-all"),
    )
    outside_allowed_accounts = credential_api.client.get(
        _path(102, "wb", "wb_api"),
        headers=_headers("admin-scoped"),
    )
    wrong_provider = credential_api.client.get(
        _path(101, "avito", "avito_oauth_client"),
        headers=_headers("admin-all"),
    )

    assert cross_tenant.status_code == 404
    assert cross_tenant.json()["error"]["code"] == "credential_account_not_found"
    assert outside_allowed_accounts.status_code == 403
    assert (
        outside_allowed_accounts.json()["error"]["code"]
        == "credential_management_access_denied"
    )
    assert wrong_provider.status_code == 409
    assert (
        wrong_provider.json()["error"]["code"]
        == "credential_account_identity_mismatch"
    )

    unsupported_kind = credential_api.client.get(
        _path(201, "avito", "avito_oauth_access"),
        headers=_headers("admin-all"),
    )
    assert unsupported_kind.status_code == 400
    assert unsupported_kind.json()["error"]["code"] == "CREDENTIAL_KIND_UNSUPPORTED"


def test_wb_create_replace_revoke_is_verified_encrypted_only_and_actor_audited(
    credential_api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routers import cabinet as cabinet_router

    verified_tokens: list[str] = []

    def verify_seller(token: str) -> str:
        verified_tokens.append(token)
        return WB_SELLER_ID

    monkeypatch.setattr(cabinet_router, "fetch_wb_seller_id", verify_seller)
    path = _path(101, "wb", "wb_api")

    created = credential_api.client.put(
        path,
        json={"wbToken": CANARY},
        headers=_headers("admin-all"),
    )
    replaced = credential_api.client.put(
        path,
        json={"wbToken": f"{CANARY}-replacement"},
        headers=_headers("admin-all"),
    )
    revoked = credential_api.client.delete(
        path,
        params={"reasonCode": "operator_revoked"},
        headers=_headers("admin-all"),
    )
    status = credential_api.client.get(path, headers=_headers("admin-all"))

    assert created.status_code == 200
    assert replaced.status_code == 200
    assert revoked.status_code == 200
    assert status.status_code == 200
    assert created.json()["data"]["status"] == "active"
    assert replaced.json()["data"]["status"] == "active"
    assert revoked.json()["data"]["status"] == "revoked"
    assert status.json()["data"]["status"] == "revoked"
    assert verified_tokens == [CANARY, f"{CANARY}-replacement"]
    forbidden = (
        CANARY,
        LEGACY_CANARY,
        "ciphertext",
        "nonce",
        "verifier",
        "keyVersion",
        "credentialId",
        "generation",
    )
    combined_responses = "\n".join(
        response.text for response in (created, replaced, revoked, status)
    )
    assert all(value not in combined_responses for value in forbidden)

    with credential_api.factory() as session:
        legacy = session.get(LkUserWbTokenRow, 1)
        rows = session.scalars(
            select(MarketplaceAccountCredentialRow).order_by(
                MarketplaceAccountCredentialRow.generation
            )
        ).all()
        audits = session.scalars(
            select(LkAuditEventRow).order_by(LkAuditEventRow.event_id)
        ).all()
    assert legacy is not None and legacy.wb_token == LEGACY_CANARY
    assert [row.generation for row in rows] == [1, 2]
    assert [row.revocation_reason_code for row in rows] == [
        "credential_replaced",
        "operator_revoked",
    ]
    assert all(CANARY.encode() not in row.ciphertext for row in rows)
    assert [row.actor_user_id for row in audits] == [
        "admin-all",
        "admin-all",
        "admin-all",
    ]
    assert all(
        row.details["marketplaceAccountId"] == 101
        and row.details["provider"] == "wb"
        for row in audits
    )
    assert CANARY not in json.dumps(
        [row.details for row in audits],
        sort_keys=True,
    )


def test_wb_writer_rejects_seller_identity_mismatch_before_persistence(
    credential_api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routers import cabinet as cabinet_router

    monkeypatch.setattr(
        cabinet_router,
        "fetch_wb_seller_id",
        lambda _token: OTHER_WB_SELLER_ID,
    )
    response = credential_api.client.put(
        _path(101, "wb", "wb_api"),
        json={"wbToken": CANARY},
        headers=_headers("admin-all"),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "WB_SELLER_IDENTITY_MISMATCH"
    assert CANARY not in response.text
    with credential_api.factory() as session:
        assert session.scalars(select(MarketplaceAccountCredentialRow)).all() == []


def test_unavailable_keyring_fails_before_wb_provider_verification(
    credential_api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routers import cabinet as cabinet_router

    provider_called = False

    def unexpected_provider_call(_token: str) -> str:
        nonlocal provider_called
        provider_called = True
        return WB_SELLER_ID

    def unavailable_keyring():
        raise credential_store.CredentialStoreError(
            "credential_configuration_invalid"
        )

    monkeypatch.setattr(
        cabinet_router,
        "fetch_wb_seller_id",
        unexpected_provider_call,
    )
    monkeypatch.setattr(credential_store, "_load_keyring", unavailable_keyring)

    response = credential_api.client.put(
        _path(101, "wb", "wb_api"),
        json={"wbToken": CANARY},
        headers=_headers("admin-all"),
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "credential_configuration_invalid"
    assert provider_called is False
    assert CANARY not in response.text


def test_avito_writer_uses_only_explicit_account_and_never_returns_secrets(
    credential_api,
) -> None:
    path = _path(201, "avito", "avito_oauth_client")
    response = credential_api.client.put(
        path,
        json={"clientId": "synthetic-client", "clientSecret": CANARY},
        headers=_headers("admin-scoped"),
    )

    assert response.status_code == 200
    assert response.json()["data"]["marketplaceAccountId"] == 201
    assert response.json()["data"]["status"] == "active"
    assert CANARY not in response.text
    assert "synthetic-client" not in response.text
    resolved = resolve_marketplace_credential(
        MarketplaceAccountCredentialOwner(1, 201, "avito"),
        "avito_oauth_client",
    )
    assert resolved.reveal() == {
        "clientId": "synthetic-client",
        "clientSecret": CANARY,
    }


def test_corrupt_encrypted_row_fails_closed_without_legacy_reader(
    credential_api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routers import cabinet as cabinet_router

    legacy_reader_called = False

    def legacy_reader(*_args, **_kwargs):
        nonlocal legacy_reader_called
        legacy_reader_called = True
        raise AssertionError("legacy credential reader must not run")

    monkeypatch.setattr(cabinet_router, "get_user_wb_token", legacy_reader)
    monkeypatch.setattr(
        cabinet_router,
        "fetch_wb_seller_id",
        lambda _token: WB_SELLER_ID,
    )
    path = _path(101, "wb", "wb_api")
    created = credential_api.client.put(
        path,
        json={"wbToken": CANARY},
        headers=_headers("admin-all"),
    )
    assert created.status_code == 200
    with credential_api.factory() as session:
        row = session.scalars(select(MarketplaceAccountCredentialRow)).one()
        row.ciphertext = bytes((row.ciphertext[0] ^ 1,)) + row.ciphertext[1:]
        session.commit()

    response = credential_api.client.get(path, headers=_headers("admin-all"))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "credential_auth_failed"
    assert CANARY not in response.text
    assert LEGACY_CANARY not in response.text
    assert legacy_reader_called is False


def test_unknown_credential_key_is_rejected_without_reflection(credential_api) -> None:
    response = credential_api.client.put(
        _path(201, "avito", "avito_oauth_client"),
        json={"clientId": "synthetic-client", "clientSecret": CANARY, CANARY: CANARY},
        headers=_headers("admin-all"),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert CANARY not in response.text
    issues = response.json()["error"]["details"]["issues"]
    assert len(issues) == 1
    assert issues[0]["loc"] == ["body"]
    assert issues[0]["msg"] == "Value error, CREDENTIAL_PAYLOAD_INVALID"
    assert "input" not in issues[0]
    assert "ctx" not in issues[0]
    with credential_api.factory() as session:
        assert session.scalars(select(MarketplaceAccountCredentialRow)).all() == []
        assert session.scalars(select(LkAuditEventRow)).all() == []


def test_validation_errors_and_openapi_hide_secret_values_and_storage_metadata(
    credential_api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.routers import cabinet as cabinet_router

    path = _path(201, "avito", "avito_oauth_client")
    invalid = credential_api.client.put(
        path,
        json={
            "clientId": "synthetic-client",
            "clientSecret": CANARY,
            "unexpectedSecret": CANARY,
        },
        headers=_headers("admin-all"),
    )

    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"
    assert CANARY not in invalid.text

    provider_called = False

    def unexpected_provider_call(_token: str) -> str:
        nonlocal provider_called
        provider_called = True
        return WB_SELLER_ID

    monkeypatch.setattr(
        cabinet_router,
        "fetch_wb_seller_id",
        unexpected_provider_call,
    )
    oversized = credential_api.client.put(
        _path(101, "wb", "wb_api"),
        json={"wbToken": "s" * 8_193},
        headers=_headers("admin-all"),
    )
    assert oversized.status_code == 422
    assert provider_called is False
    assert "s" * 128 not in oversized.text

    route_template = (
        "/api/v1/cabinet/marketplace-accounts/{marketplaceAccountId}"
        "/credentials/{provider}/{credentialKind}"
    )
    operation = credential_api.client.get("/openapi.json").json()["paths"][
        route_template
    ]
    response_contract = json.dumps(
        {
            method: value.get("responses", {})
            for method, value in operation.items()
            if isinstance(value, dict)
        },
        sort_keys=True,
    ).lower()
    assert all(
        forbidden not in response_contract
        for forbidden in ("ciphertext", "nonce", "verifier", "keyversion")
    )


def test_legacy_cabinet_endpoint_contract_remains_registered(credential_api) -> None:
    @event.listens_for(credential_api.factory, "after_begin")
    def _non_utc_transaction(_session, _transaction, connection):
        connection.exec_driver_sql("SET LOCAL TIME ZONE 'Europe/Moscow'")

    with credential_api.factory() as session:
        updated_at = session.get(LkUserWbTokenRow, 1).updated_at
        assert updated_at.utcoffset() == timedelta(hours=3)

    response = credential_api.client.get(
        "/api/v1/cabinet/wb-token",
        headers=_headers("admin-all"),
    )

    assert response.status_code == 200
    assert set(response.json()["data"]) == {
        "userId",
        "hasToken",
        "tokenMasked",
        "updatedAt",
    }
    published_at = datetime.fromisoformat(response.json()["data"]["updatedAt"])
    assert published_at.utcoffset() == timedelta(0)
    assert published_at == updated_at
