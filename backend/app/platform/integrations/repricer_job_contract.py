"""Exact, redacted values for the dormant repricer executor boundary."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from threading import Lock
from uuid import UUID

OPERATION = "wb.price_apply.v1"
ERRORS = frozenset({"REPRICER_CONTRACT_INVALID", "REPRICER_ACCESS_DENIED", "REPRICER_NOT_FOUND",
    "REPRICER_CONFLICT", "REPRICER_AUTHORITY_DENIED", "REPRICER_FENCE_INVALID",
    "REPRICER_PERSISTENCE_FAILED", "READBACK_REQUIRED"})
TERMINAL = frozenset({"applied", "failed", "ambiguous", "rejected", "blocked"})


class RepricerJobError(ValueError):
    def __init__(self, code):
        self.code = code if type(code) is str and code in ERRORS else "REPRICER_PERSISTENCE_FAILED"
        super().__init__(self.code)

    def __repr__(self):
        return f"RepricerJobError(code={self.code!r})"


class Redacted:
    __slots__ = ()

    def __repr__(self):
        return f"<{type(self).__name__} redacted>"

    __str__ = __repr__

    def __reduce_ex__(self, protocol):
        raise TypeError("REPRICER_CONTRACT_INVALID")


def integer(value, maximum=2**31 - 1):
    if type(value) is not int or not 1 <= value <= maximum:
        raise RepricerJobError("REPRICER_CONTRACT_INVALID")
    return value


def version(value):
    """Scale-free NUMERIC version; no float, bool, BIGINT or digit-string coercion."""
    if type(value) is int and value >= 0:
        return value
    if type(value) is Decimal and value.is_finite() and value >= 0 and value == value.to_integral_value():
        return int(value)
    raise RepricerJobError("REPRICER_CONTRACT_INVALID")


def uuid4_value(value):
    if type(value) is not UUID or value.version != 4:
        raise RepricerJobError("REPRICER_CONTRACT_INVALID")
    return value


def exact_text(value, maximum=None):
    if (type(value) is not str or not value or value != value.strip()
            or (maximum is not None and len(value) > maximum)
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise RepricerJobError("REPRICER_CONTRACT_INVALID")
    return value


def digest(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise RepricerJobError("REPRICER_CONTRACT_INVALID")
    return value


def upload_id(value):
    # Original POST adapter must perform the lossless projection. Floats, bools,
    # exponent strings, signs and padding cannot be normalized into evidence here.
    if type(value) is not str or re.fullmatch(r"[1-9][0-9]*", value) is None:
        raise RepricerJobError("REPRICER_CONTRACT_INVALID")
    return value


def timestamp(value):
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise RepricerJobError("REPRICER_CONTRACT_INVALID")
    try:
        return value.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise RepricerJobError("REPRICER_CONTRACT_INVALID") from None


class InitiationAction(Enum):
    CLAIM = "claim"
    RESERVE = "reserve"
    DISPATCH = "dispatch"
    FETCH = "fetch"
    BEFORE_PROVIDER_IO = "before_provider_io"


class ClosingAction(Enum):
    OUTCOME = "outcome"
    RECEIPT = "receipt"
    READBACK = "readback"


@dataclass(frozen=True, slots=True, repr=False)
class RepricerJobLocator(Redacted):
    organization_id: int
    marketplace_account_id: int
    job_id: UUID

    def __post_init__(self):
        integer(self.organization_id)
        integer(self.marketplace_account_id)
        uuid4_value(self.job_id)

    def queue_payload(self):
        return {"organization_id": self.organization_id, "marketplace_account_id": self.marketplace_account_id,
                "job_id": str(self.job_id)}

    @classmethod
    def from_queue(cls, payload):
        try:
            if type(payload) is not dict or set(payload) != {"organization_id", "marketplace_account_id", "job_id"}:
                raise RepricerJobError("REPRICER_CONTRACT_INVALID")
            raw = payload["job_id"]
            if type(raw) is not str:
                raise RepricerJobError("REPRICER_CONTRACT_INVALID")
            parsed = UUID(raw)
            if str(parsed) != raw:
                raise RepricerJobError("REPRICER_CONTRACT_INVALID")
            return cls(payload["organization_id"], payload["marketplace_account_id"], parsed)
        except (ValueError, TypeError, AttributeError):
            raise RepricerJobError("REPRICER_CONTRACT_INVALID") from None


@dataclass(frozen=True, slots=True, repr=False)
class RepricerApprovalBinding(Redacted):
    organization_id: int
    marketplace_account_id: int
    approval_row_id: UUID
    approval_id: str
    action_key: str
    request_checksum: str
    canonical_request_bytes: bytes

    def __post_init__(self):
        integer(self.organization_id)
        integer(self.marketplace_account_id)
        uuid4_value(self.approval_row_id)
        exact_text(self.approval_id)
        digest(self.action_key)
        digest(self.request_checksum)
        if (type(self.canonical_request_bytes) is not bytes or not 1 <= len(self.canonical_request_bytes) <= 4096
                or hashlib.sha256(self.canonical_request_bytes).hexdigest() != self.request_checksum):
            raise RepricerJobError("REPRICER_CONTRACT_INVALID")


@dataclass(frozen=True, slots=True, repr=False)
class RepricerAuthorityPolicy(Redacted):
    reference: str
    version: int

    def __post_init__(self):
        exact_text(self.reference, 128)
        integer(self.version)


@dataclass(frozen=True, slots=True, repr=False)
class RepricerAuthorityMetadata(Redacted):
    locator: RepricerJobLocator
    external_account_id: str
    credential_ref: str | None
    credential_id: UUID
    generation: int
    payload_schema_version: int
    credential_expires_at: None
    authority_expires_at: datetime
    policy: RepricerAuthorityPolicy

    def __post_init__(self):
        if type(self.locator) is not RepricerJobLocator or type(self.policy) is not RepricerAuthorityPolicy:
            raise RepricerJobError("REPRICER_CONTRACT_INVALID")
        self.locator.__post_init__()
        self.policy.__post_init__()
        exact_text(self.external_account_id,128)
        if self.credential_ref is not None:
            exact_text(self.credential_ref,255)
        if type(self.credential_id) is not UUID or type(self.payload_schema_version) is not int or self.payload_schema_version != 1 or self.credential_expires_at is not None:
            raise RepricerJobError("REPRICER_CONTRACT_INVALID")
        integer(self.generation,2**63-1)
        timestamp(self.authority_expires_at)


@dataclass(frozen=True, slots=True, repr=False)
class RepricerExpectedState(Redacted):
    approval: RepricerApprovalBinding
    approval_version: int | Decimal
    attempt_id: UUID | None = None
    attempt_version: int | None = None
    dispatch_key: str | None = None
    claim_version: int | Decimal | None = None

    def __post_init__(self):
        if type(self.approval) is not RepricerApprovalBinding:
            raise RepricerJobError("REPRICER_CONTRACT_INVALID")
        self.approval.__post_init__()
        object.__setattr__(self, "approval_version", version(self.approval_version))
        if self.attempt_id is None:
            if any(v is not None for v in (self.attempt_version, self.dispatch_key, self.claim_version)):
                raise RepricerJobError("REPRICER_CONTRACT_INVALID")
        else:
            uuid4_value(self.attempt_id)
            if type(self.attempt_version) is not int or not 0 <= self.attempt_version <= 2:
                raise RepricerJobError("REPRICER_CONTRACT_INVALID")
            digest(self.dispatch_key)
            object.__setattr__(self, "claim_version", version(self.claim_version))
            if self.claim_version < 1:
                raise RepricerJobError("REPRICER_CONTRACT_INVALID")


@dataclass(frozen=True, slots=True, repr=False)
class RepricerJobView(Redacted):
    locator: RepricerJobLocator
    approval: RepricerApprovalBinding
    created_at: datetime
    created_audit_id: UUID

    def __post_init__(self):
        if type(self.locator) is not RepricerJobLocator or type(self.approval) is not RepricerApprovalBinding:
            raise RepricerJobError("REPRICER_CONTRACT_INVALID")
        self.locator.__post_init__()
        self.approval.__post_init__()
        if (self.locator.organization_id,self.locator.marketplace_account_id) != (self.approval.organization_id,self.approval.marketplace_account_id):
            raise RepricerJobError("REPRICER_CONTRACT_INVALID")
        timestamp(self.created_at)
        uuid4_value(self.created_audit_id)


@dataclass(frozen=True, slots=True, repr=False)
class RepricerUploadObservation(Redacted):
    """Trusted original POST projection only; history query IDs are not evidence."""
    wb_upload_id: str
    observed_at: datetime

    def __post_init__(self):
        upload_id(self.wb_upload_id)
        timestamp(self.observed_at)


@dataclass(frozen=True, slots=True, repr=False)
class RepricerReceiptView(Redacted):
    locator: RepricerJobLocator
    receipt_id: UUID
    approval_row_id: UUID
    attempt_id: UUID
    action_key: str
    request_checksum: str
    dispatch_key: str
    wb_upload_id: str
    observed_at: datetime
    recorded_at: datetime
    created_audit_id: UUID

    def __post_init__(self):
        if type(self.locator) is not RepricerJobLocator:
            raise RepricerJobError("REPRICER_CONTRACT_INVALID")
        self.locator.__post_init__()
        for value in (self.receipt_id,self.approval_row_id,self.attempt_id,self.created_audit_id):
            uuid4_value(value)
        for value in (self.action_key,self.request_checksum,self.dispatch_key):
            digest(value)
        upload_id(self.wb_upload_id)
        timestamp(self.observed_at)
        timestamp(self.recorded_at)


@dataclass(frozen=True, slots=True, repr=False)
class RepricerReadback(Redacted):
    locator: RepricerJobLocator
    approval_status: str
    expected: RepricerExpectedState
    attempt_status: str | None
    receipt: RepricerReceiptView | None

    def __post_init__(self):
        if (type(self.locator) is not RepricerJobLocator or type(self.expected) is not RepricerExpectedState
                or self.approval_status not in TERMINAL | {"pending", "applying"}
                or self.attempt_status not in {None,"reserved","dispatched","applied","failed","ambiguous"}
                or (self.receipt is not None and type(self.receipt) is not RepricerReceiptView)):
            raise RepricerJobError("REPRICER_CONTRACT_INVALID")
        self.locator.__post_init__()
        self.expected.__post_init__()
        if (self.locator.organization_id,self.locator.marketplace_account_id) != (self.expected.approval.organization_id,self.expected.approval.marketplace_account_id):
            raise RepricerJobError("REPRICER_CONTRACT_INVALID")
        if self.receipt is not None:
            self.receipt.__post_init__()
            if self.receipt.locator != self.locator or self.receipt.attempt_id != self.expected.attempt_id:
                raise RepricerJobError("REPRICER_CONTRACT_INVALID")


class RepricerReadbackRequired(RepricerJobError):
    """Scoped durable lookup after uncertain COMMIT, never a new POST instruction."""
    def __init__(self, locator=None, approval=None):
        super().__init__("READBACK_REQUIRED")
        self.locator, self.approval = locator, approval


_COMMITTED = object()


class CommittedRepricerDispatch(Redacted):
    """In-process, one-consumption marker result; never reconstructed from queue.

    Not persistent send authority. An uncertain dispatch commit never mints this.
    A fresh executor/live guard and exact paired credential are still mandatory.
    """
    __slots__ = ("_locator", "_expected", "_proof", "_used", "_consume_lock")

    def __init__(self, locator, expected, proof):
        if proof is not _COMMITTED or type(locator) is not RepricerJobLocator or type(expected) is not RepricerExpectedState:
            raise RepricerJobError("REPRICER_FENCE_INVALID")
        locator.__post_init__()
        expected.__post_init__()
        if expected.attempt_id is None or expected.attempt_version != 1:
            raise RepricerJobError("REPRICER_FENCE_INVALID")
        self._locator, self._expected, self._proof, self._used = locator, expected, proof, False
        self._consume_lock = Lock()

    @property
    def locator(self):
        return self._locator

    @property
    def expected(self):
        return self._expected

    def _consume(self):
        with self._consume_lock:
            if self._proof is not _COMMITTED or self._used:
                raise RepricerJobError("REPRICER_FENCE_INVALID")
            self._used = True
