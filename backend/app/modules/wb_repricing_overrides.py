"""Pure full-row SKU override commands; no persistence, authorization or formulas.

All nullable fields are explicit. Resource limits belong to the trusted caller;
decimal encoding neither rounds through the ambient context nor expands compact
exponents before checking that limit. A checksum is not a CAS or permission.
"""

import hashlib
import json
import re
from dataclasses import dataclass, fields
from decimal import Decimal


class OverrideCommandValidationError(ValueError):
    def __init__(self):
        super().__init__("invalid_override_command")


def _integer(value, minimum=0, maximum=None):
    if (
        type(value) is not int
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        raise OverrideCommandValidationError()


def _decimal_text(value: Decimal, budget: int) -> str:
    if value.is_zero():
        return "0"
    sign, digits, exponent = value.as_tuple()
    end = len(digits)
    while digits[end - 1] == 0:
        end -= 1
        exponent += 1
    point = end + exponent
    length = sign + (point if exponent >= 0 else end + 1 if point > 0 else 2 - exponent)
    if length > budget:
        raise OverrideCommandValidationError()
    coefficient = "".join(str(digit) for digit in digits[:end])
    if exponent >= 0:
        text = coefficient + "0" * exponent
    elif point > 0:
        text = coefficient[:point] + "." + coefficient[point:]
    else:
        text = "0." + "0" * -point + coefficient
    return ("-" if sign else "") + text


@dataclass(frozen=True, slots=True)
class OverrideValues:
    automation_enabled: bool | None
    allow_negative_margin: bool | None
    night_median_enabled: bool | None
    p_min_kopecks: int | None
    p_max_kopecks: int | None
    rrp_kopecks: int | None
    min_margin_kopecks: int | None
    max_margin_kopecks: int | None
    min_margin_pct: Decimal | None
    max_margin_pct: Decimal | None
    price_step_pct: Decimal | None
    price_step_minutes: int | None
    basket_norm_manual: int | None
    basket_norm_mode: str | None

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if value is None:
                continue
            if field.name.endswith("_kopecks"):
                _integer(value)
            elif field.name.endswith("_pct"):
                if type(value) is not Decimal or not value.is_finite():
                    raise OverrideCommandValidationError()
            elif field.name == "price_step_minutes":
                _integer(value, 1, 2**31 - 1)
            elif field.name == "basket_norm_manual":
                _integer(value, 0, 2**31 - 1)
            elif field.name == "basket_norm_mode":
                if type(value) is not str or value not in (
                    "auto",
                    "manual",
                    "fallback_by_type",
                ):
                    raise OverrideCommandValidationError()
            elif type(value) is not bool:
                raise OverrideCommandValidationError()


@dataclass(frozen=True, slots=True)
class OverrideChange:
    organization_id: int
    marketplace_account_id: int
    catalog_sku_id: int
    actor_membership_id: int
    command_id: str
    expected_version: int
    values: OverrideValues

    def __post_init__(self):
        for value in (
            self.organization_id,
            self.marketplace_account_id,
            self.catalog_sku_id,
            self.actor_membership_id,
        ):
            _integer(value, 1, 2**31 - 1)
        _integer(self.expected_version)
        if (
            type(self.command_id) is not str
            or re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                self.command_id,
            )
            is None
        ):
            raise OverrideCommandValidationError()
        if type(self.values) is not OverrideValues:
            raise OverrideCommandValidationError()

    def canonical_bytes(self, *, max_bytes: int) -> bytes:
        _integer(max_bytes, 1)
        row = {}
        for field in fields(self.values):
            value = getattr(self.values, field.name)
            if value is not None and field.name.endswith("_kopecks"):
                value = _decimal_text(Decimal(value), max_bytes)
            elif type(value) is Decimal:
                value = _decimal_text(value, max_bytes)
            row[field.name] = value
        result = json.dumps(
            {
                "schema": "wb-repricing-sku-overrides/v1",
                "commandKind": "replace_overrides",
                "organizationId": self.organization_id,
                "marketplaceAccountId": self.marketplace_account_id,
                "catalogSkuId": self.catalog_sku_id,
                "actorMembershipId": self.actor_membership_id,
                "commandId": self.command_id,
                "expectedVersion": _decimal_text(
                    Decimal(self.expected_version), max_bytes
                ),
                "values": row,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
        if len(result) > max_bytes:
            raise OverrideCommandValidationError()
        return result

    def checksum(self, *, max_bytes: int) -> str:
        return hashlib.sha256(self.canonical_bytes(max_bytes=max_bytes)).hexdigest()
