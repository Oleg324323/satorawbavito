"""Pure Stage4A assignment command encoding; no repository/CAS claims."""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.modules.wb_repricing_assignments import AssignmentChange


def command(**changes):
    values = {
        "organization_id": 7,
        "marketplace_account_id": 42,
        "catalog_sku_id": 99,
        "actor_membership_id": 77,
        "command_id": "12345678-1234-4234-8234-123456789abc",
        "expected_version": 0,
        "strategy_id": "plan_fact_interval",
        "interval_hours": 6,
        "assigned_at": datetime(2026, 9, 9, 3, tzinfo=UTC),
        "source": "manual",
    }
    return AssignmentChange(**(values | changes))


def test_canonical_bytes_have_explicit_owner_actor_version_and_full_assignment():
    assert command().canonical_bytes == (
        b'{"actorMembershipId":77,"assignedAt":"2026-09-09T03:00:00.000000Z",'
        b'"catalogSkuId":99,"commandId":"12345678-1234-4234-8234-123456789abc",'
        b'"commandKind":"replace_assignment","expectedVersion":"0","intervalHours":6,'
        b'"marketplaceAccountId":42,"organizationId":7,"schema":"wb-repricing-assignment/v1",'
        b'"source":"manual","strategyId":"plan_fact_interval"}'
    )
    assert command().checksum == command().checksum
    with pytest.raises(FrozenInstanceError):
        command().expected_version = 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("organization_id", 8),
        ("marketplace_account_id", 43),
        ("catalog_sku_id", 100),
        ("actor_membership_id", 78),
        ("expected_version", 1),
        ("interval_hours", 12),
        ("source", "legacy_import"),
        ("assigned_at", datetime(2026, 9, 10, tzinfo=UTC)),
        ("command_id", "12345678-1234-4234-8234-123456789abd"),
    ],
)
def test_changed_intent_cannot_reuse_same_checksum(field, value):
    assert command(**{field: value}).checksum != command().checksum


@pytest.mark.parametrize(
    "strategy", [None, "baskets_orders", "night_price_mode", "illiquid"]
)
def test_supported_assignment_or_explicit_clear_has_no_invented_interval(strategy):
    change = command(strategy_id=strategy, interval_hours=None)
    assert change.strategy_id == strategy and change.interval_hours is None
    assert change.checksum != command().checksum


@pytest.mark.parametrize(
    "field",
    [
        "organization_id",
        "marketplace_account_id",
        "catalog_sku_id",
        "actor_membership_id",
    ],
)
@pytest.mark.parametrize("value", [True, 0, -1, "42", 1.0, 2**31])
def test_internal_owner_ids_are_exact_positive_int4(field, value):
    with pytest.raises(ValueError):
        command(**{field: value})


@pytest.mark.parametrize(
    "changes",
    [
        {"strategy_id": "unknown"},
        {"strategy_id": "bundles"},
        {"strategy_id": "cross_marketplace"},
        {"strategy_id": None},
        {"interval_hours": None},
        {"interval_hours": 0},
        {"interval_hours": True},
        {"interval_hours": 1.5},
        {"interval_hours": 2**31},
        {"source": "scheduler"},
        {"command_id": ""},
        {"command_id": "not-a-uuid"},
        {"expected_version": -1},
        {"expected_version": True},
        {"expected_version": 1.0},
        {"assigned_at": datetime(2026, 9, 9)},  # noqa: DTZ001 -- intentional rejection
    ],
)
def test_unknown_or_inconsistent_inputs_block_instead_of_defaulting(changes):
    with pytest.raises(ValueError):
        command(**changes)


def test_timezone_normalization_preserves_instant_and_request_identity():
    same_instant = datetime(2026, 9, 9, 6, tzinfo=timezone(timedelta(hours=3)))
    assert (
        command(assigned_at=same_instant).canonical_bytes == command().canonical_bytes
    )
    assert command(assigned_at=same_instant).assigned_at.tzinfo is UTC


def test_expected_version_is_exact_decimal_text_without_bigint_narrowing():
    assert (
        b'"expectedVersion":"1208925819614629174706176"'
        in command(expected_version=2**80).canonical_bytes
    )
    assert replace(command(), expected_version=2**80).checksum != command().checksum
