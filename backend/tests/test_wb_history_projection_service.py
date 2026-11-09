"""Dormant factory admission; no substitute for real guarded PostgreSQL proof."""
import importlib

import pytest

from app.platform.integrations.user_orders_job_contract import (
    OrdersExecutionPolicy,
    OrdersJobError,
)
from app.platform.integrations.wb_history_projection_role import (
    HistoryProjectionRoleIdentity,
)


def roles():
    return HistoryProjectionRoleIdentity(1001, "synthetic_runtime", 1002, "synthetic_helper")


def module():
    try:
        return importlib.import_module("app.platform.integrations.wb_history_projection")
    except ModuleNotFoundError as error:
        if error.name != "app.platform.integrations.wb_history_projection":
            raise
        pytest.fail("Genuine history projection service is unavailable")


def test_dormant_service_requires_explicit_dependencies_without_io():
    def forbidden(**kwargs):
        pytest.fail("Factory admission must not invoke dependencies")

    service = module().WbHistoryProjectionJobs(session_factory=forbidden,
        execution_policy=OrdersExecutionPolicy("synthetic-explicit", 1, 2, 30, (1,)),
        authority_deadline_provider=forbidden, max_selection_pages=2, role_identity=roles())
    assert "redacted" in repr(service)


@pytest.mark.parametrize("missing", ["execution_policy", "authority_deadline_provider", "max_selection_pages"])
def test_dormant_service_has_no_authority_defaults(missing):
    values = {"session_factory": lambda: None,
        "execution_policy": OrdersExecutionPolicy("synthetic-explicit", 1, 2, 30, (1,)),
        "authority_deadline_provider": lambda **kwargs: None, "max_selection_pages": 2, "role_identity": roles()}
    del values[missing]
    with pytest.raises(TypeError):
        module().WbHistoryProjectionJobs(**values)


def test_dormant_service_requires_explicit_role_identity():
    with pytest.raises(TypeError):
        module().WbHistoryProjectionJobs(session_factory=lambda: None,
            execution_policy=OrdersExecutionPolicy("synthetic-explicit", 1, 2, 30, (1,)),
            authority_deadline_provider=lambda **values: None, max_selection_pages=2)


def test_wrapper_rejects_duck_authority_before_data_capture():
    with pytest.raises(OrdersJobError, match="^JOB_FENCE_INVALID$"):
        module().WbHistoryProjectionHandle(object())


@pytest.mark.parametrize("name", ["apply_history_initial_parent", "load_history_reconciliation_count"])
def test_narrow_history_helpers_reject_duck_authority_before_sql(name):
    helper = getattr(module(), name, None)
    assert helper is not None, "Typed history participant helper is missing"
    values = {"handle": object(), "sync_run_id": 1}
    if name == "apply_history_initial_parent":
        values["order_id"] = 2
    with pytest.raises(OrdersJobError, match="^JOB_FENCE_INVALID$"):
        helper(None, **values)


@pytest.mark.parametrize("kind,code", [("unexpected", "JOB_PERSISTENCE_FAILED"), ("domain", "JOB_CONFLICT"),
    ("known", "JOB_FENCE_INVALID"), ("chained", "JOB_FENCE_INVALID")])
def test_participant_error_normalizer_drops_marker_and_exception_context(kind, code):
    from app.modules.orders import OrderContractValidationError

    call = getattr(module(), "_bounded_call", None)
    assert call is not None, "Private participant/store error boundary is missing"
    marker = "synthetic-secret-marker-never-return"
    def fail():
        if kind == "domain":
            raise OrderContractValidationError(marker)
        if kind == "known":
            raise OrdersJobError("JOB_FENCE_INVALID")
        if kind == "chained":
            try:
                raise RuntimeError(marker)
            except RuntimeError as original:
                raise OrdersJobError("JOB_FENCE_INVALID") from original
        raise RuntimeError(marker)

    with pytest.raises(OrdersJobError) as caught:
        call(fail)
    assert caught.value.code == code
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    assert marker not in repr(caught.value) + str(caught.value)


def test_public_receipt_reader_discards_sql_exception_chain(monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    def fail(*args, **kwargs):
        raise SQLAlchemyError("synthetic-receipt-sql-marker")
    monkeypatch.setattr(module().store, "read_receipt", fail)
    with pytest.raises(OrdersJobError) as caught:
        module().load_history_projection_receipt(None, organization_id=1, marketplace_account_id=2, sync_run_id=3)
    assert caught.value.code == "JOB_PERSISTENCE_FAILED"
    assert caught.value.__context__ is None and caught.value.__cause__ is None


def test_deadline_callback_error_is_closed_without_retained_context():
    call = getattr(module(), "_deadline_call", None)
    assert call is not None, "Closed deadline callback boundary is missing"
    def fail(**kwargs):
        raise RuntimeError("synthetic-deadline-marker")
    with pytest.raises(OrdersJobError) as caught:
        call(fail, {})
    assert caught.value.code == "JOB_CONTRACT_INVALID"
    assert caught.value.__context__ is None and caught.value.__cause__ is None


def test_new_public_root_boundary_discards_legacy_safe_error_chain(monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def failed_root(factory):
        try:
            raise RuntimeError("synthetic-root-error-marker")
        except RuntimeError as original:
            raise OrdersJobError("READBACK_REQUIRED") from original
        yield  # pragma: no cover -- explicit failing entry, never fake positive authority.

    monkeypatch.setattr(module().authority, "_root", failed_root)
    service = module().WbHistoryProjectionJobs(session_factory=lambda: None,
        execution_policy=OrdersExecutionPolicy("synthetic-explicit", 1, 2, 30, (1,)),
        authority_deadline_provider=lambda **values: None, max_selection_pages=2, role_identity=roles())
    with pytest.raises(OrdersJobError) as caught:
        service.publish_chunk(claim=None, participant=lambda *args, **kwargs: None)
    assert caught.value.code == "READBACK_REQUIRED"
    assert caught.value.__context__ is None and caught.value.__cause__ is None
