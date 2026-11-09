from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, cast

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.infra.db import set_tenant_context
from app.platform.catalog.orm import CatalogSkuRow
from app.platform.clock import utc_now
from app.platform.economics.orm import CatalogCostVersionRow
from app.platform.identity.orm import IamMembershipRow


CostValueState = Literal["configured", "assumed", "missing"]
EvidenceStatus = Literal["dated", "undated", "period_end_fallback"]


class CostValidationError(ValueError):
    pass


class CostConflictError(ValueError):
    pass


class CostNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class CostValue:
    cost_version_id: int | None
    catalog_sku_id: int
    amount_kopecks: int | None
    value_state: CostValueState
    effective_from: datetime | None = None
    source: str | None = None
    source_reference: str | None = None
    evidence_status: EvidenceStatus | None = None
    supersedes_cost_version_id: int | None = None
    created_by_membership_id: int | None = None
    created_at: datetime | None = None


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CostValidationError("effective_from must include a timezone")
    return value.astimezone(timezone.utc)


def _db_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _view(row: Any) -> CostValue:
    return CostValue(
        cost_version_id=row.cost_version_id,
        catalog_sku_id=row.catalog_sku_id,
        amount_kopecks=row.amount_kopecks,
        value_state=cast(CostValueState, row.value_state),
        effective_from=_db_utc(row.effective_from),
        source=row.source,
        source_reference=row.source_reference,
        evidence_status=cast(EvidenceStatus, row.evidence_status),
        supersedes_cost_version_id=row.supersedes_cost_version_id,
        created_by_membership_id=row.created_by_membership_id,
        created_at=_db_utc(row.created_at),
    )


