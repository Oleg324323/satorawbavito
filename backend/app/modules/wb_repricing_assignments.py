"""Pure Stage4A full assignment command encoding, not state or authorization.

The future repository owns history, replay lookup, revision/head CAS and audit.
Only the four documented Stage4A strategies are represented; unsupported legacy
configs must remain blocked, not dropped into an arbitrary JSON field. No current
assignment registry or formula reads this module.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal


def _internal_int(value):
    if type(value) is not int or not 0 < value <= 2**31 - 1:
        raise ValueError("positive internal INT4 required")


@dataclass(frozen=True, slots=True)
class AssignmentChange:
    organization_id: int
    marketplace_account_id: int
    catalog_sku_id: int
    actor_membership_id: int
    command_id: str
    expected_version: int
    strategy_id: str | None
    interval_hours: int | None
    assigned_at: datetime
    source: str

    def __post_init__(self):
        for value in (
            self.organization_id,
            self.marketplace_account_id,
            self.catalog_sku_id,
            self.actor_membership_id,
        ):
            _internal_int(value)
        if (
            type(self.command_id) is not str
            or re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                self.command_id,
            )
            is None
        ):
            raise ValueError("canonical command UUID4 required")
        if type(self.expected_version) is not int or self.expected_version < 0:
            raise ValueError("nonnegative expected version required")
        if self.strategy_id is not None and (
            type(self.strategy_id) is not str
            or self.strategy_id
            not in (
                "baskets_orders",
                "night_price_mode",
                "plan_fact_interval",
                "illiquid",
            )
        ):
            raise ValueError("unsupported assignment strategy")
        if self.strategy_id == "plan_fact_interval":
            _internal_int(self.interval_hours)
        elif self.interval_hours is not None:
            raise ValueError("interval is only valid for plan_fact_interval")
        if type(self.source) is not str or self.source not in (
            "manual",
            "legacy_import",
        ):
            raise ValueError("unsupported assignment source")
        if (
            type(self.assigned_at) is not datetime
            or self.assigned_at.tzinfo is None
            or self.assigned_at.utcoffset() is None
        ):
            raise ValueError("aware assignment time required")
        try:
            object.__setattr__(self, "assigned_at", self.assigned_at.astimezone(UTC))
        except (ValueError, OverflowError):
            raise ValueError("assignment time outside supported UTC range") from None

    @property
    def canonical_bytes(self) -> bytes:
        return json.dumps(
            {
                "schema": "wb-repricing-assignment/v1",
                "commandKind": "replace_assignment",
                "organizationId": self.organization_id,
                "marketplaceAccountId": self.marketplace_account_id,
                "catalogSkuId": self.catalog_sku_id,
                "actorMembershipId": self.actor_membership_id,
                "commandId": self.command_id,
                "expectedVersion": format(Decimal(self.expected_version), "f"),
                "strategyId": self.strategy_id,
                "intervalHours": self.interval_hours,
                "assignedAt": self.assigned_at.isoformat(
                    timespec="microseconds"
                ).replace("+00:00", "Z"),
                "source": self.source,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()
