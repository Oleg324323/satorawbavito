from __future__ import annotations

import ast
import inspect
from datetime import datetime, timedelta, timezone

import pytest

from app.modules import wb_repricing_repository
from app.modules.wb_repricing import (
    ApprovalStatus,
    ApprovalValidationError,
    PriceApprovalSnapshot,
    build_action_key,
    claim_approval,
    record_apply_failure,
    record_apply_success,
)
from app.modules.wb_repricing_repository import (
    UNRESOLVED_LEGACY_IDENTITY_BLOCKER,
    ApprovalIdentityBlockedError,
    ApprovalOutcomeCommand,
    ApprovalRepositoryScope,
    AuthenticatedApprovalActor,
    PriceApprovalRepository,
    bridge_actor_membership_id,
    bridge_internal_id,
    bridge_snapshot_identity,
)


CHECKSUM = "a" * 64
CREATED_AT = datetime(2026, 9, 8, 9, 0, tzinfo=timezone.utc)
TRANSITION_AT = CREATED_AT + timedelta(minutes=1)


def _pending_snapshot(
    *,
    organization_id: int = 7,
    marketplace_account_id: str = "42",
    approval_id: str = "price-apr-legacy-1",
    catalog_sku_id: str | None = "99",
) -> PriceApprovalSnapshot:
    return PriceApprovalSnapshot(
        organization_id=organization_id,
        marketplace_account_id=marketplace_account_id,
        approval_id=approval_id,
        catalog_sku_id=catalog_sku_id,
        nm_id=123456,
        article_id="FBBT_42",
        recommended_price_kopecks=129_900,
        request_checksum=CHECKSUM,
        status=ApprovalStatus.pending,
        version=0,
        action_key=build_action_key(
            organization_id,
            marketplace_account_id,
            approval_id,
            CHECKSUM,
        ),
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
    )


def test_internal_ids_bridge_to_plain_decimal_kernel_identity() -> None:
    scope = ApprovalRepositoryScope(
        organization_id=7,
        marketplace_account_id=42,
        approval_id="price-apr-legacy-1",
    )
    actor = AuthenticatedApprovalActor(organization_id=7, membership_id=314)

    assert bridge_internal_id(42) == "42"
    assert scope.kernel_marketplace_account_id == "42"
    assert bridge_actor_membership_id(scope, actor) == "314"


@pytest.mark.parametrize("invalid", [None, True, False, 0, -1, 1.0, "42"])
def test_bridge_rejects_non_internal_or_non_positive_ids(invalid: object) -> None:
    with pytest.raises(ApprovalIdentityBlockedError) as exc_info:
        bridge_internal_id(invalid)  # type: ignore[arg-type]

    assert exc_info.value.blocker_code == UNRESOLVED_LEGACY_IDENTITY_BLOCKER
    assert str(invalid) not in str(exc_info.value)


@pytest.mark.parametrize("field", ["organization_id", "marketplace_account_id"])
def test_repository_scope_rejects_legacy_external_identity_strings(field: str) -> None:
    values: dict[str, object] = {
        "organization_id": 7,
        "marketplace_account_id": 42,
        "approval_id": "price-apr-legacy-1",
    }
    values[field] = "external-account-slug"

    with pytest.raises(ApprovalIdentityBlockedError) as exc_info:
        ApprovalRepositoryScope(**values)  # type: ignore[arg-type]

    assert exc_info.value.blocker_code == UNRESOLVED_LEGACY_IDENTITY_BLOCKER
    assert "external-account-slug" not in str(exc_info.value)


def test_snapshot_bridge_preserves_checksum_action_key_and_object_identity() -> None:
    scope = ApprovalRepositoryScope(7, 42, "price-apr-legacy-1")
    snapshot = _pending_snapshot()
    action_key = snapshot.action_key
    request_checksum = snapshot.request_checksum

    bridged = bridge_snapshot_identity(
        scope=scope,
        catalog_sku_id=99,
        snapshot=snapshot,
    )

    assert bridged is snapshot
    assert bridged.action_key == action_key
    assert bridged.request_checksum == request_checksum


@pytest.mark.parametrize(
    ("scope", "catalog_sku_id", "snapshot"),
    [
        (
            ApprovalRepositoryScope(8, 42, "price-apr-legacy-1"),
            99,
            _pending_snapshot(),
        ),
        (
            ApprovalRepositoryScope(7, 43, "price-apr-legacy-1"),
            99,
            _pending_snapshot(),
        ),
        (
            ApprovalRepositoryScope(7, 42, "price-apr-other"),
            99,
            _pending_snapshot(),
        ),
        (
            ApprovalRepositoryScope(7, 42, "price-apr-legacy-1"),
            100,
            _pending_snapshot(),
        ),
    ],
)
def test_snapshot_bridge_blocks_scope_or_catalog_mismatch(
    scope: ApprovalRepositoryScope,
    catalog_sku_id: int | None,
    snapshot: PriceApprovalSnapshot,
) -> None:
    with pytest.raises(ApprovalIdentityBlockedError) as exc_info:
        bridge_snapshot_identity(
            scope=scope,
            catalog_sku_id=catalog_sku_id,
            snapshot=snapshot,
        )

    assert exc_info.value.blocker_code == UNRESOLVED_LEGACY_IDENTITY_BLOCKER


