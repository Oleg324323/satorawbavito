"""Strict frozen history selection and required trusted runtime inputs.

These values carry no publication authority and never resolve a credential.
"""
import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC
from uuid import UUID

from app.orders.history_bridge import HistoryPageEvidence
from app.platform.integrations.user_orders_job_contract import (
    OrdersExecutionPolicy,
    OrdersJobError,
    TrustedOrdersSourceBinding,
    _Redacted,
    integer,
    uuid4_value,
)

OPERATION = "orders.wb-history.project.v1"
SOURCE_CONTRACT = "wb-history-positive-partial-v1"
BINDING = TrustedOrdersSourceBinding("wb", "wb-statistics-supplier-orders",
    "wb-statistics-orders-stream-v1", "wb-statistics-status-v1", SOURCE_CONTRACT)


def checksum_value(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise OrdersJobError("JOB_CONTRACT_INVALID")
    return value


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False).encode("ascii")


@dataclass(frozen=True, slots=True, repr=False)
class WbHistoryProjectionRequest(_Redacted):
    organization_id: int
    marketplace_account_id: int
    history_job_id: UUID
    history_run_id: UUID
    history_selection_digest: str
    history_page_count: int
    history_terminal_page_id: UUID

    def __post_init__(self):
        integer(self.organization_id)
        integer(self.marketplace_account_id)
        for value in (self.history_job_id, self.history_run_id, self.history_terminal_page_id):
            uuid4_value(value)
        checksum_value(self.history_selection_digest)
        integer(self.history_page_count)

    @property
    def binding(self):
        return BINDING

    def object(self):
        self.__post_init__()
        return {"schemaVersion": 1, "operationKind": OPERATION,
            "organizationId": self.organization_id, "marketplaceAccountId": self.marketplace_account_id,
            "provider": BINDING.provider, "sourceKind": BINDING.source_kind,
            "adapterVersion": BINDING.adapter_version, "mappingVersion": BINDING.mapping_version,
            "sourceContractVersion": SOURCE_CONTRACT, "requestedFrom": None, "requestedTo": None,
            "sourceRequest": {"historyJobId": str(self.history_job_id), "sourceRunId": str(self.history_run_id),
                "selectionDigest": self.history_selection_digest, "pageCount": self.history_page_count,
                "terminalPageId": str(self.history_terminal_page_id)}}

    @property
    def canonical_bytes(self):
        return canonical_bytes(self.object())

    @property
    def checksum(self):
        return hashlib.sha256(self.canonical_bytes).hexdigest()

    @classmethod
    def from_bytes(cls, value):
        if type(value) is not bytes or not 1 <= len(value) <= 4096:
            raise OrdersJobError("JOB_CONTRACT_INVALID")

        def pairs(items):
            result = {}
            for key, item in items:
                if key in result:
                    raise ValueError()
                result[key] = item
            return result

        try:
            data = json.loads(value.decode("ascii"), object_pairs_hook=pairs)
            source = data["sourceRequest"]
            result = cls(data["organizationId"], data["marketplaceAccountId"], UUID(source["historyJobId"]),
                UUID(source["sourceRunId"]), source["selectionDigest"], source["pageCount"], UUID(source["terminalPageId"]))
            if result.canonical_bytes != value:
                raise ValueError()
            return result
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            raise OrdersJobError("JOB_CONTRACT_INVALID") from None


@dataclass(frozen=True, slots=True, repr=False)
class HistoryProjectionDependencies(_Redacted):
    """Required trusted composition; constructing it performs no IO or callbacks."""
    session_factory: Callable
    execution_policy: OrdersExecutionPolicy
    authority_deadline_provider: Callable
    max_selection_pages: int

    def __post_init__(self):
        if (not callable(self.session_factory) or type(self.execution_policy) is not OrdersExecutionPolicy
                or not callable(self.authority_deadline_provider)):
            raise OrdersJobError("JOB_CONTRACT_INVALID")
        self.execution_policy.__post_init__()
        integer(self.max_selection_pages)

    def validate_request(self, request):
        if type(request) is not WbHistoryProjectionRequest:
            raise OrdersJobError("JOB_CONTRACT_INVALID")
        request.__post_init__()
        integer(request.history_page_count, self.max_selection_pages)


def history_header_bytes(page):
    """Same header representation used by the frozen T3 chunk codec."""
    if type(page) is not HistoryPageEvidence:
        raise OrdersJobError("JOB_CONTRACT_INVALID")
    page.__post_init__()
    value = asdict(page)
    value["published_at"] = page.published_at.astimezone(UTC).isoformat(timespec="microseconds")
    return canonical_bytes(value)


@dataclass(frozen=True, slots=True, repr=False)
class FrozenHistorySelection(_Redacted):
    request: WbHistoryProjectionRequest
    pages: tuple[HistoryPageEvidence, ...]
    header_checksums: tuple[str, ...]


