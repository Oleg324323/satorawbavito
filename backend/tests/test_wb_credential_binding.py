from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.cabinet import store
from app.cabinet.orm import (
    LkAuditEventRow,
    LkOrganizationRow,
    LkUserRow,
    LkUserWbTokenRow,
)
from app.infra.models import Base
from app.platform.integrations import wb_credentials
from app.platform.integrations.orm import MarketplaceAccountRow
from app.platform.identity.orm import IamMembershipRow


SELLER_ID = "64f8d3e5-3d25-4b5c-9f23-0fd3937af451"


@pytest.fixture
def binding_db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    with factory() as session:
        session.add_all(
            [
                LkOrganizationRow(organization_id=2, slug="two", name="Two"),
                LkUserRow(
                    user_id="owner-two",
                    organization_id=2,
                    email="owner-two@example.com",
                    password_hash="unused",
                    full_name="Owner Two",
                    permission_profile="admin",
                    is_active=True,
                ),
                LkUserWbTokenRow(
                    token_id=1,
                    user_id="owner-two",
                    organization_id=2,
                    wb_token="original-secret",
                    token_masked="***",
                ),
                MarketplaceAccountRow(
                    marketplace_account_id=2,
                    organization_id=2,
                    marketplace="wb",
                    external_account_id="legacy-org-2",
                    status="connected",
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )
        session.commit()
    return engine, factory


def test_fetch_wb_seller_id_accepts_only_verified_uuid() -> None:
    client = SimpleNamespace(
        request=lambda request: SimpleNamespace(
            ok=True,
            statusCode=200,
            data={"sid": SELLER_ID},
        )
    )

    assert wb_credentials.fetch_wb_seller_id("secret", client=client) == SELLER_ID

    client.request = lambda request: SimpleNamespace(
        ok=True,
        statusCode=200,
        data={"sid": "not-a-wb-seller-id"},
    )
    with pytest.raises(wb_credentials.WbCredentialBindingError):
        wb_credentials.fetch_wb_seller_id("secret", client=client)


def test_bind_wb_credential_replaces_only_expected_legacy_id(
    binding_db, monkeypatch
) -> None:
    _engine, factory = binding_db
    monkeypatch.setattr(wb_credentials, "get_session_factory", lambda: factory)
    monkeypatch.setattr(wb_credentials, "fetch_wb_seller_id", lambda token: SELLER_ID)

    result = wb_credentials.bind_wb_credential(
        2,
        marketplace_account_id=2,
        token_id=1,
        expected_external_account_id="legacy-org-2",
    )

    assert result == {
        "marketplaceAccountId": 2,
        "credentialRef": "lk_user_wb_tokens:1",
        "sellerIdentityVerified": True,
    }
    with factory() as session:
        account = session.get(MarketplaceAccountRow, 2)
        assert account is not None
        assert account.external_account_id == SELLER_ID
        assert account.credential_ref == "lk_user_wb_tokens:1"
        audit = session.query(LkAuditEventRow).one()
        assert audit.action == "integration.wb_credential.bind"
        assert audit.details == {"sellerIdentityVerified": True, "tokenId": 1}


def test_bind_wb_credential_rejects_token_rotation_during_probe(
    binding_db, monkeypatch
) -> None:
    _engine, factory = binding_db
    monkeypatch.setattr(wb_credentials, "get_session_factory", lambda: factory)

    def rotate_token(_token: str) -> str:
        with factory() as session:
            row = session.get(LkUserWbTokenRow, 1)
            assert row is not None
            row.wb_token = "rotated-secret"
            session.commit()
        return SELLER_ID

    monkeypatch.setattr(wb_credentials, "fetch_wb_seller_id", rotate_token)

    with pytest.raises(wb_credentials.WbCredentialBindingError):
        wb_credentials.bind_wb_credential(
            2,
            marketplace_account_id=2,
            token_id=1,
            expected_external_account_id="legacy-org-2",
        )

    with factory() as session:
        account = session.get(MarketplaceAccountRow, 2)
        assert account is not None
        assert account.external_account_id == "legacy-org-2"
        assert account.credential_ref is None


def test_bind_wb_credential_never_replaces_a_different_verified_seller(
    binding_db, monkeypatch
) -> None:
    _engine, factory = binding_db
    other_seller_id = "5e2e5772-c32a-44ec-897a-485497d67585"
    with factory() as session:
        account = session.get(MarketplaceAccountRow, 2)
        assert account is not None
        account.external_account_id = other_seller_id
        session.commit()
    monkeypatch.setattr(wb_credentials, "get_session_factory", lambda: factory)
    monkeypatch.setattr(wb_credentials, "fetch_wb_seller_id", lambda token: SELLER_ID)

    with pytest.raises(wb_credentials.WbCredentialBindingError):
        wb_credentials.bind_wb_credential(
            2,
            marketplace_account_id=2,
            token_id=1,
            expected_external_account_id=other_seller_id,
        )


def test_repeated_verified_binding_preserves_account_and_token_identity(binding_db, monkeypatch):
    _engine, factory = binding_db
    monkeypatch.setattr(wb_credentials, "get_session_factory", lambda: factory)
    monkeypatch.setattr(wb_credentials, "fetch_wb_seller_id", lambda _token: SELLER_ID)
    for _ in range(2):
        wb_credentials.bind_wb_credential(2, marketplace_account_id=2, token_id=1,
            expected_external_account_id="legacy-org-2")
    with factory() as session:
        account_id, reference, token = wb_credentials.resolve_bound_wb_credential(session, 2, marketplace_account_id=2)
        assert (account_id, reference, token) == (2, "lk_user_wb_tokens:1", "original-secret")
        assert session.query(MarketplaceAccountRow).count() == 1
        assert session.query(LkUserWbTokenRow).count() == 1
        assert session.query(IamMembershipRow).count() == 0


@pytest.mark.parametrize("invalid", ["foreign_organization", "inactive_owner", "missing_token"])
def test_verified_binding_rejects_invalid_token_ownership_before_provider_probe(binding_db, monkeypatch, invalid):
    _engine, factory = binding_db
    monkeypatch.setattr(wb_credentials, "get_session_factory", lambda: factory)
    def unexpected_probe(_token):
        pytest.fail("Invalid token ownership must be rejected before a WB request")
    monkeypatch.setattr(wb_credentials, "fetch_wb_seller_id", unexpected_probe)
    token_id = 1
    with factory() as session:
        if invalid == "foreign_organization":
            session.add_all([
                LkOrganizationRow(organization_id=3, slug="three", name="Three"),
                LkUserRow(user_id="owner-three", organization_id=3, email="three@example.com",
                    password_hash="unused", full_name="Three", permission_profile="admin", is_active=True),
                LkUserWbTokenRow(token_id=3, user_id="owner-three", organization_id=3,
                    wb_token="synthetic-foreign-token", token_masked="***"),
            ])
            token_id = 3
        elif invalid == "inactive_owner":
            session.get(LkUserRow, "owner-two").is_active = False
        else:
            session.delete(session.get(LkUserWbTokenRow, token_id))
        session.commit()
    with pytest.raises(wb_credentials.WbCredentialBindingError):
        wb_credentials.bind_wb_credential(2, marketplace_account_id=2, token_id=token_id,
            expected_external_account_id="legacy-org-2")
    with factory() as session:
        assert session.get(MarketplaceAccountRow, 2).credential_ref is None
        assert session.query(LkAuditEventRow).count() == 0


def test_resolver_still_rejects_multiple_connected_accounts_after_verified_binding(binding_db, monkeypatch):
    _engine, factory = binding_db
    monkeypatch.setattr(wb_credentials, "get_session_factory", lambda: factory)
    monkeypatch.setattr(wb_credentials, "fetch_wb_seller_id", lambda _token: SELLER_ID)
    wb_credentials.bind_wb_credential(2, marketplace_account_id=2, token_id=1,
        expected_external_account_id="legacy-org-2")
    with factory() as session:
        session.add(MarketplaceAccountRow(marketplace_account_id=3, organization_id=2,
            marketplace="wb", external_account_id="another-seller", status="connected"))
        session.commit()
        with pytest.raises(wb_credentials.WbCredentialBindingError):
            wb_credentials.resolve_bound_wb_credential(session, 2, marketplace_account_id=2)


def test_funnel_scope_requires_explicit_binding_and_preserves_membership_checks(binding_db, monkeypatch):
    from fastapi import HTTPException
    from app.routers import wb_sales_funnel as router

    _engine, factory = binding_db
    monkeypatch.setattr(wb_credentials, "get_session_factory", lambda: factory)
    monkeypatch.setattr(wb_credentials, "fetch_wb_seller_id", lambda _token: SELLER_ID)
    actor = SimpleNamespace(organization_id=2, user_id="owner-two")
    monkeypatch.setattr(router, "actor_from_request", lambda _request: actor)
    start, end = date(2026, 10, 19), date(2026, 10, 25)
    with factory() as session:
        with pytest.raises(HTTPException) as error:
            router._scope(None, session, start, end, 2)
        assert error.value.status_code == 409
        assert session.get(MarketplaceAccountRow, 2).credential_ref is None
        assert session.query(LkAuditEventRow).count() == 0
    wb_credentials.bind_wb_credential(2, marketplace_account_id=2, token_id=1,
        expected_external_account_id="legacy-org-2")
    with factory() as session:
        with pytest.raises(HTTPException) as error:
            router._scope(None, session, start, end, 2)
        assert error.value.status_code == 403  # A binding does not grant membership.
        membership = IamMembershipRow(organization_id=2, user_id="owner-two", role="admin",
            permissions=[], scope_mode="selected", allowed_account_ids=[], is_active=True)
        session.add(membership)
        session.commit()
        with pytest.raises(HTTPException) as error:
            router._scope(None, session, start, end, 2)
        assert error.value.status_code == 403  # An unrelated scope remains denied.
        membership.allowed_account_ids = [2]
        session.commit()
        resolved_actor, account, scope, period = router._scope(None, session, start, end, 2)
        assert resolved_actor is actor and account == 2
        assert scope == router.scope_key(2, "original-secret")
        assert (period.date_from, period.date_to) == (start, end)


def test_token_rotation_and_delete_invalidate_credential_binding(
    binding_db, monkeypatch
) -> None:
    engine, factory = binding_db
    monkeypatch.setattr(store, "get_engine", lambda: engine)
    monkeypatch.setattr(store, "get_session_factory", lambda: factory)
    monkeypatch.setattr(store, "_append_audit_event", lambda **_kwargs: None)

    class TokenView(SimpleNamespace):
        def model_dump(self, **_kwargs):
            return vars(self)

    monkeypatch.setattr(store, "UserWbTokenView", TokenView)

    with factory() as session:
        account = session.get(MarketplaceAccountRow, 2)
        assert account is not None
        account.credential_ref = "lk_user_wb_tokens:1"
        session.commit()

    store.upsert_user_wb_token(
        user_id="owner-two",
        organization_id=2,
        actor_user_id="owner-two",
        wb_token="rotated-secret",
    )
    with factory() as session:
        assert session.get(MarketplaceAccountRow, 2).credential_ref is None
        session.get(MarketplaceAccountRow, 2).credential_ref = "lk_user_wb_tokens:1"
        session.commit()

    store.upsert_user_wb_token(
        user_id="owner-two",
        organization_id=2,
        actor_user_id="owner-two",
        wb_token="rotated-secret",
    )
    with factory() as session:
        assert session.get(MarketplaceAccountRow, 2).credential_ref == (
            "lk_user_wb_tokens:1"
        )

    store.delete_user_wb_token(
        user_id="owner-two",
        organization_id=2,
        actor_user_id="owner-two",
    )
    with factory() as session:
        assert session.get(MarketplaceAccountRow, 2).credential_ref is None
        assert session.get(LkUserWbTokenRow, 1) is None
