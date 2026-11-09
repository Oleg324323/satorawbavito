"""Pure boundary between canonical integer identities and approval persistence.

This module deliberately contains no repository implementation.  It validates
the identities that a future PostgreSQL adapter must receive, bridges those
identities to the string representation fixed by :mod:`app.modules.wb_repricing`,
and defines the minimum repository protocol shared by API and worker callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from app.modules.wb_repricing_dispatch import (
        ApplyAttempt,
        ApplyOutcome,
        AttemptResult,
        CanonicalApplyRequest,
    )

from app.modules.wb_repricing import (
    ApprovalStatus,
    ApprovalValidationError,
    PriceApprovalSnapshot,
    _require_postgres_text,
)

UNRESOLVED_LEGACY_IDENTITY_BLOCKER = (
    "WB_REPRICER_APPROVAL_IDENTITY_UNRESOLVED"
)


class ApprovalIdentityBlockedError(ApprovalValidationError):
    """A canonical internal identity is missing, invalid, or out of scope."""

    blocker_code = UNRESOLVED_LEGACY_IDENTITY_BLOCKER

    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(f"{self.blocker_code}:{field}")


def _positive_internal_id(value: object, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ApprovalIdentityBlockedError(field)
    return value


def _exact_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ApprovalValidationError(
            f"{field} must be a non-empty string without edge whitespace"
        )
    _require_postgres_text(value)
    return value


def _nonnegative_version(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ApprovalValidationError(
            f"{field} must be a non-negative integer"
        )
    return value


def bridge_internal_id(value: int) -> str:
    """Return the kernel representation of a verified positive internal ID.

    Decimal text without a prefix, padding, or external identifier fallback is
    the only representation.  Existing action keys are not recomputed here.
    """

    return str(_positive_internal_id(value, "internal_id"))


@dataclass(frozen=True, slots=True)
class ApprovalRepositoryScope:
    """Required composite lookup scope for one canonical approval."""

    organization_id: int
    marketplace_account_id: int
    approval_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "organization_id",
            _positive_internal_id(self.organization_id, "organization_id"),
        )
        object.__setattr__(
            self,
            "marketplace_account_id",
            _positive_internal_id(
                self.marketplace_account_id,
                "marketplace_account_id",
            ),
        )
        object.__setattr__(
            self,
            "approval_id",
            _exact_text(self.approval_id, "approval_id"),
        )

    @property
    def kernel_marketplace_account_id(self) -> str:
        return bridge_internal_id(self.marketplace_account_id)


@dataclass(frozen=True, slots=True)
class AuthenticatedApprovalActor:
    """Membership identity resolved by authentication before repository use."""

    organization_id: int
    membership_id: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "organization_id",
            _positive_internal_id(self.organization_id, "organization_id"),
        )
        object.__setattr__(
            self,
            "membership_id",
            _positive_internal_id(self.membership_id, "membership_id"),
        )


def bridge_actor_membership_id(
    scope: ApprovalRepositoryScope,
    actor: AuthenticatedApprovalActor,
) -> str:
    """Return the kernel actor ID only when actor and approval share an org."""

    if not isinstance(scope, ApprovalRepositoryScope):
        raise ApprovalIdentityBlockedError("scope")
    if not isinstance(actor, AuthenticatedApprovalActor):
        raise ApprovalIdentityBlockedError("actor")
    if actor.organization_id != scope.organization_id:
        raise ApprovalIdentityBlockedError("actor_organization_id")
    return bridge_internal_id(actor.membership_id)


def bridge_snapshot_identity(
    *,
    scope: ApprovalRepositoryScope,
    catalog_sku_id: int | None,
    snapshot: PriceApprovalSnapshot,
) -> PriceApprovalSnapshot:
    """Validate a canonical DB scope without rewriting the kernel snapshot.

    Returning the original frozen value is intentional: request checksum and
    action key remain byte-for-byte unchanged.  A legacy account/catalog string
    that cannot be resolved to an internal integer is blocked before insert.
    """

    if not isinstance(scope, ApprovalRepositoryScope):
        raise ApprovalIdentityBlockedError("scope")
    if not isinstance(snapshot, PriceApprovalSnapshot):
        raise ApprovalIdentityBlockedError("snapshot")
    kernel_catalog_sku_id = (
        None
        if catalog_sku_id is None
        else bridge_internal_id(
            _positive_internal_id(catalog_sku_id, "catalog_sku_id")
        )
    )
    if snapshot.organization_id != scope.organization_id:
        raise ApprovalIdentityBlockedError("snapshot_organization_id")
    if (
        snapshot.marketplace_account_id
        != scope.kernel_marketplace_account_id
    ):
        raise ApprovalIdentityBlockedError("snapshot_marketplace_account_id")
    if snapshot.approval_id != scope.approval_id:
        raise ApprovalIdentityBlockedError("snapshot_approval_id")
    if snapshot.catalog_sku_id != kernel_catalog_sku_id:
        raise ApprovalIdentityBlockedError("snapshot_catalog_sku_id")
    return snapshot


@dataclass(frozen=True, slots=True)
class ApprovalOutcomeCommand:
    """One durable attempt outcome recorded against an applying version."""

    attempt_id: str
    expected_version: int
    outcome: PriceApprovalSnapshot

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "attempt_id",
            _exact_text(self.attempt_id, "attempt_id"),
        )
        expected_version = _nonnegative_version(
            self.expected_version,
            "expected_version",
        )
        object.__setattr__(self, "expected_version", expected_version)
        if not isinstance(self.outcome, PriceApprovalSnapshot):
            raise ApprovalValidationError(
                "outcome must be a PriceApprovalSnapshot"
            )
        if self.outcome.status not in {
            ApprovalStatus.applied,
            ApprovalStatus.failed,
            ApprovalStatus.ambiguous,
        }:
            raise ApprovalValidationError(
                "outcome must be applied, failed, or ambiguous"
            )
        if self.outcome.version != expected_version + 1:
            raise ApprovalValidationError(
                "outcome version must increment expected_version exactly once"
            )


@runtime_checkable
class PriceApprovalRepository(Protocol):
    """Durable owner contract, including dispatch amendment (2026-09-09).

    Every mutation includes the specified audit in one transaction. Returned
    writes are committed; failed commit is an explicit exception, never fallback.
    Real adapters use trusted transaction time and fresh authorization, not
    client timestamps/membership claims. No UUID-only operation is permitted.
    """

    def get(
        self,
        *,
        scope: ApprovalRepositoryScope,
    ) -> PriceApprovalSnapshot | None:
        """Load by organization, account, and approval identity."""

        ...

    def insert(
        self,
        *,
        scope: ApprovalRepositoryScope,
        catalog_sku_id: int | None,
        snapshot: PriceApprovalSnapshot,
    ) -> PriceApprovalSnapshot:
        """Backfill-only: preserve snapshot + imported audit, request_format=legacy.

        No reserve/dispatch of legacy imports in this wave. Identical replay
        returns stored value without audit; changed immutable content conflicts.
        New actionable intents must use create_intent.
        """

        ...

    def claim(
        self,
        *,
        scope: ApprovalRepositoryScope,
        expected_version: int,
        actor: AuthenticatedApprovalActor,
        now: datetime,
    ) -> PriceApprovalSnapshot:
        """One scoped pending/version CAS + actor audit; require bound v1 intent.

        Updates approval V -> V+1. Closed/legacy/stale rows cannot be claimed.
        A claim is not permission to call a provider.
        """

        ...

    def record_outcome(
        self,
        *,
        scope: ApprovalRepositoryScope,
        command: ApprovalOutcomeCommand,
    ) -> PriceApprovalSnapshot:
        """Deprecated compatibility boundary; no direct arbitrary outcome write.

        Delegate to record_attempt_outcome at attempt version 1 only, requiring
        scoped dispatched attempt, complete immutable/actor snapshot equality,
        and new outcome allowlist. Cannot finalize reserved or imported rows.
        """

        ...

    def create_intent(
        self, *, scope: ApprovalRepositoryScope, request: CanonicalApplyRequest,
        actor: AuthenticatedApprovalActor, now: datetime,
    ) -> PriceApprovalSnapshot:
        """Create pending version 0, canonical bytes/hash/key + created audit.

        Scope must equal request.scope. Exact byte-for-byte replay returns the
        current snapshot with no write/audit; changed request under same scoped
        approval or action key conflicts, even when hashes claim equality.
        """
        ...

    def reserve_attempt(
        self, *, scope: ApprovalRepositoryScope, expected_version: int,
        actor: AuthenticatedApprovalActor, now: datetime,
    ) -> ApplyAttempt:
        """Reserve one attempt per approval + audit; approval stays applying.

        Repository generates UUID4 once for insert winner; caller/queue cannot
        supply it. Return existing reserved attempt on exact retry, no new audit;
        dispatched/closed/stale/differently bound attempts conflict. Actor must
        match original claim and remain authorized. Attempt starts at version 0.
        """
        ...

    def mark_dispatch(
        self, *, scope: ApprovalRepositoryScope, attempt_id: str,
        expected_version: int, expected_attempt_version: int,
        actor: AuthenticatedApprovalActor, now: datetime,
    ) -> ApplyAttempt:
        """Exclusive scoped reserved/0 -> dispatched/1 marker + audit commit.

        Match approval applying/version/claim actor, immutable request/key,
        attempt identity/claim_version/version and NULL marker in one atomic
        transaction. Only the first committed winner returns success. Replay,
        zero matched rows, or uncertain commit returns error, never permission.
        No lock may span the subsequent provider call. Approval version unchanged.
        """
        ...

    def record_attempt_outcome(
        self, *, scope: ApprovalRepositoryScope, attempt_id: str,
        expected_version: int, expected_attempt_version: int,
        outcome: ApplyOutcome, now: datetime,
    ) -> AttemptResult:
        """Worker-only: scoped attempt/result + approval V+1 + audit transaction.

        Apply finish_attempt rules. Match applying approval/version and attempt
        binding/version; all terminal replays conflict and append nothing.
        Worker audit membership is NULL; original approval claimant is retained.
        """
        ...

    def reject(
        self, *, scope: ApprovalRepositoryScope, expected_version: int,
        actor: AuthenticatedApprovalActor, reason_code: str, now: datetime,
    ) -> PriceApprovalSnapshot:
        """Scoped pending/version CAS + membership decision audit, V -> V+1."""
        ...

    def block(
        self, *, scope: ApprovalRepositoryScope, expected_version: int,
        actor: AuthenticatedApprovalActor, safe_blocker_code: str, now: datetime,
    ) -> PriceApprovalSnapshot:
        """Scoped pending/version CAS + membership blocker audit, V -> V+1."""
        ...


__all__ = (
    "UNRESOLVED_LEGACY_IDENTITY_BLOCKER",
    "ApprovalIdentityBlockedError",
    "ApprovalOutcomeCommand",
    "ApprovalRepositoryScope",
    "AuthenticatedApprovalActor",
    "PriceApprovalRepository",
    "bridge_actor_membership_id",
    "bridge_internal_id",
    "bridge_snapshot_identity",
)