@dataclass(frozen=True, slots=True, repr=False)
class HistoryProjectionReceipt(_Redacted):
    """Immutable past-commit evidence, never a current authorization capability."""
    organization_id: int
    marketplace_account_id: int
    sync_run_id: int
    history_job_id: str
    history_run_id: str
    history_page_id: str
    first_ordinal: int
    next_ordinal: int
    input_checksum: str
    source_run_key: str
    source_snapshot: str
    source_contract_version: str
    credential_id: str
    credential_generation: int
    account_incarnation: int
    reconciliation_count: int
    coverage_state: str

    def __post_init__(self):
        integer(self.organization_id)
        integer(self.marketplace_account_id)
        integer(self.sync_run_id, 2**63 - 1)
        integer(self.credential_generation, 2**63 - 1)
        integer(self.account_incarnation, 2**63 - 1)
        integer(self.first_ordinal, 99000, zero=True)
        integer(self.next_ordinal, 100000, zero=True)
        integer(self.reconciliation_count, 1000, zero=True)
        checksum_value(self.input_checksum)
        try:
            for value in (self.history_job_id, self.history_run_id, self.history_page_id, self.credential_id):
                if type(value) is not str or str(UUID(value)) != value:
                    raise ValueError()
        except (TypeError, ValueError, AttributeError):
            raise OrdersJobError("JOB_CONTRACT_INVALID") from None
        if (self.first_ordinal % 1000 or not self.first_ordinal <= self.next_ordinal <= self.first_ordinal + 1000
                or (self.next_ordinal == self.first_ordinal and self.first_ordinal != 0)
                or self.coverage_state != "partial" or self.source_contract_version != SOURCE_CONTRACT
                or self.source_run_key != f"wb-history-chunk-v1:{self.history_job_id}:{self.history_page_id}:{self.first_ordinal}:{self.next_ordinal}"
                or self.source_snapshot != f"wb-history-run-v1:{self.history_job_id}:{self.history_run_id}"):
            raise OrdersJobError("JOB_CONTRACT_INVALID")


def freeze_history_selection(*, organization_id, marketplace_account_id, history_job_id,
        history_run_id, credential_id, credential_generation, account_incarnation, pages,
        max_selection_pages):
    """Validate bounded captured metadata, without creating permission or a job."""
    from app.wb_live.statistics_orders import _instant

    integer(organization_id)
    integer(marketplace_account_id)
    integer(credential_generation, 2**63 - 1)
    integer(account_incarnation, 2**63 - 1)
    integer(max_selection_pages)
    for value in (history_job_id, history_run_id, credential_id):
        uuid4_value(value)
    expected = (organization_id, marketplace_account_id, str(history_job_id), str(history_run_id),
        str(credential_id), credential_generation, account_incarnation)
    try:
        if type(pages) is not tuple or not 1 <= len(pages) <= max_selection_pages:
            raise ValueError()
        by_input, ids, outputs = {}, set(), set()
        for page in pages:
            if type(page) is not HistoryPageEvidence:
                raise ValueError()
            page.__post_init__()
            if ((page.organization_id, page.marketplace_account_id, page.job_id, page.run_id,
                    page.credential_id, page.credential_generation, page.account_incarnation) != expected
                    or page.input_date_from in by_input or page.page_id in ids):
                raise ValueError()
            start = _instant(page.input_date_from, allow_date=True)
            end = _instant(page.next_date_from, allow_date=True)
            if not page.terminal and end <= start:
                raise ValueError()
            by_input[page.input_date_from] = page
            ids.add(page.page_id)
            if not page.terminal:
                outputs.add(page.next_date_from)
        starts = [page for page in pages if page.input_date_from not in outputs]
        if len(starts) != 1:
            raise ValueError()
        ordered, seen = [], set()
        current = starts[0]
        while True:
            if current.page_id in seen:
                raise ValueError()
            ordered.append(current)
            seen.add(current.page_id)
            if current.terminal:
                break
            current = by_input[current.next_date_from]
        if len(ordered) != len(pages):
            raise ValueError()
        headers = tuple(hashlib.sha256(history_header_bytes(page)).hexdigest() for page in ordered)
        selection = {"version": "wb-history-selection-v1", "organizationId": organization_id,
            "marketplaceAccountId": marketplace_account_id, "historyJobId": str(history_job_id),
            "sourceRunId": str(history_run_id), "pages": [{"pageId": page.page_id, "headerChecksum": digest}
                for page, digest in zip(ordered, headers, strict=True)]}
        request = WbHistoryProjectionRequest(organization_id, marketplace_account_id, history_job_id,
            history_run_id, hashlib.sha256(canonical_bytes(selection)).hexdigest(), len(ordered), UUID(ordered[-1].page_id))
        return FrozenHistorySelection(request, tuple(ordered), headers)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise OrdersJobError("JOB_CONFLICT") from None
