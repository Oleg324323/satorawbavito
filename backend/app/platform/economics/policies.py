from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any, Literal, cast

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.infra.db import set_tenant_context
from app.platform.catalog.orm import CatalogSkuRow
from app.platform.economics.costs import EvidenceStatus, _aware_utc, _db_utc
from app.platform.economics.orm import (
    CatalogEconomicsOverrideVersionRow,
    OrganizationEconomicsVersionRow,
)

EconomicsValueState = Literal["configured", "assumed", "missing"]


class EconomicsValidationError(ValueError):
    pass


class EconomicsConflictError(ValueError):
    pass


class EconomicsNotFoundError(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class OrganizationEconomicsVersion:
    organization_economics_version_id: int
    tax_basis_points: int | None
    other_expense_price_basis_points: int | None
    other_expense_per_sale_kopecks: int | None
    value_state: EconomicsValueState
    effective_from: datetime
    source: str
    source_reference: str
    evidence_status: EvidenceStatus
    supersedes_organization_economics_version_id: int | None
    tax_value_state: Literal["configured"] | None = None
    tax_evidence_status: Literal["dated"] | None = None


@dataclass(frozen=True, slots=True)
class CatalogEconomicsOverrideVersion:
    catalog_economics_override_version_id: int
    catalog_sku_id: int
    tax_basis_points: int | None
    other_expense_price_basis_points: int | None
    other_expense_per_sale_kopecks: int | None
    value_state: Literal["configured", "assumed"]
    effective_from: datetime
    source: str
    source_reference: str
    evidence_status: EvidenceStatus
    supersedes_catalog_economics_override_version_id: int | None


@dataclass(frozen=True, slots=True)
class EconomicsPolicy:
    catalog_sku_id: int
    tax_basis_points: int | None
    other_expense_price_basis_points: int | None
    other_expense_per_sale_kopecks: int | None
    value_state: EconomicsValueState
    evidence_status: EvidenceStatus | None
    effective_from: datetime | None
    organization_economics_version_id: int | None
    catalog_economics_override_version_id: int | None
    tax_value_state: EconomicsValueState = "missing"
    tax_evidence_status: EvidenceStatus | None = None
    tax_effective_from: datetime | None = None

    @property
    def amounts(self) -> tuple[int | None, int | None, int | None]:
        return (
            self.tax_basis_points,
            self.other_expense_price_basis_points,
            self.other_expense_per_sale_kopecks,
        )


def _organization_view(
    row: OrganizationEconomicsVersionRow,
) -> OrganizationEconomicsVersion:
    return OrganizationEconomicsVersion(
        row.organization_economics_version_id,
        row.tax_basis_points,
        row.other_expense_price_basis_points,
        row.other_expense_per_sale_kopecks,
        cast(EconomicsValueState, row.value_state),
        _db_utc(row.effective_from),
        row.source,
        row.source_reference,
        cast(EvidenceStatus, row.evidence_status),
        row.supersedes_organization_economics_version_id,
        cast(Literal["configured"] | None, row.tax_value_state),
        cast(Literal["dated"] | None, row.tax_evidence_status),
    )


def _override_view(
    row: CatalogEconomicsOverrideVersionRow,
) -> CatalogEconomicsOverrideVersion:
    return CatalogEconomicsOverrideVersion(
        row.catalog_economics_override_version_id,
        row.catalog_sku_id,
        row.tax_basis_points,
        row.other_expense_price_basis_points,
        row.other_expense_per_sale_kopecks,
        cast(Literal["configured", "assumed"], row.value_state),
        _db_utc(row.effective_from),
        row.source,
        row.source_reference,
        cast(EvidenceStatus, row.evidence_status),
        row.supersedes_catalog_economics_override_version_id,
    )


def _missing(catalog_sku_id: int) -> EconomicsPolicy:
    return EconomicsPolicy(
        catalog_sku_id, None, None, None, "missing", None, None, None, None
    )


def _legacy_number(value: Any, name: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise EconomicsValidationError(f"{name} is required")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise EconomicsValidationError(f"{name} must be numeric") from exc
    if not number.is_finite():
        raise EconomicsValidationError(f"{name} must be finite")
    return number


def _legacy_values(settings: dict[str, Any]) -> tuple[int, int, int]:
    tax = int(
        (_legacy_number(settings.get("taxPct"), "taxPct") * 100).quantize(
            Decimal("1"), rounding=ROUND_HALF_EVEN
        )
    )
    other_pct = int(
        (
            _legacy_number(settings.get("otherExpensePricePct"), "otherExpensePricePct")
            * 100
        ).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)
    )
    if "otherExpensePerSaleKopecks" in settings:
        per_sale = int(
            _legacy_number(
                settings.get("otherExpensePerSaleKopecks"),
                "otherExpensePerSaleKopecks",
            ).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)
        )
    else:
        per_sale = int(
            (
                _legacy_number(
                    settings.get("otherExpensePerSaleRub"),
                    "otherExpensePerSaleRub",
                )
                * 100
            ).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)
        )
    EconomicsService._validate_values(
        tax_basis_points=tax,
        other_expense_price_basis_points=other_pct,
        other_expense_per_sale_kopecks=per_sale,
    )
    return tax, other_pct, per_sale


