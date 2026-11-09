"""Metadata adapter tests; pure doubles are not a PostgreSQL/RLS authorization proof."""
import importlib
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql

from app.control_plane.auth import ActorContext
from app.modules.wb_repricing_override_service import OverridePersistenceError
from app.modules.wb_repricing_overrides import OverrideCommandValidationError
from app.platform.integrations.publication_guard import (
    ExpectedAccountBinding,
    PublicationGuardError,
    UserSessionPrincipal,
)

ACTOR = ActorContext("server-actor", "current-user", 9, "admin", frozenset({"settings:write"}), session_id="current-session")


def api():
    assert importlib.util.find_spec("app.platform.integrations.sku_override_context"), "missing SKU metadata resolver"
    return importlib.import_module("app.platform.integrations.sku_override_context")


class DatabaseDouble:
    def __init__(self):
        self.member = 17
        self.account = SimpleNamespace(external_account_id="wb-seller", credential_ref="binding-reference")
        self.events, self.statements = [], []
        self.fail = None

    def begin(self):
        self.events.append("begin")

    def scalar(self, statement):
        self.statements.append(statement)
        return self.member

    def execute(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(one_or_none=lambda: self.account)

    def commit(self):
        self.events.append("commit")
        if self.fail == "commit":
            raise RuntimeError("private-driver-diagnostic")

    def rollback(self):
        self.events.append("rollback")

    def close(self):
        self.events.append("close")
        if self.fail == "close":
            raise RuntimeError("private-cleanup-diagnostic")


def resolver(monkeypatch, *, denied=None):
    module = api()
    db = DatabaseDouble()
    engine = create_engine("postgresql+psycopg://")
    def session(given_engine, *, autoflush):
        assert given_engine is engine and autoflush is False
        return db
    monkeypatch.setattr(module, "Session", session)
    monkeypatch.setattr(module, "set_tenant_context", lambda session, org: db.events.append(("tenant", org)))
    def guard(session, *, principal, required_permissions, accounts, authorities):
        assert principal == UserSessionPrincipal(9, "current-user", 17, "current-session")
        assert required_permissions == frozenset({"settings:read"})
        assert accounts == (ExpectedAccountBinding(23, "wb", "wb-seller", "binding-reference"),)
        assert authorities == ()
        db.events.append("guard")
        if denied is not None:
            raise PublicationGuardError(denied)
        return SimpleNamespace(revalidate_before_write=lambda: db.events.append("revalidate"))
    monkeypatch.setattr(module, "acquire_publication_guard", guard)
    return module.SkuOverrideContextResolver(engine=engine), db


def test_metadata_is_scoped_to_real_actor_and_exact_wb_account(monkeypatch):
    resolve, db = resolver(monkeypatch)
    principal, binding = resolve(ACTOR, 23)
    assert principal == UserSessionPrincipal(9, "current-user", 17, "current-session")
    assert binding == ExpectedAccountBinding(23, "wb", "wb-seller", "binding-reference")
    sql = [str(statement.compile(dialect=postgresql.dialect())) for statement in db.statements]
    params = [statement.compile().params for statement in db.statements]
    assert len(sql) == 2
    assert "iam_memberships.user_id =" in sql[0] and "iam_memberships.organization_id =" in sql[0]
    assert "iam_memberships.is_active IS true" in sql[0]
    assert set(params[0].values()) == {9, "current-user"}
    assert "marketplace_accounts.organization_id =" in sql[1]
    assert "marketplace_accounts.marketplace_account_id =" in sql[1]
    assert "marketplace_accounts.marketplace =" in sql[1]
    assert set(params[1].values()) == {9, 23, "wb"}
    assert not any(word in " ".join(sql) for word in ("ciphertext", "marketplace_account_credentials", "ingestion_token", "wb_token"))
    assert db.events.index("guard") < db.events.index("revalidate") < db.events.index("commit") < db.events.index("close")


@pytest.mark.parametrize("account", [True, "23", 0, -1, 2**31])
def test_invalid_account_does_not_reach_database(monkeypatch, account):
    resolve, db = resolver(monkeypatch)
    with pytest.raises(OverrideCommandValidationError):
        resolve(ACTOR, account)
    assert db.events == []


@pytest.mark.parametrize("actor", [None, replace(ACTOR, session_id=None), replace(ACTOR, user_id=""), replace(ACTOR, organization_id=True)])
def test_non_server_or_invalid_actor_is_denied_before_database(monkeypatch, actor):
    resolve, db = resolver(monkeypatch)
    with pytest.raises(PublicationGuardError, match="^publication_access_denied$"):
        resolve(actor, 23)
    assert db.events == []


@pytest.mark.parametrize("missing", ["member", "account"])
def test_missing_current_membership_or_exact_account_is_denied(monkeypatch, missing):
    resolve, db = resolver(monkeypatch)
    setattr(db, missing, None)
    with pytest.raises(PublicationGuardError, match="^publication_access_denied$"):
        resolve(ACTOR, 23)
    assert "commit" not in db.events and db.events[-1] == "close"


@pytest.mark.parametrize("reason", ["publication_access_denied", "publication_expired", "publication_binding_changed"])
def test_guard_denial_is_not_replaced_by_actor_permission_cache(monkeypatch, reason):
    resolve, db = resolver(monkeypatch, denied=reason)
    with pytest.raises(PublicationGuardError, match="^publication_access_denied$") as error:
        resolve(ACTOR, 23)
    assert error.value.__context__ is None
    assert "commit" not in db.events


@pytest.mark.parametrize("failure", ["commit", "close"])
def test_unknown_commit_or_cleanup_never_returns_snapshot(monkeypatch, failure):
    resolve, db = resolver(monkeypatch)
    db.fail = failure
    with pytest.raises(OverridePersistenceError, match="^override_persistence_failed$") as error:
        resolve(ACTOR, 23)
    assert error.value.__context__ is None
    assert db.events[-1] == "close"


def test_only_postgresql_engine_is_admitted():
    module = api()
    with pytest.raises(OverridePersistenceError):
        module.SkuOverrideContextResolver(engine=create_engine("sqlite://"))


# Existing disposable allocator + exact local API grants. Selecting only pure
# tests never creates these fixtures or opens a PostgreSQL connection.
from tests import test_notification_preferences_postgres as preferences_pg

cluster = preferences_pg.cluster
database = preferences_pg.database
state = preferences_pg.state


@pytest.fixture
def postgres_context(state):
    from sqlalchemy import text
    from sqlalchemy.orm import Session

    from app.platform.integrations.orm import MarketplaceAccountRow

    org = state.actor.organization_id
    with Session(state.owner) as session, session.begin():
        session.add(MarketplaceAccountRow(marketplace_account_id=org, organization_id=org, marketplace="wb",
            external_account_id="synthetic-seller", credential_ref="metadata-ref", status="connected"))
        session.execute(text("UPDATE iam_memberships SET permissions='[\"settings:read\"]',scope_mode='selected',"
                             "allowed_account_ids=CAST(:ids AS json) WHERE membership_id=:id"),
                        {"ids": "[" + str(org) + "]", "id": org})
    return state


def test_postgres_resolver_uses_real_session_and_metadata_only(postgres_context):
    from sqlalchemy import event

    state = postgres_context
    statements = []
    def capture(connection, cursor, statement, parameters, context, many):
        statements.append(statement)
    event.listen(state.runtime, "before_cursor_execute", capture)
    try:
        principal, binding = api().SkuOverrideContextResolver(engine=state.runtime)(state.actor, state.actor.organization_id)
    finally:
        event.remove(state.runtime, "before_cursor_execute", capture)
    assert principal == UserSessionPrincipal(state.actor.organization_id, state.actor.user_id,
        state.actor.organization_id, state.actor.session_id)
    assert binding == ExpectedAccountBinding(state.actor.organization_id, "wb", "synthetic-seller", "metadata-ref")
    assert not any(word in " ".join(statements) for word in ("ciphertext", "marketplace_account_credentials", "ingestion_token", "wb_token"))


@pytest.mark.parametrize("change", ["session", "membership", "permission", "scope", "provider"])
def test_postgres_current_authority_denies_stale_actor_cache(postgres_context, change):
    from sqlalchemy import text

    state = postgres_context
    sql = {"session": "UPDATE lk_sessions SET revoked_at=clock_timestamp() WHERE session_id=:id",
           "membership": "UPDATE iam_memberships SET is_active=false WHERE membership_id=:id",
           "permission": "UPDATE iam_memberships SET permissions='[]' WHERE membership_id=:id",
           "scope": "UPDATE iam_memberships SET allowed_account_ids='[]' WHERE membership_id=:id",
           "provider": "UPDATE marketplace_accounts SET marketplace='avito' WHERE marketplace_account_id=:id"}[change]
    with state.owner.begin() as c:
        c.execute(text(sql), {"id": state.actor.session_id if change == "session" else state.actor.organization_id})
    actor = replace(state.actor, permission_profile="admin", permissions=frozenset({"settings:read", "settings:write"}))
    with pytest.raises(PublicationGuardError, match="^publication_access_denied$"):
        api().SkuOverrideContextResolver(engine=state.runtime)(actor, state.actor.organization_id)


def test_postgres_wrong_organization_cannot_resolve_account(postgres_context):
    state = postgres_context
    actor = replace(state.actor, organization_id=state.actor.organization_id + 1)
    with pytest.raises(PublicationGuardError, match="^publication_access_denied$"):
        api().SkuOverrideContextResolver(engine=state.runtime)(actor, state.actor.organization_id)


def test_postgres_closing_guard_rejects_session_revocation_and_rolls_it_back(postgres_context):
    from sqlalchemy import event, text
    from sqlalchemy.orm import Session

    state = postgres_context
    def revoke_before_guard(session):
        session.execute(text("UPDATE lk_sessions SET revoked_at=clock_timestamp() WHERE session_id=:id"),
                        {"id": state.actor.session_id})
    event.listen(Session, "before_commit", revoke_before_guard)
    try:
        with pytest.raises(PublicationGuardError, match="^publication_access_denied$"):
            api().SkuOverrideContextResolver(engine=state.runtime)(state.actor, state.actor.organization_id)
    finally:
        event.remove(Session, "before_commit", revoke_before_guard)
    with state.owner.connect() as c:
        assert c.scalar(text("SELECT revoked_at FROM lk_sessions WHERE session_id=:id"), {"id": state.actor.session_id}) is None
