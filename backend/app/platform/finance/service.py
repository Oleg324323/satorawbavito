from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal

from sqlalchemy import case, delete, func, insert, literal, select, text, union_all
from sqlalchemy.orm import Session

from app.config import get_settings
from app.infra.db import get_session_factory, set_tenant_context
from app.platform.clock import utc_now
from app.platform.finance.orm import (
    WbFinanceOperationRow,
    WbFinanceSyncRunOperationRow,
    WbFinanceSyncRunRow,
    WbFinanceSyncRunSkuDailyPnlRollupRow,
    WbFinanceSyncRunSkuPnlRollupRow,
    WbFinanceSyncRunSkuRollupRow,
)
from app.platform.integrations.orm import MarketplaceAccountRow
from app.platform.period import MOSCOW, Period, PeriodTemporalState, PeriodValidationError

ReportType = Literal["main", "redemptions", "unknown"]
OperationKind = Literal["sale", "return", "correction", "other"]
_ADJUSTMENT_WORDS = ("штраф", "компенса", "коррект", "возврат")
_REPORT_TYPES: dict[object, ReportType] = {
    1: "main",
    "1": "main",
    "main": "main",
    "основной": "main",
    2: "redemptions",
    "2": "redemptions",
    "redemptions": "redemptions",
    "по выкупам": "redemptions",
}
logger = logging.getLogger(__name__)
FINANCE_FORMULA_VERSION = "wb-finance-v4-rrdate"
_PNL_FIELDS = (
    "nm_id",
    "seller_article",
    "operation_count",
    "revenue_kopecks",
    "sales_revenue_kopecks",
    "returns_revenue_kopecks",
    "main_revenue_kopecks",
    "redemptions_revenue_kopecks",
    "late_correction_revenue_kopecks",
    "unknown_revenue_kopecks",
    "sales_units",
    "returns_units",
    "net_units",
    "commission_kopecks",
    "logistics_kopecks",
    "storage_kopecks",
    "acceptance_kopecks",
    "penalty_kopecks",
    "deduction_kopecks",
    "additional_payment_kopecks",
    "acquiring_kopecks",
    "cashback_amount_kopecks",
    "cashback_discount_kopecks",
    "cashback_commission_change_kopecks",
    "payable_kopecks",
    "advertising_kopecks",
    "compensation_kopecks",
)
_NULLABLE_PNL_FIELDS = {
    "advertising_kopecks",
    "cashback_amount_kopecks",
    "cashback_discount_kopecks",
    "cashback_commission_change_kopecks",
    "payable_kopecks",
}


class FinanceNormalizationError(ValueError):
    pass


