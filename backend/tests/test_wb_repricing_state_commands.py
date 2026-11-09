import json
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from app.modules.wb_repricing_state_commands import (
    AlgorithmSettingsChange,
    AlgorithmSettingsValues,
    BasketNormDefault,
    LiquidationCampaign,
    LiquidationChange,
    LiquidationRevision,
    LiquidationValues,
    StateCommandConflictError,
    StateCommandValidationError,
    validate_liquidation_transition,
)


def vectors():
    return json.loads(
        (
            Path(__file__).parent / "fixtures/wb_repricing_state_golden_v1.json"
        ).read_text()
    )


def command(vector):
    data = dict(vector["input"])
    values = dict(data["values"])
    if vector["kind"] == "settings":
        for field in fields(AlgorithmSettingsValues):
            if field.type is Decimal:
                values[field.name] = Decimal(values[field.name])
        data["values"] = AlgorithmSettingsValues(**values)
        data["basket_defaults"] = tuple(
            BasketNormDefault(**row) for row in data["basket_defaults"]
        )
        return AlgorithmSettingsChange(**data)
    campaign = dict(data["campaign"])
    campaign["created_at"] = datetime.fromisoformat(campaign["created_at"])
    data["campaign"] = LiquidationCampaign(**campaign)
    values["step_pct"] = Decimal(values["step_pct"])
    for key in ("confirmed_at", "next_step_at"):
        if values[key] is not None:
            values[key] = datetime.fromisoformat(values[key])
    data["values"] = LiquidationValues(**values)
    return LiquidationChange(**data)


@pytest.mark.parametrize("index", range(5))
def test_literal_state_vectors(index):
    fixture = vectors()
    row = fixture["vectors"][index]
    value = command(row)
    with localcontext() as ctx:
        ctx.prec = 3
        raw = value.canonical_bytes(max_bytes=fixture["synthetic_max_bytes"])
        assert raw == row["canonical_ascii"].encode("ascii")
        assert len(raw) == row["byte_count"]
        assert value.checksum(max_bytes=fixture["synthetic_max_bytes"]) == row["sha256"]


@pytest.mark.parametrize(
    "field", [field.name for field in fields(AlgorithmSettingsValues)]
)
def test_every_setting_is_explicit_nonnullable(field):
    with pytest.raises(StateCommandValidationError):
        replace(command(vectors()["vectors"][0]).values, **{field: None})


@pytest.mark.parametrize(
    "change",
    [
        {"night_median_enabled": 1},
        {"price_step_pct": 1.0},
        {"target_margin_pct": Decimal("NaN")},
        {"target_margin_pct": Decimal("Infinity")},
        {"sync_interval_minutes": 0},
        {"warmup_days": True},
        {"night_median_window_start_hour": 24},
        {"warmup_exit_baskets": -1},
        {"night_median_mode": "unknown"},
        {"basket_signal_mode": "unknown"},
        {"basket_norm_mode": "unknown"},
        {"plan_fact_metric": "unknown"},
        {"night_median_timezone": " "},
        {"night_median_timezone": "x\x00y"},
    ],
)
def test_invalid_setting_values_are_rejected(change):
    with pytest.raises(StateCommandValidationError):
        replace(command(vectors()["vectors"][0]).values, **change)


def test_no_new_percent_clamps_and_child_rows_sorted_and_detached():
    original = command(vectors()["vectors"][0])
    children = list(reversed(original.basket_defaults))
    same = replace(original, basket_defaults=children)
    children.clear()
    assert same.canonical_bytes(max_bytes=16384) == original.canonical_bytes(
        max_bytes=16384
    )
    changed = replace(
        original, values=replace(original.values, price_step_pct=Decimal(-1234))
    )
    assert changed.checksum(max_bytes=16384) != original.checksum(max_bytes=16384)
    with pytest.raises(StateCommandValidationError):
        replace(original, basket_defaults=original.basket_defaults[:2])
    with pytest.raises(StateCommandValidationError):
        replace(
            original,
            basket_defaults=original.basket_defaults + original.basket_defaults[:1],
        )