class EconomicsService:
    def __init__(self, session: Session, organization_id: int) -> None:
        if type(organization_id) is not int or organization_id < 1:
            raise EconomicsValidationError("organization_id must be positive")
        self.session = session
        self.organization_id = organization_id

    def _prepare(self) -> None:
        set_tenant_context(self.session, self.organization_id)

    def reconcile_legacy_organization(
        self,
        settings: dict[str, Any],
        *,
        effective_from: datetime,
        source_reference: str,
        value_state: Literal["configured", "assumed"] = "configured",
        source: str = "legacy_repricer",
        evidence_status: EvidenceStatus = "dated",
        created_by_membership_id: int | None = None,
        tax_value_state: Literal["configured"] | None = None,
        tax_evidence_status: Literal["dated"] | None = None,
    ) -> OrganizationEconomicsVersion | None:
        values = _legacy_values(settings)
        effective_at = _aware_utc(effective_from)
        self._prepare()
        current = self.session.scalar(
            select(OrganizationEconomicsVersionRow)
            .where(
                OrganizationEconomicsVersionRow.organization_id == self.organization_id,
                OrganizationEconomicsVersionRow.effective_from <= effective_at,
            )
            .order_by(
                OrganizationEconomicsVersionRow.effective_from.desc(),
                OrganizationEconomicsVersionRow.created_at.desc(),
                OrganizationEconomicsVersionRow.organization_economics_version_id.desc(),
            )
            .limit(1)
        )
        if (
            current is not None
            and (
                current.tax_basis_points,
                current.other_expense_price_basis_points,
                current.other_expense_per_sale_kopecks,
                current.value_state,
                current.evidence_status,
            )
            == (*values, value_state, evidence_status)
            and (
                (tax_value_state is None and tax_evidence_status is None)
                or (current.tax_value_state, current.tax_evidence_status)
                == (tax_value_state, tax_evidence_status)
            )
        ):
            return None
        return self.set_organization_policy(
            tax_basis_points=values[0],
            other_expense_price_basis_points=values[1],
            other_expense_per_sale_kopecks=values[2],
            value_state=value_state,
            effective_from=effective_at,
            source=source,
            source_reference=source_reference,
            evidence_status=evidence_status,
            supersedes_organization_economics_version_id=(
                current.organization_economics_version_id if current else None
            ),
            created_by_membership_id=created_by_membership_id,
            tax_value_state=tax_value_state,
            tax_evidence_status=tax_evidence_status,
        )

    def reconcile_legacy_sku_override(
        self,
        article_id: str,
        settings: dict[str, Any],
        *,
        effective_from: datetime,
        source_reference: str,
        value_state: Literal["configured", "assumed"] = "configured",
        source: str = "legacy_repricer",
        evidence_status: EvidenceStatus = "dated",
        created_by_membership_id: int | None = None,
    ) -> CatalogEconomicsOverrideVersion | None:
        article = article_id.strip()
        if not article:
            raise EconomicsValidationError("article_id is required")
        values = _legacy_values(settings)
        effective_at = _aware_utc(effective_from)
        self._prepare()
        catalog_sku_id = self.session.scalar(
            select(CatalogSkuRow.catalog_sku_id).where(
                CatalogSkuRow.organization_id == self.organization_id,
                CatalogSkuRow.code == article,
            )
        )
        if catalog_sku_id is None:
            raise EconomicsNotFoundError("catalog SKU not found for article")
        current = self.session.scalar(
            select(CatalogEconomicsOverrideVersionRow)
            .where(
                CatalogEconomicsOverrideVersionRow.organization_id
                == self.organization_id,
                CatalogEconomicsOverrideVersionRow.catalog_sku_id == catalog_sku_id,
                CatalogEconomicsOverrideVersionRow.effective_from <= effective_at,
            )
            .order_by(
                CatalogEconomicsOverrideVersionRow.effective_from.desc(),
                CatalogEconomicsOverrideVersionRow.created_at.desc(),
                CatalogEconomicsOverrideVersionRow.catalog_economics_override_version_id.desc(),
            )
            .limit(1)
        )
        if current is not None and (
            current.tax_basis_points,
            current.other_expense_price_basis_points,
            current.other_expense_per_sale_kopecks,
            current.value_state,
            current.evidence_status,
        ) == (*values, value_state, evidence_status):
            return None
        return self.set_sku_override(
            catalog_sku_id=catalog_sku_id,
            tax_basis_points=values[0],
            other_expense_price_basis_points=values[1],
            other_expense_per_sale_kopecks=values[2],
            value_state=value_state,
            effective_from=effective_at,
            source=source,
            source_reference=source_reference,
            evidence_status=evidence_status,
            supersedes_catalog_economics_override_version_id=(
                current.catalog_economics_override_version_id if current else None
            ),
            created_by_membership_id=created_by_membership_id,
        )

    @staticmethod
    def _validate_basis_points(value: int | None, name: str) -> None:
        if value is None:
            return
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 10_000
        ):
            raise EconomicsValidationError(f"{name} must be between 0 and 10000")

    @classmethod
    def _validate_values(
        cls,
        *,
        tax_basis_points: int | None,
        other_expense_price_basis_points: int | None,
        other_expense_per_sale_kopecks: int | None,
    ) -> None:
        cls._validate_basis_points(tax_basis_points, "tax_basis_points")
        cls._validate_basis_points(
            other_expense_price_basis_points,
            "other_expense_price_basis_points",
        )
        if other_expense_per_sale_kopecks is not None and (
            isinstance(other_expense_per_sale_kopecks, bool)
            or not isinstance(other_expense_per_sale_kopecks, int)
            or other_expense_per_sale_kopecks < 0
        ):
            raise EconomicsValidationError(
                "other_expense_per_sale_kopecks must be non-negative"
            )

    @staticmethod
    def _validate_metadata(
        source: str, source_reference: str, evidence_status: str
    ) -> tuple[str, str]:
        normalized_source = source.strip()
        normalized_reference = source_reference.strip()
        if not normalized_source or not normalized_reference:
            raise EconomicsValidationError("source and source_reference are required")
        if evidence_status not in {"dated", "undated", "period_end_fallback"}:
            raise EconomicsValidationError("invalid evidence_status")
        return normalized_source, normalized_reference

    def set_organization_policy(
        self,
        *,
        tax_basis_points: int | None,
        other_expense_price_basis_points: int | None,
        other_expense_per_sale_kopecks: int | None,
        value_state: EconomicsValueState,
        effective_from: datetime,
        source: str,
        source_reference: str,
        evidence_status: EvidenceStatus,
        supersedes_organization_economics_version_id: int | None = None,
        created_by_membership_id: int | None = None,
        tax_value_state: Literal["configured"] | None = None,
        tax_evidence_status: Literal["dated"] | None = None,
    ) -> OrganizationEconomicsVersion:
        self._validate_values(
            tax_basis_points=tax_basis_points,
            other_expense_price_basis_points=other_expense_price_basis_points,
            other_expense_per_sale_kopecks=other_expense_per_sale_kopecks,
        )
        values = (
            tax_basis_points,
            other_expense_price_basis_points,
            other_expense_per_sale_kopecks,
        )
        if value_state in {"configured", "assumed"} and any(
            value is None for value in values
        ):
            raise EconomicsValidationError(
                "configured or assumed policy must be complete"
            )
        if value_state == "missing" and any(value is not None for value in values):
            raise EconomicsValidationError("missing policy cannot contain values")
        if value_state not in {"configured", "assumed", "missing"}:
            raise EconomicsValidationError("invalid value_state")
        if (
            (tax_value_state, tax_evidence_status)
            not in {
                (None, None),
                ("configured", "dated"),
            }
            or tax_value_state is not None
            and tax_basis_points is None
        ):
            raise EconomicsValidationError(
                "tax confirmation requires a rate and configured, dated evidence"
            )
        source, source_reference = self._validate_metadata(
            source, source_reference, evidence_status
        )
        effective_at = _aware_utc(effective_from)
        self._prepare()
        existing = self.session.scalar(
            select(OrganizationEconomicsVersionRow).where(
                OrganizationEconomicsVersionRow.organization_id == self.organization_id,
                OrganizationEconomicsVersionRow.source == source,
                OrganizationEconomicsVersionRow.source_reference == source_reference,
            )
        )
        command = {
            "tax_basis_points": tax_basis_points,
            "other_expense_price_basis_points": other_expense_price_basis_points,
            "other_expense_per_sale_kopecks": other_expense_per_sale_kopecks,
            "value_state": value_state,
            "effective_from": effective_at,
            "source": source,
            "source_reference": source_reference,
            "evidence_status": evidence_status,
            "supersedes_organization_economics_version_id": supersedes_organization_economics_version_id,
            "created_by_membership_id": created_by_membership_id,
            "tax_value_state": tax_value_state,
            "tax_evidence_status": tax_evidence_status,
        }
        if existing is not None:
            if self._same_organization(existing, command):
                return _organization_view(existing)
            raise EconomicsConflictError(
                "source reference already belongs to another organization policy"
            )
        if supersedes_organization_economics_version_id is not None:
            superseded = self.session.scalar(
                select(OrganizationEconomicsVersionRow).where(
                    OrganizationEconomicsVersionRow.organization_id
                    == self.organization_id,
                    OrganizationEconomicsVersionRow.organization_economics_version_id
                    == supersedes_organization_economics_version_id,
                )
            )
            if superseded is None:
                raise EconomicsValidationError(
                    "superseded organization policy not found"
                )
        row = OrganizationEconomicsVersionRow(
            organization_id=self.organization_id, **command
        )
        self.session.add(row)
        row = cast(
            OrganizationEconomicsVersionRow,
            self._commit_or_resolve(
                row,
                OrganizationEconomicsVersionRow,
                source,
                source_reference,
                lambda candidate: self._same_organization(candidate, command),
            ),
        )
        return _organization_view(row)

    def set_sku_override(
        self,
        *,
        catalog_sku_id: int,
        tax_basis_points: int | None,
        other_expense_price_basis_points: int | None,
        other_expense_per_sale_kopecks: int | None,
        value_state: Literal["configured", "assumed"],
        effective_from: datetime,
        source: str,
        source_reference: str,
        evidence_status: EvidenceStatus,
        supersedes_catalog_economics_override_version_id: int | None = None,
        created_by_membership_id: int | None = None,
    ) -> CatalogEconomicsOverrideVersion:
        if type(catalog_sku_id) is not int or catalog_sku_id < 1:
            raise EconomicsValidationError("catalog_sku_id must be positive")
        self._validate_values(
            tax_basis_points=tax_basis_points,
            other_expense_price_basis_points=other_expense_price_basis_points,
            other_expense_per_sale_kopecks=other_expense_per_sale_kopecks,
        )
        if value_state not in {"configured", "assumed"}:
            raise EconomicsValidationError("invalid override value_state")
        source, source_reference = self._validate_metadata(
            source, source_reference, evidence_status
        )
        effective_at = _aware_utc(effective_from)
        self._prepare()
        sku = self.session.scalar(
            select(CatalogSkuRow.catalog_sku_id).where(
                CatalogSkuRow.organization_id == self.organization_id,
                CatalogSkuRow.catalog_sku_id == catalog_sku_id,
            )
        )
        if sku is None:
            raise EconomicsNotFoundError("catalog SKU not found")
        existing = self.session.scalar(
            select(CatalogEconomicsOverrideVersionRow).where(
                CatalogEconomicsOverrideVersionRow.organization_id
                == self.organization_id,
                CatalogEconomicsOverrideVersionRow.source == source,
                CatalogEconomicsOverrideVersionRow.source_reference == source_reference,
            )
        )
        command = {
            "catalog_sku_id": catalog_sku_id,
            "tax_basis_points": tax_basis_points,
            "other_expense_price_basis_points": other_expense_price_basis_points,
            "other_expense_per_sale_kopecks": other_expense_per_sale_kopecks,
            "value_state": value_state,
            "effective_from": effective_at,
            "source": source,
            "source_reference": source_reference,
            "evidence_status": evidence_status,
            "supersedes_catalog_economics_override_version_id": supersedes_catalog_economics_override_version_id,
            "created_by_membership_id": created_by_membership_id,
        }
        if existing is not None:
            if self._same_override(existing, command):
                return _override_view(existing)
            raise EconomicsConflictError(
                "source reference already belongs to another SKU override"
            )
        if supersedes_catalog_economics_override_version_id is not None:
            superseded = self.session.scalar(
                select(CatalogEconomicsOverrideVersionRow).where(
                    CatalogEconomicsOverrideVersionRow.organization_id
                    == self.organization_id,
                    CatalogEconomicsOverrideVersionRow.catalog_sku_id == catalog_sku_id,
                    CatalogEconomicsOverrideVersionRow.catalog_economics_override_version_id
                    == supersedes_catalog_economics_override_version_id,
                )
            )
            if superseded is None:
                raise EconomicsValidationError("superseded SKU override not found")
        row = CatalogEconomicsOverrideVersionRow(
            organization_id=self.organization_id, **command
        )
        self.session.add(row)
        row = cast(
            CatalogEconomicsOverrideVersionRow,
            self._commit_or_resolve(
                row,
                CatalogEconomicsOverrideVersionRow,
                source,
                source_reference,
                lambda candidate: self._same_override(candidate, command),
            ),
        )
        return _override_view(row)

    @staticmethod
    def _same_organization(
        row: OrganizationEconomicsVersionRow, command: dict[str, object]
    ) -> bool:
        return all(
            (
                _db_utc(getattr(row, key)) == value
                if key == "effective_from"
                else getattr(row, key) == value
            )
            for key, value in command.items()
        )

    @staticmethod
    def _same_override(
        row: CatalogEconomicsOverrideVersionRow, command: dict[str, object]
    ) -> bool:
        return all(
            (
                _db_utc(getattr(row, key)) == value
                if key == "effective_from"
                else getattr(row, key) == value
            )
            for key, value in command.items()
        )

    def _commit_or_resolve(
        self,
        row: OrganizationEconomicsVersionRow | CatalogEconomicsOverrideVersionRow,
        model: (
            type[OrganizationEconomicsVersionRow]
            | type[CatalogEconomicsOverrideVersionRow]
        ),
        source: str,
        source_reference: str,
        same: Callable[[object], bool],
    ) -> OrganizationEconomicsVersionRow | CatalogEconomicsOverrideVersionRow:
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            existing = self.session.scalar(
                select(model).where(
                    model.organization_id == self.organization_id,
                    model.source == source,
                    model.source_reference == source_reference,
                )
            )
            if existing is None or not same(existing):
                raise EconomicsConflictError(
                    "economics command conflicts with persisted data"
                ) from exc
            row = existing
        self._prepare()
        self.session.refresh(row)
        return row

    def get_policies_for_points(
        self, points: list[tuple[int, datetime]]
    ) -> dict[tuple[int, datetime], EconomicsPolicy]:
        # Validate before set deduplication: True and 1.0 otherwise alias ID 1.
        if any(type(sku_id) is not int or sku_id < 1 for sku_id, _ in points):
            raise EconomicsValidationError(
                "catalog_sku_id must be a positive internal integer"
            )
        normalized = {
            instant: _aware_utc(instant)
            for instant in dict.fromkeys(instant for _, instant in points)
        }
        requested = sorted(
            {
                (catalog_sku_id, normalized[instant])
                for catalog_sku_id, instant in points
            },
            key=lambda point: (point[0], point[1]),
        )
        if not requested:
            return {}
        self._prepare()
        sku_ids = sorted({catalog_sku_id for catalog_sku_id, _ in requested})
        valid_sku_ids = set(
            self.session.scalars(
                select(CatalogSkuRow.catalog_sku_id).where(
                    CatalogSkuRow.organization_id == self.organization_id,
                    CatalogSkuRow.catalog_sku_id.in_(sku_ids),
                )
            ).all()
        )
        max_instant = max(instant for _, instant in requested)
        organization_rows = self.session.execute(
            select(OrganizationEconomicsVersionRow.__table__)
            .where(
                OrganizationEconomicsVersionRow.organization_id == self.organization_id,
                OrganizationEconomicsVersionRow.effective_from <= max_instant,
            )
            .order_by(
                OrganizationEconomicsVersionRow.effective_from,
                OrganizationEconomicsVersionRow.created_at,
                OrganizationEconomicsVersionRow.organization_economics_version_id,
            )
        ).all()
        override_rows = self.session.execute(
            select(CatalogEconomicsOverrideVersionRow.__table__)
            .where(
                CatalogEconomicsOverrideVersionRow.organization_id
                == self.organization_id,
                CatalogEconomicsOverrideVersionRow.catalog_sku_id.in_(valid_sku_ids),
                CatalogEconomicsOverrideVersionRow.effective_from <= max_instant,
            )
            .order_by(
                CatalogEconomicsOverrideVersionRow.catalog_sku_id,
                CatalogEconomicsOverrideVersionRow.effective_from,
                CatalogEconomicsOverrideVersionRow.created_at,
                CatalogEconomicsOverrideVersionRow.catalog_economics_override_version_id,
            )
        ).all()
        instants = sorted({instant for _, instant in requested})
        organization_at = self._versions_at(organization_rows, instants)
        tax_organization_at = self._versions_at(
            [row for row in organization_rows if row.tax_value_state == "configured"],
            instants,
        )
        overrides_by_sku: dict[int, list[CatalogEconomicsOverrideVersionRow]] = {}
        for row in override_rows:
            overrides_by_sku.setdefault(row.catalog_sku_id, []).append(row)
        override_at = {
            sku_id: self._versions_at(rows, instants)
            for sku_id, rows in overrides_by_sku.items()
        }
        confirmed_override_at = {
            sku_id: self._versions_at(
                [
                    row
                    for row in rows
                    if row.value_state == "configured"
                    and row.evidence_status == "dated"
                ],
                instants,
            )
            for sku_id, rows in overrides_by_sku.items()
        }
        result: dict[tuple[int, datetime], EconomicsPolicy] = {}
        cache: dict[tuple[int | None, ...], EconomicsPolicy] = {}
        for sku_id, instant in requested:
            if sku_id not in valid_sku_ids:
                key = (sku_id, None, None)
                if key not in cache:
                    cache[key] = _missing(sku_id)
                result[(sku_id, instant)] = cache[key]
                continue
            organization = organization_at[instant]
            override = override_at.get(sku_id, {}).get(instant)
            tax_organization = tax_organization_at[instant]
            confirmed_override = confirmed_override_at.get(sku_id, {}).get(instant)
            key = (
                sku_id,
                getattr(organization, "organization_economics_version_id", None),
                getattr(override, "catalog_economics_override_version_id", None),
                getattr(tax_organization, "organization_economics_version_id", None),
                getattr(
                    confirmed_override, "catalog_economics_override_version_id", None
                ),
            )
            if key not in cache:
                cache[key] = self._resolve(
                    sku_id, organization, override, tax_organization, confirmed_override
                )
            result[(sku_id, instant)] = cache[key]
        return result

    @staticmethod
    def _versions_at(
        rows: list[object], instants: list[datetime]
    ) -> dict[datetime, object | None]:
        resolved: dict[datetime, object | None] = {}
        position = 0
        current: object | None = None
        for instant in instants:
            while (
                position < len(rows)
                and _db_utc(getattr(rows[position], "effective_from")) <= instant
            ):
                current = rows[position]
                position += 1
            resolved[instant] = current
        return resolved

    @staticmethod
    def _resolve(
        sku_id: int,
        organization: object | None,
        override: object | None,
        tax_organization: object | None,
        confirmed_override: object | None,
    ) -> EconomicsPolicy:
        fields = (
            "tax_basis_points",
            "other_expense_price_basis_points",
            "other_expense_per_sale_kopecks",
        )
        sources = [
            (
                override
                if override is not None and getattr(override, field) is not None
                else organization
            )
            for field in fields
        ]
        if tax_organization is not None:
            # One dated owner event replaces older SKU tax assumptions, while
            # expense overrides and subsequent confirmed SKU tax remain intact.
            sources[0] = (
                confirmed_override
                if confirmed_override is not None
                and getattr(confirmed_override, "tax_basis_points") is not None
                and _db_utc(getattr(confirmed_override, "effective_from"))
                > _db_utc(getattr(tax_organization, "effective_from"))
                else tax_organization
            )
        values = tuple(
            getattr(row, field) if row is not None else None
            for row, field in zip(sources, fields, strict=True)
        )
        tax_source = sources[0]
        tax_state = (
            getattr(tax_source, "tax_value_state", None)
            or getattr(tax_source, "value_state")
            if tax_source is not None
            else "missing"
        )
        tax_evidence = (
            getattr(tax_source, "tax_evidence_status", None)
            or getattr(tax_source, "evidence_status")
            if tax_source is not None
            else None
        )
        expense_sources = [row for row in sources[1:] if row is not None]
        contributors = [row for row in sources if row is not None]
        state: EconomicsValueState = (
            "missing"
            if any(value is None for value in values)
            else (
                "assumed"
                if tax_state == "assumed"
                or any(
                    getattr(row, "value_state") == "assumed" for row in expense_sources
                )
                else "configured"
            )
        )
        evidence_rank = {"dated": 0, "undated": 1, "period_end_fallback": 2}
        evidence = (
            max(
                [getattr(row, "evidence_status") for row in expense_sources]
                + ([tax_evidence] if tax_evidence is not None else []),
                key=evidence_rank.__getitem__,
            )
            if contributors
            else None
        )
        effective = (
            max(_db_utc(getattr(row, "effective_from")) for row in contributors)
            if contributors
            else None
        )
        return EconomicsPolicy(
            sku_id,
            values[0],
            values[1],
            values[2],
            state,
            cast(EvidenceStatus | None, evidence),
            effective,
            getattr(organization, "organization_economics_version_id", None),
            getattr(override, "catalog_economics_override_version_id", None),
            cast(EconomicsValueState, tax_state),
            cast(EvidenceStatus | None, tax_evidence),
            (
                _db_utc(getattr(tax_source, "effective_from"))
                if tax_source is not None
                else None
            ),
        )

    def revision(self) -> int:
        self._prepare()
        organization_count = self.session.scalar(
            select(func.count())
            .select_from(OrganizationEconomicsVersionRow)
            .where(
                OrganizationEconomicsVersionRow.organization_id == self.organization_id
            )
        )
        override_count = self.session.scalar(
            select(func.count())
            .select_from(CatalogEconomicsOverrideVersionRow)
            .where(
                CatalogEconomicsOverrideVersionRow.organization_id
                == self.organization_id
            )
        )
        return int(organization_count or 0) + int(override_count or 0)
