from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.cabinet.orm import LkOrganizationRow, LkUserRow, LkSessionRow
from app.platform.identity.orm import IamMembershipRow
from app.platform.integrations.orm import MarketplaceAccountRow
from app.repricer_cache.orm import WbRepricerSourceCacheRow
from app.control_plane.auth import ActorContext

from ops.local_wb_browser_prices import install_local_browser_price_storage
from app.routers import wb_browser_prices as browser


@pytest.fixture
def local(monkeypatch):
    from app import config
    from app.infra import db
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    factory = sessionmaker(engine)
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(environment="local"))
    monkeypatch.setattr(db, "get_session_factory", lambda: factory)
    for name in ("_session", "_now", "_refresh_lock", "require_live_actor"):
        monkeypatch.setattr(browser, name, getattr(browser, name))
    install_local_browser_price_storage()
    yield factory
    engine.dispose()


def test_sqlite_session_and_database_utc_clock(local):
    with browser._session() as session:
        assert session.scalar(text("SELECT 1")) == 1
        assert browser._now(session).tzinfo == timezone.utc


def test_transaction_rolls_back_and_storage_failure_is_explicit(local):
    with local() as session, session.begin():
        session.execute(text("CREATE TABLE sample (value INTEGER)"))
    with pytest.raises(HTTPException) as failure:
        with browser._session() as session:
            session.execute(text("INSERT INTO sample VALUES (1)"))
            session.execute(text("SELECT * FROM nonexistent"))
    assert failure.value.status_code == 503
    assert failure.value.detail["code"] == "WB_BROWSER_STORAGE_UNAVAILABLE"
    with local() as session:
        assert session.scalar(text("SELECT COUNT(*) FROM sample")) == 0


def test_refresh_lock_is_per_account_and_releases_on_failure(local):
    with browser._refresh_lock(1):
        with pytest.raises(HTTPException) as failure:
            with browser._refresh_lock(1):
                pytest.fail("duplicate refresh acquired")
        assert failure.value.status_code == 409
        with browser._refresh_lock(2):
            pass
    with browser._refresh_lock(1):
        pass


def test_adapter_refuses_nonlocal_environment(monkeypatch):
    from app import config
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(environment="production"))
    with pytest.raises(RuntimeError, match="must not run in production"):
        install_local_browser_price_storage()


@pytest.fixture
def account(local, monkeypatch):
    now = datetime.now(timezone.utc)
    for model in (LkOrganizationRow, LkUserRow, LkSessionRow, IamMembershipRow,
                  MarketplaceAccountRow, WbRepricerSourceCacheRow):
        model.__table__.create(local.kw["bind"])
    with local() as session, session.begin():
        session.add_all([LkOrganizationRow(organization_id=i, slug=f"test-{i}", name="Synthetic") for i in (1, 2)])
        session.add(LkUserRow(user_id="test-user", organization_id=1, email="test@example.invalid",
            password_hash="unused", full_name="Synthetic", permission_profile="custom", is_active=True))
        session.add(LkSessionRow(session_id="test-login", user_id="test-user", issued_at=now,
            last_seen_at=now, expires_at=now + timedelta(hours=1)))
        session.add(IamMembershipRow(membership_id=1, organization_id=1, user_id="test-user",
            role="custom", permissions=["integrations:write"], scope_mode="selected",
            allowed_account_ids=[10], is_active=True))
        session.add_all([MarketplaceAccountRow(marketplace_account_id=i, organization_id=org,
            marketplace="wb", external_account_id=f"synthetic-{i}", status="connected")
            for i, org in ((10, 1), (20, 2))])
    actor = ActorContext("test-user", "test-user", 1, "custom", frozenset(), session_id="test-login")
    monkeypatch.setattr(browser, "actor_from_request", lambda request: actor)
    app = FastAPI()
    app.include_router(browser.router)
    return SimpleNamespace(factory=local, client=TestClient(app), actor=actor)


def test_account_discovery_and_unconfigured_connection_are_read_only(account):
    response = account.client.get("/api/v1/wb/browser-prices/accounts")
    assert response.status_code == 200
    assert [a["marketplaceAccountId"] for a in response.json()["data"]] == [10]
    response = account.client.get("/api/v1/wb/browser-prices/connection?marketplaceAccountId=10")
    assert response.status_code == 200
    assert response.json()["configured"] is False
    with account.factory() as session:
        assert session.query(WbRepricerSourceCacheRow).count() == 0


@pytest.mark.parametrize("failure", ["expired", "revoked", "inactive_user", "inactive_membership",
    "permissions", "wrong_org", "wrong_login_owner", "missing_login"])
def test_live_access_denials(account, failure):
    with account.factory() as session, session.begin():
        user = session.get(LkUserRow, "test-user")
        member = session.get(IamMembershipRow, 1)
        login = session.get(LkSessionRow, "test-login")
        if failure == "expired":
            login.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        elif failure == "revoked":
            login.revoked_at = datetime.now(timezone.utc)
        elif failure == "inactive_user":
            user.is_active = False
        elif failure == "inactive_membership":
            member.is_active = False
        elif failure == "permissions":
            member.permissions = []
        elif failure == "wrong_org":
            user.organization_id = 2
        elif failure == "wrong_login_owner":
            login.user_id = "another-user"
        elif failure == "missing_login":
            session.delete(login)
    for route in ("accounts", "connection?marketplaceAccountId=10"):
        assert account.client.get("/api/v1/wb/browser-prices/" + route).status_code == 403


def test_out_of_scope_and_foreign_account_denied(account):
    assert account.client.get("/api/v1/wb/browser-prices/connection?marketplaceAccountId=20").status_code == 403
    with account.factory() as session, session.begin():
        session.get(IamMembershipRow, 1).allowed_account_ids = []
    assert account.client.get("/api/v1/wb/browser-prices/accounts").json() == {"data": []}
    assert account.client.get("/api/v1/wb/browser-prices/connection?marketplaceAccountId=10").status_code == 403


def test_local_auth_requires_adapter_transaction(account):
    with account.factory() as session, session.begin():
        with pytest.raises(browser.WbLiveError):
            browser.require_live_actor(session, account.actor)
    with browser._session() as session:
        assert browser.require_live_actor(session, account.actor)[0].user_id == "test-user"
    # Keeping a closed Session object must not keep serialized admission.
    with session.begin():
        with pytest.raises(browser.WbLiveError):
            browser.require_live_actor(session, account.actor)


def test_production_auth_still_rejects_sqlite(account):
    from app.wb_live.auth import require_live_actor
    with browser._session() as session:
        with pytest.raises(browser.WbLiveError):
            require_live_actor(session, account.actor)


def test_adapter_does_not_patch_postgres(monkeypatch):
    from app import config
    from app.infra import db
    from contextlib import nullcontext
    original = {name: getattr(browser, name) for name in
        ("_session", "_now", "_refresh_lock", "require_live_actor")}
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(environment="local"))
    fake_session = SimpleNamespace(get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")))
    monkeypatch.setattr(db, "get_session_factory", lambda: lambda: nullcontext(fake_session))
    install_local_browser_price_storage()
    assert all(getattr(browser, name) is function for name, function in original.items())
