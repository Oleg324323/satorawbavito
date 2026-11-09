"""Pure lifecycle contract for durable WB price approvals.

The module owns no clock, repository, framework, or marketplace adapter.  A
caller supplies the current immutable snapshot, its expected version, actor
identity, and transition time; a successful command returns a new snapshot.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum


class ApprovalStatus(str, Enum):
    pending = "pending"
    applying = "applying"
    applied = "applied"
    rejected = "rejected"
    blocked = "blocked"
    failed = "failed"
    ambiguous = "ambiguous"


class ApprovalConflictError(Exception):
    """The command lost optimistic concurrency or targets a closed state."""


class ApprovalValidationError(ValueError):
    """Approval identity, money, time, or result metadata is invalid."""


SAFE_APPLY_ERROR_CODES = frozenset(
    {
        "INTERNAL_APPLY_ERROR",
        "WB_APPLY_AUTHORIZATION_FAILED",
        "WB_APPLY_RATE_LIMITED",
        "WB_APPLY_REJECTED",
        "WB_APPLY_TIMEOUT",
        "WB_APPLY_TRANSPORT_ERROR",
        "WB_APPLY_VALIDATION_FAILED",
        "WB_RESULT_UNAVAILABLE",
    }
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SAFE_CODE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,127}")


def _require_positive_integer(value: object, field_name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ApprovalValidationError(f"{field_name} must be a positive integer")
    return value


def _require_nonblank(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ApprovalValidationError(f"{field_name} must be non-blank")
    if value != value.strip():
        raise ApprovalValidationError(f"{field_name} must not have surrounding whitespace")
    _require_postgres_text(value)
    return value


def _require_postgres_text(value: str) -> None:
    """Reject non-scalar/NUL text; never normalize identity or reflect input."""
    if any(char == "\x00" or 0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ApprovalValidationError("text is not representable in PostgreSQL UTF-8")


def _require_sha256(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ApprovalValidationError(f"{field_name} must be lowercase SHA-256 hex")
    return value


def _require_safe_code(value: object, field_name: str) -> str:
    code = _require_nonblank(value, field_name)
    if _SAFE_CODE_RE.fullmatch(code) is None:
        raise ApprovalValidationError(f"{field_name} must be a safe machine code")
    return code


def _require_aware_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ApprovalValidationError(f"{field_name} must be timezone-aware")
    return value


def build_action_key(
    organization_id: int,
    marketplace_account_id: str,
    approval_id: str,
    request_checksum: str,
) -> str:
    """Build the stable SHA-256 of a canonical JSON identity tuple."""

    organization = _require_positive_integer(organization_id, "organization_id")
    account = _require_nonblank(marketplace_account_id, "marketplace_account_id")
    approval = _require_nonblank(approval_id, "approval_id")
    checksum = _require_sha256(request_checksum, "request_checksum")
    canonical_identity = json.dumps(
        [organization, account, approval, checksum],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical_identity.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PriceApprovalSnapshot:
    organization_id: int
    marketplace_account_id: str
    approval_id: str
    catalog_sku_id: str | None
    nm_id: int
    article_id: str
    recommended_price_kopecks: int
    request_checksum: str
    status: ApprovalStatus
    version: int
    action_key: str
    created_at: datetime
    updated_at: datetime
    claimed_by_membership_id: str | None = None
    decided_by_membership_id: str | None = None
    reason_code: str | None = None
    safe_error_code: str | None = None
    wb_upload_id: str | None = None
    result_code: str | None = None

    def __post_init__(self) -> None:
        _require_positive_integer(self.organization_id, "organization_id")
        _require_nonblank(self.marketplace_account_id, "marketplace_account_id")
        _require_nonblank(self.approval_id, "approval_id")
        if self.catalog_sku_id is not None:
            _require_nonblank(self.catalog_sku_id, "catalog_sku_id")
        _require_positive_integer(self.nm_id, "nm_id")
        _require_nonblank(self.article_id, "article_id")
        _require_positive_integer(
            self.recommended_price_kopecks,
            "recommended_price_kopecks",
        )
        _require_sha256(self.request_checksum, "request_checksum")
        if not isinstance(self.status, ApprovalStatus):
            raise ApprovalValidationError("status must be an ApprovalStatus")
        if type(self.version) is not int or self.version < 0:
            raise ApprovalValidationError("version must be a non-negative integer")
        _require_sha256(self.action_key, "action_key")
        created_at = _require_aware_datetime(self.created_at, "created_at")
        updated_at = _require_aware_datetime(self.updated_at, "updated_at")
        if updated_at < created_at:
            raise ApprovalValidationError("updated_at cannot precede created_at")
        expected_action_key = build_action_key(
            self.organization_id,
            self.marketplace_account_id,
            self.approval_id,
            self.request_checksum,
        )
        if self.action_key != expected_action_key:
            raise ApprovalValidationError("action_key does not match approval ownership")

        for field_name in (
            "claimed_by_membership_id",
            "decided_by_membership_id",
            "reason_code",
            "safe_error_code",
            "wb_upload_id",
            "result_code",
        ):
            value = getattr(self, field_name)
            if value is not None:
                _require_nonblank(value, field_name)
        if self.reason_code is not None:
            _require_safe_code(self.reason_code, "reason_code")
        if self.safe_error_code is not None:
            _require_safe_code(self.safe_error_code, "safe_error_code")
        if self.result_code is not None:
            _require_safe_code(self.result_code, "result_code")

        self._validate_state_metadata()

    def _validate_state_metadata(self) -> None:
        has_claim = self.claimed_by_membership_id is not None
        has_decision = self.decided_by_membership_id is not None
        has_reason = self.reason_code is not None
        has_error = self.safe_error_code is not None
        has_upload = self.wb_upload_id is not None
        has_result = self.result_code is not None

        if self.status is ApprovalStatus.pending:
            if any((has_claim, has_decision, has_reason, has_error, has_upload, has_result)):
                raise ApprovalValidationError("pending approval cannot contain transition metadata")
            return
        if self.status is ApprovalStatus.applying:
            if not has_claim or any((has_decision, has_reason, has_error, has_upload, has_result)):
                raise ApprovalValidationError("applying approval has inconsistent metadata")
            return
        if self.status in {ApprovalStatus.rejected, ApprovalStatus.blocked}:
            if not has_decision or not has_reason or any(
                (has_claim, has_error, has_upload, has_result)
            ):
                raise ApprovalValidationError("decision approval has inconsistent metadata")
            return
        if self.status is ApprovalStatus.applied:
            if not has_claim or not has_upload or any((has_decision, has_reason, has_error)):
                raise ApprovalValidationError("applied approval has inconsistent metadata")
            return
        if self.status in {ApprovalStatus.failed, ApprovalStatus.ambiguous} and (
            not has_claim or not has_error or any(
                (has_decision, has_reason, has_upload, has_result)
            )
        ):
            raise ApprovalValidationError("failed approval has inconsistent metadata")


def _prepare_transition(
    snapshot: PriceApprovalSnapshot,
    expected_version: int,
    required_status: ApprovalStatus,
    now: datetime,
) -> datetime:
    if type(expected_version) is not int or expected_version < 0:
        raise ApprovalValidationError("expected_version must be a non-negative integer")
    if snapshot.version != expected_version:
        raise ApprovalConflictError("approval version is stale")
    if snapshot.status is not required_status:
        raise ApprovalConflictError(
            f"approval cannot transition from {snapshot.status.value}"
        )
    transition_at = _require_aware_datetime(now, "now")
    if transition_at < snapshot.updated_at:
        raise ApprovalValidationError("transition time cannot precede updated_at")
    return transition_at


def claim_approval(
    snapshot: PriceApprovalSnapshot,
    expected_version: int,
    actor_membership_id: str,
    now: datetime,
) -> PriceApprovalSnapshot:
    transition_at = _prepare_transition(
        snapshot,
        expected_version,
        ApprovalStatus.pending,
        now,
    )
    actor_id = _require_nonblank(actor_membership_id, "actor_membership_id")
    return replace(
        snapshot,
        status=ApprovalStatus.applying,
        version=snapshot.version + 1,
        updated_at=transition_at,
        claimed_by_membership_id=actor_id,
    )


def reject_approval(
    snapshot: PriceApprovalSnapshot,
    expected_version: int,
    actor_membership_id: str,
    reason_code: str,
    now: datetime,
) -> PriceApprovalSnapshot:
    transition_at = _prepare_transition(
        snapshot,
        expected_version,
        ApprovalStatus.pending,
        now,
    )
    actor_id = _require_nonblank(actor_membership_id, "actor_membership_id")
    decision_reason = _require_safe_code(reason_code, "reason_code")
    return replace(
        snapshot,
        status=ApprovalStatus.rejected,
        version=snapshot.version + 1,
        updated_at=transition_at,
        decided_by_membership_id=actor_id,
        reason_code=decision_reason,
    )


def block_approval(
    snapshot: PriceApprovalSnapshot,
    expected_version: int,
    actor_membership_id: str,
    safe_blocker_code: str,
    now: datetime,
) -> PriceApprovalSnapshot:
    transition_at = _prepare_transition(
        snapshot,
        expected_version,
        ApprovalStatus.pending,
        now,
    )
    actor_id = _require_nonblank(actor_membership_id, "actor_membership_id")
    blocker_code = _require_safe_code(safe_blocker_code, "safe_blocker_code")
    return replace(
        snapshot,
        status=ApprovalStatus.blocked,
        version=snapshot.version + 1,
        updated_at=transition_at,
        decided_by_membership_id=actor_id,
        reason_code=blocker_code,
    )


def record_apply_success(
    snapshot: PriceApprovalSnapshot,
    expected_version: int,
    wb_upload_id: str,
    result_code: str,
    now: datetime,
) -> PriceApprovalSnapshot:
    transition_at = _prepare_transition(
        snapshot,
        expected_version,
        ApprovalStatus.applying,
        now,
    )
    upload_id = _require_nonblank(wb_upload_id, "wb_upload_id")
    safe_result_code = _require_safe_code(result_code, "result_code")
    return replace(
        snapshot,
        status=ApprovalStatus.applied,
        version=snapshot.version + 1,
        updated_at=transition_at,
        wb_upload_id=upload_id,
        result_code=safe_result_code,
    )


def record_apply_failure(
    snapshot: PriceApprovalSnapshot,
    expected_version: int,
    safe_error_code: str,
    ambiguous: bool,
    now: datetime,
) -> PriceApprovalSnapshot:
    transition_at = _prepare_transition(
        snapshot,
        expected_version,
        ApprovalStatus.applying,
        now,
    )
    if type(ambiguous) is not bool:
        raise ApprovalValidationError("ambiguous must be a boolean")
    error_code = _require_safe_code(safe_error_code, "safe_error_code")
    if error_code not in SAFE_APPLY_ERROR_CODES:
        raise ApprovalValidationError("safe_error_code is not allowlisted")
    return replace(
        snapshot,
        status=ApprovalStatus.ambiguous if ambiguous else ApprovalStatus.failed,
        version=snapshot.version + 1,
        updated_at=transition_at,
        safe_error_code=error_code,
    )


__all__ = (
    "SAFE_APPLY_ERROR_CODES",
    "ApprovalConflictError",
    "ApprovalStatus",
    "ApprovalValidationError",
    "PriceApprovalSnapshot",
    "block_approval",
    "build_action_key",
    "claim_approval",
    "record_apply_failure",
    "record_apply_success",
    "reject_approval",
)
