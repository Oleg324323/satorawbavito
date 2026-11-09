"""Exact Stage4A settings/liquidation command bytes, not persistence or authority.

Every operational value is supplied explicitly. These preferences do not activate
workers or authorize a price action. Repositories must validate authenticated
ownership, scoped historical replay, revision/head CAS and atomic audit separately.
"""

import hashlib
import json
import re
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from decimal import Decimal

from app.modules.wb_repricing_overrides import (
    OverrideCommandValidationError,
    _decimal_text,
)


class StateCommandValidationError(ValueError):
    def __init__(self):
        super().__init__("invalid_repricing_state_command")


class StateCommandConflictError(ValueError):
    def __init__(self):
        super().__init__("repricing_state_conflict")


def _integer(value, minimum=0, maximum=None):
    if (
        type(value) is not int
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        raise StateCommandValidationError()


def _internal_id(value):
    _integer(value, 1, 2**31 - 1)


def _text(value):
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or "\x00" in value
        or any(0xD800 <= ord(char) <= 0xDFFF for char in value)
    ):
        raise StateCommandValidationError()


def _uuid(value):
    if (
        type(value) is not str
        or re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            value,
        )
        is None
    ):
        raise StateCommandValidationError()


def _time(value):
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise StateCommandValidationError()
    try:
        return value.astimezone(UTC)
    except (ValueError, OverflowError):
        raise StateCommandValidationError() from None


