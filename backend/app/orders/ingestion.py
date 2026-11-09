"""Pure validation of normalized evidence, not provider completeness or publication."""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Literal

from app.modules.orders import (
    ExternalOrderIdentity,
    ExternalOrderItemIdentity,
    MappedMarketplaceStatus,
    Marketplace,
    OrderContractValidationError,
    make_avito_source_line_key,
    make_wb_source_line_key,
    map_avito_status,
    map_wb_statistics_status,
)


def _text(value: object) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise OrderContractValidationError("Expected exact nonempty text")


def _integer(value: object, minimum: int = 1) -> None:
    if type(value) is not int or value < minimum:
        raise OrderContractValidationError("Integer outside allowed range")


def _instant(value: object) -> None:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise OrderContractValidationError("Timezone-aware instant required")


def _source(marketplace: Marketplace, source_kind: str) -> None:
    allowed = (
        {"avito-order-management", "avito-browser"}
        if marketplace == Marketplace.AVITO
        else {"wb-statistics-supplier-orders"}
    )
    _text(source_kind)
    if source_kind not in allowed:
        raise OrderContractValidationError("Source does not match marketplace")


@dataclass(frozen=True, slots=True)
class ObservedOrderItem:
    identity: ExternalOrderItemIdentity
    quantity: int
    stable_order_line_id: str | None = None
    stable_unit_id: str | None = None

    def __post_init__(self):
        if not isinstance(self.identity, ExternalOrderItemIdentity):
            raise OrderContractValidationError("Item identity required")
        _integer(self.quantity)
        order = self.identity.order_identity
        if order.marketplace == Marketplace.WB:
            if self.stable_order_line_id is not None:
                raise OrderContractValidationError(
                    "Avito line evidence is invalid for WB"
                )
            expected_key = make_wb_source_line_key(self.stable_unit_id)
        else:
            if self.stable_unit_id is not None:
                raise OrderContractValidationError(
                    "WB unit evidence is invalid for Avito"
                )
            expected_key = make_avito_source_line_key(
                order.external_order_id,
                self.identity.external_item_id,
                self.identity.occurrence_index,
                self.stable_order_line_id,
            )
        if self.identity.source_line_key != expected_key:
            raise OrderContractValidationError(
                "Source line key differs from explicit evidence"
            )


@dataclass(frozen=True, slots=True)
class OrderObservation:
    identity: ExternalOrderIdentity
    source_kind: str
    adapter_version: str
    source_revision: str | None
    effective_at: datetime | None
    observed_at: datetime
    status: MappedMarketplaceStatus
    items: tuple[ObservedOrderItem, ...]
    wb_is_cancelled: bool | None = None
    wb_cancel_evidence_present: bool | None = None

    def __post_init__(self):
        if not isinstance(self.identity, ExternalOrderIdentity):
            raise OrderContractValidationError("Order identity required")
        _source(self.identity.marketplace, self.source_kind)
        _text(self.adapter_version)
        if self.source_revision is not None:
            _text(self.source_revision)
        if self.effective_at is not None:
            _instant(self.effective_at)
        _instant(self.observed_at)
        if not isinstance(self.status, MappedMarketplaceStatus):
            raise OrderContractValidationError("Mapped status evidence required")
        if self.identity.marketplace == Marketplace.AVITO:
            if (
                self.wb_is_cancelled is not None
                or self.wb_cancel_evidence_present is not None
            ):
                raise OrderContractValidationError("WB evidence is invalid for Avito")
            expected = map_avito_status(self.status.raw_status)
        else:
            expected = map_wb_statistics_status(
                self.status.raw_status,
                self.wb_is_cancelled,
                self.wb_cancel_evidence_present,
            )
        if self.status != expected:
            raise OrderContractValidationError(
                "Status differs from canonical mapping contract"
            )
        if type(self.items) is not tuple:
            raise OrderContractValidationError("Immutable item tuple required")
        keys = set()
        for item in self.items:
            if (
                not isinstance(item, ObservedOrderItem)
                or item.identity.order_identity != self.identity
            ):
                raise OrderContractValidationError(
                    "Item belongs to another order scope"
                )
            key = item.identity.source_line_key
            if key in keys:
                raise OrderContractValidationError("Duplicate source line key")
            keys.add(key)


@dataclass(frozen=True, slots=True)
class OrderPage:
    number: int
    source_snapshot: str
    terminal: bool
    observations: tuple[OrderObservation, ...]

    def __post_init__(self):
        _integer(self.number)
        _text(self.source_snapshot)
        if type(self.terminal) is not bool or type(self.observations) is not tuple:
            raise OrderContractValidationError("Invalid immutable page")
        if any(not isinstance(row, OrderObservation) for row in self.observations):
            raise OrderContractValidationError("Normalized observation required")