class CostsService:
    def __init__(self, session: Session, organization_id: int, *, now: Callable[[], datetime] = utc_now):
        if type(organization_id) is not int or organization_id < 1:
            raise CostValidationError("organization_id must be positive")
        self.session = session
        self.organization_id = organization_id
        self.now = now

    def _prepare(self) -> None:
        set_tenant_context(self.session, self.organization_id)

    def _sku(self, catalog_sku_id: int) -> CatalogSkuRow:
        self._validate_sku_id(catalog_sku_id)
        self._prepare()
        row = self.session.scalar(
            select(CatalogSkuRow).where(
                CatalogSkuRow.organization_id == self.organization_id,
                CatalogSkuRow.catalog_sku_id == catalog_sku_id,
            )
        )
        if row is None:
            raise CostNotFoundError("catalog SKU not found")
        return row

    @staticmethod
    def _validate_sku_id(catalog_sku_id: int) -> None:
        if type(catalog_sku_id) is not int or catalog_sku_id < 1:
            raise CostValidationError("catalog_sku_id must be a positive internal integer")

    def _by_reference(self, source: str, source_reference: str) -> CatalogCostVersionRow | None:
        self._prepare()
        return self.session.scalar(
            select(CatalogCostVersionRow).where(
                CatalogCostVersionRow.organization_id == self.organization_id,
                CatalogCostVersionRow.source == source,
                CatalogCostVersionRow.source_reference == source_reference,
            )
        )

    @staticmethod
    def _validate_amount(amount_kopecks: int | None, value_state: str) -> None:
        if isinstance(amount_kopecks, bool) or (amount_kopecks is not None and not isinstance(amount_kopecks, int)):
            raise CostValidationError("amount_kopecks must be an integer or null")
        if value_state == "configured" and amount_kopecks is not None and amount_kopecks >= 0:
            return
        if value_state == "assumed" and amount_kopecks is not None and amount_kopecks > 0:
            return
        if value_state == "missing" and amount_kopecks is None:
            return
        raise CostValidationError("amount_kopecks does not match value_state")

    @staticmethod
    def _same_command(
        row: CatalogCostVersionRow,
        *,
        catalog_sku_id: int,
        amount_kopecks: int | None,
        value_state: str,
        effective_from: datetime,
        evidence_status: str,
        supersedes_cost_version_id: int | None,
        created_by_membership_id: int | None,
    ) -> bool:
        return (
            row.catalog_sku_id == catalog_sku_id
            and row.amount_kopecks == amount_kopecks
            and row.value_state == value_state
            and _db_utc(row.effective_from) == effective_from
            and row.evidence_status == evidence_status
            and row.supersedes_cost_version_id == supersedes_cost_version_id
            and row.created_by_membership_id == created_by_membership_id
        )

    def set_cost(
        self,
        *,
        catalog_sku_id: int,
        amount_kopecks: int | None,
        value_state: CostValueState,
        effective_from: datetime,
        source: str,
        source_reference: str,
        evidence_status: EvidenceStatus,
        supersedes_cost_version_id: int | None = None,
        created_by_membership_id: int | None = None,
        created_by_user_id: str | None = None,
    ) -> CostValue:
        self._validate_amount(amount_kopecks, value_state)
        if evidence_status not in {"dated", "undated", "period_end_fallback"}:
            raise CostValidationError("invalid evidence_status")
        normalized_source = source.strip()
        normalized_reference = source_reference.strip()
        if not normalized_source or not normalized_reference:
            raise CostValidationError("source and source_reference are required")
        effective_at = _aware_utc(effective_from)
        self._sku(catalog_sku_id)
        self.session.scalar(select(CatalogSkuRow).where(
            CatalogSkuRow.organization_id == self.organization_id,
            CatalogSkuRow.catalog_sku_id == catalog_sku_id,
        ).with_for_update())
        if created_by_user_id is not None:
            if created_by_membership_id is not None:
                raise CostValidationError("provide either created_by_user_id or created_by_membership_id")
            created_by_membership_id = self.session.scalar(
                select(IamMembershipRow.membership_id).where(
                    IamMembershipRow.organization_id == self.organization_id,
                    IamMembershipRow.user_id == created_by_user_id,
                    IamMembershipRow.is_active.is_(True),
                )
            )
            if created_by_membership_id is None:
                raise CostValidationError("active membership not found")

        existing = self._by_reference(normalized_source, normalized_reference)
        command = {
            "catalog_sku_id": catalog_sku_id,
            "amount_kopecks": amount_kopecks,
            "value_state": value_state,
            "effective_from": effective_at,
            "evidence_status": evidence_status,
            "supersedes_cost_version_id": supersedes_cost_version_id,
            "created_by_membership_id": created_by_membership_id,
        }
        if existing is not None:
            if self._same_command(existing, **command):
                return _view(existing)
            raise CostConflictError("source reference already belongs to another cost command")

        if supersedes_cost_version_id is not None:
            superseded = self.session.scalar(
                select(CatalogCostVersionRow).where(
                    CatalogCostVersionRow.organization_id == self.organization_id,
                    CatalogCostVersionRow.catalog_sku_id == catalog_sku_id,
                    CatalogCostVersionRow.cost_version_id == supersedes_cost_version_id,
                )
            )
            if superseded is None:
                raise CostValidationError("superseded cost version not found for this SKU")

        row = CatalogCostVersionRow(
            organization_id=self.organization_id,
            source=normalized_source,
            source_reference=normalized_reference,
            **command,
        )
        self.session.add(row)
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            existing = self._by_reference(normalized_source, normalized_reference)
            if existing is not None and self._same_command(existing, **command):
                return _view(existing)
            raise CostConflictError("cost command conflicts with persisted data") from exc
        self._prepare()
        self.session.refresh(row)
        return _view(row)

    def get_cost_at(self, catalog_sku_id: int, at: datetime) -> CostValue:
        effective_at = _aware_utc(at)
        self._sku(catalog_sku_id)
        return self.get_costs_for_points([(catalog_sku_id, effective_at)])[(catalog_sku_id, effective_at)]

    def get_current_cost(self, catalog_sku_id: int) -> CostValue:
        return self.get_cost_at(catalog_sku_id, self.now())

    def set_current_cost(
        self, *, catalog_sku_id: int, amount_kopecks: int,
        expected_cost_version_id: int | None, source_reference: str,
        created_by_user_id: str,
    ) -> CostValue:
        """Interactive edit: server time, optimistic version and retry-safe identity.

        Lock the SKU, not the cost row: a first write has no cost row to lock.
        All interactive writers use this transaction boundary. Historical import
        remains a separate operation and must not be triggered by the editor.
        """
        if self.session.get_bind().dialect.name == "sqlite":
            connection = self.session.connection()
            if not connection.connection.driver_connection.in_transaction:
                self.session.execute(text("BEGIN IMMEDIATE"))
        self._sku(catalog_sku_id)
        self.session.scalar(select(CatalogSkuRow).where(
            CatalogSkuRow.organization_id == self.organization_id,
            CatalogSkuRow.catalog_sku_id == catalog_sku_id,
        ).with_for_update())
        existing = self._by_reference("manual-current", source_reference)
        effective_at = _db_utc(existing.effective_from) if existing else self.now()
        if existing is None:
            current = self.get_cost_at(catalog_sku_id, effective_at)
            if current.cost_version_id != expected_cost_version_id:
                raise CostConflictError("cost version changed; reload before saving")
        return self.set_cost(
            catalog_sku_id=catalog_sku_id, amount_kopecks=amount_kopecks,
            value_state="configured", effective_from=effective_at,
            source="manual-current", source_reference=source_reference,
            evidence_status="dated", supersedes_cost_version_id=expected_cost_version_id,
            created_by_user_id=created_by_user_id,
        )

    def replace_example_cost(
        self, *, catalog_sku_id: int, amount_kopecks: int,
        expected_cost_version_id: int, source_reference: str, created_by_user_id: str,
    ) -> CostValue:
        """Explicitly replace a user-requested example, retaining its audit history."""
        if self.session.get_bind().dialect.name == "sqlite":
            connection = self.session.connection()
            if not connection.connection.driver_connection.in_transaction:
                self.session.execute(text("BEGIN IMMEDIATE"))
        self._sku(catalog_sku_id)
        self.session.scalar(select(CatalogSkuRow).where(
            CatalogSkuRow.organization_id == self.organization_id,
            CatalogSkuRow.catalog_sku_id == catalog_sku_id,
        ).with_for_update())
        existing = self._by_reference('example-replacement', source_reference)
        example = self.session.scalar(select(CatalogCostVersionRow).where(
            CatalogCostVersionRow.organization_id == self.organization_id,
            CatalogCostVersionRow.catalog_sku_id == catalog_sku_id,
            CatalogCostVersionRow.cost_version_id == expected_cost_version_id,
            CatalogCostVersionRow.source == 'user-example',
            CatalogCostVersionRow.value_state == 'assumed',
        ))
        if example is None:
            raise CostConflictError('Only an explicitly marked example can be replaced historically')
        if existing is None and self.get_current_cost(catalog_sku_id).cost_version_id != expected_cost_version_id:
            raise CostConflictError('cost version changed; reload before saving')
        return self.set_cost(catalog_sku_id=catalog_sku_id, amount_kopecks=amount_kopecks,
            value_state='configured', effective_from=_db_utc(example.effective_from),
            source='example-replacement', source_reference=source_reference, evidence_status='dated',
            supersedes_cost_version_id=expected_cost_version_id, created_by_user_id=created_by_user_id)

    def get_costs_at(self, catalog_sku_ids: list[int], at: datetime) -> dict[int, CostValue]:
        for sku_id in catalog_sku_ids:
            self._validate_sku_id(sku_id)
        effective_at = _aware_utc(at)
        sku_ids = sorted(set(catalog_sku_ids))
        if not sku_ids:
            return {}
        resolved = self.get_costs_for_points([(sku_id, effective_at) for sku_id in sku_ids])
        return {
            sku_id: resolved[(sku_id, effective_at)]
            for sku_id in sku_ids
        }

    def get_costs_on_dates(
        self,
        catalog_sku_ids: list[int],
        at: list[datetime],
    ) -> dict[tuple[int, datetime], CostValue]:
        for sku_id in catalog_sku_ids:
            self._validate_sku_id(sku_id)
        sku_ids = sorted(set(catalog_sku_ids))
        instants = sorted({_aware_utc(value) for value in at})
        if not sku_ids or not instants:
            return {}
        return self.get_costs_for_points(
            [(sku_id, instant) for sku_id in sku_ids for instant in instants]
        )

    def get_costs_for_points(
        self,
        points: list[tuple[int, datetime]],
    ) -> dict[tuple[int, datetime], CostValue]:
        for sku_id, _ in points:
            self._validate_sku_id(sku_id)
        normalized = {
            instant: _aware_utc(instant)
            for instant in dict.fromkeys(instant for _, instant in points)
        }
        requested = sorted(
            {(sku_id, normalized[instant]) for sku_id, instant in points}
        )
        if not requested:
            return {}
        instants_by_sku: dict[int, list[datetime]] = {}
        for sku_id, instant in requested:
            instants_by_sku.setdefault(sku_id, []).append(instant)
        sku_ids = list(instants_by_sku)
        self._prepare()
        rows = self.session.execute(
            select(CatalogCostVersionRow.__table__)
            .where(
                CatalogCostVersionRow.organization_id == self.organization_id,
                CatalogCostVersionRow.catalog_sku_id.in_(sku_ids),
                CatalogCostVersionRow.effective_from
                <= max(instant for _, instant in requested),
            )
            .order_by(
                CatalogCostVersionRow.catalog_sku_id.asc(),
                CatalogCostVersionRow.effective_from.asc(),
                CatalogCostVersionRow.created_at.asc(),
                CatalogCostVersionRow.cost_version_id.asc(),
            )
        ).all()
        history: dict[int, list[CatalogCostVersionRow]] = {}
        for row in rows:
            history.setdefault(row.catalog_sku_id, []).append(row)

        resolved: dict[tuple[int, datetime], CostValue] = {}
        for sku_id in sku_ids:
            versions = history.get(sku_id, [])
            position = 0
            current_value = CostValue(None, sku_id, None, "missing")
            for instant in instants_by_sku[sku_id]:
                while position < len(versions) and _db_utc(
                    versions[position].effective_from
                ) <= instant:
                    candidate = _view(versions[position])
                    repeated_legacy_value = (
                        current_value.value_state == "configured"
                        and current_value.evidence_status == "dated"
                        and candidate.amount_kopecks == current_value.amount_kopecks
                        and candidate.value_state == "assumed"
                        and candidate.evidence_status == "undated"
                        and (candidate.source or "").startswith("legacy_")
                    )
                    # Reimporting the same value cannot revoke its dated evidence.
                    # A changed amount or an explicit missing value still takes effect.
                    if not repeated_legacy_value:
                        current_value = candidate
                    position += 1
                resolved[(sku_id, instant)] = current_value
        return resolved

    def revision(self) -> int:
        self._prepare()
        return int(
            self.session.scalar(
                select(func.max(CatalogCostVersionRow.cost_version_id)).where(
                    CatalogCostVersionRow.organization_id == self.organization_id
                )
            )
            or 0
        )

    def list_cost_history(self, catalog_sku_id: int) -> list[CostValue]:
        self._sku(catalog_sku_id)
        rows = self.session.scalars(
            select(CatalogCostVersionRow)
            .where(
                CatalogCostVersionRow.organization_id == self.organization_id,
                CatalogCostVersionRow.catalog_sku_id == catalog_sku_id,
            )
            .order_by(
                CatalogCostVersionRow.effective_from.desc(),
                CatalogCostVersionRow.created_at.desc(),
                CatalogCostVersionRow.cost_version_id.desc(),
            )
        ).all()
        return [_view(row) for row in rows]
