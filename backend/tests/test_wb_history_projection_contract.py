"""Pure request/dependency admission, not publication authority or SQL proof."""
import hashlib
import importlib
import json
from dataclasses import replace
from datetime import date
from uuid import UUID

import pytest

from app.platform.integrations.user_orders_job_contract import (
    OrdersExecutionPolicy,
    OrdersJobError,
    OrdersJobRequest,
    TrustedOrdersSourceBinding,
    WbOrdersSourceRequest,
)

JOB = UUID("11111111-1111-4111-8111-111111111111")
RUN = UUID("22222222-2222-4222-8222-222222222222")
PAGE = UUID("33333333-3333-4333-8333-333333333333")


def module():
    try:
        return importlib.import_module("app.platform.integrations.wb_history_projection_contract")
    except ModuleNotFoundError as error:
        if error.name != "app.platform.integrations.wb_history_projection_contract":
            raise
        pytest.fail("New projection request/dependency contract is unavailable")


def request():
    return module().WbHistoryProjectionRequest(1, 2, JOB, RUN, "a" * 64, 2, PAGE)


def test_receipt_is_closed_partial_data_not_authority():
    factory = getattr(module(), "HistoryProjectionReceipt", None)
    assert factory is not None, "Immutable partial receipt view is missing"
    value = factory(1, 2, 3, str(JOB), str(RUN), str(PAGE), 0, 1, "a" * 64,
        f"wb-history-chunk-v1:{JOB}:{PAGE}:0:1", f"wb-history-run-v1:{JOB}:{RUN}",
        "wb-history-positive-partial-v1", str(JOB), 1, 1, 0, "partial")
    assert "redacted" in repr(value)
    with pytest.raises(OrdersJobError, match="^JOB_CONTRACT_INVALID$"):
        replace(value, coverage_state="complete")
    with pytest.raises(OrdersJobError, match="^JOB_CONTRACT_INVALID$"):
        replace(value, source_run_key="arbitrary")


def test_new_request_preserves_frozen_selection_identity():
    value = request()
    expected = (b'{"adapterVersion":"wb-statistics-orders-stream-v1","mappingVersion":"wb-statistics-status-v1",'
        b'"marketplaceAccountId":2,"operationKind":"orders.wb-history.project.v1","organizationId":1,'
        b'"provider":"wb","requestedFrom":null,"requestedTo":null,"schemaVersion":1,'
        b'"sourceContractVersion":"wb-history-positive-partial-v1","sourceKind":"wb-statistics-supplier-orders",'
        b'"sourceRequest":{"historyJobId":"11111111-1111-4111-8111-111111111111","pageCount":2,'
        b'"selectionDigest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        b'"sourceRunId":"22222222-2222-4222-8222-222222222222",'
        b'"terminalPageId":"33333333-3333-4333-8333-333333333333"}}')
    assert value.canonical_bytes == expected
    assert value.checksum == hashlib.sha256(expected).hexdigest()
    assert type(value).from_bytes(expected) == value
    assert str(JOB) not in repr(value)


@pytest.mark.parametrize("field,bad", [
    ("organization_id", True), ("marketplace_account_id", 0),
    ("history_job_id", str(JOB)), ("history_run_id", UUID(int=0)),
    ("history_terminal_page_id", None), ("history_selection_digest", "A" * 64),
    ("history_selection_digest", "a" * 63), ("history_page_count", True),
    ("history_page_count", 0), ("history_page_count", 2**31),
])
def test_request_rejects_lossy_or_unbounded_selection(field, bad):
    with pytest.raises(OrdersJobError, match="^JOB_CONTRACT_INVALID$"):
        replace(request(), **{field: bad})


@pytest.mark.parametrize("mutation", ["extra", "duplicate", "space", "date", "operation", "version"])
def test_request_decoder_rejects_noncanonical_or_legacy_coercion(mutation):
    raw = request().canonical_bytes
    if mutation == "duplicate":
        raw = raw.replace(b'"schemaVersion":1', b'"schemaVersion":1,"schemaVersion":1')
    elif mutation == "space":
        raw += b" "
    else:
        value = json.loads(raw)
        if mutation == "extra":
            value["untrusted"] = "safe-synthetic-canary"
        elif mutation == "date":
            value["sourceRequest"] = {"dateFrom": "2026-09-01"}
        elif mutation == "operation":
            value["operationKind"] = "orders.sync.v1"
        else:
            value["schemaVersion"] = True
        raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(OrdersJobError, match="^JOB_CONTRACT_INVALID$") as caught:
        type(request()).from_bytes(raw)
    assert "safe-synthetic-canary" not in str(caught.value)


def dependencies(**changes):
    values = {"session_factory": lambda: None,
        "execution_policy": OrdersExecutionPolicy("synthetic-only", 1, 2, 30, (5,)),
        "authority_deadline_provider": lambda **kw: kw["database_now"], "max_selection_pages": 2}
    values.update(changes)
    return module().HistoryProjectionDependencies(**values)


@pytest.mark.parametrize("field,bad", [("session_factory", None), ("execution_policy", {}),
    ("authority_deadline_provider", None), ("max_selection_pages", True),
    ("max_selection_pages", 0), ("max_selection_pages", 2**31)])
def test_dependencies_reject_untrusted_policy_or_missing_bound(field, bad):
    with pytest.raises(OrdersJobError, match="^JOB_CONTRACT_INVALID$"):
        dependencies(**{field: bad})


def test_dependency_bound_fails_closed_instead_of_truncating_selection():
    configured = dependencies()
    configured.validate_request(request())
    with pytest.raises(OrdersJobError, match="^JOB_CONTRACT_INVALID$"):
        configured.validate_request(replace(request(), history_page_count=3))


def test_legacy_wb_request_bytes_remain_calendar_date_contract():
    binding = TrustedOrdersSourceBinding("wb", "wb-statistics-supplier-orders", "synthetic-wb-v1",
        "wb-statistics-status-v1", "synthetic-source-v1")
    value = OrdersJobRequest(1, 2, binding, WbOrdersSourceRequest(date(2026, 9, 1)))
    expected = (b'{"adapterVersion":"synthetic-wb-v1","mappingVersion":"wb-statistics-status-v1",'
        b'"marketplaceAccountId":2,"operationKind":"orders.sync.v1","organizationId":1,"provider":"wb",'
        b'"requestedFrom":null,"requestedTo":null,"schemaVersion":1,"sourceContractVersion":"synthetic-source-v1",'
        b'"sourceKind":"wb-statistics-supplier-orders","sourceRequest":{"dateFrom":"2026-09-01"}}')
    assert value.canonical_bytes == expected
    assert OrdersJobRequest.from_bytes(expected, trusted_binding=binding) == value
