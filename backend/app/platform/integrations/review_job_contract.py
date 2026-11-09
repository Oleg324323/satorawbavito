"""Review authority values. Metadata and CAS witnesses are never authentication."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import re
from uuid import UUID

from app.platform.integrations.publication_guard import ExpectedAccountBinding, ExpectedCredential, UserSessionPrincipal

_MINT = object()
_CODES = frozenset({"REVIEW_CONTRACT_INVALID", "REVIEW_ACCESS_DENIED", "REVIEW_AUTHORITY_DENIED",
    "REVIEW_NOT_FOUND", "REVIEW_CONFLICT", "REVIEW_FENCE_INVALID", "REVIEW_PERSISTENCE_FAILED",
    "REVIEW_READBACK_REQUIRED"})


class ReviewJobError(ValueError):
    def __init__(self, code):
        self.code = code if type(code) is str and code in _CODES else "REVIEW_PERSISTENCE_FAILED"
        super().__init__(self.code)


class Redacted:
    __slots__ = ()

    def __repr__(self):
        return f"<{type(self).__name__} redacted>"

    __str__ = __repr__

    def __copy__(self):
        raise TypeError("REVIEW_CONTRACT_INVALID")

    def __deepcopy__(self, memo):
        raise TypeError("REVIEW_CONTRACT_INVALID")

    def __reduce_ex__(self, protocol):
        raise TypeError("REVIEW_CONTRACT_INVALID")


def require(value, code="REVIEW_CONTRACT_INVALID"):
    if not value:
        raise ReviewJobError(code)


def number(value):
    require(type(value) in (int, Decimal))
    if type(value) is Decimal:
        require(value.is_finite() and value == value.to_integral_value() and value > 0)
    else:
        require(value > 0)
    return int(value)


def integer(value):
    require(type(value) is int and 0 < value <= 2**31 - 1)
    return value


def uuid(value):
    require(type(value) is UUID and value.int != 0)
    return value


def timestamp(value):
    require(type(value) is datetime and value.tzinfo is not None and value.utcoffset() is not None)
    try:
        value.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        raise ReviewJobError("REVIEW_CONTRACT_INVALID") from None
    return value


@dataclass(frozen=True, slots=True, repr=False)
class ReviewJobLocator(Redacted):
    organization_id: int
    marketplace_account_id: int
    marketplace: str
    command_id: UUID

    def __post_init__(self):
        integer(self.organization_id)
        integer(self.marketplace_account_id)
        require(type(self.marketplace) is str and self.marketplace in {"wb", "avito"})
        uuid(self.command_id)


@dataclass(frozen=True, slots=True, repr=False)
class ReviewSendIntent(Redacted):
    locator: ReviewJobLocator
    review_id: UUID
    draft_id: UUID
    draft_revision: int
    decision_id: UUID
    idempotency_key: UUID
    request_payload: bytes
    request_checksum: str
    binding_checksum: str
    text_checksum: str

    def __post_init__(self):
        require(type(self.locator) is ReviewJobLocator)
        self.locator.__post_init__()
        for value in (self.review_id, self.draft_id, self.decision_id, self.idempotency_key):
            uuid(value)
        object.__setattr__(self, "draft_revision", number(self.draft_revision))
        require(type(self.request_payload) is bytes and bool(self.request_payload))
        for value in (self.request_checksum, self.binding_checksum, self.text_checksum):
            require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None)
        require(sha256(self.request_payload).hexdigest() == self.request_checksum)


@dataclass(frozen=True, slots=True, repr=False)
class ReviewAuthorityPolicy(Redacted):
    reference: str
    version: int
    lease_seconds: int

    def __post_init__(self):
        require(type(self.reference) is str and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", self.reference) is not None)
        object.__setattr__(self, "version", number(self.version))
        object.__setattr__(self, "lease_seconds", number(self.lease_seconds))


@dataclass(frozen=True, slots=True, repr=False)
class ReviewExpectedState(Redacted):
    locator: ReviewJobLocator
    version: int
    state: str
    attempt_id: UUID | None
    lease_token: UUID | None
    attempt_command_version: int | None
    attempt_state: str | None
    dispatched_at: datetime | None

    def __post_init__(self):
        require(type(self.locator) is ReviewJobLocator)
        self.locator.__post_init__()
        object.__setattr__(self, "version", number(self.version))
        require(type(self.state) is str and self.state in {"queued", "leased", "ambiguous", "sent", "conflict", "blocked", "cancelled"})
        if self.attempt_id is None:
            require(all(v is None for v in (self.lease_token, self.attempt_command_version, self.attempt_state, self.dispatched_at)))
        else:
            uuid(self.attempt_id)
            uuid(self.lease_token)
            object.__setattr__(self, "attempt_command_version", number(self.attempt_command_version))
            require(self.attempt_state in {"claimed", "dispatched", "abandoned", "ambiguous", "sent", "conflict", "blocked"})
            if self.dispatched_at is not None:
                timestamp(self.dispatched_at)


class ReviewAction(str, Enum):
    CREATE = "create"
    CANCEL = "cancel"
    CLAIM = "claim"
    RENEW = "renew"
    DISPATCH = "dispatch"
    RECLAIM = "reclaim"
    FETCH = "fetch"
    BEFORE_POST = "before_post"
    BLOCK = "block"
    ACK = "ack"
    AMBIGUOUS = "ambiguous"
    APPEND_ACK = "append_ack"
    READBACK = "readback"
    READ_CAPTURE = "read_capture"
    RECONCILE = "reconcile"


@dataclass(frozen=True, slots=True, repr=False)
class ReviewReadback(Redacted):
    expected: ReviewExpectedState
    lease_expires_at: datetime | None
    result_evidence_id: UUID | None
    reason_code: str | None


@dataclass(frozen=True, slots=True, repr=False)
class ReviewCreated(Redacted):
    locator: ReviewJobLocator
    review_id: UUID
    created_at: datetime
    created_audit_event_id: UUID


class ReviewReadbackRequired(ReviewJobError):
    def __init__(self, locator):
        self.locator = locator
        super().__init__("REVIEW_READBACK_REQUIRED")


@dataclass(frozen=True, slots=True, repr=False, init=False)
class CommittedReviewDispatch(Redacted):
    locator: ReviewJobLocator
    expected: ReviewExpectedState
    _used: bool

    def __init__(self, locator, expected, mint):
        require(mint is _MINT, "REVIEW_FENCE_INVALID")
        object.__setattr__(self, "locator", locator)
        object.__setattr__(self, "expected", expected)
        object.__setattr__(self, "_used", False)

    def _consume(self):
        require(not self._used, "REVIEW_FENCE_INVALID")
        object.__setattr__(self, "_used", True)


@dataclass(frozen=True, slots=True, repr=False)
class ReviewAuthorityCapture(Redacted):
    locator: ReviewJobLocator
    principal: UserSessionPrincipal
    account: ExpectedAccountBinding
    credential: ExpectedCredential
    policy: ReviewAuthorityPolicy
    authority_expires_at: datetime

    def __post_init__(self):
        require(type(self.locator) is ReviewJobLocator and type(self.principal) is UserSessionPrincipal
            and type(self.account) is ExpectedAccountBinding and type(self.credential) is ExpectedCredential
            and type(self.policy) is ReviewAuthorityPolicy)
        for value in (self.locator, self.principal, self.account, self.credential, self.policy):
            value.__post_init__()
        timestamp(self.authority_expires_at)
        require(self.principal.organization_id == self.locator.organization_id
            and self.account.marketplace_account_id == self.credential.marketplace_account_id == self.locator.marketplace_account_id
            and self.account.provider == self.locator.marketplace
            and self.credential.kind == ("wb_api" if self.locator.marketplace == "wb" else "avito_oauth_access"))


@dataclass(frozen=True, slots=True, repr=False, init=False)
class ReviewReadAuthority(Redacted):
    """In-process fresh read capture, never accepted from HTTP or a queue."""
    locator: ReviewJobLocator
    intent: ReviewSendIntent
    expected: ReviewExpectedState
    principal: UserSessionPrincipal
    account: ExpectedAccountBinding
    credential: ExpectedCredential
    read_id: UUID
    started_at: datetime
    resolved_credential: object
    _used: bool
    _owner: object

    def __init__(self, *, locator, intent, expected, principal, account, credential, read_id,
                 started_at, resolved_credential, owner, mint):
        require(mint is _MINT, "REVIEW_FENCE_INVALID")
        require(type(intent) is ReviewSendIntent and intent.locator == locator, "REVIEW_FENCE_INVALID")
        intent.__post_init__()
        for key, value in (("locator", locator), ("intent", intent), ("expected", expected), ("principal", principal),
                ("account", account), ("credential", credential), ("read_id", read_id), ("started_at", started_at),
                ("resolved_credential", resolved_credential), ("_used", False), ("_owner", owner)):
            object.__setattr__(self, key, value)

    def _consume(self, owner):
        require(not self._used and self._owner is owner, "REVIEW_FENCE_INVALID")
        object.__setattr__(self, "_used", True)