class FinanceAccountNotFound(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class FinanceOperation:
    operation_id: str
    organization_id: int
    marketplace_account_id: int
    source_identity: str
    payload_checksum: str
    rrd_id: int | None
    report_id: int | None
    report_type: ReportType
    operation_kind: OperationKind
    business_date: date | None
    correction_at: datetime | None
    is_late_correction: bool
    nm_id: int | None
    seller_article: str | None
    sign: int
    units: int
    revenue_kopecks: int
    commission_kopecks: int
    logistics_kopecks: int
    storage_kopecks: int
    acceptance_kopecks: int
    penalty_kopecks: int
    deduction_kopecks: int
    additional_payment_kopecks: int
    acquiring_kopecks: int
    cashback_amount_kopecks: int | None
    cashback_discount_kopecks: int | None
    cashback_commission_change_kopecks: int | None
    payable_kopecks: int | None = None
    advertising_kopecks: int | None = None
    compensation_kopecks: int = 0


@dataclass(frozen=True, slots=True)
class FinanceSnapshot:
    sync_run_id: str
    marketplace_account_id: int
    period: Period
    snapshot_checksum: str
    formula_version: str
    operation_count: int
    captured_at: datetime
    last_observed_at: datetime


@dataclass(frozen=True, slots=True)
class FinanceSummary:
    operation_count: int = 0
    sku_count: int = 0
    total_revenue_kopecks: int = 0
    main_revenue_kopecks: int = 0
    redemptions_revenue_kopecks: int = 0
    late_correction_revenue_kopecks: int = 0
    unknown_revenue_kopecks: int = 0
    sales_units: int = 0
    returns_units: int = 0
    net_units: int = 0


@dataclass(frozen=True, slots=True)
class FinanceSku:
    nm_id: int
    seller_article: str | None
    operation_count: int
    revenue_kopecks: int
    main_revenue_kopecks: int
    redemptions_revenue_kopecks: int
    late_correction_revenue_kopecks: int
    unknown_revenue_kopecks: int
    sales_units: int
    returns_units: int
    net_units: int


FinanceSourceState = Literal["ready", "partial", "future", "empty", "missing"]


@dataclass(frozen=True, slots=True)
class FinancePage:
    state: FinanceSourceState
    period: Period
    snapshot: FinanceSnapshot | None
    summary: FinanceSummary
    items: list[FinanceSku]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class FinancePnlFact:
    nm_id: int | None
    seller_article: str | None
    operation_count: int
    revenue_kopecks: int
    sales_revenue_kopecks: int
    returns_revenue_kopecks: int
    main_revenue_kopecks: int
    redemptions_revenue_kopecks: int
    late_correction_revenue_kopecks: int
    unknown_revenue_kopecks: int
    sales_units: int
    returns_units: int
    net_units: int
    commission_kopecks: int
    logistics_kopecks: int
    storage_kopecks: int
    acceptance_kopecks: int
    penalty_kopecks: int
    deduction_kopecks: int
    additional_payment_kopecks: int
    acquiring_kopecks: int
    cashback_amount_kopecks: int | None
    cashback_discount_kopecks: int | None
    cashback_commission_change_kopecks: int | None
    payable_kopecks: int | None = None
    payable_basis: bool = False
    advertising_kopecks: int | None = None
    compensation_kopecks: int = 0

    @property
    def loyalty_net_cost_kopecks(self) -> int | None:
        if (
            self.cashback_amount_kopecks is None
            or self.cashback_discount_kopecks is None
            or self.cashback_commission_change_kopecks is None
        ):
            return None
        if self.payable_basis:
            if self.payable_kopecks is None:
                return None
            # Discount compensation is already included in reconciled payable.
            return (
                self.cashback_amount_kopecks + self.cashback_commission_change_kopecks
            )
        return (
            self.cashback_amount_kopecks
            + self.cashback_commission_change_kopecks
            - self.cashback_discount_kopecks
        )


@dataclass(frozen=True, slots=True)
class FinancePnlDailyBasis:
    revenue_kopecks: int
    sales_units: int


@dataclass(frozen=True, slots=True)
class FinancePnlSource:
    state: FinanceSourceState
    period: Period
    snapshot: FinanceSnapshot | None
    facts: list[FinancePnlFact]
    daily_net_units: dict[tuple[int | None, date], int]
    daily_economics_basis: dict[tuple[int | None, date], FinancePnlDailyBasis]


def _integer(raw: Any) -> int | None:
    if raw in (None, "") or isinstance(raw, bool):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _positive_integer(raw: Any) -> int | None:
    value = _integer(raw)
    return value if value is not None and value > 0 else None


def _money(raw: Any) -> int:
    if isinstance(raw, bool):
        raise FinanceNormalizationError("invalid money value")
    if raw in (None, ""):
        return 0
    try:
        value = Decimal(str(raw).replace(",", ".")) * 100
    except InvalidOperation as exc:
        raise FinanceNormalizationError(f"invalid money value: {raw!r}") from exc
    if not value.is_finite():
        raise FinanceNormalizationError(f"invalid money value: {raw!r}")
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _first_money(row: dict[str, Any], *keys: str) -> int:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return _money(row[key])
    return 0


def _optional_first_money(row: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return _money(row[key])
    return None


def _text(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _instant(raw: Any) -> datetime | None:
    if raw in (None, ""):
        return None
    text = str(raw).strip()
    try:
        if len(text) == 10:
            return datetime.combine(
                date.fromisoformat(text), time.min, MOSCOW
            ).astimezone(timezone.utc)
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FinanceNormalizationError(f"invalid finance timestamp: {raw!r}") from exc
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=MOSCOW)
    return value.astimezone(timezone.utc)


def _report_type(raw: Any) -> ReportType:
    key = raw.casefold().strip() if isinstance(raw, str) else raw
    return _REPORT_TYPES.get(key, "unknown")


def _hash(payload: Any) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def normalize_operation(
    row: dict[str, Any],
    organization_id: int,
    marketplace_account_id: int,
    period: Period,
) -> FinanceOperation:
    if organization_id < 1 or marketplace_account_id < 1:
        raise FinanceNormalizationError(
            "organization and marketplace account must be positive"
        )

    rrd_id = _positive_integer(row.get("rrdId") or row.get("rrd_id"))
    report_id = _positive_integer(row.get("reportId") or row.get("report_id"))
    report_type = _report_type(row.get("reportType") or row.get("report_type"))
    doc_type = _text(row, "docTypeName", "doc_type_name").casefold()
    from app.wb_api.financial_reports import is_merchandise
    trade = is_merchandise({"docTypeName": doc_type,
        "sellerOperName": _text(row, "sellerOperName", "seller_oper_name")})
    raw_quantity = row.get("quantity", row.get("saleQuantity"))
    if trade and (type(raw_quantity) is not int or raw_quantity < 0):
        raise FinanceNormalizationError("invalid trade quantity")
    if _text(row, "currency").upper() not in {"", "RUB", "RUR", "643"}:
        raise FinanceNormalizationError("unsupported finance currency")
    document_sign = -1 if doc_type == "возврат" else 1
    quantity = max(0, _integer(row.get("quantity") or row.get("saleQuantity")) or 0)
    units = document_sign * quantity if trade else 0
    observed_revenue = _optional_first_money(row, "retailAmount", "retail_amount")
    revenue = 0
    if trade:
        retail_amount = observed_revenue
        if retail_amount is None:
            raise FinanceNormalizationError("missing trade revenue")
        revenue = document_sign * retail_amount
    commission = document_sign * _first_money(
        row,
        "ppvzSalesCommission",
        "ppvz_sales_commission",
        "commission",
        "commissionRub",
    )
    acquiring = document_sign * _first_money(row, "acquiringFee", "acquiring_fee")
    payable = _optional_first_money(row, "forPay", "ppvz_for_pay")
    # A service/compensation document may be labelled Продажа. Its payout is
    # an adjustment, never merchandise or negative retail commission.
    nontrade_payable = (document_sign * payable) if not trade and payable is not None else 0
    if not trade:
        commission = acquiring = 0
    deduction = _first_money(row, "deduction", "deductionRub")
    bonus_type = _text(row, "bonusTypeName", "bonus_type_name")
    is_promotion = "wb продвижение" in bonus_type.casefold()
    advertising = deduction if is_promotion else None if deduction and not bonus_type else 0
    if is_promotion:
        deduction = 0
    reward_adjustment = _first_money(row, "additionalPayment", "additional_payment")
    operation_name = _text(row, "sellerOperName", "seller_oper_name").casefold()
    compensation = nontrade_payable if "компенсац" in operation_name and "коррек" not in operation_name else 0
    payment_schedule = _first_money(row, "paymentSchedule", "payment_schedule")
    correction_at = _instant(row.get("rrDate") or row.get("rr_date"))
    sale_at = _instant(row.get("saleDt") or row.get("sale_dt") or row.get("date"))
    business_date = (
        sale_at.astimezone(MOSCOW).date()
        if sale_at
        else (correction_at.astimezone(MOSCOW).date() if correction_at else None)
    )
    adjustment_text = (
        f"{_text(row, 'sellerOperName', 'seller_oper_name')} {bonus_type}".casefold()
    )
    is_late = bool(row.get("isLateCorrection")) or bool(
        correction_at and correction_at.astimezone(MOSCOW).date() > period.date_to
    )
    is_adjustment = is_late or any(
        word in adjustment_text for word in _ADJUSTMENT_WORDS
    )
    operation_kind: OperationKind = ("sale" if trade and doc_type == "продажа" else
        "return" if trade and doc_type == "возврат" else "correction" if is_adjustment else "other")
    amounts = {
        "units": units,
        "revenue_kopecks": revenue,
        "commission_kopecks": commission,
        "logistics_kopecks": _first_money(
            row, "deliveryService", "delivery_service", "deliveryRub", "delivery_rub"
        ),
        "storage_kopecks": _first_money(
            row, "paidStorage", "storageFee", "storage_fee"
        ),
        "acceptance_kopecks": _first_money(row, "paidAcceptance", "acceptance"),
        "penalty_kopecks": _first_money(row, "penalty", "penaltyRub"),
        "deduction_kopecks": deduction,
        "additional_payment_kopecks": nontrade_payable - payment_schedule - reward_adjustment,
        "acquiring_kopecks": acquiring,
        "advertising_kopecks": advertising,
        "compensation_kopecks": compensation,
        "payable_kopecks": None if payable is None else document_sign * payable,
        "cashback_amount_kopecks": _optional_first_money(
            row, "cashbackAmount", "cashback_amount"
        ),
        "cashback_discount_kopecks": _optional_first_money(
            row, "cashbackDiscount", "cashback_discount"
        ),
        "cashback_commission_change_kopecks": _optional_first_money(
            row, "cashbackCommissionChange", "cashback_commission_change"
        ),
    }
    normalized = {
        "rrd_id": rrd_id,
        "report_id": report_id,
        "report_type": report_type,
        "operation_kind": operation_kind,
        "business_date": business_date.isoformat() if business_date else None,
        "correction_at": correction_at.isoformat() if correction_at else None,
        "is_late_correction": is_late,
        "nm_id": _positive_integer(
            row.get("nmId") or row.get("nmID") or row.get("nm_id")
        ),
        "seller_article": _text(row, "vendorCode", "vendor_code") or None,
        "doc_type": doc_type,
        "srid": _text(row, "srid") or None,
        **amounts,
    }
    # Immutable evidence must change even for an unclassified correction;
    # ignoring its merchandise quantity must not hide source revisions.
    payload_checksum = _hash({"normalized": normalized, "source": row})
    source_identity = (
        f"rrd:{rrd_id}"
        if rrd_id and rrd_id > 0
        else f"fp:{_hash(normalized | {'rrd_id': None})}"
    )
    operation_id = _hash(
        {
            "organization_id": organization_id,
            "marketplace_account_id": marketplace_account_id,
            "source_identity": source_identity,
            "payload_checksum": payload_checksum,
        }
    )
    monetary_sign = next(
        (1 if value > 0 else -1 for value in amounts.values() if value), 0
    )
    sign = document_sign if doc_type in {"продажа", "возврат"} else monetary_sign
    return FinanceOperation(
        operation_id=operation_id,
        organization_id=organization_id,
        marketplace_account_id=marketplace_account_id,
        source_identity=source_identity,
        payload_checksum=payload_checksum,
        rrd_id=rrd_id,
        report_id=report_id,
        report_type=report_type,
        operation_kind=operation_kind,
        business_date=business_date,
        correction_at=correction_at,
        is_late_correction=is_late,
        nm_id=normalized["nm_id"],
        seller_article=normalized["seller_article"],
        sign=sign,
        **amounts,
    )


def normalize_operations(
    rows: list[dict[str, Any]],
    *,
    organization_id: int,
    marketplace_account_id: int,
    period: Period,
) -> list[FinanceOperation]:
    unique: dict[str, FinanceOperation] = {}
    for row in rows:
        operation = normalize_operation(
            row, organization_id, marketplace_account_id, period
        )
        previous = unique.get(operation.source_identity)
        if (
            previous is not None
            and previous.payload_checksum != operation.payload_checksum
        ):
            raise FinanceNormalizationError(
                f"source identity {operation.source_identity} has conflicting payloads"
            )
        unique[operation.source_identity] = operation
    return sorted(unique.values(), key=lambda item: item.operation_id)


def snapshot_checksum(operations: list[FinanceOperation]) -> str:
    return _hash(
        [
            operation.operation_id
            for operation in sorted(operations, key=lambda item: item.operation_id)
        ]
    )


def operation_values(operation: FinanceOperation) -> dict[str, Any]:
    return asdict(operation)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _insert_missing(
    session: Session,
    model: (
        type[WbFinanceOperationRow]
        | type[WbFinanceSyncRunOperationRow]
        | type[WbFinanceSyncRunSkuRollupRow]
    ),
    values: list[dict[str, Any]],
    conflict_columns: list[str],
) -> None:
    if not values:
        return
    dialect = session.get_bind().dialect.name
    for index in range(0, len(values), 1_000):
        chunk = values[index : index + 1_000]
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as dialect_insert
        elif dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as dialect_insert
        else:
            session.execute(insert(model), chunk)
            continue
        statement = (
            dialect_insert(model)
            .values(chunk)
            .on_conflict_do_nothing(index_elements=conflict_columns)
        )
        session.execute(statement)


def _insert_memberships(session: Session, values: list[dict[str, Any]]) -> None:
    if not values:
        return
    if session.get_bind().dialect.name != "postgresql":
        _insert_missing(
            session,
            WbFinanceSyncRunOperationRow,
            values,
            [
                "organization_id",
                "marketplace_account_id",
                "sync_run_id",
                "operation_id",
            ],
        )
        return
    first = values[0]
    session.execute(
        text(
            """
            CREATE TEMPORARY TABLE wb_finance_membership_changes_stage (
                operation_id varchar(64) PRIMARY KEY,
                is_present boolean NOT NULL
            ) ON COMMIT DROP
            """
        )
    )
    session.execute(
        text(
            """
            INSERT INTO wb_finance_membership_changes_stage (
                operation_id,
                is_present
            )
            SELECT operation_id, is_present
            FROM jsonb_to_recordset(CAST(:changes AS jsonb)) AS change(
                operation_id varchar(64),
                is_present boolean
            )
            """
        ),
        {
            "changes": json.dumps(
                [
                    {
                        "operation_id": value["operation_id"],
                        "is_present": value["is_present"],
                    }
                    for value in values
                ],
                separators=(",", ":"),
            )
        },
    )
    session.execute(text("ANALYZE wb_finance_membership_changes_stage"))
    result = session.execute(
        text(
            """
            INSERT INTO wb_finance_sync_run_operations (
                organization_id,
                marketplace_account_id,
                sync_run_id,
                operation_id,
                is_present
            )
            SELECT
                :organization_id,
                :marketplace_account_id,
                :sync_run_id,
                operation.operation_id,
                change.is_present
            FROM wb_finance_membership_changes_stage AS change
            JOIN wb_finance_operations AS operation
              ON operation.operation_id = change.operation_id
             AND operation.organization_id = :organization_id
             AND operation.marketplace_account_id = :marketplace_account_id
            """
        ),
        {
            "organization_id": first["organization_id"],
            "marketplace_account_id": first["marketplace_account_id"],
            "sync_run_id": first["sync_run_id"],
        },
    )
    if result.rowcount != len(values):
        raise FinanceNormalizationError("snapshot membership operation is missing")


def _snapshot(row: WbFinanceSyncRunRow) -> FinanceSnapshot:
    return FinanceSnapshot(
        sync_run_id=row.sync_run_id,
        marketplace_account_id=row.marketplace_account_id,
        period=Period(row.date_from, row.date_to),
        snapshot_checksum=row.snapshot_checksum,
        formula_version=row.formula_version,
        operation_count=row.operation_count,
        captured_at=_utc(row.captured_at),
        last_observed_at=_utc(row.last_observed_at),
    )


def _source_state(
    run: WbFinanceSyncRunRow, temporal_state: PeriodTemporalState, operation_count: int
) -> FinanceSourceState:
    if (
        temporal_state == "partial"
        or _utc(run.last_observed_at)
        < Period(run.date_from, run.date_to).end_exclusive_at
    ):
        return "partial"
    return "empty" if operation_count == 0 else "ready"


class FinanceService:
    def __init__(
        self,
        session: Session,
        organization_id: int,
        *,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        if organization_id < 1:
            raise FinanceNormalizationError("organization must be positive")
        self.session = session
        self.organization_id = organization_id
        self.now = now

    def _prepare(self) -> None:
        set_tenant_context(self.session, self.organization_id)

    def _account(self, marketplace_account_id: int) -> MarketplaceAccountRow:
        self._prepare()
        account = self.session.scalar(
            select(MarketplaceAccountRow).where(
                MarketplaceAccountRow.organization_id == self.organization_id,
                MarketplaceAccountRow.marketplace_account_id == marketplace_account_id,
                MarketplaceAccountRow.marketplace == "wb",
            )
        )
        if account is None:
            raise FinanceAccountNotFound("WB marketplace account not found")
        return account

    def _latest_snapshot(
        self, marketplace_account_id: int
    ) -> WbFinanceSyncRunRow | None:
        return self.session.scalar(
            select(WbFinanceSyncRunRow)
            .where(
                WbFinanceSyncRunRow.organization_id == self.organization_id,
                WbFinanceSyncRunRow.marketplace_account_id == marketplace_account_id,
                WbFinanceSyncRunRow.formula_version == FINANCE_FORMULA_VERSION,
                WbFinanceSyncRunRow.is_materialized.is_(True),
            )
            .order_by(
                WbFinanceSyncRunRow.created_at.desc(),
                WbFinanceSyncRunRow.captured_at.desc(),
                WbFinanceSyncRunRow.sync_run_id.desc(),
            )
            .limit(1)
        )

    def _effective_membership(self, run: WbFinanceSyncRunRow) -> Any:
        runs = WbFinanceSyncRunRow.__table__
        memberships = WbFinanceSyncRunOperationRow.__table__
        if run.parent_sync_run_id is None:
            return (
                select(memberships.c.operation_id)
                .where(
                    memberships.c.organization_id == self.organization_id,
                    memberships.c.marketplace_account_id
                    == run.marketplace_account_id,
                    memberships.c.sync_run_id == run.sync_run_id,
                    memberships.c.is_present.is_(True),
                )
                .subquery("wb_finance_snapshot_memberships")
            )
        chain = (
            select(
                runs.c.sync_run_id,
                runs.c.parent_sync_run_id,
                literal(0).label("depth"),
            )
            .where(
                runs.c.organization_id == self.organization_id,
                runs.c.marketplace_account_id == run.marketplace_account_id,
                runs.c.sync_run_id == run.sync_run_id,
                runs.c.is_materialized.is_(True),
            )
            .cte("wb_finance_snapshot_chain", recursive=True)
        )
        parent = runs.alias("wb_finance_parent_snapshot")
        chain = chain.union_all(
            select(
                parent.c.sync_run_id,
                parent.c.parent_sync_run_id,
                (chain.c.depth + 1).label("depth"),
            )
            .select_from(
                parent.join(
                    chain,
                    parent.c.sync_run_id == chain.c.parent_sync_run_id,
                )
            )
            .where(
                parent.c.organization_id == self.organization_id,
                parent.c.marketplace_account_id == run.marketplace_account_id,
                parent.c.is_materialized.is_(True),
            )
        )
        parent_parent_id = self.session.scalar(
            select(runs.c.parent_sync_run_id).where(
                runs.c.organization_id == self.organization_id,
                runs.c.marketplace_account_id == run.marketplace_account_id,
                runs.c.sync_run_id == run.parent_sync_run_id,
            )
        )
        append_only_delta = False
        if parent_parent_id is None:
            latest = (
                select(memberships.c.operation_id, memberships.c.is_present)
                .where(
                    memberships.c.organization_id == self.organization_id,
                    memberships.c.marketplace_account_id
                    == run.marketplace_account_id,
                    memberships.c.sync_run_id == run.sync_run_id,
                )
                .subquery("wb_finance_snapshot_delta_latest")
            )
            append_only_delta = not bool(
                self.session.scalar(
                    select(
                        select(literal(1))
                        .where(
                            memberships.c.organization_id
                            == self.organization_id,
                            memberships.c.marketplace_account_id
                            == run.marketplace_account_id,
                            memberships.c.sync_run_id == run.sync_run_id,
                            memberships.c.is_present.is_(False),
                        )
                        .exists()
                    )
                )
            )
        else:
            ranked = (
                select(
                    memberships.c.operation_id,
                    memberships.c.is_present,
                    func.row_number()
                    .over(
                        partition_by=memberships.c.operation_id,
                        order_by=chain.c.depth,
                    )
                    .label("position"),
                )
                .select_from(
                    memberships.join(
                        chain,
                        memberships.c.sync_run_id == chain.c.sync_run_id,
                    )
                )
                .where(
                    memberships.c.organization_id == self.organization_id,
                    memberships.c.marketplace_account_id
                    == run.marketplace_account_id,
                    chain.c.parent_sync_run_id.is_not(None),
                )
                .subquery("wb_finance_snapshot_delta_ranked")
            )
            latest = (
                select(ranked.c.operation_id, ranked.c.is_present)
                .where(ranked.c.position == 1)
                .subquery("wb_finance_snapshot_delta_latest")
            )
        base = (
            select(memberships.c.operation_id)
            .select_from(
                memberships.join(
                    chain,
                    memberships.c.sync_run_id == chain.c.sync_run_id,
                )
            )
            .where(
                memberships.c.organization_id == self.organization_id,
                memberships.c.marketplace_account_id == run.marketplace_account_id,
                chain.c.parent_sync_run_id.is_(None),
                memberships.c.is_present.is_(True),
            )
            .subquery("wb_finance_snapshot_base")
        )
        if append_only_delta:
            return union_all(
                select(base.c.operation_id),
                select(latest.c.operation_id).where(latest.c.is_present.is_(True)),
            ).subquery("wb_finance_snapshot_memberships")
        overridden = (
            select(literal(1))
            .where(latest.c.operation_id == base.c.operation_id)
            .exists()
        )
        return union_all(
            select(base.c.operation_id).where(~overridden),
            select(latest.c.operation_id).where(latest.c.is_present.is_(True)),
        ).subquery("wb_finance_snapshot_memberships")

    def _operation_rollup_rows(
        self, run: WbFinanceSyncRunRow, period: Period
    ) -> list[Any]:
        operation = WbFinanceOperationRow
        membership = self._effective_membership(run)
        criteria = [
            operation.organization_id == self.organization_id,
            operation.marketplace_account_id == run.marketplace_account_id,
        ]
        criteria.extend((operation.correction_at >= period.start_at,
                         operation.correction_at < period.end_exclusive_at))
        relation = membership.join(
            operation.__table__,
            membership.c.operation_id == operation.operation_id,
        )
        late = operation.is_late_correction.is_(True)
        main = (operation.report_type == "main") & ~late
        redemptions = (operation.report_type == "redemptions") & ~late
        unknown = (operation.report_type == "unknown") & ~late

        def summed(condition: Any, column: Any) -> Any:
            return func.coalesce(func.sum(case((condition, column), else_=0)), 0)

        return self.session.execute(
            select(
                operation.nm_id,
                func.max(operation.seller_article),
                func.count(operation.operation_id),
                func.coalesce(func.sum(operation.revenue_kopecks), 0),
                summed(main, operation.revenue_kopecks),
                summed(redemptions, operation.revenue_kopecks),
                summed(late, operation.revenue_kopecks),
                summed(unknown, operation.revenue_kopecks),
                summed(operation.units > 0, operation.units),
                summed(operation.units < 0, -operation.units),
                func.coalesce(func.sum(operation.units), 0),
            )
            .select_from(relation)
            .where(*criteria)
            .group_by(operation.nm_id)
        ).all()

    def _materialized_rollup_rows(self, run: WbFinanceSyncRunRow) -> list[Any]:
        rollup = WbFinanceSyncRunSkuRollupRow
        return [
            (None if row[0] == 0 else row[0], *row[1:])
            for row in self.session.execute(
                select(
                    rollup.nm_id,
                    rollup.seller_article,
                    rollup.operation_count,
                    rollup.revenue_kopecks,
                    rollup.main_revenue_kopecks,
                    rollup.redemptions_revenue_kopecks,
                    rollup.late_correction_revenue_kopecks,
                    rollup.unknown_revenue_kopecks,
                    rollup.sales_units,
                    rollup.returns_units,
                    rollup.net_units,
                ).where(
                    rollup.organization_id == self.organization_id,
                    rollup.marketplace_account_id == run.marketplace_account_id,
                    rollup.sync_run_id == run.sync_run_id,
                )
            ).all()
        ]

    def _materialize_rollup(self, run: WbFinanceSyncRunRow) -> None:
        self.session.execute(
            delete(WbFinanceSyncRunSkuRollupRow).where(
                WbFinanceSyncRunSkuRollupRow.organization_id
                == self.organization_id,
                WbFinanceSyncRunSkuRollupRow.marketplace_account_id
                == run.marketplace_account_id,
                WbFinanceSyncRunSkuRollupRow.sync_run_id == run.sync_run_id,
            )
        )
        rows = self._operation_rollup_rows(
            run, Period(run.date_from, run.date_to)
        )
        _insert_missing(
            self.session,
            WbFinanceSyncRunSkuRollupRow,
            [
                {
                    "organization_id": self.organization_id,
                    "marketplace_account_id": run.marketplace_account_id,
                    "sync_run_id": run.sync_run_id,
                    "nm_id": int(row[0] or 0),
                    "seller_article": row[1],
                    "operation_count": int(row[2]),
                    "revenue_kopecks": int(row[3]),
                    "main_revenue_kopecks": int(row[4]),
                    "redemptions_revenue_kopecks": int(row[5]),
                    "late_correction_revenue_kopecks": int(row[6]),
                    "unknown_revenue_kopecks": int(row[7]),
                    "sales_units": int(row[8]),
                    "returns_units": int(row[9]),
                    "net_units": int(row[10]),
                }
                for row in rows
            ],
            [
                "organization_id",
                "marketplace_account_id",
                "sync_run_id",
                "nm_id",
            ],
        )

    def _pnl_operation_rows(
        self,
        run: WbFinanceSyncRunRow,
        period: Period,
        *,
        by_day: bool = False,
    ) -> list[Any]:
        operation = WbFinanceOperationRow
        membership = self._effective_membership(run)
        criteria = [
            operation.organization_id == self.organization_id,
            operation.marketplace_account_id == run.marketplace_account_id,
        ]
        criteria.extend((operation.correction_at >= period.start_at,
                         operation.correction_at < period.end_exclusive_at))
        relation = membership.join(
            operation.__table__,
            membership.c.operation_id == operation.operation_id,
        )
        late = operation.is_late_correction.is_(True)
        main = (operation.report_type == "main") & ~late
        redemptions = (operation.report_type == "redemptions") & ~late
        unknown = (operation.report_type == "unknown") & ~late

        def summed(condition: Any, column: Any) -> Any:
            return func.coalesce(func.sum(case((condition, column), else_=0)), 0)

        def complete_sum(column: Any) -> Any:
            return case(
                (
                    func.count(column) == func.count(operation.operation_id),
                    func.coalesce(func.sum(column), 0),
                ),
                else_=None,
            )

        dimensions = (
            (operation.business_date, operation.nm_id)
            if by_day
            else (operation.nm_id,)
        )
        return self.session.execute(
            select(
                *dimensions,
                func.max(operation.seller_article),
                func.count(operation.operation_id),
                func.coalesce(func.sum(operation.revenue_kopecks), 0),
                summed(operation.operation_kind == "sale", operation.revenue_kopecks),
                summed(operation.operation_kind == "return", -operation.revenue_kopecks),
                summed(main, operation.revenue_kopecks),
                summed(redemptions, operation.revenue_kopecks),
                summed(late, operation.revenue_kopecks),
                summed(unknown, operation.revenue_kopecks),
                summed(operation.units > 0, operation.units),
                summed(operation.units < 0, -operation.units),
                func.coalesce(func.sum(operation.units), 0),
                func.coalesce(
                    func.sum(case(
                        (
                            operation.payable_kopecks.is_not(None) & operation.operation_kind.in_(("sale", "return")),
                            operation.revenue_kopecks
                            - operation.payable_kopecks
                            - operation.acquiring_kopecks,
                        ),
                        else_=operation.commission_kopecks,
                    )),
                    0,
                ),
                func.coalesce(func.sum(operation.logistics_kopecks), 0),
                func.coalesce(func.sum(operation.storage_kopecks), 0),
                func.coalesce(func.sum(operation.acceptance_kopecks), 0),
                func.coalesce(func.sum(operation.penalty_kopecks), 0),
                func.coalesce(func.sum(operation.deduction_kopecks), 0),
                func.coalesce(func.sum(operation.additional_payment_kopecks), 0),
                func.coalesce(func.sum(operation.acquiring_kopecks), 0),
                complete_sum(operation.cashback_amount_kopecks),
                complete_sum(operation.cashback_discount_kopecks),
                complete_sum(operation.cashback_commission_change_kopecks),
                complete_sum(operation.payable_kopecks),
                complete_sum(operation.advertising_kopecks),
                func.coalesce(func.sum(operation.compensation_kopecks), 0),
            )
            .select_from(relation)
            .where(*criteria)
            .group_by(*dimensions)
        ).all()

    def _materialized_pnl_rows(self, run: WbFinanceSyncRunRow) -> list[Any]:
        rollup = WbFinanceSyncRunSkuPnlRollupRow
        return [
            (None if row[0] == 0 else row[0], *row[1:])
            for row in self.session.execute(
                select(
                    rollup.nm_id,
                    rollup.seller_article,
                    rollup.operation_count,
                    rollup.revenue_kopecks,
                    rollup.sales_revenue_kopecks,
                    rollup.returns_revenue_kopecks,
                    rollup.main_revenue_kopecks,
                    rollup.redemptions_revenue_kopecks,
                    rollup.late_correction_revenue_kopecks,
                    rollup.unknown_revenue_kopecks,
                    rollup.sales_units,
                    rollup.returns_units,
                    rollup.net_units,
                    rollup.commission_kopecks,
                    rollup.logistics_kopecks,
                    rollup.storage_kopecks,
                    rollup.acceptance_kopecks,
                    rollup.penalty_kopecks,
                    rollup.deduction_kopecks,
                    rollup.additional_payment_kopecks,
                    rollup.acquiring_kopecks,
                    rollup.cashback_amount_kopecks,
                    rollup.cashback_discount_kopecks,
                    rollup.cashback_commission_change_kopecks,
                    rollup.payable_kopecks,
                    rollup.advertising_kopecks,
                    rollup.compensation_kopecks,
                ).where(
                    rollup.organization_id == self.organization_id,
                    rollup.marketplace_account_id == run.marketplace_account_id,
                    rollup.sync_run_id == run.sync_run_id,
                )
            ).all()
        ]

    def _materialize_pnl_rollup(self, run: WbFinanceSyncRunRow) -> None:
        self.session.execute(
            delete(WbFinanceSyncRunSkuPnlRollupRow).where(
                WbFinanceSyncRunSkuPnlRollupRow.organization_id
                == self.organization_id,
                WbFinanceSyncRunSkuPnlRollupRow.marketplace_account_id
                == run.marketplace_account_id,
                WbFinanceSyncRunSkuPnlRollupRow.sync_run_id == run.sync_run_id,
            )
        )
        rows = self._pnl_operation_rows(run, Period(run.date_from, run.date_to))
        _insert_missing(
            self.session,
            WbFinanceSyncRunSkuPnlRollupRow,
            [
                {
                    "organization_id": self.organization_id,
                    "marketplace_account_id": run.marketplace_account_id,
                    "sync_run_id": run.sync_run_id,
                    **{
                        field: (
                            value
                            if field == "seller_article"
                            or field in _NULLABLE_PNL_FIELDS
                            and value is None
                            else int(value or 0)
                        )
                        for field, value in zip(_PNL_FIELDS, row, strict=True)
                    },
                }
                for row in rows
            ],
            [
                "organization_id",
                "marketplace_account_id",
                "sync_run_id",
                "nm_id",
            ],
        )

    def _materialized_daily_pnl_rows(
        self,
        run: WbFinanceSyncRunRow,
        period: Period,
    ) -> list[Any]:
        rollup = WbFinanceSyncRunSkuDailyPnlRollupRow
        return [
            (None if row[0] == 0 else row[0], *row[1:])
            for row in self.session.execute(
                select(
                    rollup.nm_id,
                    func.max(rollup.seller_article),
                    *(
                        case(
                            (
                                func.count(getattr(rollup, field)) == func.count(),
                                func.coalesce(func.sum(getattr(rollup, field)), 0),
                            ),
                            else_=None,
                        )
                        if field in _NULLABLE_PNL_FIELDS
                        else func.coalesce(func.sum(getattr(rollup, field)), 0)
                        for field in _PNL_FIELDS[2:]
                    ),
                )
                .where(
                    rollup.organization_id == self.organization_id,
                    rollup.marketplace_account_id == run.marketplace_account_id,
                    rollup.sync_run_id == run.sync_run_id,
                    rollup.business_date.between(period.date_from, period.date_to),
                )
                .group_by(rollup.nm_id)
            ).all()
        ]

    def _materialized_daily_bases(
        self,
        run: WbFinanceSyncRunRow,
        period: Period,
        *,
        include_business_dates_outside_period: bool = False,
    ) -> tuple[
        dict[tuple[int | None, date], int],
        dict[tuple[int | None, date], FinancePnlDailyBasis],
    ]:
        rollup = WbFinanceSyncRunSkuDailyPnlRollupRow
        rows = self.session.execute(
            select(
                rollup.nm_id,
                rollup.business_date,
                rollup.net_units,
                rollup.revenue_kopecks,
                rollup.sales_units,
            ).where(
                rollup.organization_id == self.organization_id,
                rollup.marketplace_account_id == run.marketplace_account_id,
                rollup.sync_run_id == run.sync_run_id,
                literal(True) if include_business_dates_outside_period else rollup.business_date.between(period.date_from, period.date_to),
                (rollup.net_units != 0)
                | (rollup.revenue_kopecks != 0)
                | (rollup.sales_units != 0),
            )
        ).all()
        return (
            {
                (None if row[0] == 0 else row[0], row[1]): int(row[2])
                for row in rows
                if int(row[2]) != 0
            },
            {
                (None if row[0] == 0 else row[0], row[1]): FinancePnlDailyBasis(
                    int(row[3]), int(row[4])
                )
                for row in rows
                if int(row[3]) != 0 or int(row[4]) != 0
            },
        )

    def _materialize_daily_pnl_rollup(self, run: WbFinanceSyncRunRow) -> None:
        rollup = WbFinanceSyncRunSkuDailyPnlRollupRow
        self.session.execute(
            delete(rollup).where(
                rollup.organization_id == self.organization_id,
                rollup.marketplace_account_id == run.marketplace_account_id,
                rollup.sync_run_id == run.sync_run_id,
            )
        )
        rows = self._pnl_operation_rows(
            run,
            Period(run.date_from, run.date_to),
            by_day=True,
        )
        fields = ("business_date", *_PNL_FIELDS)
        _insert_missing(
            self.session,
            rollup,
            [
                {
                    "organization_id": self.organization_id,
                    "marketplace_account_id": run.marketplace_account_id,
                    "sync_run_id": run.sync_run_id,
                    **{
                        field: (
                            value
                            if field in {"business_date", "seller_article"}
                            or field in _NULLABLE_PNL_FIELDS
                            and value is None
                            else int(value or 0)
                        )
                        for field, value in zip(fields, row, strict=True)
                    },
                }
                for row in rows
            ],
            [
                "organization_id",
                "marketplace_account_id",
                "sync_run_id",
                "business_date",
                "nm_id",
            ],
        )

    def _daily_net_units(
        self, run: WbFinanceSyncRunRow, period: Period
    ) -> dict[tuple[int | None, date], int]:
        operation = WbFinanceOperationRow
        membership = self._effective_membership(run)
        rows = self.session.execute(
            select(
                operation.nm_id,
                operation.business_date,
                func.coalesce(func.sum(operation.units), 0),
            )
            .select_from(
                membership.join(
                    operation.__table__,
                    membership.c.operation_id == operation.operation_id,
                )
            )
            .where(
                operation.organization_id == self.organization_id,
                operation.marketplace_account_id == run.marketplace_account_id,
                operation.correction_at >= period.start_at,
                operation.correction_at < period.end_exclusive_at,
            )
            .group_by(operation.nm_id, operation.business_date)
        ).all()
        return {
            (row[0], row[1]): int(row[2])
            for row in rows
            if row[1] is not None and int(row[2]) != 0
        }

    def _daily_economics_basis(
        self, run: WbFinanceSyncRunRow, period: Period
    ) -> dict[tuple[int | None, date], FinancePnlDailyBasis]:
        rows = self._pnl_operation_rows(run, period, by_day=True)
        revenue_index = 1 + _PNL_FIELDS.index("revenue_kopecks")
        sales_units_index = 1 + _PNL_FIELDS.index("sales_units")
        return {
            (row[1], row[0]): FinancePnlDailyBasis(
                int(row[revenue_index]), int(row[sales_units_index])
            )
            for row in rows
            if int(row[revenue_index]) != 0 or int(row[sales_units_index]) != 0
        }

    def ingest_snapshot(
        self,
        marketplace_account_id: int,
        period: Period,
        rows: list[dict[str, Any]],
        *,
        observed_at: datetime | None = None,
    ) -> FinanceSnapshot:
        self._account(marketplace_account_id)
        observed = _utc(observed_at or self.now())
        operations = normalize_operations(
            rows,
            organization_id=self.organization_id,
            marketplace_account_id=marketplace_account_id,
            period=period,
        )
        checksum = snapshot_checksum(operations)
        existing = self.session.scalar(
            select(WbFinanceSyncRunRow).where(
                WbFinanceSyncRunRow.organization_id == self.organization_id,
                WbFinanceSyncRunRow.marketplace_account_id == marketplace_account_id,
                WbFinanceSyncRunRow.date_from == period.date_from,
                WbFinanceSyncRunRow.date_to == period.date_to,
                WbFinanceSyncRunRow.snapshot_checksum == checksum,
                WbFinanceSyncRunRow.formula_version == FINANCE_FORMULA_VERSION,
            )
        )
        if existing is not None and existing.is_materialized:
            changed = False
            try:
                if not existing.is_rollup_materialized:
                    self._materialize_rollup(existing)
                    existing.is_rollup_materialized = True
                    changed = True
                if not existing.is_pnl_rollup_materialized:
                    self._materialize_pnl_rollup(existing)
                    existing.is_pnl_rollup_materialized = True
                    changed = True
                if not existing.is_daily_pnl_rollup_materialized:
                    self._materialize_daily_pnl_rollup(existing)
                    existing.is_daily_pnl_rollup_materialized = True
                    changed = True
                if _utc(existing.last_observed_at) < observed:
                    existing.last_observed_at = observed
                    changed = True
                if changed:
                    self.session.commit()
                    self._prepare()
            except Exception:
                self.session.rollback()
                self._prepare()
                raise
            return _snapshot(existing)

        operation_by_id = {
            operation.operation_id: operation for operation in operations
        }
        desired_ids = set(operation_by_id)
        parent = (
            self.session.scalar(
                select(WbFinanceSyncRunRow).where(
                    WbFinanceSyncRunRow.organization_id == self.organization_id,
                    WbFinanceSyncRunRow.marketplace_account_id
                    == marketplace_account_id,
                    WbFinanceSyncRunRow.sync_run_id
                    == existing.parent_sync_run_id,
                    WbFinanceSyncRunRow.is_materialized.is_(True),
                )
            )
            if existing is not None and existing.parent_sync_run_id is not None
            else None if existing is not None else self._latest_snapshot(marketplace_account_id)
        )
        previous_ids = (
            set(
                self.session.scalars(
                    select(
                        self._effective_membership(parent).c.operation_id
                    )
                ).all()
            )
            if parent is not None
            else set()
        )
        added_ids = desired_ids - previous_ids
        removed_ids = previous_ids - desired_ids
        if existing is None and (
            parent is None or len(added_ids) + len(removed_ids) >= len(desired_ids)
        ):
            parent = None
            added_ids = desired_ids
            removed_ids = set()

        parent_sync_run_id = parent.sync_run_id if parent is not None else None
        _insert_missing(
            self.session,
            WbFinanceOperationRow,
            [operation_values(operation_by_id[item]) for item in sorted(added_ids)],
            ["operation_id"],
        )
        if added_ids:
            # Operations are immutable and invisible without a completed snapshot.
            # Committing them first avoids pathological FK checks against a large
            # uncommitted parent set; a retry safely reuses any orphaned rows.
            self.session.commit()
            self._prepare()

        run = existing
        if run is None:
            run = WbFinanceSyncRunRow(
                sync_run_id=str(uuid.uuid4()),
                organization_id=self.organization_id,
                marketplace_account_id=marketplace_account_id,
                parent_sync_run_id=parent_sync_run_id,
                date_from=period.date_from,
                date_to=period.date_to,
                snapshot_checksum=checksum,
                formula_version=FINANCE_FORMULA_VERSION,
                operation_count=len(operations),
                is_materialized=False,
                is_rollup_materialized=False,
                is_pnl_rollup_materialized=False,
                is_daily_pnl_rollup_materialized=False,
                captured_at=observed,
                last_observed_at=observed,
            )
            self.session.add(run)
            self.session.commit()
            self._prepare()
        try:
            _insert_memberships(
                self.session,
                [
                    *(
                        {
                            "organization_id": self.organization_id,
                            "marketplace_account_id": marketplace_account_id,
                            "sync_run_id": run.sync_run_id,
                            "operation_id": operation_id,
                            "is_present": True,
                        }
                        for operation_id in sorted(added_ids)
                    ),
                    *(
                        {
                            "organization_id": self.organization_id,
                            "marketplace_account_id": marketplace_account_id,
                            "sync_run_id": run.sync_run_id,
                            "operation_id": operation_id,
                            "is_present": False,
                        }
                        for operation_id in sorted(removed_ids)
                    ),
                ],
            )
            if _utc(run.last_observed_at) < observed:
                run.last_observed_at = observed
            run.is_materialized = True
            self.session.flush()
            self._materialize_rollup(run)
            run.is_rollup_materialized = True
            self._materialize_pnl_rollup(run)
            run.is_pnl_rollup_materialized = True
            self._materialize_daily_pnl_rollup(run)
            run.is_daily_pnl_rollup_materialized = True
            self.session.commit()
            self._prepare()
        except Exception:
            self.session.rollback()
            self._prepare()
            raise
        return _snapshot(run)

    def _covering_snapshot(
        self, marketplace_account_id: int, period: Period
    ) -> WbFinanceSyncRunRow | None:
        exact_period = (
            (WbFinanceSyncRunRow.date_from == period.date_from)
            & (WbFinanceSyncRunRow.date_to == period.date_to)
        )
        return self.session.scalar(
            select(WbFinanceSyncRunRow)
            .where(
                WbFinanceSyncRunRow.organization_id == self.organization_id,
                WbFinanceSyncRunRow.marketplace_account_id == marketplace_account_id,
                WbFinanceSyncRunRow.date_from <= period.date_from,
                WbFinanceSyncRunRow.date_to >= period.date_to,
                WbFinanceSyncRunRow.formula_version == FINANCE_FORMULA_VERSION,
                WbFinanceSyncRunRow.is_materialized.is_(True),
            )
            .order_by(
                case((exact_period, 0), else_=1),
                WbFinanceSyncRunRow.last_observed_at.desc(),
                WbFinanceSyncRunRow.captured_at.desc(),
                WbFinanceSyncRunRow.sync_run_id.desc(),
            )
            .limit(1)
        )

    @staticmethod
    def _empty_summary() -> FinanceSummary:
        return FinanceSummary()

    def get_page(
        self,
        marketplace_account_id: int,
        period: Period,
        *,
        limit: int,
        offset: int,
    ) -> FinancePage:
        if not 1 <= limit <= 500 or offset < 0:
            raise FinanceNormalizationError("invalid page window")
        self._account(marketplace_account_id)
        temporal_state = period.temporal_state(self.now())
        if temporal_state == "future":
            return FinancePage(
                "future", period, None, self._empty_summary(), [], 0, limit, offset
            )

        run = self._covering_snapshot(marketplace_account_id, period)
        if run is None:
            state: FinanceSourceState = (
                "partial" if temporal_state == "partial" else "missing"
            )
            return FinancePage(
                state, period, None, self._empty_summary(), [], 0, limit, offset
            )

        row_result = (
            self._materialized_rollup_rows(run)
            if run.is_rollup_materialized
            and run.date_from == period.date_from
            and run.date_to == period.date_to
            else self._operation_rollup_rows(run, period)
        )
        item_rows = sorted(
            (row for row in row_result if row[0] is not None),
            key=lambda row: (-int(row[3]), int(row[0])),
        )
        summary = FinanceSummary(
            sum(int(row[2]) for row in row_result),
            len(item_rows),
            *(sum(int(row[index]) for row in row_result) for index in range(3, 11)),
        )
        total = summary.sku_count
        items = [
            FinanceSku(
                nm_id=int(row[0]),
                seller_article=row[1],
                operation_count=int(row[2]),
                revenue_kopecks=int(row[3]),
                main_revenue_kopecks=int(row[4]),
                redemptions_revenue_kopecks=int(row[5]),
                late_correction_revenue_kopecks=int(row[6]),
                unknown_revenue_kopecks=int(row[7]),
                sales_units=int(row[8]),
                returns_units=int(row[9]),
                net_units=int(row[10]),
            )
            for row in item_rows[offset : offset + limit]
        ]
        state = _source_state(run, temporal_state, summary.operation_count)
        return FinancePage(
            state,
            period,
            _snapshot(run),
            summary,
            items,
            total,
            limit,
            offset,
        )

    def get_pnl_source(
        self,
        marketplace_account_id: int,
        period: Period,
    ) -> FinancePnlSource:
        self._account(marketplace_account_id)
        temporal_state = period.temporal_state(self.now())
        if temporal_state == "future":
            return FinancePnlSource("future", period, None, [], {}, {})

        run = self._covering_snapshot(marketplace_account_id, period)
        if run is None:
            state: FinanceSourceState = (
                "partial" if temporal_state == "partial" else "missing"
            )
            return FinancePnlSource(state, period, None, [], {}, {})

        exact = run.date_from == period.date_from and run.date_to == period.date_to
        # Exact membership can include fees with an earlier or unknown sale date.
        if run.is_pnl_rollup_materialized and exact:
            rows = self._materialized_pnl_rows(run)
        else:
            # Business-day rollups cannot select posting-day membership for a
            # subperiod. Read immutable operations; keep original sale dates
            # separately for dated costs/tax.
            rows = self._pnl_operation_rows(run, period)
        facts = [
            FinancePnlFact(
                nm_id=row[0],
                seller_article=row[1],
                operation_count=int(row[2]),
                revenue_kopecks=int(row[3]),
                sales_revenue_kopecks=int(row[4]),
                returns_revenue_kopecks=int(row[5]),
                main_revenue_kopecks=int(row[6]),
                redemptions_revenue_kopecks=int(row[7]),
                late_correction_revenue_kopecks=int(row[8]),
                unknown_revenue_kopecks=int(row[9]),
                sales_units=int(row[10]),
                returns_units=int(row[11]),
                net_units=int(row[12]),
                commission_kopecks=int(row[13]),
                logistics_kopecks=int(row[14]),
                storage_kopecks=int(row[15]),
                acceptance_kopecks=int(row[16]),
                penalty_kopecks=int(row[17]),
                deduction_kopecks=int(row[18]),
                additional_payment_kopecks=int(row[19]),
                acquiring_kopecks=int(row[20]),
                cashback_amount_kopecks=(None if row[21] is None else int(row[21])),
                cashback_discount_kopecks=(None if row[22] is None else int(row[22])),
                cashback_commission_change_kopecks=(
                    None if row[23] is None else int(row[23])
                ),
                payable_kopecks=None if row[24] is None else int(row[24]),
                payable_basis=run.formula_version == FINANCE_FORMULA_VERSION,
                advertising_kopecks=None if row[25] is None else int(row[25]),
                compensation_kopecks=int(row[26]),
            )
            for row in rows
        ]
        operation_count = sum(fact.operation_count for fact in facts)
        state = _source_state(run, temporal_state, operation_count)
        if run.is_daily_pnl_rollup_materialized and exact:
            daily_net_units, daily_economics_basis = (
                self._materialized_daily_bases(run, period, include_business_dates_outside_period=True)
            )
        else:
            daily_net_units = self._daily_net_units(run, period)
            daily_economics_basis = self._daily_economics_basis(run, period)
        return FinancePnlSource(
            state,
            period,
            _snapshot(run),
            facts,
            daily_net_units,
            daily_economics_basis,
        )

    def backfill_pnl_rollups(self, marketplace_account_id: int) -> int:
        self._account(marketplace_account_id)
        runs = self.session.scalars(
            select(WbFinanceSyncRunRow)
            .where(
                WbFinanceSyncRunRow.organization_id == self.organization_id,
                WbFinanceSyncRunRow.marketplace_account_id
                == marketplace_account_id,
                WbFinanceSyncRunRow.is_materialized.is_(True),
                WbFinanceSyncRunRow.is_pnl_rollup_materialized.is_(False),
            )
            .order_by(
                WbFinanceSyncRunRow.captured_at.asc(),
                WbFinanceSyncRunRow.sync_run_id.asc(),
            )
        ).all()
        completed = 0
        for run in runs:
            try:
                self._materialize_pnl_rollup(run)
                run.is_pnl_rollup_materialized = True
                self.session.commit()
                self._prepare()
            except Exception:
                self.session.rollback()
                self._prepare()
                raise
            completed += 1
        return completed

    def backfill_daily_pnl_rollups(self, marketplace_account_id: int) -> int:
        self._account(marketplace_account_id)
        runs = self.session.scalars(
            select(WbFinanceSyncRunRow)
            .where(
                WbFinanceSyncRunRow.organization_id == self.organization_id,
                WbFinanceSyncRunRow.marketplace_account_id
                == marketplace_account_id,
                WbFinanceSyncRunRow.is_materialized.is_(True),
                WbFinanceSyncRunRow.is_daily_pnl_rollup_materialized.is_(False),
            )
            .order_by(
                WbFinanceSyncRunRow.captured_at.asc(),
                WbFinanceSyncRunRow.sync_run_id.asc(),
            )
        ).all()
        completed = 0
        for run in runs:
            try:
                self._materialize_daily_pnl_rollup(run)
                run.is_daily_pnl_rollup_materialized = True
                self.session.commit()
                self._prepare()
            except Exception:
                self.session.rollback()
                self._prepare()
                raise
            completed += 1
        return completed


def ingest_legacy_finance_payload(
    organization_id: int,
    payload: dict[str, Any],
    *,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    rows = payload.get("rows")
    if not isinstance(rows, list):
        return {"state": "skipped", "reason": "raw_rows_missing"}
    try:
        period = Period(
            date.fromisoformat(str(payload.get("dateFrom"))[:10]),
            date.fromisoformat(str(payload.get("dateTo"))[:10]),
        )
    except (TypeError, ValueError, PeriodValidationError):
        return {"state": "skipped", "reason": "invalid_period"}

    try:
        with get_session_factory()() as session:
            set_tenant_context(session, organization_id)
            account_ids = session.scalars(
                select(MarketplaceAccountRow.marketplace_account_id).where(
                    MarketplaceAccountRow.organization_id == organization_id,
                    MarketplaceAccountRow.marketplace == "wb",
                )
            ).all()
            if len(account_ids) != 1:
                return {
                    "state": "skipped",
                    "reason": (
                        "wb_account_missing"
                        if not account_ids
                        else "wb_account_ambiguous"
                    ),
                    "accountCount": len(account_ids),
                }
            snapshot = FinanceService(session, organization_id).ingest_snapshot(
                int(account_ids[0]),
                period,
                [row for row in rows if isinstance(row, dict)],
                observed_at=observed_at,
            )
            return {
                "state": "ready",
                "marketplaceAccountId": snapshot.marketplace_account_id,
                "syncRunId": snapshot.sync_run_id,
                "snapshotChecksum": snapshot.snapshot_checksum,
                "operationCount": snapshot.operation_count,
            }
    except Exception as exc:  # shadow writes must not break the established sync
        logger.exception(
            "canonical WB finance shadow ingest failed for organization %s",
            organization_id,
        )
        return {"state": "failed", "errorCode": type(exc).__name__}


def shadow_ingest_legacy_finance_payload(
    organization_id: int,
    payload: dict[str, Any],
    *,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    if (
        not settings.finance_shadow_ingest_enabled
        or organization_id not in settings.finance_shadow_ingest_organization_ids
    ):
        return {"state": "disabled"}
    return ingest_legacy_finance_payload(
        organization_id,
        payload,
        observed_at=observed_at,
    )
