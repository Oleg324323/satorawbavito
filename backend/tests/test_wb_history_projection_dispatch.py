"""New-operation data dispatch; no synthetic publication guard or claim."""
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest

from app.platform.integrations.user_orders_job_contract import OrdersJobError
from app.platform.integrations.user_orders_job_store import JobSnapshot
from tests.test_wb_history_projection_contract import request


def snapshot():
    r = request()
    return JobSnapshot({"operation_kind": "orders.wb-history.project.v1", "request_bytes": r.canonical_bytes,
        "organization_id": r.organization_id, "marketplace_account_id": r.marketplace_account_id,
        "request_checksum": r.checksum,
        "provider": r.binding.provider, "source_kind": r.binding.source_kind,
        "adapter_version": r.binding.adapter_version, "mapping_version": r.binding.mapping_version,
        "source_contract_version": r.binding.source_contract_version,
        "history_job_id": r.history_job_id, "history_run_id": r.history_run_id,
        "history_selection_digest": r.history_selection_digest, "history_page_count": r.history_page_count,
        "history_terminal_page_id": r.history_terminal_page_id,
        "history_cursor_page": 0, "history_cursor_ordinal": 0, "history_progress_version": 0}, {})


def test_job_snapshot_decodes_history_without_date_coercion():
    assert snapshot().request == request()


def test_history_progress_is_not_a_new_delegation():
    original = snapshot()
    moved = replace(original, row={**original.row, "history_cursor_page": 1,
        "history_cursor_ordinal": 0, "history_progress_version": 1})
    assert original.same_delegation(moved)


@pytest.mark.parametrize("field,bad", [("history_job_id", None), ("history_run_id", None),
    ("history_selection_digest", "b" * 64), ("history_page_count", 3), ("history_terminal_page_id", None)])
def test_selection_identity_is_always_part_of_delegation(field, bad):
    original = snapshot()
    assert not original.same_delegation(replace(original, row={**original.row, field: bad}))


def test_history_operation_cannot_decode_legacy_binding():
    value = snapshot()
    value = replace(value, row={**value.row, "source_contract_version": "legacy"})
    with pytest.raises(OrdersJobError, match="^JOB_CONTRACT_INVALID$"):
        _ = value.request


@pytest.mark.parametrize("method", ["create", "replay"])
def test_legacy_service_rejects_history_binding_before_session_creation(method):
    from app.platform.integrations.user_orders_job_contract import (
        OrdersExecutionPolicy,
        OrdersJobRequest,
        WbOrdersSourceRequest,
    )
    from app.platform.integrations.user_orders_jobs import UserOrdersJobs

    def forbidden_session():
        pytest.fail("History binding entered legacy authority root")

    service = UserOrdersJobs(session_factory=forbidden_session, trusted_sources=(request().binding,))
    legacy = OrdersJobRequest(1, 2, request().binding, WbOrdersSourceRequest(date(2026, 9, 1)))
    with pytest.raises(OrdersJobError, match="^SOURCE_CONTRACT_UNAVAILABLE$"):
        getattr(service, method)(authenticated_actor=None, request=legacy,
            idempotency_key=request().history_job_id,
            execution_policy=OrdersExecutionPolicy("synthetic-explicit", 1, 2, 30, (1,)),
            authority_expires_at=datetime(2026, 9, 30, tzinfo=UTC))


def test_new_operation_is_not_fetch_or_legacy_completion_authority():
    from app.platform.integrations import user_orders_jobs

    reject = getattr(user_orders_jobs, "_require_legacy_operation", None)
    assert reject is not None, "Legacy publication/fetch operation discriminator is missing"
    with pytest.raises(OrdersJobError, match="^SOURCE_CONTRACT_UNAVAILABLE$"):
        reject(snapshot())


def test_generic_recovery_rejects_history_composition_before_session():
    from app.platform.integrations.user_orders_jobs import UserOrdersJobs

    def forbidden():
        pytest.fail("Unsupported history recovery entered a root")
    service = UserOrdersJobs(session_factory=forbidden, trusted_sources=(request().binding,))
    with pytest.raises(OrdersJobError, match="^SOURCE_CONTRACT_UNAVAILABLE$"):
        service.recover_delivery(organization_id=1, marketplace_account_id=2, limit=1, deliver=lambda value: None)


@pytest.mark.parametrize("field,value", [("organization_id", 8), ("marketplace_account_id", 8), ("request_checksum", "f" * 64)])
def test_corrupt_snapshot_cannot_rebind_canonical_history_request(field, value):
    original = snapshot()
    corrupt = replace(original, row={**original.row, field: value})
    with pytest.raises(OrdersJobError, match="^JOB_CONTRACT_INVALID$"):
        _ = corrupt.request
