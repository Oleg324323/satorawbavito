from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from app.reviews.canonical_contract import ExternalReviewIdentity
from app.reviews.send_recovery import (
    RecoveryAction, RecoveryError, RecoverySnapshot, VerifiedAnswerEvidence,
    evaluate_recovery,
)

NOW = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)


def snapshot(**changes):
    values = dict(identity=ExternalReviewIdentity(1, 11, "wb", "test-review"),
                  command_id=UUID(int=1), attempt_id=UUID(int=2), version=3,
                  text_checksum="a" * 64, state="leased",
                  lease_expires_at=NOW, dispatched_at=None)
    values.update(changes)
    return RecoverySnapshot(**values)


def evidence(command, **changes):
    values = dict(identity=command.identity, command_id=command.command_id,
                  attempt_id=command.attempt_id, observed_at=NOW,
                  answer_checksum="a" * 64, provider_answer_id="test-answer")
    values.update(changes)
    return VerifiedAnswerEvidence(**values)


def test_expired_undispatched_lease_only_proposes_cas_reclaim():
    assert evaluate_recovery(snapshot(), now=NOW) is RecoveryAction.reclaim


def test_live_lease_is_not_stolen_at_duplicate_delivery():
    assert evaluate_recovery(snapshot(lease_expires_at=NOW + timedelta(seconds=1)), now=NOW) is RecoveryAction.wait


@pytest.mark.parametrize("state", ["sent", "conflict", "cancelled"])
def test_terminal_commands_never_reopen(state):
    assert evaluate_recovery(snapshot(state=state), now=NOW) is RecoveryAction.keep_terminal


@pytest.mark.parametrize("state", ["leased", "ambiguous"])
def test_dispatch_without_result_never_reclaims(state):
    command = snapshot(state=state, dispatched_at=NOW - timedelta(seconds=1))
    assert evaluate_recovery(command, now=NOW) is RecoveryAction.ambiguous


@pytest.mark.parametrize("change", [
    dict(answer_checksum=None, provider_answer_id=None),
    dict(answer_checksum="a" * 64, provider_answer_id=None),
    dict(answer_checksum=None, provider_answer_id="test-answer"),
])
def test_empty_or_incomplete_provider_evidence_never_authorizes_retry(change):
    command = snapshot(state="ambiguous", dispatched_at=NOW - timedelta(seconds=2))
    assert evaluate_recovery(command, now=NOW, evidence=evidence(command, **change), reconciliation_started_at=NOW) is RecoveryAction.ambiguous


@pytest.mark.parametrize("provider", ["wb", "avito"])
def test_verified_exact_answer_and_id_can_confirm_sent(provider):
    command = snapshot(identity=ExternalReviewIdentity(1, 11, provider, "test-review"),
                       state="ambiguous", dispatched_at=NOW - timedelta(seconds=2))
    assert evaluate_recovery(command, now=NOW, evidence=evidence(command), reconciliation_started_at=NOW) is RecoveryAction.confirm_sent


def test_other_verified_answer_is_conflict_not_success_or_retry():
    command = snapshot(dispatched_at=NOW - timedelta(seconds=2))
    assert evaluate_recovery(command, now=NOW, evidence=evidence(command, answer_checksum="b" * 64), reconciliation_started_at=NOW) is RecoveryAction.conflict


@pytest.mark.parametrize("change", [
    dict(identity=ExternalReviewIdentity(2, 11, "wb", "test-review")),
    dict(identity=ExternalReviewIdentity(1, 12, "wb", "test-review")),
    dict(identity=ExternalReviewIdentity(1, 11, "avito", "test-review")),
    dict(identity=ExternalReviewIdentity(1, 11, "wb", "test-other")),
    dict(command_id=UUID(int=7)), dict(attempt_id=UUID(int=7)),
    dict(observed_at=NOW - timedelta(seconds=3)),
    dict(observed_at=NOW + timedelta(seconds=1)),
])
def test_wrong_scope_attempt_or_stale_evidence_cannot_resolve_command(change):
    command = snapshot(dispatched_at=NOW - timedelta(seconds=2))
    with pytest.raises(RecoveryError):
        evaluate_recovery(command, now=NOW, evidence=evidence(command, **change), reconciliation_started_at=NOW)


@pytest.mark.parametrize("change", [
    dict(version=True), dict(version=0), dict(state="unknown"), dict(state=[]),
    dict(command_id=UUID(int=0)), dict(text_checksum="private payload"),
    dict(lease_expires_at=NOW.replace(tzinfo=None)),
    dict(state="ambiguous", dispatched_at=None),
])
def test_invalid_snapshot_fails_closed_without_payload_in_error(change):
    with pytest.raises(RecoveryError) as error:
        snapshot(**change)
    assert str(error.value) == "REVIEW_RECOVERY_INVALID"


def test_evidence_without_dispatch_is_inconsistent_not_reclaimable():
    command = snapshot()
    with pytest.raises(RecoveryError):
        evaluate_recovery(command, now=NOW, evidence=evidence(command))


def test_future_dispatch_or_naive_clock_is_rejected():
    with pytest.raises(RecoveryError):
        evaluate_recovery(snapshot(dispatched_at=NOW + timedelta(seconds=1)), now=NOW)
    with pytest.raises(RecoveryError):
        evaluate_recovery(snapshot(), now=NOW.replace(tzinfo=None))


def test_current_version_and_attempt_survive_pure_evaluation_unchanged():
    command = snapshot()
    evaluate_recovery(command, now=NOW)
    assert command.version == 3 and command.attempt_id == UUID(int=2)
    assert replace(command, version=4).version == 4


def test_ambiguous_state_does_not_bypass_live_lease():
    command = snapshot(state="ambiguous", dispatched_at=NOW - timedelta(seconds=2),
                       lease_expires_at=NOW + timedelta(seconds=1))
    assert evaluate_recovery(command, now=NOW, evidence=evidence(command), reconciliation_started_at=NOW) is RecoveryAction.wait


def test_post_dispatch_but_prior_reconciliation_evidence_is_rejected():
    command = snapshot(dispatched_at=NOW - timedelta(days=3))
    old = evidence(command, observed_at=NOW - timedelta(days=1))
    with pytest.raises(RecoveryError):
        evaluate_recovery(command, now=NOW, evidence=old, reconciliation_started_at=NOW)


def test_evidence_requires_explicit_current_read_boundary():
    command = snapshot(dispatched_at=NOW - timedelta(seconds=2))
    with pytest.raises(RecoveryError):
        evaluate_recovery(command, now=NOW, evidence=evidence(command))
