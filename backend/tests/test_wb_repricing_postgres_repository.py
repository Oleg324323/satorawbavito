"""Actual dormant repository on fresh 0066 DB/runtime, no provider requests."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.modules.wb_repricing import (
    ApprovalConflictError,
    ApprovalStatus,
    ApprovalValidationError,
)
from app.modules.wb_repricing_dispatch import (
    ApplyOutcome,
    AttemptStatus,
    CanonicalApplyRequest,
)
from app.modules.wb_repricing_postgres import (
    ApprovalPersistenceError,
    ApprovalTransaction,
)
from app.modules.wb_repricing_repository import (
    ApprovalRepositoryScope,
    AuthenticatedApprovalActor,
)
from tests import test_repricer_approvals_rls as schema_fixtures
from tests.test_marketplace_credential_fetch_postgres import wait_blocked

ACTOR = AuthenticatedApprovalActor(7, 77)
cluster = schema_fixtures.cluster
latest_db = schema_fixtures.latest_db


@pytest.mark.parametrize("command", ["mark_dispatch", "record_attempt_outcome"])
def test_unknown_attempt_is_a_conflict_not_an_implicit_reservation(latest_db, command):
    _, runtime = latest_db
    key = scope()
    with Session(runtime) as session, session.begin():
        repo = ApprovalTransaction(session, key)
        repo.create_intent(request(key), ACTOR)
        repo.claim(0, ACTOR)
    with (
        pytest.raises(ApprovalConflictError),
        Session(runtime) as session,
        session.begin(),
    ):
        repo = ApprovalTransaction(session, key)
        if command == "mark_dispatch":
            repo.mark_dispatch(str(uuid4()), 1, 0, ACTOR)
        else:
            repo.record_attempt_outcome(
                str(uuid4()),
                1,
                0,
                ApplyOutcome(AttemptStatus.failed, "WB_APPLY_REJECTED"),
            )


def test_cross_org_and_two_accounts_do_not_share_logical_approval(latest_db):
    _, runtime = latest_db
    first = scope()
    second = ApprovalRepositoryScope(7, 43, first.approval_id)
    foreign = ApprovalRepositoryScope(8, 44, first.approval_id)
    hashes = []
    for key, actor, catalog in [
        (first, ACTOR, 99),
        (second, ACTOR, 99),
        (foreign, AuthenticatedApprovalActor(8, 79), 100),
    ]:
        with Session(runtime) as session, session.begin():
            repo = ApprovalTransaction(session, key)
            assert repo.get() is None
            created = repo.create_intent(
                CanonicalApplyRequest(key, catalog, 123, "synthetic", 129950, 0), actor
            )
            hashes.append(created.action_key)
            assert audit_count(session, key) == 1
    assert len(set(hashes)) == 3


@pytest.mark.parametrize(
    "status", ["applied", "failed", "ambiguous", "rejected", "blocked"]
)
@pytest.mark.parametrize(
    "command", ["claim", "reject", "block", "reserve", "dispatch", "outcome"]
)
def test_terminal_approvals_reject_every_command_without_new_audit(
    latest_db, status, command
):
    _, runtime = latest_db
    key = scope()
    attempt_id = str(uuid4())
    with Session(runtime) as session, session.begin():
        repo = ApprovalTransaction(session, key)
        repo.create_intent(request(key), ACTOR)
        if status in ("rejected", "blocked"):
            terminal = getattr(repo, "reject" if status == "rejected" else "block")(
                0, ACTOR, "SYNTHETIC_REASON"
            )
            expected_count = 2
        else:
            repo.claim(0, ACTOR)
            reserved = repo.reserve_attempt(1, ACTOR)
            attempt_id = reserved.attempt_id
            repo.mark_dispatch(attempt_id, 1, 0, ACTOR)
            result = (
                ApplyOutcome(
                    AttemptStatus.applied,
                    wb_upload_id="synthetic",
                    result_code="ACCEPTED",
                )
                if status == "applied"
                else ApplyOutcome(
                    AttemptStatus(status),
                    "WB_APPLY_REJECTED" if status == "failed" else "WB_APPLY_TIMEOUT",
                )
            )
            terminal = repo.record_attempt_outcome(attempt_id, 1, 1, result).approval
            expected_count = 5
    with (
        pytest.raises(ApprovalConflictError),
        Session(runtime) as session,
        session.begin(),
    ):
        repo = ApprovalTransaction(session, key)
        if command == "claim":
            repo.claim(terminal.version, ACTOR)
        elif command in ("reject", "block"):
            getattr(repo, command)(terminal.version, ACTOR, "SYNTHETIC_REASON")
        elif command == "reserve":
            repo.reserve_attempt(terminal.version, ACTOR)
        elif command == "dispatch":
            repo.mark_dispatch(attempt_id, terminal.version, 2, ACTOR)
        else:
            repo.record_attempt_outcome(
                attempt_id,
                terminal.version,
                2,
                ApplyOutcome(AttemptStatus.failed, "WB_APPLY_REJECTED"),
            )
    with Session(runtime) as session, session.begin():
        assert ApprovalTransaction(session, key).get() == terminal
        assert audit_count(session, key) == expected_count


def scope(account=42):
    return ApprovalRepositoryScope(7, account, uuid4().hex)


def request(key, price=129950):
    return CanonicalApplyRequest(key, 99, 123, "Футболка🚀", price, 0)


def audit_count(session, key):
    return session.execute(
        text(
            "SELECT count(*) FROM wb_repricer_price_approval_audit e JOIN wb_repricer_price_approvals a USING (organization_id,marketplace_account_id,approval_row_id) WHERE a.organization_id=:org AND a.marketplace_account_id=:account AND a.approval_id=:approval"
        ),
        {
            "org": key.organization_id,
            "account": key.marketplace_account_id,
            "approval": key.approval_id,
        },
    ).scalar_one()


def test_committed_create_restart_replay_and_changed_payload_conflict(latest_db):
    _, runtime = latest_db
    key = scope()
    with Session(runtime) as session, session.begin():
        original = ApprovalTransaction(session, key).create_intent(request(key), ACTOR)
    with Session(runtime) as session, session.begin():
        repo = ApprovalTransaction(session, key)
        assert repo.get() == original
        assert repo.create_intent(request(key), ACTOR) == original
        assert audit_count(session, key) == 1
    with (
        pytest.raises(ApprovalConflictError),
        Session(runtime) as session,
        session.begin(),
    ):
        ApprovalTransaction(session, key).create_intent(
            request(key, price=129951), ACTOR
        )


def test_claim_is_persisted_cas_and_closed_commands_do_not_write(latest_db):
    _, runtime = latest_db
    key = scope()
    with Session(runtime) as session, session.begin():
        repo = ApprovalTransaction(session, key)
        repo.create_intent(request(key), ACTOR)
        claimed = repo.claim(0, ACTOR)
        assert claimed.version == 1 and claimed.status is ApprovalStatus.applying
    with (
        pytest.raises(ApprovalConflictError),
        Session(runtime) as session,
        session.begin(),
    ):
        ApprovalTransaction(session, key).claim(0, ACTOR)
    with Session(runtime) as session, session.begin():
        repo = ApprovalTransaction(session, key)
        assert repo.get() == claimed and audit_count(session, key) == 2


def test_two_actual_sessions_claim_one_winner_and_one_fake_effect(latest_db):
    _, runtime = latest_db
    key = scope()
    with Session(runtime) as session, session.begin():
        ApprovalTransaction(session, key).create_intent(request(key), ACTOR)
    barrier = Barrier(2)
    effects = []

    def worker():
        barrier.wait(timeout=10)
        try:
            with Session(runtime) as session, session.begin():
                repo = ApprovalTransaction(session, key)
                repo.claim(0, ACTOR)
                attempt = repo.reserve_attempt(1, ACTOR)
                repo.mark_dispatch(attempt.attempt_id, 1, 0, ACTOR)
            # Synthetic effect only after successful physical root commit.
            effects.append("fake-only")
            return "winner"
        except ApprovalConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: worker(), range(2)))
    assert sorted(results) == ["conflict", "winner"]
    assert effects == ["fake-only"]
    with Session(runtime) as session, session.begin():
        ApprovalTransaction(session, key)
        assert audit_count(session, key) == 4


def test_reserve_replay_marker_replay_and_ambiguous_cannot_resend(latest_db):
    _, runtime = latest_db
    key = scope()
    with Session(runtime) as session, session.begin():
        repo = ApprovalTransaction(session, key)
        repo.create_intent(request(key), ACTOR)
        repo.claim(0, ACTOR)
        reserved = repo.reserve_attempt(1, ACTOR)
        assert repo.reserve_attempt(1, ACTOR) == reserved
        marked = repo.mark_dispatch(reserved.attempt_id, 1, 0, ACTOR)
    with (
        pytest.raises(ApprovalConflictError),
        Session(runtime) as session,
        session.begin(),
    ):
        ApprovalTransaction(session, key).mark_dispatch(marked.attempt_id, 1, 1, ACTOR)
    with Session(runtime) as session, session.begin():
        result = ApprovalTransaction(session, key).record_attempt_outcome(
            marked.attempt_id,
            1,
            1,
            ApplyOutcome(AttemptStatus.ambiguous, "WB_APPLY_TIMEOUT"),
        )
        assert result.approval.status is ApprovalStatus.ambiguous
        assert result.approval.version == result.attempt.version == 2
    with (
        pytest.raises(ApprovalConflictError),
        Session(runtime) as session,
        session.begin(),
    ):
        ApprovalTransaction(session, key).record_attempt_outcome(
            marked.attempt_id,
            2,
            2,
            ApplyOutcome(
                AttemptStatus.applied,
                wb_upload_id="fake-upload",
                result_code="ACCEPTED",
            ),
        )


@pytest.mark.parametrize("decision", ["reject", "block"])
def test_pending_decisions_and_full_transaction_rollback(latest_db, decision):
    _, runtime = latest_db
    key = scope()
    with (
        pytest.raises(RuntimeError, match="injected"),
        Session(runtime) as session,
        session.begin(),
    ):
        repo = ApprovalTransaction(session, key)
        repo.create_intent(request(key), ACTOR)
        getattr(repo, decision)(0, ACTOR, "SYNTHETIC_REASON")
        raise RuntimeError("injected")
    with Session(runtime) as session, session.begin():
        assert ApprovalTransaction(session, key).get() is None


def test_same_org_other_account_cannot_get_or_claim_scope(latest_db):
    _, runtime = latest_db
    key = scope()
    with Session(runtime) as session, session.begin():
        ApprovalTransaction(session, key).create_intent(request(key), ACTOR)
    other = ApprovalRepositoryScope(7, 43, key.approval_id)
    with Session(runtime) as session, session.begin():
        assert ApprovalTransaction(session, other).get() is None
    with (
        pytest.raises(ApprovalConflictError),
        Session(runtime) as session,
        session.begin(),
    ):
        ApprovalTransaction(session, other).claim(0, ACTOR)


def test_database_role_is_not_owner_or_bypass(latest_db):
    _, runtime = latest_db
    with Session(runtime) as session, session.begin():
        assert (
            session.execute(
                text(
                    "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user"
                )
            ).scalar_one()
            is False
        )


@pytest.mark.parametrize("command", ["mark_dispatch", "record_attempt_outcome"])
@pytest.mark.parametrize("attempt_id", [None, "", "not-an-id", str(uuid4())])
def test_required_attempt_identity_never_selects_an_implicit_attempt(
    latest_db, command, attempt_id
):
    _, runtime = latest_db
    key = scope()
    with Session(runtime) as session, session.begin():
        repo = ApprovalTransaction(session, key)
        repo.create_intent(request(key), ACTOR)
        repo.claim(0, ACTOR)
        reserved = repo.reserve_attempt(1, ACTOR)
    error = (
        ApprovalConflictError
        if attempt_id and len(attempt_id) == 36
        else ApprovalValidationError
    )
    with pytest.raises(error), Session(runtime) as session, session.begin():
        repo = ApprovalTransaction(session, key)
        if command == "mark_dispatch":
            repo.mark_dispatch(attempt_id, 1, 0, ACTOR)
        else:
            repo.record_attempt_outcome(
                attempt_id,
                1,
                0,
                ApplyOutcome(AttemptStatus.failed, "WB_APPLY_REJECTED"),
            )
    with Session(runtime) as session, session.begin():
        repo = ApprovalTransaction(session, key)
        assert repo.reserve_attempt(1, ACTOR) == reserved
        assert audit_count(session, key) == 3


@pytest.mark.parametrize(
    "dispatched,outcome,expected,audits",
    [
        (
            True,
            ApplyOutcome(
                AttemptStatus.applied,
                wb_upload_id="synthetic-upload",
                result_code="ACCEPTED",
            ),
            "applied",
            5,
        ),
        (True, ApplyOutcome(AttemptStatus.failed, "WB_APPLY_REJECTED"), "failed", 5),
        (
            False,
            ApplyOutcome(AttemptStatus.failed, "INTERNAL_APPLY_ERROR"),
            "failed",
            4,
        ),
    ],
)
def test_committed_outcome_and_audit_survive_new_session(
    latest_db, dispatched, outcome, expected, audits
):
    _, runtime = latest_db
    key = scope()
    with Session(runtime) as session, session.begin():
        repo = ApprovalTransaction(session, key)
        repo.create_intent(request(key), ACTOR)
        repo.claim(0, ACTOR)
        attempt = repo.reserve_attempt(1, ACTOR)
        if dispatched:
            attempt = repo.mark_dispatch(attempt.attempt_id, 1, 0, ACTOR)
    with Session(runtime) as session, session.begin():
        result = ApprovalTransaction(session, key).record_attempt_outcome(
            attempt.attempt_id, 1, attempt.version, outcome
        )
    with Session(runtime) as session, session.begin():
        loaded = ApprovalTransaction(session, key).get()
        assert loaded == result.approval
        assert loaded.status.value == expected and loaded.version == 2
        assert loaded.wb_upload_id == outcome.wb_upload_id
        assert loaded.safe_error_code == outcome.safe_error_code
        assert audit_count(session, key) == audits


def test_audit_sql_failure_rolls_back_both_outcome_rows_without_raw_error(latest_db):
    _, runtime = latest_db
    key = scope()
    with Session(runtime) as session, session.begin():
        repo = ApprovalTransaction(session, key)
        repo.create_intent(request(key), ACTOR)
        repo.claim(0, ACTOR)
        attempt = repo.reserve_attempt(1, ACTOR)
        repo.mark_dispatch(attempt.attempt_id, 1, 0, ACTOR)

    def fail_audit(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO wb_repricer_price_approval_audit"):
            # Fail the real database statement after both outcome UPDATEs.
            return "SELECT 1/0", {}
        return statement, parameters

    event.listen(runtime, "before_cursor_execute", fail_audit, retval=True)
    try:
        with (
            pytest.raises(ApprovalPersistenceError) as caught,
            Session(runtime) as session,
            session.begin(),
        ):
            ApprovalTransaction(session, key).record_attempt_outcome(
                attempt.attempt_id,
                1,
                1,
                ApplyOutcome(
                    AttemptStatus.applied,
                    wb_upload_id="synthetic-upload",
                    result_code="ACCEPTED",
                ),
            )
        assert str(caught.value) == "approval_persistence_failed"
    finally:
        event.remove(runtime, "before_cursor_execute", fail_audit)
    with Session(runtime) as session, session.begin():
        repo = ApprovalTransaction(session, key)
        assert repo.get().status is ApprovalStatus.applying
        assert audit_count(session, key) == 4
        # Same expected versions still work: neither partial result survived.
        repo.record_attempt_outcome(
            attempt.attempt_id,
            1,
            1,
            ApplyOutcome(AttemptStatus.ambiguous, "WB_APPLY_TIMEOUT"),
        )


def test_dispatch_race_observes_actual_postgres_lock_and_one_fake_effect(latest_db):
    owner, runtime = latest_db
    key = scope()
    with Session(runtime) as session, session.begin():
        repo = ApprovalTransaction(session, key)
        repo.create_intent(request(key), ACTOR)
        repo.claim(0, ACTOR)
        attempt = repo.reserve_attempt(1, ACTOR)
    waiter_started = Event()
    waiter_pid = []
    effects = []

    def competing_dispatch():
        try:
            with Session(runtime) as session, session.begin():
                waiter_pid.append(
                    session.connection().connection.driver_connection.info.backend_pid
                )
                waiter_started.set()
                ApprovalTransaction(session, key).mark_dispatch(
                    attempt.attempt_id, 1, 0, ACTOR
                )
            effects.append("loser-must-not-send")
        except ApprovalConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=1) as pool:
        with Session(runtime) as winner:
            with winner.begin():
                repo = ApprovalTransaction(winner, key)
                blocker_pid = (
                    winner.connection().connection.driver_connection.info.backend_pid
                )
                repo.mark_dispatch(attempt.attempt_id, 1, 0, ACTOR)
                future = pool.submit(competing_dispatch)
                assert waiter_started.wait(5)
                with owner.connect() as observer:
                    wait_blocked(observer, waiter_pid[0], blocker_pid)
            effects.append("winner-fake-only")
        assert future.result(timeout=10) == "conflict"
    assert effects == ["winner-fake-only"]


@pytest.mark.parametrize(
    "command", ["claim", "reject", "block", "reserve", "dispatch", "outcome"]
)
def test_every_stale_command_preserves_durable_rows_and_audit(latest_db, command):
    _, runtime = latest_db
    key = scope()
    with Session(runtime) as session, session.begin():
        repo = ApprovalTransaction(session, key)
        expected = repo.create_intent(request(key), ACTOR)
        count = 1
        if command in ("reserve", "dispatch", "outcome"):
            expected = repo.claim(0, ACTOR)
            attempt = repo.reserve_attempt(1, ACTOR)
            count = 3
    with (
        pytest.raises(ApprovalConflictError),
        Session(runtime) as session,
        session.begin(),
    ):
        repo = ApprovalTransaction(session, key)
        if command == "claim":
            repo.claim(99, ACTOR)
        elif command in ("reject", "block"):
            getattr(repo, command)(99, ACTOR, "SYNTHETIC_REASON")
        elif command == "reserve":
            repo.reserve_attempt(99, ACTOR)
        elif command == "dispatch":
            repo.mark_dispatch(attempt.attempt_id, 99, 0, ACTOR)
        else:
            repo.record_attempt_outcome(
                attempt.attempt_id,
                99,
                0,
                ApplyOutcome(AttemptStatus.failed, "WB_APPLY_REJECTED"),
            )
    with Session(runtime) as session, session.begin():
        assert ApprovalTransaction(session, key).get() == expected
        assert audit_count(session, key) == count
