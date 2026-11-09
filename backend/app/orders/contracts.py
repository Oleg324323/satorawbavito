"""Pure read views. Authorization and snapshot retention are repository obligations."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.modules.orders import ExternalOrderItemIdentity, OrderContractValidationError
from app.orders.ingestion import OrderObservation, _instant, _integer, _text


@dataclass(frozen=True, slots=True)
class DeadlineEvidence:
    kind: str
    source_at: datetime | None
    computed_at: datetime | None
    rule_id: str | None
    rule_version: str | None
    timezone: str
    evidence_source: str
    observed_at: datetime

    def __post_init__(self):
        for value in (self.kind, self.timezone, self.evidence_source):
            _text(value)
        _instant(self.observed_at)
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise OrderContractValidationError("Unknown deadline timezone") from exc
        if self.source_at is None and self.computed_at is None:
            raise OrderContractValidationError("Deadline evidence is missing")
        for value in (self.source_at, self.computed_at):
            if value is not None:
                _instant(value)
        if self.computed_at is not None:
            _text(self.rule_id)
            _text(self.rule_version)
        elif self.rule_id is not None or self.rule_version is not None:
            raise OrderContractValidationError(
                "Rule metadata requires computed deadline"
            )


@dataclass(frozen=True, slots=True)
class CatalogResolution:
    state: Literal["resolved", "unmapped", "ambiguous", "stale", "manual_override"]
    marketplace_product_id: int | None
    marketplace_offer_id: int | None
    catalog_sku_id: int | None
    evidence_version: str

    def __post_init__(self):
        if self.state not in (
            "resolved",
            "unmapped",
            "ambiguous",
            "stale",
            "manual_override",
        ):
            raise OrderContractValidationError("Unknown resolution state")
        _text(self.evidence_version)
        for value in (
            self.marketplace_product_id,
            self.marketplace_offer_id,
            self.catalog_sku_id,
        ):
            if value is not None:
                _integer(value)
        if (
            self.marketplace_offer_id is not None
            and self.marketplace_product_id is None
        ):
            raise OrderContractValidationError("Offer requires product reference")
        if self.state in ("resolved", "manual_override"):
            if self.catalog_sku_id is None:
                raise OrderContractValidationError("Resolved assignment requires SKU")
            if self.state == "resolved" and self.marketplace_product_id is None:
                raise OrderContractValidationError(
                    "Automatic resolution requires product evidence"
                )
        elif self.catalog_sku_id is not None:
            raise OrderContractValidationError(
                "Unresolved state cannot claim current SKU"
            )


@dataclass(frozen=True, slots=True)
class OrderReadRow:
    observation: OrderObservation
    item_identity: ExternalOrderItemIdentity
    row_version: int
    resolution: CatalogResolution
    readiness_blockers: tuple[str, ...]
    deadlines: tuple[DeadlineEvidence, ...] = ()

    def __post_init__(self):
        if not isinstance(self.observation, OrderObservation) or not isinstance(
            self.resolution, CatalogResolution
        ):
            raise OrderContractValidationError(
                "Normalized observation and resolution required"
            )
        if not isinstance(
            self.item_identity, ExternalOrderItemIdentity
        ) or self.item_identity not in {
            item.identity for item in self.observation.items
        }:
            raise OrderContractValidationError("Read item must belong to observation")
        _integer(self.row_version)
        if type(self.readiness_blockers) is not tuple:
            raise OrderContractValidationError("Immutable blockers required")
        for blocker in self.readiness_blockers:
            _text(blocker)
        if len(set(self.readiness_blockers)) != len(self.readiness_blockers):
            raise OrderContractValidationError("Duplicate blockers")
        needs_blocker = (
            self.resolution.state not in ("resolved", "manual_override")
            or self.observation.status.canonical_status is None
            or self.observation.status.canonical_status
            in ("cancelled", "returning", "returned", "disputed")
            or self.observation.identity.marketplace == "wb"
            or not self.observation.items
        )
        if needs_blocker and not self.readiness_blockers:
            raise OrderContractValidationError(
                "Evidence requires explicit readiness blocker"
            )
        if type(self.deadlines) is not tuple or any(
            not isinstance(value, DeadlineEvidence) for value in self.deadlines
        ):
            raise OrderContractValidationError("Immutable deadline evidence required")


@dataclass(frozen=True, slots=True)
class AccountCoverage:
    marketplace_account_id: int
    source_kind: str
    source_version: str
    state: Literal["complete", "partial", "missing"]
    source_snapshot: str | None
    requested_from: datetime | None
    requested_to: datetime | None

    def __post_init__(self):
        _integer(self.marketplace_account_id)
        _text(self.source_kind)
        _text(self.source_version)
        if self.state not in ("complete", "partial", "missing"):
            raise OrderContractValidationError("Unknown coverage state")
        if self.state == "missing":
            if self.source_snapshot is not None:
                raise OrderContractValidationError(
                    "Missing coverage has no source snapshot"
                )
        else:
            _text(self.source_snapshot)
        if (self.requested_from is None) != (self.requested_to is None):
            raise OrderContractValidationError("Coverage bounds must be paired")
        if self.requested_from is not None:
            _instant(self.requested_from)
            _instant(self.requested_to)
            if self.requested_from.astimezone(UTC) >= self.requested_to.astimezone(UTC):
                raise OrderContractValidationError(
                    "Coverage requires nonempty half-open interval"
                )


@dataclass(frozen=True, slots=True)
class OrderReadPage:
    organization_id: int
    marketplace_account_ids: tuple[int, ...]
    snapshot_id: str
    high_water_mark: str
    published_at: datetime
    coverage_state: Literal["complete", "partial", "missing"]
    rows: tuple[OrderReadRow, ...]
    next_cursor: str | None
    account_coverage: tuple[AccountCoverage, ...]

    def __post_init__(self):
        _integer(self.organization_id)
        if type(self.marketplace_account_ids) is not tuple:
            raise OrderContractValidationError("Immutable account scope required")
        for account in self.marketplace_account_ids:
            _integer(account)
        if len(set(self.marketplace_account_ids)) != len(self.marketplace_account_ids):
            raise OrderContractValidationError("Duplicate account scope")
        _text(self.snapshot_id)
        _text(self.high_water_mark)
        _instant(self.published_at)
        if self.next_cursor is not None:
            _text(self.next_cursor)
        if self.coverage_state not in ("complete", "partial", "missing"):
            raise OrderContractValidationError("Unknown coverage state")
        if type(self.rows) is not tuple:
            raise OrderContractValidationError("Immutable read rows required")
        if type(self.account_coverage) is not tuple or any(
            not isinstance(value, AccountCoverage) for value in self.account_coverage
        ):
            raise OrderContractValidationError("Immutable account coverage required")
        coverage = {
            value.marketplace_account_id: value for value in self.account_coverage
        }
        if len(coverage) != len(self.account_coverage) or set(coverage) != set(
            self.marketplace_account_ids
        ):
            raise OrderContractValidationError(
                "Coverage must match account scope exactly"
            )
        states = {value.state for value in self.account_coverage}
        aggregate = (
            "complete"
            if states == {"complete"}
            else "missing"
            if states <= {"missing"}
            else "partial"
        )
        if self.coverage_state != aggregate:
            raise OrderContractValidationError(
                "Aggregate coverage differs from account evidence"
            )
        seen = set()
        order_observations = {}
        for row in self.rows:
            if not isinstance(row, OrderReadRow):
                raise OrderContractValidationError("Read row required")
            identity = row.observation.identity
            if (
                identity.organization_id != self.organization_id
                or identity.marketplace_account_id not in self.marketplace_account_ids
            ):
                raise OrderContractValidationError("Read row outside account scope")
            account = coverage[identity.marketplace_account_id]
            if (
                account.state == "missing"
                or account.source_kind != row.observation.source_kind
                or account.source_version != row.observation.adapter_version
            ):
                raise OrderContractValidationError(
                    "Read row differs from source coverage"
                )
            if (
                identity in order_observations
                and order_observations[identity] != row.observation
            ):
                raise OrderContractValidationError(
                    "Mixed order observations in one page"
                )
            order_observations[identity] = row.observation
            key = (identity, row.item_identity.source_line_key)
            if key in seen:
                raise OrderContractValidationError("Duplicate read row")
            seen.add(key)
        if self.coverage_state == "missing" and (
            self.rows or self.next_cursor is not None
        ):
            raise OrderContractValidationError(
                "Missing coverage cannot contain rows or cursor"
            )
