"""Fixed, redacted local Orders contracts. No broker, handlers, or policy defaults."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

OPERATION = "orders.sync.v1"
RETRY_REASONS = frozenset({"SOURCE_READ_UNAVAILABLE", "SOURCE_READ_RATE_LIMITED", "LOCAL_TRANSIENT_FAILURE"})
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "revoked", "expired", "blocked"})
SAFE_REASONS = RETRY_REASONS | frozenset({"LEASE_EXPIRED", "PERMANENT_SOURCE_FAILURE", "RETRY_BUDGET_EXHAUSTED",
    "USER_CANCELLED", "AUTHORITY_REVOKED", "AUTHORITY_EXPIRED", "PERMISSION_DENIED", "ACCOUNT_SCOPE_DENIED",
    "ACCOUNT_DISCONNECTED", "BINDING_CHANGED", "SOURCE_CONTRACT_UNAVAILABLE"})
ERROR_CODES = frozenset({"JOB_CONTRACT_INVALID", "JOB_ACCESS_DENIED", "JOB_NOT_FOUND", "JOB_CONFLICT",
    "JOB_NOT_DUE", "JOB_NOT_CLAIMABLE", "JOB_FENCE_INVALID", "JOB_AUTHORITY_DENIED", "JOB_PERSISTENCE_FAILED",
    "READBACK_REQUIRED", "DELIVERY_UNCONFIRMED", "SOURCE_CONTRACT_UNAVAILABLE"})


class OrdersJobError(ValueError):
    def __init__(self, code: str):
        self.code = code if type(code) is str and code in ERROR_CODES else "JOB_PERSISTENCE_FAILED"
        super().__init__(self.code)

    def __repr__(self):
        return f"OrdersJobError(code={self.code!r})"


def integer(value, maximum=2**31 - 1, *, zero=False):
    if type(value) is not int or not (0 if zero else 1) <= value <= maximum:
        raise OrdersJobError("JOB_CONTRACT_INVALID")
    return value


def exact_text(value, maximum=128):
    if (type(value) is not str or not 0 < len(value) <= maximum or value != value.strip()
            or "\x00" in value or any(0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise OrdersJobError("JOB_CONTRACT_INVALID")
    return value


def uuid4_value(value):
    if type(value) is not UUID or value.version != 4:
        raise OrdersJobError("JOB_CONTRACT_INVALID")
    return value


def timestamp(value):
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise OrdersJobError("JOB_CONTRACT_INVALID")
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        raise OrdersJobError("JOB_CONTRACT_INVALID") from None


def plus_seconds(value, seconds):
    integer(seconds, zero=True)
    try:
        return timestamp(value) + timedelta(seconds=seconds)
    except (OverflowError, ValueError):
        raise OrdersJobError("JOB_CONTRACT_INVALID") from None


def calendar_date(value, *, nullable=False):
    if value is None and nullable:
        return None
    if type(value) is not date:
        raise OrdersJobError("JOB_CONTRACT_INVALID")
    return value


class _Redacted:
    __slots__ = ()

    def __repr__(self):
        return f"<{type(self).__name__} redacted>"

    __str__ = __repr__

    def __reduce_ex__(self, protocol):
        raise TypeError("JOB_CONTRACT_INVALID")


@dataclass(frozen=True, slots=True, repr=False)
class TrustedOrdersSourceBinding(_Redacted):
    """Constructed by trusted service composition, never JSON/queue/import paths.

    Recognition describes selectors only. This object does not register a
    complete-sync handler, establish snapshot semantics, or approve activation.
    """
    provider: str
    source_kind: str
    adapter_version: str
    mapping_version: str
    source_contract_version: str

    def __post_init__(self):
        expected = {"wb": ("wb-statistics-supplier-orders", "wb-statistics-status-v1"),
                    "avito": ("avito-order-management", "avito-order-status-v1")}
        if type(self.provider) is not str or expected.get(self.provider) != (self.source_kind, self.mapping_version):
            raise OrdersJobError("JOB_CONTRACT_INVALID")
        for value in (self.source_kind, self.adapter_version, self.mapping_version, self.source_contract_version):
            exact_text(value)

    @property
    def credential_kind(self):
        return "wb_api" if self.provider == "wb" else "avito_oauth_access"


@dataclass(frozen=True, slots=True, repr=False)
class AvitoOrdersSourceRequest(_Redacted):
    date_from: date | None
    statuses: tuple[str, ...]
    limit: int
    page: int

    def __post_init__(self):
        calendar_date(self.date_from, nullable=True)
        if type(self.statuses) is not tuple:
            raise OrdersJobError("JOB_CONTRACT_INVALID")
        for value in self.statuses:
            exact_text(value)
            if "," in value:
                raise OrdersJobError("JOB_CONTRACT_INVALID")
        integer(self.limit, 20)
        integer(self.page, 2**63 - 1)

    def object(self):
        return {"dateFrom": self.date_from.isoformat() if self.date_from is not None else None,
                "statuses": list(self.statuses), "limit": self.limit, "page": self.page}


@dataclass(frozen=True, slots=True, repr=False)
class WbOrdersSourceRequest(_Redacted):
    date_from: date

    def __post_init__(self):
        calendar_date(self.date_from)

    def object(self):
        return {"dateFrom": self.date_from.isoformat()}


@dataclass(frozen=True, slots=True, repr=False)
class OrdersJobRequest(_Redacted):
    organization_id: int
    marketplace_account_id: int
    binding: TrustedOrdersSourceBinding
    source_request: AvitoOrdersSourceRequest | WbOrdersSourceRequest

    def __post_init__(self):
        integer(self.organization_id)
        integer(self.marketplace_account_id)
        if type(self.binding) is not TrustedOrdersSourceBinding:
            raise OrdersJobError("JOB_CONTRACT_INVALID")
        self.binding.__post_init__()
        expected = WbOrdersSourceRequest if self.binding.provider == "wb" else AvitoOrdersSourceRequest
        if type(self.source_request) is not expected:
            raise OrdersJobError("JOB_CONTRACT_INVALID")
        self.source_request.__post_init__()

    def object(self):
        b = self.binding
        return {"schemaVersion": 1, "operationKind": OPERATION, "organizationId": self.organization_id,
                "marketplaceAccountId": self.marketplace_account_id, "provider": b.provider,
                "sourceKind": b.source_kind, "adapterVersion": b.adapter_version,
                "mappingVersion": b.mapping_version, "sourceContractVersion": b.source_contract_version,
                "sourceRequest": self.source_request.object(), "requestedFrom": None, "requestedTo": None}

    @property
    def canonical_bytes(self):
        self.__post_init__()
        return json.dumps(self.object(), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")

    @property
    def checksum(self):
        return hashlib.sha256(self.canonical_bytes).hexdigest()

    @classmethod
    def from_bytes(cls, value, *, trusted_binding):
        """Persisted exact bytes only; duplicate fields and lossy coercion rejected."""
        if type(value) is not bytes or type(trusted_binding) is not TrustedOrdersSourceBinding:
            raise OrdersJobError("JOB_CONTRACT_INVALID")

        def pairs(items):
            result = {}
            for key, item in items:
                if key in result:
                    raise OrdersJobError("JOB_CONTRACT_INVALID")
                result[key] = item
            return result

        try:
            data = json.loads(value.decode("ascii"), object_pairs_hook=pairs,
                              parse_constant=lambda _: (_ for _ in ()).throw(OrdersJobError("JOB_CONTRACT_INVALID")))
            source = data["sourceRequest"]
            raw_date = source["dateFrom"]
            if raw_date is None:
                parsed_date = None
            else:
                if type(raw_date) is not str:
                    raise OrdersJobError("JOB_CONTRACT_INVALID")
                parsed_date = date.fromisoformat(raw_date)
                if parsed_date.isoformat() != raw_date:
                    raise OrdersJobError("JOB_CONTRACT_INVALID")
            if trusted_binding.provider == "wb":
                if set(source) != {"dateFrom"}:
                    raise OrdersJobError("JOB_CONTRACT_INVALID")
                request_source = WbOrdersSourceRequest(parsed_date)
            else:
                if set(source) != {"dateFrom", "statuses", "limit", "page"} or type(source["statuses"]) is not list:
                    raise OrdersJobError("JOB_CONTRACT_INVALID")
                request_source = AvitoOrdersSourceRequest(parsed_date, tuple(source["statuses"]), source["limit"], source["page"])
            result = cls(data["organizationId"], data["marketplaceAccountId"], trusted_binding, request_source)
            if result.canonical_bytes != value:
                raise OrdersJobError("JOB_CONTRACT_INVALID")
            return result
        except (KeyError, TypeError, ValueError, UnicodeError, OverflowError):
            raise OrdersJobError("JOB_CONTRACT_INVALID") from None


@dataclass(frozen=True, slots=True, repr=False)
class OrdersExecutionPolicy(_Redacted):
    reference: str
    version: int
    max_attempts: int
    lease_seconds: int
    retry_backoff_seconds: tuple[int, ...]

    def __post_init__(self):
        exact_text(self.reference)
        integer(self.version)
        integer(self.max_attempts, 1000)  # representation/resource bound, not a production policy
        integer(self.lease_seconds)
        if type(self.retry_backoff_seconds) is not tuple or len(self.retry_backoff_seconds) != self.max_attempts - 1:
            raise OrdersJobError("JOB_CONTRACT_INVALID")
        for seconds in self.retry_backoff_seconds:
            integer(seconds, zero=True)

    def lease_end(self, now):
        return plus_seconds(now, self.lease_seconds)

    def next_due(self, now, attempt_count):
        integer(attempt_count, self.max_attempts)
        if attempt_count >= self.max_attempts:
            raise OrdersJobError("JOB_NOT_CLAIMABLE")
        return plus_seconds(now, self.retry_backoff_seconds[attempt_count - 1])


@dataclass(frozen=True, slots=True, repr=False)
class OrdersCredentialDependency(_Redacted):
    organization_id: int
    marketplace_account_id: int
    provider: str
    credential_id: UUID
    credential_kind: str
    generation: int
    payload_schema_version: int
    expires_at: datetime | None

    def __post_init__(self):
        integer(self.organization_id)
        integer(self.marketplace_account_id)
        if type(self.credential_id) is not UUID:
            raise OrdersJobError("JOB_CONTRACT_INVALID")
        integer(self.generation, 2**63 - 1)
        if type(self.payload_schema_version) is not int or self.payload_schema_version != 1:
            raise OrdersJobError("JOB_CONTRACT_INVALID")
        if (self.provider, self.credential_kind) not in {("wb", "wb_api"), ("avito", "avito_oauth_access")}:
            raise OrdersJobError("JOB_CONTRACT_INVALID")
        if self.provider == "avito":
            timestamp(self.expires_at)
        elif self.expires_at is not None:
            raise OrdersJobError("JOB_CONTRACT_INVALID")


@dataclass(frozen=True, slots=True, repr=False)
class OrdersJobLocator(_Redacted):
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
            if type(payload) is not dict or set(payload) != {"organization_id", "marketplace_account_id", "job_id"} or type(payload["job_id"]) is not str:
                raise OrdersJobError("JOB_CONTRACT_INVALID")
            value = UUID(payload["job_id"])
            if str(value) != payload["job_id"]:
                raise OrdersJobError("JOB_CONTRACT_INVALID")
            return cls(payload["organization_id"], payload["marketplace_account_id"], value)
        except (ValueError, TypeError, AttributeError):
            raise OrdersJobError("JOB_CONTRACT_INVALID") from None


@dataclass(frozen=True, slots=True, repr=False)
class StoredOrdersJobView(_Redacted):
    job_id: UUID
    state: str
    version: int
    attempt_count: int
    next_attempt_at: datetime | None
    completed_at: datetime | None
    safe_reason: str | None
    result_sync_run_id: int | None
    result_coverage_state: str | None

    def __post_init__(self):
        uuid4_value(self.job_id)
        integer(self.version, 2**63 - 1)
        integer(self.attempt_count, 1000, zero=True)
        if type(self.state) is not str or self.state not in TERMINAL | {"queued", "running"} or (self.safe_reason is not None and (type(self.safe_reason) is not str or self.safe_reason not in SAFE_REASONS)):
            raise OrdersJobError("JOB_CONTRACT_INVALID")
        for value in (self.next_attempt_at, self.completed_at):
            if value is not None:
                timestamp(value)
        if (self.state in TERMINAL) != (self.completed_at is not None):
            raise OrdersJobError("JOB_CONTRACT_INVALID")
        if (self.state == "queued") != (self.next_attempt_at is not None):
            raise OrdersJobError("JOB_CONTRACT_INVALID")
        reasons = {"running": {None}, "succeeded": {None}, "failed": {"PERMANENT_SOURCE_FAILURE", "RETRY_BUDGET_EXHAUSTED"},
            "cancelled": {"USER_CANCELLED"}, "revoked": {"AUTHORITY_REVOKED"}, "expired": {"AUTHORITY_EXPIRED"},
            "blocked": {"PERMISSION_DENIED", "ACCOUNT_SCOPE_DENIED", "ACCOUNT_DISCONNECTED", "BINDING_CHANGED", "SOURCE_CONTRACT_UNAVAILABLE"},
            "queued": RETRY_REASONS | {None, "LEASE_EXPIRED"}}
        if self.safe_reason not in reasons[self.state]:
            raise OrdersJobError("JOB_CONTRACT_INVALID")
        if (self.state == "succeeded") != (self.result_sync_run_id is not None):
            raise OrdersJobError("JOB_CONTRACT_INVALID")
        if self.result_sync_run_id is not None:
            integer(self.result_sync_run_id, 2**63 - 1)
            if self.result_coverage_state not in {"complete", "partial"}:
                raise OrdersJobError("JOB_CONTRACT_INVALID")
        elif self.result_coverage_state is not None:
            raise OrdersJobError("JOB_CONTRACT_INVALID")

    def delivery_result(self):
        """Only bounded state and job ID belong in a transport result backend."""
        return {"job_id": str(self.job_id), "state": self.state}


_COMMITTED = object()


@dataclass(frozen=True, slots=True, repr=False)
class ClaimedUserOrdersJob(_Redacted):
    locator: OrdersJobLocator
    attempt_id: UUID
    claimant_token: UUID
    expected_job_version: int
    expected_attempt_version: int
    lease_expires_at: datetime
    authority_expires_at: datetime
    _commit_proof: object

    def __post_init__(self):
        if type(self.locator) is not OrdersJobLocator or self._commit_proof is not _COMMITTED:
            raise OrdersJobError("JOB_FENCE_INVALID")
        self.locator.__post_init__()
        uuid4_value(self.attempt_id)
        uuid4_value(self.claimant_token)
        integer(self.expected_job_version, 2**63 - 1)
        integer(self.expected_attempt_version, 2**63 - 1)
        if timestamp(self.lease_expires_at) > timestamp(self.authority_expires_at):
            raise OrdersJobError("JOB_FENCE_INVALID")

    @property
    def source_run_key(self):
        return f"orders-job-v1:{self.locator.job_id}:{self.attempt_id}"
