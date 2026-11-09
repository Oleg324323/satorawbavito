"""Pure dispatch amendment. Values are not committed database send permissions.

The repository must implement the documented CAS/transaction predicates. This
module owns no clock, random ID generator, provider, persistence or recovery job.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum

from app.modules.wb_repricing import (
    SAFE_APPLY_ERROR_CODES,
    ApprovalConflictError,
    ApprovalStatus,
    ApprovalValidationError,
    PriceApprovalSnapshot,
    _require_postgres_text,
    build_action_key,
    record_apply_failure,
    record_apply_success,
)
from app.modules.wb_repricing_repository import (
    ApprovalRepositoryScope,
    AuthenticatedApprovalActor,
    bridge_actor_membership_id,
    bridge_snapshot_identity,
)


def _integer(value: object, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise ApprovalValidationError("integer outside allowed range")


def _text(value: object) -> None:
    if type(value) is not str or not value or value.strip() != value:
        raise ApprovalValidationError("exact nonblank text required")
    _require_postgres_text(value)


def _hash(value: object) -> None:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ApprovalValidationError("lowercase SHA-256 required")


def _code(value: object) -> None:
    if type(value) is not str or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", value) is None:
        raise ApprovalValidationError("safe machine code required")


def _time(value: datetime, previous: datetime | None = None) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ApprovalValidationError("aware datetime required")
    if previous is not None and value < previous:
        raise ApprovalValidationError("time cannot go backwards")


def _uuid4(value: str) -> None:
    # Validate canonical spelling before a parser can echo untrusted input.
    if type(value) is not str or re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", value
    ) is None:
        raise ApprovalValidationError("canonical UUID4 required")


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


@dataclass(frozen=True, slots=True)
class CanonicalApplyRequest:
    scope: ApprovalRepositoryScope
    catalog_sku_id: int | None
    nm_id: int
    article_id: str
    price_kopecks: int
    discount_pct: int
    size_id: int | None = None
    min_price_kopecks: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, ApprovalRepositoryScope):
            raise ApprovalValidationError("approval scope required")
        _integer(self.nm_id, 1)
        _integer(self.price_kopecks, 50)
        _integer(self.discount_pct)
        if self.discount_pct > 99:
            raise ApprovalValidationError("discount outside 0..99")
        for value in (self.catalog_sku_id, self.size_id):
            if value is not None:
                _integer(value, 1)
        if self.min_price_kopecks is not None:
            _integer(self.min_price_kopecks, 50)
        _text(self.article_id)
        if any(ord(char) < 32 or ord(char) == 127 for char in self.article_id):
            raise ApprovalValidationError("article contains control characters")
        if len(self.canonical_bytes) > 4096:
            raise ApprovalValidationError("canonical request exceeds 4096 bytes")

    @property
    def canonical_bytes(self) -> bytes:
        return _json({
            "schema": "wb-price-apply/v1",
            "organizationId": self.scope.organization_id,
            "accountId": self.scope.marketplace_account_id,
            "approvalId": self.scope.approval_id,
            "catalogSkuId": self.catalog_sku_id,
            "nmId": self.nm_id, "articleId": self.article_id,
            "priceKopecks": self.price_kopecks, "discountPct": self.discount_pct,
            "sizeId": self.size_id, "minPriceKopecks": self.min_price_kopecks,
        })

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()

    @property
    def provider_bytes(self) -> bytes:
        # Positive integer HALF_UP parity with existing WB upload unit adapter.
        row = {"nmID": self.nm_id, "price": (self.price_kopecks + 50) // 100,
               "discount": self.discount_pct}
        if self.size_id is not None:
            row["sizeID"] = self.size_id
        if self.min_price_kopecks is not None:
            row["minPrice"] = (self.min_price_kopecks + 50) // 100
        return _json({"data": [row]})


def bind_request(snapshot: PriceApprovalSnapshot, request: CanonicalApplyRequest) -> PriceApprovalSnapshot:
    if not isinstance(request, CanonicalApplyRequest):
        raise ApprovalValidationError("canonical request required")
    bridge_snapshot_identity(scope=request.scope, catalog_sku_id=request.catalog_sku_id,
                             snapshot=snapshot)
    if (snapshot.nm_id, snapshot.article_id, snapshot.recommended_price_kopecks,
        snapshot.request_checksum) != (request.nm_id, request.article_id,
                                       request.price_kopecks, request.checksum):
        raise ApprovalValidationError("request does not match immutable approval")
    return snapshot


class AttemptStatus(str, Enum):
    reserved = "reserved"
    dispatched = "dispatched"
    applied = "applied"
    failed = "failed"
    ambiguous = "ambiguous"


def build_dispatch_key(scope: ApprovalRepositoryScope, action_key: str, attempt_id: str) -> str:
    if not isinstance(scope, ApprovalRepositoryScope):
        raise ApprovalValidationError("approval scope required")
    _hash(action_key)
    _uuid4(attempt_id)
    return hashlib.sha256(_json([
        "wb-price-dispatch/v1", scope.organization_id, scope.marketplace_account_id,
        scope.approval_id, action_key, attempt_id,
    ])).hexdigest()


@dataclass(frozen=True, slots=True)
class ApplyOutcome:
    status: AttemptStatus
    safe_error_code: str | None = None
    wb_upload_id: str | None = None
    result_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, AttemptStatus):
            raise ApprovalValidationError("attempt status enum required")
        if self.status is AttemptStatus.applied:
            _text(self.wb_upload_id)
            _code(self.result_code)
            if self.safe_error_code is not None:
                raise ApprovalValidationError("success cannot contain error")
        elif self.status in (AttemptStatus.failed, AttemptStatus.ambiguous):
            if type(self.safe_error_code) is not str or self.safe_error_code not in SAFE_APPLY_ERROR_CODES:
                raise ApprovalValidationError("new failure requires allowlisted code")
            if self.wb_upload_id is not None or self.result_code is not None:
                raise ApprovalValidationError("failure cannot contain success metadata")
        else:
            raise ApprovalValidationError("terminal attempt outcome required")


@dataclass(frozen=True, slots=True)
class ApplyAttempt:
    scope: ApprovalRepositoryScope
    attempt_id: str
    action_key: str
    request_checksum: str
    claim_version: int
    claimed_by_membership_id: int
    status: AttemptStatus
    version: int
    reserved_at: datetime
    updated_at: datetime
    dispatch_at: datetime | None = None
    finished_at: datetime | None = None
    outcome: ApplyOutcome | None = None

    @property
    def dispatch_key(self) -> str:
        return build_dispatch_key(self.scope, self.action_key, self.attempt_id)

    def __post_init__(self) -> None:
        build_dispatch_key(self.scope, self.action_key, self.attempt_id)
        if not isinstance(self.status, AttemptStatus):
            raise ApprovalValidationError("attempt status enum required")
        _hash(self.request_checksum)
        if self.action_key != build_action_key(self.scope.organization_id,
                                               str(self.scope.marketplace_account_id),
                                               self.scope.approval_id, self.request_checksum):
            raise ApprovalValidationError("attempt action key binding mismatch")
        _integer(self.claim_version, 1)
        _integer(self.claimed_by_membership_id, 1)
        _integer(self.version)
        _time(self.reserved_at)
        _time(self.updated_at, self.reserved_at)
        if self.dispatch_at is not None:
            _time(self.dispatch_at, self.reserved_at)
            _time(self.updated_at, self.dispatch_at)
        if self.status is AttemptStatus.reserved:
            valid = (self.version == 0 and self.dispatch_at is None and self.finished_at is None
                     and self.outcome is None and self.updated_at == self.reserved_at)
        elif self.status is AttemptStatus.dispatched:
            valid = (self.version == 1 and self.dispatch_at is not None and self.finished_at is None
                     and self.outcome is None and self.updated_at == self.dispatch_at)
        elif self.status in (AttemptStatus.applied, AttemptStatus.failed, AttemptStatus.ambiguous):
            valid = (isinstance(self.outcome, ApplyOutcome) and self.outcome.status is self.status
                     and self.finished_at is not None
                     and self.version == (2 if self.dispatch_at is not None else 1)
                     and (self.dispatch_at is not None or self.status is AttemptStatus.failed))
            if valid:
                _time(self.finished_at, self.dispatch_at or self.reserved_at)
                valid = self.updated_at == self.finished_at
                if (self.status is AttemptStatus.failed and self.dispatch_at is not None
                        and self.outcome.safe_error_code != "WB_APPLY_REJECTED"):
                    valid = False
        else:
            valid = False
        if not valid:
            raise ApprovalValidationError("attempt state metadata inconsistent")


def _applying(snapshot: PriceApprovalSnapshot, version: int, now: datetime) -> None:
    _integer(version)
    if not isinstance(snapshot, PriceApprovalSnapshot):
        raise ApprovalValidationError("approval snapshot required")
    if snapshot.status is not ApprovalStatus.applying or snapshot.version != version:
        raise ApprovalConflictError("approval not applying at expected version")
    _time(now, snapshot.updated_at)


def reserve_attempt(snapshot: PriceApprovalSnapshot, request: CanonicalApplyRequest,
                    expected_version: int, actor: AuthenticatedApprovalActor,
                    attempt_id: str, now: datetime) -> ApplyAttempt:
    """Construct reservation using repository-generated UUID4; no uniqueness proof."""
    _applying(snapshot, expected_version, now)
    bind_request(snapshot, request)
    if bridge_actor_membership_id(request.scope, actor) != snapshot.claimed_by_membership_id:
        raise ApprovalValidationError("reservation actor differs from claim")
    return ApplyAttempt(request.scope, attempt_id, snapshot.action_key,
                        snapshot.request_checksum, snapshot.version, actor.membership_id,
                        AttemptStatus.reserved, 0, now, now)


def _attempt(snapshot: PriceApprovalSnapshot, attempt: ApplyAttempt,
             expected_version: int, expected_attempt_version: int, now: datetime) -> None:
    _applying(snapshot, expected_version, now)
    _integer(expected_attempt_version)
    if not isinstance(attempt, ApplyAttempt):
        raise ApprovalValidationError("attempt required")
    if attempt.version != expected_attempt_version or attempt.status not in (AttemptStatus.reserved, AttemptStatus.dispatched):
        raise ApprovalConflictError("attempt closed or version stale")
    if (snapshot.organization_id, snapshot.marketplace_account_id, snapshot.approval_id,
        snapshot.action_key, snapshot.request_checksum, snapshot.version,
        snapshot.claimed_by_membership_id) != (
        attempt.scope.organization_id, str(attempt.scope.marketplace_account_id),
        attempt.scope.approval_id, attempt.action_key, attempt.request_checksum,
        attempt.claim_version, str(attempt.claimed_by_membership_id),
    ):
        raise ApprovalValidationError("attempt binding mismatch")
    _time(now, attempt.updated_at)


def mark_dispatched(snapshot: PriceApprovalSnapshot, attempt: ApplyAttempt,
                    expected_version: int, expected_attempt_version: int,
                    actor: AuthenticatedApprovalActor, now: datetime) -> ApplyAttempt:
    _attempt(snapshot, attempt, expected_version, expected_attempt_version, now)
    if attempt.status is not AttemptStatus.reserved:
        raise ApprovalConflictError("dispatch marker already exists")
    if bridge_actor_membership_id(attempt.scope, actor) != snapshot.claimed_by_membership_id:
        raise ApprovalValidationError("dispatch actor differs from claim")
    return replace(attempt, status=AttemptStatus.dispatched, version=1,
                   dispatch_at=now, updated_at=now)


@dataclass(frozen=True, slots=True)
class AttemptResult:
    approval: PriceApprovalSnapshot
    attempt: ApplyAttempt


def finish_attempt(snapshot: PriceApprovalSnapshot, attempt: ApplyAttempt,
                   expected_version: int, expected_attempt_version: int,
                   outcome: ApplyOutcome, now: datetime) -> AttemptResult:
    _attempt(snapshot, attempt, expected_version, expected_attempt_version, now)
    if not isinstance(outcome, ApplyOutcome):
        raise ApprovalValidationError("safe outcome required")
    if attempt.dispatch_at is None and outcome.status is not AttemptStatus.failed:
        raise ApprovalValidationError("only known local failure before dispatch")
    if (attempt.dispatch_at is not None and outcome.status is AttemptStatus.failed
            and outcome.safe_error_code != "WB_APPLY_REJECTED"):
        raise ApprovalValidationError("post-dispatch uncertainty must be ambiguous")
    if outcome.status is AttemptStatus.applied:
        after = record_apply_success(snapshot, expected_version, outcome.wb_upload_id,
                                     outcome.result_code, now)
    else:
        after = record_apply_failure(snapshot, expected_version, outcome.safe_error_code,
                                     outcome.status is AttemptStatus.ambiguous, now)
    return AttemptResult(after, replace(attempt, status=outcome.status,
                                        version=attempt.version + 1, updated_at=now,
                                        finished_at=now, outcome=outcome))


class AuditKind(str, Enum):
    created = "approval.created"
    imported = "approval.imported"
    claimed = "approval.claimed"
    rejected = "approval.rejected"
    blocked = "approval.blocked"
    reserved = "attempt.reserved"
    dispatched = "attempt.dispatched"
    applied = "apply.succeeded"
    failed = "apply.failed"
    ambiguous = "apply.ambiguous"


class AuditActorKind(str, Enum):
    membership = "membership"
    worker = "repricer_worker"
    backfill = "backfill"


@dataclass(frozen=True, slots=True)
class ApprovalAuditEvent:
    scope: ApprovalRepositoryScope
    kind: AuditKind
    actor_kind: AuditActorKind
    actor_membership_id: int | None
    before_status: ApprovalStatus | None
    after_status: ApprovalStatus
    before_version: int | None
    after_version: int
    occurred_at: datetime
    attempt_id: str | None = None
    before_attempt_version: int | None = None
    after_attempt_version: int | None = None
    reason_code: str | None = None
    safe_error_code: str | None = None
    wb_upload_id: str | None = None
    result_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, ApprovalRepositoryScope) or not isinstance(self.kind, AuditKind):
            raise ApprovalValidationError("scoped audit event required")
        if not isinstance(self.after_status, ApprovalStatus):
            raise ApprovalValidationError("audit status required")
        _time(self.occurred_at)
        _integer(self.after_version)
        member_events = (AuditKind.created, AuditKind.claimed, AuditKind.rejected,
                         AuditKind.blocked, AuditKind.reserved, AuditKind.dispatched)
        required_actor = (AuditActorKind.membership if self.kind in member_events else
                          AuditActorKind.backfill if self.kind is AuditKind.imported else AuditActorKind.worker)
        if self.actor_kind is not required_actor:
            raise ApprovalValidationError("wrong actor class for audit event")
        if required_actor is AuditActorKind.membership:
            _integer(self.actor_membership_id, 1)
        elif self.actor_membership_id is not None:
            raise ApprovalValidationError("worker/backfill actor membership must be null")
        targets = {AuditKind.created: ApprovalStatus.pending, AuditKind.claimed: ApprovalStatus.applying,
                   AuditKind.rejected: ApprovalStatus.rejected, AuditKind.blocked: ApprovalStatus.blocked,
                   AuditKind.reserved: ApprovalStatus.applying, AuditKind.dispatched: ApprovalStatus.applying,
                   AuditKind.applied: ApprovalStatus.applied, AuditKind.failed: ApprovalStatus.failed,
                   AuditKind.ambiguous: ApprovalStatus.ambiguous}
        if self.kind is not AuditKind.imported and self.after_status is not targets[self.kind]:
            raise ApprovalValidationError("audit target status mismatch")
        if self.kind in (AuditKind.created, AuditKind.imported):
            if self.before_status is not None or self.before_version is not None:
                raise ApprovalValidationError("creation has no previous state")
            if self.kind is AuditKind.created and self.after_version != 0:
                raise ApprovalValidationError("new approval starts at version zero")
        else:
            _integer(self.before_version)
            before = (ApprovalStatus.pending if self.kind in (AuditKind.claimed, AuditKind.rejected, AuditKind.blocked)
                      else ApprovalStatus.applying)
            delta = 0 if self.kind in (AuditKind.reserved, AuditKind.dispatched) else 1
            if self.before_status is not before or self.after_version != self.before_version + delta:
                raise ApprovalValidationError("audit transition mismatch")
        attempt_events = (AuditKind.reserved, AuditKind.dispatched, AuditKind.applied, AuditKind.failed, AuditKind.ambiguous)
        if self.kind in attempt_events:
            _uuid4(self.attempt_id)
            pair = (self.before_attempt_version, self.after_attempt_version)
            allowed = ((None, 0),) if self.kind is AuditKind.reserved else ((0, 1),) if self.kind is AuditKind.dispatched else ((0, 1), (1, 2)) if self.kind is AuditKind.failed else ((1, 2),)
            for value in pair:
                if value is not None:
                    _integer(value)
            if pair not in allowed:
                raise ApprovalValidationError("audit attempt version mismatch")
        elif any(value is not None for value in (self.attempt_id, self.before_attempt_version, self.after_attempt_version)):
            raise ApprovalValidationError("event cannot reference attempt")
        if self.kind in (AuditKind.rejected, AuditKind.blocked):
            _code(self.reason_code)
        elif self.reason_code is not None:
            raise ApprovalValidationError("unexpected audit reason")
        if self.kind in (AuditKind.failed, AuditKind.ambiguous, AuditKind.applied):
            ApplyOutcome(AttemptStatus(self.after_status.value), self.safe_error_code,
                         self.wb_upload_id, self.result_code)
            if (self.kind is AuditKind.failed and self.before_attempt_version == 1
                    and self.safe_error_code != "WB_APPLY_REJECTED"):
                raise ApprovalValidationError("post-dispatch failure needs known rejection")
        elif any(value is not None for value in (self.safe_error_code, self.wb_upload_id, self.result_code)):
            raise ApprovalValidationError("unexpected audit outcome metadata")
