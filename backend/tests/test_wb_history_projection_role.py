"""Trusted role identity inputs; these values alone confer no database rights."""
import importlib

import pytest

from app.platform.integrations.user_orders_job_contract import OrdersJobError


def identity(**changes):
    try:
        module = importlib.import_module("app.platform.integrations.wb_history_projection_role")
    except ModuleNotFoundError as error:
        if error.name != "app.platform.integrations.wb_history_projection_role":
            raise
        pytest.fail("Explicit projection role identity contract is missing")
    fields = {"runtime_oid": 1001, "runtime_name": "owned_projection",
        "helper_owner_oid": 1002, "helper_owner_name": "owned_projection_helper"}
    fields.update(changes)
    return module.HistoryProjectionRoleIdentity(**fields)


def test_separate_explicit_roles_are_immutable_and_redacted():
    from dataclasses import FrozenInstanceError

    roles = identity()
    assert roles.runtime_oid == 1001 and roles.helper_owner_oid == 1002
    assert "owned_projection" not in repr(roles)
    with pytest.raises(FrozenInstanceError):
        roles.runtime_oid = 1002


@pytest.mark.parametrize("changes", [{"runtime_oid": 0}, {"runtime_oid": True},
    {"helper_owner_oid": 2**32}, {"helper_owner_oid": 1001},
    {"helper_owner_name": "owned_projection"}, {"runtime_name": ""},
    {"runtime_name": "x" * 64}, {"runtime_name": "bad\x00name"}])
def test_invalid_or_shared_role_identity_fails_closed(changes):
    with pytest.raises(OrdersJobError, match="^JOB_CONTRACT_INVALID$"):
        identity(**changes)


def test_surrogate_role_name_is_a_safe_contract_error():
    with pytest.raises(OrdersJobError, match="^JOB_CONTRACT_INVALID$") as caught:
        identity(runtime_name="synthetic\ud800name")
    assert caught.value.__cause__ is None and caught.value.__context__ is None


def role_row(oid, name, login):
    return {"oid": oid, "rolname": name, "rolcanlogin": login, "rolsuper": False,
        "rolbypassrls": False, "rolcreaterole": False, "rolcreatedb": False,
        "rolinherit": False, "rolreplication": False}


def validate_rows(rows, *, session_oid=1001, current_oid=1001):
    module = importlib.import_module("app.platform.integrations.wb_history_projection_role")
    validate = getattr(module, "_validate_role_identity", None)
    assert validate is not None, "Actual role metadata admission is missing"
    validate(identity(), rows, session_oid=session_oid, current_oid=current_oid)


def test_metadata_admission_requires_exact_runtime_and_separate_nologin_owner():
    validate_rows((role_row(1001, "owned_projection", True), role_row(1002, "owned_projection_helper", False)))


@pytest.mark.parametrize("field,value", [("rolsuper", True), ("rolbypassrls", True),
    ("rolcreaterole", True), ("rolcreatedb", True), ("rolinherit", True),
    ("rolreplication", True), ("rolcanlogin", False), ("rolname", "substituted")])
def test_elevated_or_substituted_runtime_metadata_is_denied(field, value):
    row = role_row(1001, "owned_projection", True)
    row[field] = value
    with pytest.raises(OrdersJobError, match="^JOB_FENCE_INVALID$"):
        validate_rows((row, role_row(1002, "owned_projection_helper", False)))


@pytest.mark.parametrize("session_oid,current_oid", [(1002, 1001), (1001, 1002), (999, 999)])
def test_set_role_or_other_login_cannot_substitute_pinned_identity(session_oid, current_oid):
    with pytest.raises(OrdersJobError, match="^JOB_FENCE_INVALID$"):
        validate_rows((role_row(1001, "owned_projection", True), role_row(1002, "owned_projection_helper", False)),
            session_oid=session_oid, current_oid=current_oid)


@pytest.mark.parametrize("role,table,column,privilege,allowed", [
    ("runtime", "marketplace_orders", "order_id", "UPDATE", True),
    ("runtime", "marketplace_orders", "version", "UPDATE", False),
    ("runtime", "marketplace_orders", "version", "INSERT", False),
    ("runtime", "marketplace_orders", "external_order_id", "INSERT", True),
    ("runtime", "marketplace_account_credentials", "generation", "SELECT", True),
    ("runtime", "marketplace_account_credentials", "ciphertext", "SELECT", False),
    ("runtime", "iam_memberships", "permissions", "UPDATE", False),
    ("runtime", "marketplace_products", "updated_at", "UPDATE", True),
    ("runtime", "marketplace_products", "title", "UPDATE", False),
    ("helper", "marketplace_orders", "raw_status", "UPDATE", True),
    ("helper", "marketplace_account_credentials", "ciphertext", "SELECT", False),
    ("helper", "marketplace_orders", "external_order_id", "UPDATE", False),
])
def test_effective_column_allowlist_preserves_locks_without_business_or_secret_rights(role, table, column, privilege, allowed):
    module = importlib.import_module("app.platform.integrations.wb_history_projection_role")
    check = getattr(module, "_allowed_column", None)
    assert check is not None, "Effective role column allowlist is missing"
    assert check(role, table, column, privilege) is allowed