def _timestamp(value):
    return _time(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _decimal(value, *, positive=False):
    if type(value) is not Decimal or not value.is_finite() or (positive and value <= 0):
        raise StateCommandValidationError()


def _number(value, budget):
    try:
        return _decimal_text(Decimal(value), budget)
    except OverrideCommandValidationError:
        raise StateCommandValidationError() from None


def _encode(payload, budget):
    _integer(budget, 1)
    result = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    if len(result) > budget:
        raise StateCommandValidationError()
    return result


@dataclass(frozen=True, slots=True)
class BasketNormDefault:
    garment: str
    norm_units: int

    def __post_init__(self):
        if type(self.garment) is not str or self.garment not in (
            "tshirt",
            "hoodie",
            "longsleeve",
        ):
            raise StateCommandValidationError()
        _integer(self.norm_units, 0, 2**31 - 1)


@dataclass(frozen=True, slots=True)
class AlgorithmSettingsValues:
    night_median_enabled: bool
    night_median_collect_enabled: bool
    night_median_global: bool
    night_median_auto_apply_enabled: bool
    worker_auto_apply_prices_enabled: bool
    min_price_sync_enabled: bool
    price_jump_protection_enabled: bool
    discount_step_enabled: bool
    price_rounding_enabled: bool
    liquidation_auto_flag_enabled: bool
    target_margin_pct: Decimal
    price_step_pct: Decimal
    max_price_change_daily_pct: Decimal
    promo_margin_threshold_pct: Decimal
    price_jump_stock_value_min_pct: Decimal
    price_jump_spp_min_pct: Decimal
    price_jump_stock_qty_min_pct: Decimal
    csv_max_cost_drop_pct: Decimal
    csv_max_price_drop_pct: Decimal
    discount_step_pct: Decimal
    night_median_apply_delta_pct: Decimal
    warmup_margin_pct: Decimal
    warmup_daily_limit_pct: Decimal
    liquidation_step_pct: Decimal
    liquidation_min_cogs_pct: Decimal
    sync_interval_minutes: int
    basket_norm_period_days: int
    cart_comparison_days: int
    plan_fact_fact_period_days: int
    plan_fact_interval_hours: int
    warmup_days: int
    cart_high_baskets_threshold: int
    cart_low_baskets_threshold: int
    basket_norm_auto_min_orders: int
    warmup_exit_baskets: int
    night_median_window_start_hour: int
    night_median_window_end_hour: int
    night_median_mode: str
    basket_signal_mode: str
    basket_norm_mode: str
    plan_fact_metric: str
    night_median_timezone: str

    def __post_init__(self):
        enums = {
            "night_median_mode": ("conservative", "aggressive"),
            "basket_signal_mode": ("matrix", "thresholds"),
            "basket_norm_mode": ("fallback_by_type", "auto", "manual"),
            "plan_fact_metric": ("orders", "revenue", "margin"),
        }
        positive = (
            "sync_interval_minutes",
            "basket_norm_period_days",
            "cart_comparison_days",
            "plan_fact_fact_period_days",
            "plan_fact_interval_hours",
            "warmup_days",
        )
        for field in fields(self):
            value = getattr(self, field.name)
            if field.type is bool:
                if type(value) is not bool:
                    raise StateCommandValidationError()
            elif field.type is Decimal:
                _decimal(value)
            elif field.type is int:
                maximum = (
                    23 if field.name.startswith("night_median_window_") else 2**31 - 1
                )
                _integer(value, 1 if field.name in positive else 0, maximum)
            elif field.name in enums:
                if type(value) is not str or value not in enums[field.name]:
                    raise StateCommandValidationError()
            else:
                _text(value)


@dataclass(frozen=True, slots=True)
class AlgorithmSettingsChange:
    organization_id: int
    actor_membership_id: int
    command_id: str
    expected_version: int
    formula_compatibility_version: str
    values: AlgorithmSettingsValues
    basket_defaults: tuple[BasketNormDefault, ...]

    def __post_init__(self):
        _internal_id(self.organization_id)
        _internal_id(self.actor_membership_id)
        _uuid(self.command_id)
        _integer(self.expected_version)
        _text(self.formula_compatibility_version)
        if type(self.values) is not AlgorithmSettingsValues:
            raise StateCommandValidationError()
        if type(self.basket_defaults) not in (list, tuple) or any(
            type(row) is not BasketNormDefault for row in self.basket_defaults
        ):
            raise StateCommandValidationError()
        names = [row.garment for row in self.basket_defaults]
        if len(names) != len(set(names)):
            raise StateCommandValidationError()
        if self.values.basket_norm_mode == "fallback_by_type" and set(names) != {
            "tshirt",
            "hoodie",
            "longsleeve",
        }:
            raise StateCommandValidationError()
        object.__setattr__(
            self,
            "basket_defaults",
            tuple(sorted(self.basket_defaults, key=lambda row: row.garment)),
        )

    def canonical_bytes(self, *, max_bytes):
        _integer(max_bytes, 1)
        row = {
            field.name: (
                _number(getattr(self.values, field.name), max_bytes)
                if field.type is Decimal
                else getattr(self.values, field.name)
            )
            for field in fields(self.values)
        }
        return _encode(
            {
                "schema": "wb-repricing-algorithm-settings/v1",
                "commandKind": "replace_settings",
                "organizationId": self.organization_id,
                "actorMembershipId": self.actor_membership_id,
                "commandId": self.command_id,
                "expectedVersion": _number(self.expected_version, max_bytes),
                "formulaCompatibilityVersion": self.formula_compatibility_version,
                "values": row,
                "basketDefaults": [
                    {"garment": item.garment, "normUnits": item.norm_units}
                    for item in self.basket_defaults
                ],
            },
            max_bytes,
        )

    def checksum(self, *, max_bytes):
        return hashlib.sha256(self.canonical_bytes(max_bytes=max_bytes)).hexdigest()


@dataclass(frozen=True, slots=True)
class LiquidationCampaign:
    organization_id: int
    marketplace_account_id: int
    catalog_sku_id: int
    campaign_id: str
    started_by_membership_id: int
    created_at: datetime
    start_price_kopecks: int

    def __post_init__(self):
        for value in (
            self.organization_id,
            self.marketplace_account_id,
            self.catalog_sku_id,
            self.started_by_membership_id,
        ):
            _internal_id(value)
        _uuid(self.campaign_id)
        _integer(self.start_price_kopecks, 1)
        object.__setattr__(self, "created_at", _time(self.created_at))


@dataclass(frozen=True, slots=True)
class LiquidationValues:
    state: str
    current_price_kopecks: int
    target_price_kopecks: int
    step_pct: Decimal
    hold_orders_to: int
    next_step_at: datetime | None
    requires_negative_margin_confirm: bool
    confirmed_by_membership_id: int | None
    confirmed_at: datetime | None
    resulting_approval_id: str | None

    def __post_init__(self):
        if type(self.state) is not str or self.state not in (
            "active",
            "paused",
            "completed",
            "cancelled",
        ):
            raise StateCommandValidationError()
        _integer(self.current_price_kopecks, 1)
        _integer(self.target_price_kopecks, 1)
        _decimal(self.step_pct, positive=True)
        _integer(self.hold_orders_to, 0, 2**31 - 1)
        if type(self.requires_negative_margin_confirm) is not bool:
            raise StateCommandValidationError()
        if self.state == "active":
            object.__setattr__(self, "next_step_at", _time(self.next_step_at))
        elif self.next_step_at is not None:
            raise StateCommandValidationError()
        if (self.confirmed_by_membership_id is None) != (self.confirmed_at is None):
            raise StateCommandValidationError()
        if self.confirmed_at is not None:
            _internal_id(self.confirmed_by_membership_id)
            object.__setattr__(self, "confirmed_at", _time(self.confirmed_at))
        if self.resulting_approval_id is not None:
            _text(self.resulting_approval_id)


@dataclass(frozen=True, slots=True)
class LiquidationChange:
    campaign: LiquidationCampaign
    actor_membership_id: int
    command_id: str
    expected_version: int
    values: LiquidationValues

    def __post_init__(self):
        if (
            type(self.campaign) is not LiquidationCampaign
            or type(self.values) is not LiquidationValues
        ):
            raise StateCommandValidationError()
        _internal_id(self.actor_membership_id)
        _uuid(self.command_id)
        _integer(self.expected_version)
        if self.expected_version == 0 and (
            self.values.state != "active"
            or self.actor_membership_id != self.campaign.started_by_membership_id
        ):
            raise StateCommandValidationError()

    def canonical_bytes(self, *, max_bytes):
        _integer(max_bytes, 1)
        campaign = self.campaign
        values = self.values
        return _encode(
            {
                "schema": "wb-repricing-liquidation/v1",
                "commandKind": "replace_liquidation",
                "organizationId": campaign.organization_id,
                "marketplaceAccountId": campaign.marketplace_account_id,
                "catalogSkuId": campaign.catalog_sku_id,
                "campaignId": campaign.campaign_id,
                "actorMembershipId": self.actor_membership_id,
                "commandId": self.command_id,
                "expectedVersion": _number(self.expected_version, max_bytes),
                "campaign": {
                    "startedByMembershipId": campaign.started_by_membership_id,
                    "createdAt": _timestamp(campaign.created_at),
                    "startPriceKopecks": _number(
                        campaign.start_price_kopecks, max_bytes
                    ),
                },
                "values": {
                    "state": values.state,
                    "current_price_kopecks": _number(
                        values.current_price_kopecks, max_bytes
                    ),
                    "target_price_kopecks": _number(
                        values.target_price_kopecks, max_bytes
                    ),
                    "step_pct": _number(values.step_pct, max_bytes),
                    "hold_orders_to": values.hold_orders_to,
                    "next_step_at": _timestamp(values.next_step_at)
                    if values.next_step_at is not None
                    else None,
                    "requires_negative_margin_confirm": values.requires_negative_margin_confirm,
                    "confirmed_by_membership_id": values.confirmed_by_membership_id,
                    "confirmed_at": _timestamp(values.confirmed_at)
                    if values.confirmed_at is not None
                    else None,
                    "resulting_approval_id": values.resulting_approval_id,
                },
            },
            max_bytes,
        )

    def checksum(self, *, max_bytes):
        return hashlib.sha256(self.canonical_bytes(max_bytes=max_bytes)).hexdigest()


@dataclass(frozen=True, slots=True)
class LiquidationRevision:
    campaign: LiquidationCampaign
    revision: int
    values: LiquidationValues

    def __post_init__(self):
        if (
            type(self.campaign) is not LiquidationCampaign
            or type(self.values) is not LiquidationValues
        ):
            raise StateCommandValidationError()
        _integer(self.revision, 1)


def validate_liquidation_transition(
    previous: LiquidationRevision | None, command: LiquidationChange
) -> None:
    """Validate a new revision after scoped replay lookup, never reauthorize send."""
    if type(command) is not LiquidationChange:
        raise StateCommandValidationError()
    if previous is None:
        if command.expected_version != 0:
            raise StateCommandConflictError()
        return
    if type(previous) is not LiquidationRevision:
        raise StateCommandValidationError()
    if (
        previous.campaign != command.campaign
        or previous.revision != command.expected_version
    ):
        raise StateCommandConflictError()
    allowed = {
        "active": ("active", "paused", "completed", "cancelled"),
        "paused": ("active", "completed", "cancelled"),
    }
    if command.values.state not in allowed.get(previous.values.state, ()):
        raise StateCommandConflictError()
    changed_intent = (
        previous.values.target_price_kopecks,
        previous.values.step_pct,
    ) != (
        command.values.target_price_kopecks,
        command.values.step_pct,
    )
    if (
        changed_intent
        and command.values.requires_negative_margin_confirm
        and command.values.confirmed_by_membership_id is not None
    ):
        raise StateCommandValidationError()