def test_snapshot_bridge_rejects_unresolved_catalog_identity() -> None:
    scope = ApprovalRepositoryScope(7, 42, "price-apr-legacy-1")

    with pytest.raises(ApprovalIdentityBlockedError):
        bridge_snapshot_identity(
            scope=scope,
            catalog_sku_id="seller-article",  # type: ignore[arg-type]
            snapshot=_pending_snapshot(),
        )


def test_actor_bridge_rejects_membership_from_another_organization() -> None:
    scope = ApprovalRepositoryScope(7, 42, "price-apr-legacy-1")
    actor = AuthenticatedApprovalActor(organization_id=8, membership_id=314)

    with pytest.raises(ApprovalIdentityBlockedError):
        bridge_actor_membership_id(scope, actor)


@pytest.mark.parametrize("membership_id", [None, True, 0, -1, "314"])
def test_authenticated_actor_requires_positive_internal_membership_id(
    membership_id: object,
) -> None:
    with pytest.raises(ApprovalIdentityBlockedError):
        AuthenticatedApprovalActor(
            organization_id=7,
            membership_id=membership_id,  # type: ignore[arg-type]
        )


def test_outcome_command_requires_attempt_and_exact_next_version() -> None:
    pending = _pending_snapshot()
    applying = claim_approval(pending, 0, "314", TRANSITION_AT)
    applied = record_apply_success(
        applying,
        1,
        "upload-1",
        "WB_APPLY_ACCEPTED",
        TRANSITION_AT + timedelta(minutes=1),
    )

    command = ApprovalOutcomeCommand(
        attempt_id="attempt-1",
        expected_version=1,
        outcome=applied,
    )

    assert command.expected_version == 1
    assert command.outcome.version == 2


@pytest.mark.parametrize("attempt_id", ["", " "])
def test_outcome_command_rejects_blank_attempt_id(attempt_id: str) -> None:
    applying = claim_approval(_pending_snapshot(), 0, "314", TRANSITION_AT)
    failed = record_apply_failure(
        applying,
        1,
        "WB_APPLY_TIMEOUT",
        False,
        TRANSITION_AT + timedelta(minutes=1),
    )

    with pytest.raises(ApprovalValidationError):
        ApprovalOutcomeCommand(attempt_id, 1, failed)


def test_outcome_command_rejects_non_apply_terminal_and_wrong_version() -> None:
    pending = _pending_snapshot()

    with pytest.raises(ApprovalValidationError):
        ApprovalOutcomeCommand("attempt-1", 0, pending)

    applying = claim_approval(pending, 0, "314", TRANSITION_AT)
    ambiguous = record_apply_failure(
        applying,
        1,
        "WB_APPLY_TIMEOUT",
        True,
        TRANSITION_AT + timedelta(minutes=1),
    )
    with pytest.raises(ApprovalValidationError):
        ApprovalOutcomeCommand("attempt-1", 0, ambiguous)


def test_repository_protocol_has_no_uuid_only_lookup_or_provider_method() -> None:
    expected_parameters = {
        "get": ("self", "scope"),
        "insert": ("self", "scope", "catalog_sku_id", "snapshot"),
        "claim": ("self", "scope", "expected_version", "actor", "now"),
        "record_outcome": ("self", "scope", "command"),
    }

    assert {
        name: tuple(inspect.signature(getattr(PriceApprovalRepository, name)).parameters)
        for name in expected_parameters
    } == expected_parameters
    assert not any(
        name in PriceApprovalRepository.__dict__
        for name in ("get_by_id", "get_by_uuid", "apply", "send", "retry")
    )


def test_repository_contract_is_pure_and_has_no_runtime_or_framework_imports() -> None:
    source = inspect.getsource(wb_repricing_repository)
    tree = ast.parse(source)
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    forbidden = {
        "celery",
        "fastapi",
        "sqlalchemy",
        "app.repricer_bff",
        "app.repricer_execution",
        "app.repricer_persistence",
        "app.repricer_sprint_b",
        "app.repricer_sprint_c",
        "app.repricer_tasks",
        "app.wb22_apply",
        "app.wb_api",
    }
    assert not {
        imported
        for imported in imported_modules
        if any(
            imported == prefix or imported.startswith(f"{prefix}.")
            for prefix in forbidden
        )
    }
