"""Restricted-role metadata discovery in allocator-owned PostgreSQL only."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.cabinet.orm import LkSessionRow, LkUserRow
from app.control_plane.auth import ActorContext
from app.platform.identity.orm import IamMembershipRow
from app.platform.integrations import account_discovery as module
from app.platform.integrations.account_discovery import (
    AccountDiscoveryError,
    MarketplaceAccountDiscoveryService,
)
from tests import test_orders_schema_candidate as candidate
from tests.test_orders_exact_text_migration import migrated_database

cluster = candidate.cluster


@pytest.fixture(scope="module")
def discovery_db(cluster):
    with migrated_database(cluster, "head") as (owner, runtime):
        with owner.begin() as c:
            c.execute(
                text("""INSERT INTO marketplace_accounts
                (organization_id,marketplace_account_id,marketplace,external_account_id,status,display_name)
                VALUES (91001,94101,'wb','discovery-wb','disconnected',NULL),
                       (91001,94102,'avito','discovery-avito','connected','Synthetic Avito'),
                       (91002,94201,'avito','discovery-other','connected','Other org')""")
            )
        yield owner, runtime


@pytest.fixture
def actor(discovery_db):
    owner, _ = discovery_db
    user, login = uuid4().hex, uuid4().hex
    now = datetime.now(UTC)
    with Session(owner) as s, s.begin():
        s.add(
            LkUserRow(
                user_id=user,
                organization_id=91001,
                email=user + "@example.invalid",
                password_hash="synthetic-unusable",
                full_name="Synthetic",
                permission_profile="custom",
            )
        )
        s.flush()
        s.add(
            IamMembershipRow(
                organization_id=91001,
                user_id=user,
                role="custom",
                permissions=["cabinet:read"],
                scope_mode="selected",
                allowed_account_ids=[94101, "094102"],
            )
        )
        s.add(
            LkSessionRow(
                session_id=login,
                user_id=user,
                issued_at=now,
                last_seen_at=now,
                expires_at=now + timedelta(hours=1),
            )
        )
    return ActorContext("ignored", user, 91001, "custom", frozenset(), session_id=login)


def test_exact_scope_provider_metadata_and_disconnected(discovery_db, actor):
    _, runtime = discovery_db
    service = MarketplaceAccountDiscoveryService(engine=runtime, enabled=True)
    rows = service.list_accounts(actor)
    assert [r["marketplaceAccountId"] for r in rows] == [94101, 94102]
    assert rows[0]["status"] == "disconnected" and rows[0]["displayName"] is None
    assert service.list_accounts(actor, provider="avito") == [rows[1]]
    assert all(
        set(r)
        == {
            "marketplaceAccountId",
            "provider",
            "externalAccountId",
            "displayName",
            "status",
        }
        for r in rows
    )
    with pytest.raises(AccountDiscoveryError):
        service.list_accounts(replace(actor, organization_id=91002))


@pytest.mark.parametrize(
    "case",
    [
        "expired",
        "revoked",
        "user_inactive",
        "member_inactive",
        "no_permission",
        "malformed_scope",
    ],
)
def test_live_authority_denials(discovery_db, actor, case):
    owner, runtime = discovery_db
    statements = {
        "expired": "UPDATE lk_sessions SET expires_at=clock_timestamp()-interval '1 second' WHERE session_id=:login",
        "revoked": "UPDATE lk_sessions SET revoked_at=clock_timestamp() WHERE session_id=:login",
        "user_inactive": "UPDATE lk_users SET is_active=false WHERE user_id=:user",
        "member_inactive": "UPDATE iam_memberships SET is_active=false WHERE user_id=:user",
        "no_permission": "UPDATE iam_memberships SET permissions='[]'::json WHERE user_id=:user",
        "malformed_scope": "UPDATE iam_memberships SET allowed_account_ids='[true]'::json WHERE user_id=:user",
    }
    with owner.begin() as c:
        c.execute(
            text(statements[case]), {"login": actor.session_id, "user": actor.user_id}
        )
    with pytest.raises(AccountDiscoveryError) as error:
        MarketplaceAccountDiscoveryService(engine=runtime, enabled=True).list_accounts(
            actor
        )
    assert error.value.code == "ACCOUNT_DISCOVERY_ACCESS_DENIED"
    assert error.value.__context__ is None


def test_empty_selected_and_default_off(discovery_db, actor):
    owner, runtime = discovery_db
    with pytest.raises(AccountDiscoveryError) as error:
        MarketplaceAccountDiscoveryService(engine=runtime).list_accounts(actor)
    assert error.value.code == "ACCOUNT_DISCOVERY_DISABLED"
    with owner.begin() as c:
        c.execute(
            text(
                "UPDATE iam_memberships SET allowed_account_ids='[]'::json WHERE user_id=:user"
            ),
            {"user": actor.user_id},
        )
    assert (
        MarketplaceAccountDiscoveryService(engine=runtime, enabled=True).list_accounts(
            actor
        )
        == []
    )


def test_closing_rechecks_actual_session_expiry(discovery_db, actor, monkeypatch):
    _, runtime = discovery_db
    original = module.require_live_actor
    calls = []

    def require(session, current, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            session.execute(
                text(
                    "UPDATE lk_sessions SET expires_at=clock_timestamp()-interval '1 second' WHERE session_id=:login"
                ),
                {"login": actor.session_id},
            )
        return original(session, current, **kwargs)

    monkeypatch.setattr(module, "require_live_actor", require)
    with pytest.raises(AccountDiscoveryError) as error:
        MarketplaceAccountDiscoveryService(engine=runtime, enabled=True).list_accounts(
            actor
        )
    assert error.value.code == "ACCOUNT_DISCOVERY_ACCESS_DENIED" and len(calls) == 2


def test_commit_failure_does_not_return_metadata(discovery_db, actor):
    _, runtime = discovery_db

    def fail(connection):
        raise RuntimeError("synthetic sensitive COMMIT failure")

    event.listen(runtime, "commit", fail)
    try:
        with pytest.raises(AccountDiscoveryError) as error:
            MarketplaceAccountDiscoveryService(
                engine=runtime, enabled=True
            ).list_accounts(actor)
        assert error.value.code == "ACCOUNT_DISCOVERY_UNAVAILABLE"
        assert error.value.__context__ is None
    finally:
        event.remove(runtime, "commit", fail)
