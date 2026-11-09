"""Pure recovery proposals; never a worker, lease owner or permission to resend.

The repository must load the current scoped command and attempt, then CAS state,
version and attempt ID with audit atomically. A reclaim proposal only permits
trying that CAS; approval/account/credential/dispatch gates still apply afterward.
Do not deserialize request bodies into trusted reconciliation evidence.
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import re
from uuid import UUID

from app.reviews.canonical_contract import ExternalReviewIdentity


class RecoveryError(ValueError):
    def __init__(self):
        super().__init__("REVIEW_RECOVERY_INVALID")


class RecoveryAction(str, Enum):
    wait = "wait"
    reclaim = "reclaim"
    ambiguous = "ambiguous"
    confirm_sent = "confirm_sent"
    conflict = "conflict"
    keep_terminal = "keep_terminal"


def _require(condition):
    if not condition:
        raise RecoveryError()


def _time(value):
    _require(isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None)


def _checksum(value):
    _require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None)


def _identity(identity, command_id, attempt_id):
    _require(isinstance(identity, ExternalReviewIdentity))
    for value in (command_id, attempt_id):
        _require(isinstance(value, UUID) and value.int != 0)


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    identity: ExternalReviewIdentity
    command_id: UUID
    attempt_id: UUID
    version: int
    text_checksum: str
    state: str
    lease_expires_at: datetime
    dispatched_at: datetime | None

    def __post_init__(self):
        _identity(self.identity, self.command_id, self.attempt_id)
        _require(type(self.version) is int and self.version > 0)
        _checksum(self.text_checksum)
        _require(isinstance(self.state, str) and self.state in {
            "leased", "ambiguous", "sent", "conflict", "cancelled"})
        _time(self.lease_expires_at)
        if self.dispatched_at is not None:
            _time(self.dispatched_at)
        _require(self.state != "ambiguous" or self.dispatched_at is not None)


@dataclass(frozen=True, slots=True)
class VerifiedAnswerEvidence:
    """Trusted adapter output, NOT raw provider status or a client 'verified' flag.

    The adapter must establish authenticated exact-account/review ownership,
    current non-cached observation and exact UTF-8 answer checksum. Missing or
    unprovable provider identity/content stays incomplete. Matching content proves
    the desired answer exists, not that this worker caused it. No adapter is wired
    here; providers without sufficient evidence must stay ambiguous.
    """
    identity: ExternalReviewIdentity
    command_id: UUID
    attempt_id: UUID
    observed_at: datetime
    answer_checksum: str | None
    provider_answer_id: str | None = field(repr=False)

    def __post_init__(self):
        _identity(self.identity, self.command_id, self.attempt_id)
        _time(self.observed_at)
        if self.answer_checksum is not None:
            _checksum(self.answer_checksum)
        if self.provider_answer_id is not None:
            _require(isinstance(self.provider_answer_id, str)
                     and bool(self.provider_answer_id.strip())
                     and len(self.provider_answer_id) <= 512)


def evaluate_recovery(snapshot: RecoverySnapshot, *, now: datetime,
                      evidence: VerifiedAnswerEvidence | None = None,
                      reconciliation_started_at: datetime | None = None) -> RecoveryAction:
    """Caller records the current read start before fetching reconciliation data.

    Its timestamp must not be reconstructed from old evidence. This boundary
    rejects previous reads; proving a provider response is not cached remains
    the adapter's responsibility. No arbitrary provider freshness TTL is assumed.
    """
    _require(isinstance(snapshot, RecoverySnapshot))
    _time(now)
    if snapshot.dispatched_at is not None:
        _require(snapshot.dispatched_at <= now)
    if snapshot.state in {"sent", "conflict", "cancelled"}:
        return RecoveryAction.keep_terminal
    if evidence is not None:
        _require(isinstance(evidence, VerifiedAnswerEvidence))
        _require(snapshot.dispatched_at is not None)
        _time(reconciliation_started_at)
        _require((evidence.identity, evidence.command_id, evidence.attempt_id)
                 == (snapshot.identity, snapshot.command_id, snapshot.attempt_id))
        _require(snapshot.dispatched_at <= evidence.observed_at <= now)
        _require(reconciliation_started_at <= evidence.observed_at
                 and snapshot.dispatched_at <= reconciliation_started_at <= now)
    if snapshot.lease_expires_at > now:
        return RecoveryAction.wait
    if snapshot.dispatched_at is None:
        return RecoveryAction.reclaim
    if evidence is None or evidence.answer_checksum is None or evidence.provider_answer_id is None:
        return RecoveryAction.ambiguous
    if evidence.answer_checksum == snapshot.text_checksum:
        return RecoveryAction.confirm_sent
    return RecoveryAction.conflict
