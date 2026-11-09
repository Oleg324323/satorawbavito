"""Normalized user-session browser sink, not token-only HTTP or completeness proof."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.orders.publication_service import publish_orders_manifest
from app.platform.integrations.orm import MarketplaceAccountIngestionTokenRow
from app.platform.integrations.publication_guard import (
    ExpectedIngestionToken,
    PublicationGuardError,
)
from tests.test_orders_projection_repository import item_fact
from tests.test_orders_publication_service import manifest
from tests.test_orders_read_service import ACCOUNTS
from tests.test_orders_read_service import prepared as _prepared
from tests.test_orders_read_service import read_db as _read_db
from tests.test_orders_schema_candidate import cluster as _cluster

prepared, read_db, cluster = _prepared, _read_db, _cluster


@pytest.fixture
def browser_authority(prepared):
    owner, runtime, principal, _ = prepared
    now = datetime.now(UTC)
    token = ExpectedIngestionToken(
        91101, uuid4(), "avito.browser_snapshot.write", now + timedelta(hours=1)
    )
    with Session(owner) as session, session.begin():
        session.execute(
            text(
                'UPDATE iam_memberships SET permissions=\'["cabinet:read","sync:run"]\' '
                "WHERE membership_id=:id"
            ),
            {"id": principal.membership_id},
        )
        session.add(
            MarketplaceAccountIngestionTokenRow(
                token_id=token.token_id,
                organization_id=91001,
                marketplace_account_id=91101,
                provider="avito",
                verifier=b"s" * 32,
                scope=token.scope,
                issued_at=now,
                expires_at=token.expires_at,
            )
        )
    return owner, runtime, principal, token


def publish(session, principal, token, value, key):
    return publish_orders_manifest(
        session,
        principal=principal,
        account=ACCOUNTS[0],
        authorities=(token,),
        manifest=value,
        source_run_key=key,
    )


def browser_manifest(*, complete=True):
    return manifest(
        replace(item_fact(), source_kind="avito-browser"), complete=complete
    )


@pytest.mark.parametrize("complete", [True, False])
def test_browser_manifest_is_durable_and_exact_replay(browser_authority, complete):
    owner, runtime, principal, token = browser_authority
    value, key = browser_manifest(complete=complete), uuid4().hex
    with Session(runtime) as session:
        first = publish(session, principal, token, value, key)
        second = publish(session, principal, token, value, key)
        assert first.run_id == second.run_id and second.replayed
        assert not session.in_transaction()
    with owner.connect() as connection:
        row = connection.execute(
            text(
                "SELECT state,account_binding_schema_version "
                "FROM order_sync_runs WHERE sync_run_id=:id"
            ),
            {"id": first.run_id},
        ).one()
        assert tuple(row) == ("complete" if complete else "partial", 1)
        current = connection.execute(
            text(
                "SELECT last_seen_sync_run_id FROM marketplace_orders "
                "WHERE organization_id=91001 AND marketplace_account_id=91101 AND external_order_id=:id"
            ),
            {"id": value.observations[0].identity.external_order_id},
        ).scalar_one()
        assert current == (first.run_id if complete else None)


def test_browser_token_cannot_replace_authenticated_user_session(browser_authority):
    _, runtime, _, token = browser_authority
    with Session(runtime) as session, pytest.raises(ValueError, match="contracts"):
        publish(session, None, token, browser_manifest(), uuid4().hex)


@pytest.mark.parametrize("change", ["account", "token_id", "expiry"])
def test_browser_token_metadata_mismatch_is_not_accepted(browser_authority, change):
    owner, runtime, principal, token = browser_authority
    changes = {
        "account": {"marketplace_account_id": 91102},
        "token_id": {"token_id": uuid4()},
        "expiry": {"expires_at": token.expires_at + timedelta(seconds=1)},
    }
    key = uuid4().hex
    with (
        Session(runtime) as session,
        pytest.raises(PublicationGuardError, match="publication_authority_invalid"),
    ):
        publish(
            session,
            principal,
            replace(token, **changes[change]),
            browser_manifest(),
            key,
        )
    with owner.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM order_sync_runs WHERE source_run_key=:key"),
                {"key": key},
            ).scalar_one()
            == 0
        )


def test_browser_revocation_blocks_existing_run_replay(browser_authority):
    owner, runtime, principal, token = browser_authority
    value, key = browser_manifest(), uuid4().hex
    with Session(runtime) as session:
        publish(session, principal, token, value, key)
    with owner.begin() as connection:
        connection.execute(
            text(
                "UPDATE marketplace_account_ingestion_tokens "
                "SET revoked_at=clock_timestamp(),revocation_reason_code='operator_revoked' "
                "WHERE token_id=:id"
            ),
            {"id": token.token_id},
        )
    with (
        Session(runtime) as session,
        pytest.raises(PublicationGuardError, match="publication_authority_invalid"),
    ):
        publish(session, principal, token, value, key)


def test_browser_final_token_revocation_rolls_back_run(browser_authority):
    owner, runtime, principal, token = browser_authority
    key = uuid4().hex
    with Session(runtime) as session:

        def revoke_before_final_check(session):
            session.execute(
                text(
                    "UPDATE marketplace_account_ingestion_tokens "
                    "SET revoked_at=clock_timestamp(),revocation_reason_code='operator_revoked' "
                    "WHERE token_id=:id"
                ),
                {"id": token.token_id},
            )

        event.listen(session, "before_commit", revoke_before_final_check)
        with pytest.raises(
            PublicationGuardError, match="publication_authority_invalid"
        ):
            publish(session, principal, token, browser_manifest(), key)
        assert not session.in_transaction()
    with owner.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM order_sync_runs WHERE source_run_key=:key"),
                {"key": key},
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                text(
                    "SELECT revoked_at FROM marketplace_account_ingestion_tokens "
                    "WHERE token_id=:id"
                ),
                {"id": token.token_id},
            ).scalar_one()
            is None
        )