@pytest.mark.parametrize("index", range(5))
def test_owner_actor_version_and_budget_are_bound(index):
    value = command(vectors()["vectors"][index])
    digest = value.checksum(max_bytes=16384)
    for changes in (
        {"actor_membership_id": 8},
        {"expected_version": value.expected_version + 1},
        {"command_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
    ):
        # The creation actor must equal immutable campaign origin.
        if index == 2 and "actor_membership_id" in changes:
            with pytest.raises(StateCommandValidationError):
                replace(value, **changes)
        else:
            assert replace(value, **changes).checksum(max_bytes=16384) != digest
    with pytest.raises(StateCommandValidationError):
        replace(value, actor_membership_id=True)
    with pytest.raises(StateCommandValidationError):
        replace(value, expected_version=-1)
    with pytest.raises(StateCommandValidationError):
        value.canonical_bytes(max_bytes=1)
    with pytest.raises(StateCommandValidationError):
        value.canonical_bytes(max_bytes=True)


def test_large_decimal_budget_checked_before_expansion():
    value = command(vectors()["vectors"][0])
    for number in (Decimal("1e999999999"), Decimal("1e-999999999")):
        huge = replace(value, values=replace(value.values, target_margin_pct=number))
        with pytest.raises(StateCommandValidationError):
            huge.canonical_bytes(max_bytes=16384)


@pytest.mark.parametrize("before", ["active", "paused", "completed", "cancelled"])
@pytest.mark.parametrize("after", ["active", "paused", "completed", "cancelled"])
def test_liquidation_transition_matrix(before, after):
    active = command(vectors()["vectors"][2])

    def values(state):
        return replace(
            active.values,
            state=state,
            next_step_at=active.values.next_step_at if state == "active" else None,
        )

    old = LiquidationRevision(active.campaign, 1, values(before))
    change = replace(active, expected_version=1, values=values(after))
    allowed = before == "active" or (before == "paused" and after != "paused")
    if allowed:
        assert validate_liquidation_transition(old, change) is None
    else:
        with pytest.raises(StateCommandConflictError):
            validate_liquidation_transition(old, change)


def test_liquidation_creation_stale_scope_and_confirmation_binding():
    new = command(vectors()["vectors"][2])
    validate_liquidation_transition(None, new)
    previous = LiquidationRevision(new.campaign, 1, new.values)
    with pytest.raises(StateCommandConflictError):
        validate_liquidation_transition(previous, new)
    with pytest.raises(StateCommandConflictError):
        validate_liquidation_transition(None, replace(new, expected_version=1))
    foreign = replace(
        new,
        expected_version=1,
        campaign=replace(new.campaign, marketplace_account_id=9),
    )
    with pytest.raises(StateCommandConflictError):
        validate_liquidation_transition(previous, foreign)
    for changed in ({"target_price_kopecks": 60000}, {"step_pct": Decimal(4)}):
        values = replace(
            new.values,
            confirmed_by_membership_id=4,
            confirmed_at=new.campaign.created_at,
            **changed,
        )
        with pytest.raises(StateCommandValidationError):
            validate_liquidation_transition(
                previous, replace(new, expected_version=1, values=values)
            )
        cleared = replace(values, confirmed_by_membership_id=None, confirmed_at=None)
        validate_liquidation_transition(
            previous, replace(new, expected_version=1, values=cleared)
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"current_price_kopecks": 0},
        {"target_price_kopecks": True},
        {"step_pct": Decimal(0)},
        {"step_pct": Decimal("NaN")},
        {"step_pct": 3.0},
        {"hold_orders_to": -1},
        {"next_step_at": None},
        {"state": "paused"},
        {"confirmed_by_membership_id": 4},
        {"requires_negative_margin_confirm": 1},
        {"resulting_approval_id": " "},
        {"next_step_at": datetime(2026, 1, 1)},  # noqa: DTZ001 -- deliberate invalid input
    ],
)
def test_liquidation_invalid_metadata(changes):
    with pytest.raises(StateCommandValidationError):
        replace(command(vectors()["vectors"][2]).values, **changes)


def test_same_instants_produce_same_command_bytes():
    value = command(vectors()["vectors"][2])
    offset = timezone(timedelta(hours=3))
    changed = replace(
        value,
        campaign=replace(
            value.campaign, created_at=value.campaign.created_at.astimezone(offset)
        ),
        values=replace(
            value.values, next_step_at=value.values.next_step_at.astimezone(offset)
        ),
    )
    assert changed.canonical_bytes(max_bytes=16384) == value.canonical_bytes(
        max_bytes=16384
    )