@dataclass(frozen=True, slots=True)
class OrderManifest:
    organization_id: int
    marketplace_account_id: int
    marketplace: Marketplace
    source_kind: str
    adapter_version: str
    source_contract_version: str
    source_snapshot: str
    coverage_state: Literal["partial", "complete"]
    expected_order_count: int | None
    pages: tuple[OrderPage, ...]

    def __post_init__(self):
        _integer(self.organization_id)
        _integer(self.marketplace_account_id)
        try:
            marketplace = Marketplace(self.marketplace)
        except (ValueError, TypeError) as exc:
            raise OrderContractValidationError("Unsupported marketplace") from exc
        object.__setattr__(self, "marketplace", marketplace)
        _source(marketplace, self.source_kind)
        for value in (
            self.source_kind,
            self.adapter_version,
            self.source_contract_version,
            self.source_snapshot,
        ):
            _text(value)
        if self.coverage_state not in ("partial", "complete"):
            raise OrderContractValidationError("Unsupported coverage state")
        if self.expected_order_count is not None:
            _integer(self.expected_order_count, 0)
        if type(self.pages) is not tuple or any(
            not isinstance(page, OrderPage) for page in self.pages
        ):
            raise OrderContractValidationError("Immutable page tuple required")
        numbers, identities = set(), set()
        for page in self.pages:
            if page.number in numbers or page.source_snapshot != self.source_snapshot:
                raise OrderContractValidationError("Duplicate page or snapshot drift")
            numbers.add(page.number)
            for row in page.observations:
                if (
                    row.identity.organization_id != self.organization_id
                    or row.identity.marketplace_account_id
                    != self.marketplace_account_id
                    or row.identity.marketplace != marketplace
                    or row.source_kind != self.source_kind
                    or row.adapter_version != self.adapter_version
                ):
                    raise OrderContractValidationError(
                        "Observation scope or source drift"
                    )
                if row.identity in identities:
                    raise OrderContractValidationError(
                        "Duplicate order across manifest"
                    )
                identities.add(row.identity)
        if (
            self.expected_order_count is not None
            and len(identities) > self.expected_order_count
        ):
            raise OrderContractValidationError("Observed count exceeds declared total")
        if self.coverage_state == "complete":
            if not self.pages or numbers != set(range(1, len(self.pages) + 1)):
                raise OrderContractValidationError(
                    "Complete manifest requires contiguous pages"
                )
            terminal = [page.number for page in self.pages if page.terminal]
            if terminal != [len(self.pages)] or self.expected_order_count != len(
                identities
            ):
                raise OrderContractValidationError(
                    "Terminal evidence or exact total missing"
                )

    @property
    def observations(self) -> tuple[OrderObservation, ...]:
        return tuple(
            row
            for page in sorted(self.pages, key=lambda page: page.number)
            for row in page.observations
        )

    @property
    def checksum(self) -> str:
        """Versioned semantic manifest hash, never a substitute for entity identity."""
        payload = {
            "checksum_version": "orders-manifest-v1",
            "organization_id": self.organization_id,
            "marketplace_account_id": self.marketplace_account_id,
            "marketplace": self.marketplace,
            "source_kind": self.source_kind,
            "adapter_version": self.adapter_version,
            "source_contract_version": self.source_contract_version,
            "source_snapshot": self.source_snapshot,
            "coverage_state": self.coverage_state,
            "expected_order_count": self.expected_order_count,
            "pages": [
                {
                    "number": page.number,
                    "terminal": page.terminal,
                    "rows": sorted(
                        (_semantic_row(row) for row in page.observations),
                        key=lambda row: row["identity"]["external_order_id"],
                    ),
                }
                for page in sorted(self.pages, key=lambda page: page.number)
            ],
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _semantic_row(row: OrderObservation) -> dict:
    result = asdict(row)
    result.pop("observed_at")
    result["effective_at"] = (
        row.effective_at.astimezone(UTC).isoformat() if row.effective_at else None
    )
    result["items"] = sorted(
        result["items"], key=lambda item: item["identity"]["source_line_key"]
    )
    return result


def compare_observations(
    previous: OrderObservation,
    incoming: OrderObservation,
) -> Literal["replay", "changed", "out_of_order", "reconciliation_required"]:
    """Compare evidence only. Even 'changed' is not authority to update a projection."""
    if not isinstance(previous, OrderObservation) or not isinstance(
        incoming, OrderObservation
    ):
        raise OrderContractValidationError("Normalized observations required")
    if (
        previous.identity != incoming.identity
        or previous.source_kind != incoming.source_kind
    ):
        raise OrderContractValidationError(
            "Cannot compare different identities or sources"
        )
    if _semantic_row(previous) == _semantic_row(incoming):
        return "replay"
    if previous.adapter_version != incoming.adapter_version:
        return "reconciliation_required"
    if (
        previous.source_revision is not None
        and previous.source_revision == incoming.source_revision
    ):
        return "reconciliation_required"
    if previous.effective_at is None or incoming.effective_at is None:
        return "reconciliation_required"
    previous_instant = previous.effective_at.astimezone(UTC)
    incoming_instant = incoming.effective_at.astimezone(UTC)
    if incoming_instant < previous_instant:
        return "out_of_order"
    if incoming_instant == previous_instant:
        return "reconciliation_required"
    return "changed"
