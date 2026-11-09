"""Committed user commands with real live authority; never provider dispatch."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.modules.wb_repricing import ApprovalConflictError, ApprovalStatus
from app.modules.wb_repricing_commands import UserApprovalCommands
from app.modules.wb_repricing_postgres import (
    ApprovalPersistenceError,
    ApprovalTransaction,
)
from app.platform.integrations.publication_guard import (
    ExpectedAccountBinding,
    PublicationGuardError,
    UserSessionPrincipal,
)
from tests import test_wb_repricing_postgres_repository as repository_tests

cluster = repository_tests.cluster
latest_db = repository_tests.latest_db


@pytest.fixture
def principal(latest_db):
    owner, _ = latest_db
    login = uuid4().hex
    with owner.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO lk_sessions(session_id,user_id,issued_at,last_seen_at,expires_at) VALUES(:id,'repricer77',clock_timestamp(),clock_timestamp(),clock_timestamp()+interval '1 hour')"
            ),
            {"id": login},
        )
    return UserSessionPrincipal(7, "repricer77", 77, login)


def binding():
    return ExpectedAccountBinding(42, "wb", "synthetic-42", None)


def test_create_and_claim_return_only_physically_committed_values(latest_db, principal):
    _, runtime = latest_db
    key = repository_tests.scope()
    commands = UserApprovalCommands(runtime)
    original = commands.create_intent(
        repository_tests.request(key), principal, binding()
    )
    claimed = commands.claim(key, 0, principal, binding())
    assert original.version == 0 and claimed.version == 1
    assert claimed.status is ApprovalStatus.applying
    assert claimed.claimed_by_membership_id == "77"
    with Session(runtime) as session, session.begin():
        assert ApprovalTransaction(session, key).get() == claimed
        assert repository_tests.audit_count(session, key) == 2
    with pytest.raises(ApprovalConflictError):
        commands.claim(key, 0, principal, binding())


@pytest.mark.parametrize(
    "invalid",
    [
        "revoked",
        "expired",
        "wrong_member",
        "wrong_user",
        "wrong_org",
        "wrong_account",
        "wrong_binding",
    ],
)
def test_denied_authority_leaves_no_intent_or_audit(latest_db, principal, invalid):
    owner, runtime = latest_db
    key = repository_tests.scope()
    expected = binding()
    if invalid in ("revoked", "expired"):
        column = "revoked_at" if invalid == "revoked" else "expires_at"
        with owner.begin() as connection:
            connection.execute(
                text(
                    f"UPDATE lk_sessions SET {column}=clock_timestamp()-interval '1 second' WHERE session_id=:id"
                ),
                {"id": principal.session_id},
            )
    elif invalid == "wrong_member":
        principal = replace(principal, membership_id=78)
    elif invalid == "wrong_user":
        principal = replace(principal, user_id="repricer78")
    elif invalid == "wrong_org":
        principal = replace(principal, organization_id=8)
    elif invalid == "wrong_account":
        expected = replace(expected, marketplace_account_id=43)
    else:
        expected = replace(expected, external_account_id="different-synthetic")
    with pytest.raises(PublicationGuardError):
        UserApprovalCommands(runtime).create_intent(
            repository_tests.request(key), principal, expected
        )
    with Session(runtime) as session, session.begin():
        assert ApprovalTransaction(session, key).get() is None
        assert repository_tests.audit_count(session, key) == 0


@pytest.mark.parametrize(
    "decision,status",
    [("reject", ApprovalStatus.rejected), ("block", ApprovalStatus.blocked)],
)
def test_decision_preserves_authenticated_membership(
    latest_db, principal, decision, status
):
    _, runtime = latest_db
    key = repository_tests.scope()
    commands = UserApprovalCommands(runtime)
    commands.create_intent(repository_tests.request(key), principal, binding())
    decided = getattr(commands, decision)(
        key, 0, principal, binding(), "SYNTHETIC_REASON"
    )
    assert decided.status is status and decided.decided_by_membership_id == "77"
    with Session(runtime) as session, session.begin():
        assert ApprovalTransaction(session, key).get() == decided


@pytest.mark.parametrize("change", ["permission", "scope", "inactive"])
def test_live_membership_denies_claim_and_preserves_pending(
    latest_db, principal, change
):
    owner, runtime = latest_db
    key = repository_tests.scope()
    commands = UserApprovalCommands(runtime)
    original = commands.create_intent(
        repository_tests.request(key), principal, binding()
    )
    changes = {
        "permission": "role='custom',permissions='[\"cabinet:read\"]'::jsonb",
        "scope": "scope_mode='selected',allowed_account_ids='[43]'::jsonb",
        "inactive": "is_active=false",
    }
    try:
        with owner.begin() as connection:
            connection.execute(
                text(
                    "UPDATE iam_memberships SET "
                    + changes[change]
                    + " WHERE membership_id=77"
                )
            )
        with pytest.raises(PublicationGuardError):
            commands.claim(key, 0, principal, binding())
    finally:
        with owner.begin() as connection:
            connection.execute(
                text(
                    "UPDATE iam_memberships SET role='admin',permissions='[]'::jsonb,scope_mode='all',allowed_account_ids='[]'::jsonb,is_active=true WHERE membership_id=77"
                )
            )
    with Session(runtime) as session, session.begin():
        assert ApprovalTransaction(session, key).get() == original
        assert repository_tests.audit_count(session, key) == 1


def test_deferred_physical_commit_failure_is_safe_and_returns_no_snapshot(
    latest_db, principal
):
    _, runtime = latest_db
    key = repository_tests.scope()
    insert_returned = []

    def wrong_witness(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO wb_repricer_price_approval_audit"):
            return statement, {**parameters, "audit_event_id": uuid4()}
        return statement, parameters

    def observe_insert(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO wb_repricer_price_approval_audit"):
            insert_returned.append(True)

    event.listen(runtime, "before_cursor_execute", wrong_witness, retval=True)
    event.listen(runtime, "after_cursor_execute", observe_insert)
    try:
        with pytest.raises(ApprovalPersistenceError) as caught:
            UserApprovalCommands(runtime).create_intent(
                repository_tests.request(key), principal, binding()
            )
        assert str(caught.value) == "approval_persistence_failed"
        assert insert_returned == [True]  # SQL succeeded; physical commit did not.
    finally:
        event.remove(runtime, "before_cursor_execute", wrong_witness)
        event.remove(runtime, "after_cursor_execute", observe_insert)
    with Session(runtime) as session, session.begin():
        assert ApprovalTransaction(session, key).get() is None
        assert repository_tests.audit_count(session, key) == 0


def test_authenticated_concurrent_claim_has_one_committed_winner(latest_db, principal):
    _, runtime = latest_db
    key = repository_tests.scope()
    commands = UserApprovalCommands(runtime)
    commands.create_intent(repository_tests.request(key), principal, binding())
    start = Barrier(2)

    def claimant():
        start.wait(timeout=10)
        try:
            return commands.claim(key, 0, principal, binding()).status.value
        except ApprovalConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: claimant(), range(2))) == [
            "applying",
            "conflict",
        ]
    with Session(runtime) as session, session.begin():
        assert ApprovalTransaction(session, key).get().version == 1
        assert repository_tests.audit_count(session, key) == 2
