from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.infra.db import get_session_factory, set_tenant_context
from app.platform.advertising.orm import (
    WbAdvertisingCampaignSnapshotRow,
    WbAdvertisingFactRow,
    WbAdvertisingSpendDocumentRow,
    WbAdvertisingSyncRunRow,
)
from app.platform.advertising.raw import normalize_raw_advertising
from app.platform.catalog.service import CatalogService
from app.platform.clock import utc_now
from app.platform.integrations.orm import MarketplaceAccountRow
from app.platform.period import MOSCOW, Period, PeriodValidationError

AdvertisingSourceKind = Literal["finance_promotion", "ads_fullstats"]
AdvertisingSourceState = Literal["ready", "partial", "future", "empty", "missing"]
AttributionLevel = Literal["exact_sku", "campaign_sku", "campaign_only", "unknown"]

_METRICS = (
    ("spend_kopecks", "adSpendKopecks"),
    ("impressions", "adImpressions"),
    ("clicks", "adClicks"),
    ("cart_adds", "adCartAdds"),
    ("order_count", "adOrders"),
    ("order_revenue_kopecks", "adRevenueKopecks"),
)
_RECONCILIATION_METRICS = tuple(target for target, _source in _METRICS) + (
    "cancel_count",
)
_RECONCILIATION_MONEY = {"spend_kopecks", "order_revenue_kopecks"}
logger = logging.getLogger(__name__)
_RAW_ADVISORY_DOMAIN = "wb-advertising-raw-v1"


class AdvertisingNormalizationError(ValueError):
    pass


class AdvertisingAccountNotFound(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class AdvertisingFact:
    source_identity: str
    payload_checksum: str
    business_date: date | None
    campaign_id: int | None
    nm_id: int | None
    attribution_level: AttributionLevel
    spend_kopecks: int
    impressions: int | None = None
    clicks: int | None = None
    cart_adds: int | None = None
    order_count: int | None = None
    order_revenue_kopecks: int | None = None


@dataclass(frozen=True, slots=True)
class AdvertisingSnapshot:
    sync_run_id: str
    marketplace_account_id: int
    source_kind: AdvertisingSourceKind
    period: Period
    snapshot_checksum: str
    formula_version: str
    source_total_spend_kopecks: int
    fact_count: int
    evidence_status: str
    captured_at: datetime
    last_observed_at: datetime
    campaign_count: int = 0
    spend_document_count: int = 0
    document_total_spend_kopecks: int | None = None
    expected_request_count: int | None = None
    completed_request_count: int | None = None


@dataclass(frozen=True, slots=True)
class AdvertisingPnlSource:
    state: AdvertisingSourceState
    period: Period
    snapshot: AdvertisingSnapshot | None
    source_kind: AdvertisingSourceKind | None
    evidence_status: str | None
    spend_by_nm: dict[int, int]
    total_spend_kopecks: int | None
    unattributed_spend_kopecks: int | None
    blocker_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AdvertisingMetricTotals:
    spend_kopecks: int | None
    impressions: int | None
    clicks: int | None
    cart_adds: int | None
    order_count: int | None
    order_revenue_kopecks: int | None
    cancel_count: int | None


@dataclass(frozen=True, slots=True)
class AdvertisingRawReconciliation:
    state: AdvertisingSourceState
    period: Period
    snapshot: AdvertisingSnapshot | None
    formula_version: str = "wb-advertising-reconciliation-shadow-v1"
    campaign: AdvertisingMetricTotals | None = None
    source_sku: AdvertisingMetricTotals | None = None
    campaign_only: AdvertisingMetricTotals | None = None
    document_spend_kopecks: int | None = None
    unknown_spend_kopecks: int | None = None
    source_sku_by_nm: dict[int, AdvertisingMetricTotals] = field(default_factory=dict)
    mapped_catalog_sku_by_nm: dict[int, int] = field(default_factory=dict)
    unmapped_nm_ids: tuple[int, ...] = ()
    ambiguous_nm_ids: tuple[int, ...] = ()
    diagnostics: tuple[str, ...] = ()


def _metric_totals(rows: list[WbAdvertisingFactRow]) -> AdvertisingMetricTotals:
    return AdvertisingMetricTotals(
        **{
            name: (
                None
                if any(getattr(row, name) is None for row in rows)
                else sum(int(getattr(row, name)) for row in rows)
            )
            for name in _RECONCILIATION_METRICS
        }
    )


def _combine_totals(
    totals: list[AdvertisingMetricTotals],
) -> AdvertisingMetricTotals:
    return AdvertisingMetricTotals(
        **{
            name: (
                None
                if any(getattr(total, name) is None for total in totals)
                else sum(int(getattr(total, name)) for total in totals)
            )
            for name in _RECONCILIATION_METRICS
        }
    )


def _campaign_residual(
    campaign: WbAdvertisingFactRow,
    source_rows: list[WbAdvertisingFactRow],
    daily_rows: list[WbAdvertisingFactRow] | None = None,
) -> tuple[AdvertisingMetricTotals, bool, bool]:
    if daily_rows:
        # WB rounds each level independently; validate the stored daily totals
        # before combining them instead of widening the period's tolerance.
        levels = [_campaign_residual(campaign, daily_rows)]
        levels.extend(
            _campaign_residual(
                day,
                [row for row in source_rows if row.business_date == day.business_date],
            )
            for day in daily_rows
        )
        return (
            _combine_totals([residual for residual, _, _ in levels]),
            any(invalid for _, invalid, _ in levels),
            any(tolerated for _, _, tolerated in levels),
        )
    parent = _metric_totals([campaign])
    children = _metric_totals(source_rows)
    values: dict[str, int | None] = {}
    invalid = False
    tolerance_applied = False
    for name in _RECONCILIATION_METRICS:
        parent_value = getattr(parent, name)
        child_value = getattr(children, name)
        if parent_value is None or child_value is None:
            values[name] = None
            continue
        difference = parent_value - child_value
        # Parent and each child are rounded to kopecks independently. Seven
        # daily totals can differ by several kopecks even with full coverage.
        tolerance = max(1, (len(source_rows) + 1) // 2) if name in _RECONCILIATION_MONEY else 0
        if difference < -tolerance:
            values[name] = None
            invalid = True
        elif source_rows and abs(difference) <= tolerance:
            values[name] = 0
            tolerance_applied |= difference != 0
        else:
            values[name] = max(0, difference)
            tolerance_applied |= difference < 0
    return AdvertisingMetricTotals(**values), invalid, tolerance_applied


def _hash(payload: Any) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _lock_raw_advertising_account(
    session: Session, organization_id: int, marketplace_account_id: int
) -> None:
    if session.get_bind().dialect.name != "postgresql":
        return
    key = int.from_bytes(
        hashlib.sha256(
            f"{_RAW_ADVISORY_DOMAIN}:{organization_id}:{marketplace_account_id}".encode()
        ).digest()[:8],
        "big",
        signed=True,
    )
    # ponytail: per-account serialization; use period locks only if throughput demands finer locking.
    session.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": key})


def _integer(raw: Any, field: str, *, nonnegative: bool = True) -> int:
    if raw in (None, ""):
        return 0
    if isinstance(raw, bool):
        raise AdvertisingNormalizationError(f"invalid {field}")
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
        value = int(raw)
    else:
        raise AdvertisingNormalizationError(f"invalid {field}")
    if nonnegative and value < 0:
        raise AdvertisingNormalizationError(f"invalid {field}")
    return value


def _optional_positive_integer(raw: Any, field: str) -> int | None:
    if isinstance(raw, bool):
        raise AdvertisingNormalizationError(f"invalid {field}")
    if raw in (None, "", 0, "0"):
        return None
    value = _integer(raw, field)
    if value < 1:
        raise AdvertisingNormalizationError(f"invalid {field}")
    return value


def _first_present(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _money(raw: Any) -> int:
    if raw in (None, ""):
        return 0
    if isinstance(raw, bool):
        raise AdvertisingNormalizationError("invalid advertising money")
    try:
        value = Decimal(str(raw).replace(",", ".")) * 100
    except InvalidOperation as exc:
        raise AdvertisingNormalizationError("invalid advertising money") from exc
    if not value.is_finite():
        raise AdvertisingNormalizationError("invalid advertising money")
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _business_date(raw: Any) -> date | None:
    if raw in (None, ""):
        return None
    value = str(raw).strip()
    try:
        if len(value) == 10:
            return date.fromisoformat(value)
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AdvertisingNormalizationError("invalid advertising date") from exc
    if instant.tzinfo is None or instant.utcoffset() is None:
        instant = instant.replace(tzinfo=MOSCOW)
    return instant.astimezone(MOSCOW).date()


def _validate_period(period: Period, payload: dict[str, Any]) -> None:
    for key, expected in (
        ("dateFrom", period.date_from),
        ("dateTo", period.date_to),
    ):
        if key not in payload:
            continue
        try:
            actual = date.fromisoformat(str(payload[key])[:10])
        except ValueError as exc:
            raise AdvertisingNormalizationError("invalid advertising period") from exc
        if actual != expected:
            raise AdvertisingNormalizationError("advertising period mismatch")


def _fact(
    *,
    source_identity: str,
    business_date: date | None,
    campaign_id: int | None,
    nm_id: int | None,
    attribution_level: AttributionLevel,
    spend_kopecks: int,
    impressions: int | None = None,
    clicks: int | None = None,
    cart_adds: int | None = None,
    order_count: int | None = None,
    order_revenue_kopecks: int | None = None,
) -> AdvertisingFact:
    normalized = {
        "business_date": business_date.isoformat() if business_date else None,
        "campaign_id": campaign_id,
        "nm_id": nm_id,
        "attribution_level": attribution_level,
        "spend_kopecks": spend_kopecks,
        "impressions": impressions,
        "clicks": clicks,
        "cart_adds": cart_adds,
        "order_count": order_count,
        "order_revenue_kopecks": order_revenue_kopecks,
    }
    return AdvertisingFact(
        source_identity=source_identity,
        payload_checksum=_hash(normalized),
        business_date=business_date,
        campaign_id=campaign_id,
        nm_id=nm_id,
        attribution_level=attribution_level,
        spend_kopecks=spend_kopecks,
        impressions=impressions,
        clicks=clicks,
        cart_adds=cart_adds,
        order_count=order_count,
        order_revenue_kopecks=order_revenue_kopecks,
    )


def normalize_finance_promotion(
    period: Period, payload: dict[str, Any]
) -> tuple[list[AdvertisingFact], int]:
    _validate_period(period, payload)
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise AdvertisingNormalizationError("finance promotion rows are missing")
    unique: dict[str, AdvertisingFact] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise AdvertisingNormalizationError("invalid finance promotion row")
        bonus_type = str(
            row.get("bonusTypeName") or row.get("bonus_type_name") or ""
        ).strip()
        if "wb продвижение" not in bonus_type.casefold():
            continue
        rrd_id = _optional_positive_integer(
            _first_present(row, "rrdId", "rrd_id"), "rrdId"
        )
        nm_id = _optional_positive_integer(
            _first_present(row, "nmId", "nmID", "nm_id"), "nmId"
        )
        deduction = _money(
            row.get("deduction")
            if row.get("deduction") not in (None, "")
            else row.get("deductionRub")
        )
        business_date = _business_date(row.get("rrDate") or row.get("rr_date"))
        identity_seed = {
            "rrd_id": rrd_id,
            "business_date": business_date.isoformat() if business_date else None,
            "nm_id": nm_id,
            "spend_kopecks": deduction,
        }
        source_identity = (
            f"rrd:{rrd_id}" if rrd_id is not None else f"fp:{_hash(identity_seed)}"
        )
        fact = _fact(
            source_identity=source_identity,
            business_date=business_date,
            campaign_id=None,
            nm_id=nm_id,
            attribution_level="exact_sku" if nm_id is not None else "unknown",
            spend_kopecks=deduction,
        )
        previous = unique.get(source_identity)
        if previous is not None and previous.payload_checksum != fact.payload_checksum:
            raise AdvertisingNormalizationError(
                f"source identity {source_identity} has conflicting payloads"
            )
        unique[source_identity] = fact
    facts = sorted(unique.values(), key=lambda item: item.source_identity)
    return facts, sum(item.spend_kopecks for item in facts)


def _metrics(row: Any) -> tuple[int, ...]:
    if not isinstance(row, dict):
        raise AdvertisingNormalizationError("invalid fullstats row")
    return tuple(_integer(row.get(source), source) for _target, source in _METRICS)


def _metrics_by_nm(rows: Any) -> dict[int, tuple[int, ...]]:
    if not isinstance(rows, dict):
        raise AdvertisingNormalizationError("invalid fullstats aggregate")
    result: dict[int, tuple[int, ...]] = {}
    for raw_nm_id, row in rows.items():
        nm_id = _optional_positive_integer(raw_nm_id, "nmId")
        if nm_id is None or nm_id in result:
            raise AdvertisingNormalizationError("invalid fullstats nmId")
        metrics = _metrics(row)
        if any(metrics):
            result[nm_id] = metrics
    return result


def normalize_fullstats(
    period: Period, payload: dict[str, Any]
) -> tuple[list[AdvertisingFact], int]:
    _validate_period(period, payload)
    daily = payload.get("dailyAggregates")
    if not isinstance(daily, dict):
        raise AdvertisingNormalizationError("fullstats daily aggregates are missing")
    expected_days = {
        date.fromordinal(period.date_from.toordinal() + offset).isoformat()
        for offset in range(period.days)
    }
    if set(daily) != expected_days:
        raise AdvertisingNormalizationError("fullstats daily coverage mismatch")

    facts: list[AdvertisingFact] = []
    daily_by_nm: dict[int, list[int]] = {}
    for day_text in sorted(daily):
        business_date = date.fromisoformat(day_text)
        for nm_id, metrics in _metrics_by_nm(daily[day_text]).items():
            totals = daily_by_nm.setdefault(nm_id, [0] * len(_METRICS))
            for index, value in enumerate(metrics):
                totals[index] += value
            values = dict(zip((target for target, _source in _METRICS), metrics))
            facts.append(
                _fact(
                    source_identity=f"{day_text}|{nm_id}",
                    business_date=business_date,
                    campaign_id=None,
                    nm_id=nm_id,
                    attribution_level="exact_sku",
                    **values,
                )
            )

    aggregate_by_nm = _metrics_by_nm(payload.get("aggregates"))
    normalized_daily = {
        nm_id: tuple(values) for nm_id, values in daily_by_nm.items() if any(values)
    }
    if normalized_daily != aggregate_by_nm:
        raise AdvertisingNormalizationError(
            "fullstats daily aggregates do not match aggregates"
        )
    declared_totals = _metrics(payload.get("totals"))
    aggregate_totals = tuple(
        sum(values[index] for values in aggregate_by_nm.values())
        for index in range(len(_METRICS))
    )
    if declared_totals != aggregate_totals:
        raise AdvertisingNormalizationError("fullstats totals do not match aggregates")
    if (
        "totalSpendKopecks" in payload
        and _integer(payload["totalSpendKopecks"], "totalSpendKopecks")
        != aggregate_totals[0]
    ):
        raise AdvertisingNormalizationError("fullstats declared spend total mismatch")
    return sorted(facts, key=lambda item: item.source_identity), aggregate_totals[0]


def _snapshot_checksum(
    facts: list[AdvertisingFact], total: int, evidence_status: str
) -> str:
    return _hash(
        {
            "evidence_status": evidence_status,
            "facts": [
                asdict(fact)
                | {
                    "business_date": (
                        fact.business_date.isoformat() if fact.business_date else None
                    )
                }
                for fact in facts
            ],
            "source_total_spend_kopecks": total,
        }
    )


def _snapshot(row: WbAdvertisingSyncRunRow) -> AdvertisingSnapshot:
    return AdvertisingSnapshot(
        sync_run_id=row.sync_run_id,
        marketplace_account_id=row.marketplace_account_id,
        source_kind=row.source_kind,
        period=Period(row.date_from, row.date_to),
        snapshot_checksum=row.snapshot_checksum,
        formula_version=row.formula_version,
        source_total_spend_kopecks=row.source_total_spend_kopecks,
        fact_count=row.fact_count,
        campaign_count=row.campaign_count,
        spend_document_count=row.spend_document_count,
        document_total_spend_kopecks=row.document_total_spend_kopecks,
        expected_request_count=row.expected_request_count,
        completed_request_count=row.completed_request_count,
        evidence_status=row.evidence_status,
        captured_at=_utc(row.captured_at),
        last_observed_at=_utc(row.last_observed_at),
    )


def _coverage_snapshot(
    organization_id: int, period: Period, runs: list[WbAdvertisingSyncRunRow]
) -> AdvertisingSnapshot:
    if len(runs) == 1:
        return _snapshot(runs[0])
    version = "wb-advertising-daily-v1"
    checksum = _hash(
        {
            "formula_version": version,
            "organization_id": organization_id,
            "marketplace_account_id": runs[0].marketplace_account_id,
            "period": period.cache_key,
            "members": [
                (run.date_from.isoformat(), run.sync_run_id, run.snapshot_checksum)
                for run in runs
            ],
        }
    )
    return AdvertisingSnapshot(
        sync_run_id=str(uuid.uuid5(uuid.NAMESPACE_URL, checksum)),
        marketplace_account_id=runs[0].marketplace_account_id,
        source_kind="ads_fullstats",
        period=period,
        snapshot_checksum=checksum,
        formula_version=version,
        source_total_spend_kopecks=sum(run.source_total_spend_kopecks for run in runs),
        fact_count=sum(run.fact_count for run in runs),
        campaign_count=sum(run.campaign_count for run in runs),
        spend_document_count=sum(run.spend_document_count for run in runs),
        document_total_spend_kopecks=(
            None
            if any(run.document_total_spend_kopecks is None for run in runs)
            else sum(run.document_total_spend_kopecks for run in runs)
        ),
        expected_request_count=sum(run.expected_request_count for run in runs),
        completed_request_count=sum(run.completed_request_count for run in runs),
        evidence_status="raw",
        # Conservative timestamps: the newest member cannot freshen the whole period.
        captured_at=min(_utc(run.captured_at) for run in runs),
        last_observed_at=min(_utc(run.last_observed_at) for run in runs),
    )


class AdvertisingService:
    def __init__(
        self,
        session: Session,
        organization_id: int,
        *,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        if organization_id < 1:
            raise AdvertisingNormalizationError("organization must be positive")
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
            raise AdvertisingAccountNotFound("WB marketplace account not found")
        return account

    def ingest_payload(
        self,
        marketplace_account_id: int,
        period: Period,
        source_kind: AdvertisingSourceKind,
        payload: dict[str, Any],
        *,
        source_reference: str,
        observed_at: datetime | None = None,
    ) -> AdvertisingSnapshot:
        if source_kind == "finance_promotion":
            facts, total = normalize_finance_promotion(period, payload)
        elif source_kind == "ads_fullstats":
            facts, total = normalize_fullstats(period, payload)
        else:
            raise AdvertisingNormalizationError("unsupported advertising source")
        return self._ingest(
            marketplace_account_id,
            period,
            source_kind,
            source_reference,
            facts,
            total,
            evidence_status=(
                "raw" if source_kind == "finance_promotion" else "derived_legacy"
            ),
            observed_at=observed_at,
        )

    def ingest_finance_aggregate_only(
        self,
        marketplace_account_id: int,
        period: Period,
        total_spend_kopecks: int,
        *,
        source_reference: str,
        observed_at: datetime | None = None,
    ) -> AdvertisingSnapshot:
        if isinstance(total_spend_kopecks, bool) or not isinstance(
            total_spend_kopecks, int
        ):
            raise AdvertisingNormalizationError("invalid advertising total")
        fact = _fact(
            source_identity="aggregate",
            business_date=None,
            campaign_id=None,
            nm_id=None,
            attribution_level="unknown",
            spend_kopecks=total_spend_kopecks,
        )
        return self._ingest(
            marketplace_account_id,
            period,
            "finance_promotion",
            source_reference,
            [fact],
            total_spend_kopecks,
            evidence_status="aggregate_only",
            observed_at=observed_at,
        )

    def ingest_raw_payload(
        self,
        marketplace_account_id: int,
        period: Period,
        bundle: dict[str, Any],
        *,
        source_reference: str,
        observed_at: datetime | None = None,
    ) -> AdvertisingSnapshot:
        evidence = normalize_raw_advertising(period, bundle)
        if not source_reference or len(source_reference) > 255:
            raise AdvertisingNormalizationError("invalid source reference")
        observed = _utc(observed_at or self.now())
        try:
            self._account(marketplace_account_id)
            _lock_raw_advertising_account(
                self.session, self.organization_id, marketplace_account_id
            )
            existing = self.session.scalar(
                select(WbAdvertisingSyncRunRow)
                .where(
                    WbAdvertisingSyncRunRow.organization_id == self.organization_id,
                    WbAdvertisingSyncRunRow.marketplace_account_id
                    == marketplace_account_id,
                    WbAdvertisingSyncRunRow.source_kind == "ads_fullstats",
                    WbAdvertisingSyncRunRow.date_from == period.date_from,
                    WbAdvertisingSyncRunRow.date_to == period.date_to,
                    WbAdvertisingSyncRunRow.snapshot_checksum
                    == evidence.snapshot_checksum,
                    WbAdvertisingSyncRunRow.is_materialized.is_(True),
                )
                .execution_options(populate_existing=True)
            )
            if existing is not None:
                if _utc(existing.last_observed_at) < observed:
                    existing.last_observed_at = observed
                self.session.commit()
                self._prepare()
                return _snapshot(existing)

            parent = self.session.scalar(
                select(WbAdvertisingSyncRunRow)
                .where(
                    WbAdvertisingSyncRunRow.organization_id == self.organization_id,
                    WbAdvertisingSyncRunRow.marketplace_account_id
                    == marketplace_account_id,
                    WbAdvertisingSyncRunRow.source_kind == "ads_fullstats",
                    WbAdvertisingSyncRunRow.date_from == period.date_from,
                    WbAdvertisingSyncRunRow.date_to == period.date_to,
                    WbAdvertisingSyncRunRow.evidence_status == "raw",
                    WbAdvertisingSyncRunRow.is_materialized.is_(True),
                )
                .order_by(
                    WbAdvertisingSyncRunRow.last_observed_at.desc(),
                    WbAdvertisingSyncRunRow.captured_at.desc(),
                    WbAdvertisingSyncRunRow.sync_run_id.desc(),
                )
                .limit(1)
            )
            run = WbAdvertisingSyncRunRow(
                sync_run_id=str(uuid.uuid4()),
                organization_id=self.organization_id,
                marketplace_account_id=marketplace_account_id,
                parent_sync_run_id=parent.sync_run_id if parent else None,
                source_kind="ads_fullstats",
                date_from=period.date_from,
                date_to=period.date_to,
                source_reference=source_reference,
                snapshot_checksum=evidence.snapshot_checksum,
                raw_manifest=evidence.raw_manifest,
                raw_manifest_checksum=evidence.raw_manifest_checksum,
                expected_request_count=len(evidence.raw_manifest),
                completed_request_count=sum(
                    item.get("ok") is True for item in evidence.raw_manifest
                ),
                formula_version="wb-advertising-raw-v1",
                source_total_spend_kopecks=evidence.source_total_spend_kopecks,
                fact_count=len(evidence.facts),
                campaign_count=len(evidence.campaigns),
                spend_document_count=len(evidence.spend_documents),
                document_total_spend_kopecks=evidence.document_total_spend_kopecks,
                evidence_status="raw",
                is_materialized=False,
                captured_at=observed,
                last_observed_at=observed,
            )
            self.session.add(run)
            self.session.flush()
            self.session.add_all(
                [
                    WbAdvertisingFactRow(
                        advertising_fact_id=_hash(
                            {
                                "organization_id": self.organization_id,
                                "marketplace_account_id": marketplace_account_id,
                                "sync_run_id": run.sync_run_id,
                                "source_identity": fact.source_identity,
                            }
                        ),
                        organization_id=self.organization_id,
                        marketplace_account_id=marketplace_account_id,
                        sync_run_id=run.sync_run_id,
                        attribution_level=(
                            "exact_sku"
                            if fact.fact_scope == "source_sku"
                            else "campaign_only"
                        ),
                        **asdict(fact),
                    )
                    for fact in evidence.facts
                ]
            )
            self.session.add_all(
                [
                    WbAdvertisingCampaignSnapshotRow(
                        campaign_snapshot_id=_hash(
                            {
                                "organization_id": self.organization_id,
                                "marketplace_account_id": marketplace_account_id,
                                "sync_run_id": run.sync_run_id,
                                "source_identity": campaign.source_identity,
                            }
                        ),
                        organization_id=self.organization_id,
                        marketplace_account_id=marketplace_account_id,
                        sync_run_id=run.sync_run_id,
                        **(
                            asdict(campaign)
                            | {"member_nm_ids": list(campaign.member_nm_ids)}
                        ),
                    )
                    for campaign in evidence.campaigns
                ]
            )
            self.session.add_all(
                [
                    WbAdvertisingSpendDocumentRow(
                        spend_document_id=_hash(
                            {
                                "organization_id": self.organization_id,
                                "marketplace_account_id": marketplace_account_id,
                                "sync_run_id": run.sync_run_id,
                                "source_identity": document.source_identity,
                            }
                        ),
                        organization_id=self.organization_id,
                        marketplace_account_id=marketplace_account_id,
                        sync_run_id=run.sync_run_id,
                        **asdict(document),
                    )
                    for document in evidence.spend_documents
                ]
            )
            self.session.flush()

            stored_counts = tuple(
                int(
                    self.session.scalar(
                        select(func.count())
                        .select_from(row)
                        .where(
                            row.organization_id == self.organization_id,
                            row.marketplace_account_id == marketplace_account_id,
                            row.sync_run_id == run.sync_run_id,
                        )
                    )
                    or 0
                )
                for row in (
                    WbAdvertisingFactRow,
                    WbAdvertisingCampaignSnapshotRow,
                    WbAdvertisingSpendDocumentRow,
                )
            )
            stored_source_total = int(
                self.session.scalar(
                    select(
                        func.coalesce(func.sum(WbAdvertisingFactRow.spend_kopecks), 0)
                    ).where(
                        WbAdvertisingFactRow.organization_id == self.organization_id,
                        WbAdvertisingFactRow.marketplace_account_id
                        == marketplace_account_id,
                        WbAdvertisingFactRow.sync_run_id == run.sync_run_id,
                        WbAdvertisingFactRow.fact_scope == "campaign",
                        WbAdvertisingFactRow.grain == "period",
                    )
                )
                or 0
            )
            stored_document_total = int(
                self.session.scalar(
                    select(
                        func.coalesce(
                            func.sum(WbAdvertisingSpendDocumentRow.spend_kopecks), 0
                        )
                    ).where(
                        WbAdvertisingSpendDocumentRow.organization_id
                        == self.organization_id,
                        WbAdvertisingSpendDocumentRow.marketplace_account_id
                        == marketplace_account_id,
                        WbAdvertisingSpendDocumentRow.sync_run_id == run.sync_run_id,
                    )
                )
                or 0
            )
            if (
                stored_counts
                != (
                    len(evidence.facts),
                    len(evidence.campaigns),
                    len(evidence.spend_documents),
                )
                or stored_source_total != evidence.source_total_spend_kopecks
                or stored_document_total != evidence.document_total_spend_kopecks
            ):
                raise AdvertisingNormalizationError(
                    "advertising snapshot reconciliation failed"
                )
            run.is_materialized = True
            self.session.commit()
            self._prepare()
            return _snapshot(run)
        except Exception:
            self.session.rollback()
            self._prepare()
            raise

    def _ingest(
        self,
        marketplace_account_id: int,
        period: Period,
        source_kind: AdvertisingSourceKind,
        source_reference: str,
        facts: list[AdvertisingFact],
        total: int,
        *,
        evidence_status: str,
        observed_at: datetime | None,
    ) -> AdvertisingSnapshot:
        self._account(marketplace_account_id)
        if not source_reference or len(source_reference) > 255:
            raise AdvertisingNormalizationError("invalid source reference")
        observed = _utc(observed_at or self.now())
        checksum = _snapshot_checksum(facts, total, evidence_status)
        existing = self.session.scalar(
            select(WbAdvertisingSyncRunRow).where(
                WbAdvertisingSyncRunRow.organization_id == self.organization_id,
                WbAdvertisingSyncRunRow.marketplace_account_id
                == marketplace_account_id,
                WbAdvertisingSyncRunRow.source_kind == source_kind,
                WbAdvertisingSyncRunRow.date_from == period.date_from,
                WbAdvertisingSyncRunRow.date_to == period.date_to,
                WbAdvertisingSyncRunRow.snapshot_checksum == checksum,
            )
        )
        if existing is not None and existing.is_materialized:
            if _utc(existing.last_observed_at) < observed:
                existing.last_observed_at = observed
                self.session.commit()
                self._prepare()
            return _snapshot(existing)

        run = existing or WbAdvertisingSyncRunRow(
            sync_run_id=str(uuid.uuid4()),
            organization_id=self.organization_id,
            marketplace_account_id=marketplace_account_id,
            source_kind=source_kind,
            date_from=period.date_from,
            date_to=period.date_to,
            source_reference=source_reference,
            snapshot_checksum=checksum,
            formula_version="wb-advertising-v1",
            source_total_spend_kopecks=total,
            fact_count=len(facts),
            evidence_status=evidence_status,
            is_materialized=False,
            captured_at=observed,
            last_observed_at=observed,
        )
        try:
            if existing is not None:
                self.session.execute(
                    delete(WbAdvertisingFactRow).where(
                        WbAdvertisingFactRow.organization_id == self.organization_id,
                        WbAdvertisingFactRow.marketplace_account_id
                        == marketplace_account_id,
                        WbAdvertisingFactRow.sync_run_id == run.sync_run_id,
                    )
                )
                run.source_reference = source_reference
                run.source_total_spend_kopecks = total
                run.fact_count = len(facts)
                run.evidence_status = evidence_status
                run.captured_at = observed
                run.last_observed_at = observed
            else:
                self.session.add(run)
            self.session.flush()
            self.session.add_all(
                [
                    WbAdvertisingFactRow(
                        advertising_fact_id=_hash(
                            {
                                "organization_id": self.organization_id,
                                "marketplace_account_id": marketplace_account_id,
                                "source_kind": source_kind,
                                "period": period.cache_key,
                                "snapshot_checksum": checksum,
                                "source_identity": fact.source_identity,
                                "payload_checksum": fact.payload_checksum,
                            }
                        ),
                        organization_id=self.organization_id,
                        marketplace_account_id=marketplace_account_id,
                        sync_run_id=run.sync_run_id,
                        grain="day" if fact.business_date is not None else "period",
                        date_from=fact.business_date or period.date_from,
                        date_to=fact.business_date or period.date_to,
                        fact_scope=(
                            "source_sku"
                            if fact.nm_id is not None
                            else (
                                "campaign"
                                if fact.campaign_id is not None
                                else "account"
                            )
                        ),
                        **asdict(fact),
                    )
                    for fact in facts
                ]
            )
            self.session.flush()
            count, stored_total = self.session.execute(
                select(
                    func.count(WbAdvertisingFactRow.advertising_fact_id),
                    func.coalesce(func.sum(WbAdvertisingFactRow.spend_kopecks), 0),
                ).where(
                    WbAdvertisingFactRow.organization_id == self.organization_id,
                    WbAdvertisingFactRow.marketplace_account_id
                    == marketplace_account_id,
                    WbAdvertisingFactRow.sync_run_id == run.sync_run_id,
                )
            ).one()
            if int(count) != len(facts) or int(stored_total) != total:
                raise AdvertisingNormalizationError(
                    "advertising snapshot reconciliation failed"
                )
            run.is_materialized = True
            self.session.commit()
            self._prepare()
        except Exception:
            self.session.rollback()
            self._prepare()
            raise
        return _snapshot(run)

    def _raw_coverage(
        self, marketplace_account_id: int, period: Period, now: datetime
    ) -> list[WbAdvertisingSyncRunRow]:
        # ponytail: scan period versions; rank in SQL if correction history grows large.
        candidates = self.session.scalars(
            select(WbAdvertisingSyncRunRow)
            .where(
                WbAdvertisingSyncRunRow.organization_id == self.organization_id,
                WbAdvertisingSyncRunRow.marketplace_account_id
                == marketplace_account_id,
                WbAdvertisingSyncRunRow.source_kind == "ads_fullstats",
                WbAdvertisingSyncRunRow.evidence_status == "raw",
                WbAdvertisingSyncRunRow.date_from >= period.date_from,
                WbAdvertisingSyncRunRow.date_to <= period.date_to,
                (
                    (WbAdvertisingSyncRunRow.date_from == period.date_from)
                    & (WbAdvertisingSyncRunRow.date_to == period.date_to)
                )
                | (WbAdvertisingSyncRunRow.date_from == WbAdvertisingSyncRunRow.date_to),
                WbAdvertisingSyncRunRow.is_materialized.is_(True),
                WbAdvertisingSyncRunRow.expected_request_count > 0,
                WbAdvertisingSyncRunRow.expected_request_count
                == WbAdvertisingSyncRunRow.completed_request_count,
            )
            .order_by(
                WbAdvertisingSyncRunRow.last_observed_at.desc(),
                WbAdvertisingSyncRunRow.captured_at.desc(),
                WbAdvertisingSyncRunRow.sync_run_id.desc(),
            )
        )
        exact = None
        daily: dict[date, WbAdvertisingSyncRunRow] = {}
        for run in candidates:
            run_period = Period(run.date_from, run.date_to)
            if (
                run_period.temporal_state(now) == "complete"
                and _utc(run.last_observed_at) < run_period.end_exclusive_at
            ):
                continue
            if run_period == period and exact is None:
                exact = run
            if run.date_from == run.date_to:
                daily.setdefault(run.date_from, run)
        if len(daily) == period.days and (
            exact is None
            or min(_utc(run.last_observed_at) for run in daily.values())
            > _utc(exact.last_observed_at)
        ):
            return [daily[day] for day in sorted(daily)]
        return [exact] if exact is not None else []

    def get_raw_reconciliation(
        self, marketplace_account_id: int, period: Period
    ) -> AdvertisingRawReconciliation:
        self._account(marketplace_account_id)
        now = self.now()
        temporal_state = period.temporal_state(now)
        if temporal_state == "future":
            return AdvertisingRawReconciliation("future", period, None)

        runs = self._raw_coverage(marketplace_account_id, period, now)
        if not runs:
            return AdvertisingRawReconciliation(
                "partial" if temporal_state == "partial" else "missing",
                period,
                None,
                diagnostics=("WB_ADS_RAW_EVIDENCE_MISSING",),
            )

        run_ids = [run.sync_run_id for run in runs]
        facts = self.session.scalars(
            select(WbAdvertisingFactRow).where(
                WbAdvertisingFactRow.organization_id == self.organization_id,
                WbAdvertisingFactRow.marketplace_account_id
                == marketplace_account_id,
                WbAdvertisingFactRow.sync_run_id.in_(run_ids),
            )
        ).all()
        campaign_rows = [
            fact
            for fact in facts
            if fact.fact_scope == "campaign" and fact.grain == "period"
        ]
        source_rows = [fact for fact in facts if fact.fact_scope == "source_sku"]
        daily_by_campaign: dict[tuple[str, int], list[WbAdvertisingFactRow]] = {}
        for fact in facts:
            if fact.fact_scope == "campaign" and fact.grain == "day":
                daily_by_campaign.setdefault(
                    (fact.sync_run_id, int(fact.campaign_id)), []
                ).append(fact)
        source_by_campaign: dict[tuple[str, int], list[WbAdvertisingFactRow]] = {}
        source_by_nm: dict[int, list[WbAdvertisingFactRow]] = {}
        for fact in source_rows:
            if fact.campaign_id is not None:
                source_by_campaign.setdefault(
                    (fact.sync_run_id, int(fact.campaign_id)), []
                ).append(fact)
            if fact.nm_id is not None:
                source_by_nm.setdefault(int(fact.nm_id), []).append(fact)
        residuals: list[AdvertisingMetricTotals] = []
        hierarchy_invalid = False
        hierarchy_tolerance_applied = False
        for campaign in campaign_rows:
            residual, invalid, tolerance_applied = _campaign_residual(
                campaign,
                [
                    fact
                    for fact in source_by_campaign.get(
                        (campaign.sync_run_id, int(campaign.campaign_id)), []
                    )
                    if fact.date_from >= campaign.date_from
                    and fact.date_to <= campaign.date_to
                ],
                [
                    day
                    for day in daily_by_campaign.get(
                        (campaign.sync_run_id, int(campaign.campaign_id)), []
                    )
                    if day.date_from >= campaign.date_from
                    and day.date_to <= campaign.date_to
                ],
            )
            residuals.append(residual)
            hierarchy_invalid |= invalid
            hierarchy_tolerance_applied |= tolerance_applied

        campaign_totals = _metric_totals(campaign_rows)
        source_totals = _metric_totals(source_rows)
        campaign_only = _combine_totals(residuals)
        source_sku_by_nm = {
            nm_id: _metric_totals(source_by_nm[nm_id]) for nm_id in sorted(source_by_nm)
        }

        documents = self.session.scalars(
            select(WbAdvertisingSpendDocumentRow).where(
                WbAdvertisingSpendDocumentRow.organization_id == self.organization_id,
                WbAdvertisingSpendDocumentRow.marketplace_account_id
                == marketplace_account_id,
                WbAdvertisingSpendDocumentRow.sync_run_id.in_(run_ids),
            )
        ).all()
        campaign_windows: dict[tuple[str, int], list[WbAdvertisingFactRow]] = {}
        for campaign in campaign_rows:
            campaign_windows.setdefault(
                (campaign.sync_run_id, int(campaign.campaign_id)), []
            ).append(campaign)
        unknown_documents = [
            document
            for document in documents
            if not any(
                campaign.spend_kopecks is not None
                and campaign.date_from <= document.business_date <= campaign.date_to
                for campaign in campaign_windows.get(
                    (document.sync_run_id, document.campaign_id), []
                )
            )
        ]

        mapped, ambiguous = CatalogService(
            self.session, self.organization_id, now=self.now
        ).resolve_wb_product_skus(marketplace_account_id, list(source_sku_by_nm))
        unmapped = tuple(sorted(set(source_sku_by_nm) - set(mapped) - ambiguous))
        diagnostics: list[str] = []
        if any(
            getattr(source_totals, name) is None
            for name in _RECONCILIATION_METRICS
        ):
            diagnostics.append("WB_ADS_SOURCE_METRIC_INCOMPLETE")
        if hierarchy_invalid:
            diagnostics.append("WB_ADS_HIERARCHY_RECONCILIATION_FAILED")
        if hierarchy_tolerance_applied:
            diagnostics.append("WB_ADS_HIERARCHY_TOLERANCE_APPLIED")
        if unknown_documents:
            diagnostics.append("WB_ADS_DOCUMENT_SCOPE_UNKNOWN")
        if ambiguous:
            diagnostics.append("WB_ADS_PRODUCT_MAPPING_AMBIGUOUS")
        if unmapped:
            diagnostics.append("WB_ADS_PRODUCT_MAPPING_MISSING")

        state: AdvertisingSourceState = (
            "partial"
            if temporal_state == "partial"
            else "empty"
            if not facts and not documents
            else "ready"
        )
        return AdvertisingRawReconciliation(
            state=state,
            period=period,
            snapshot=_coverage_snapshot(self.organization_id, period, runs),
            campaign=campaign_totals,
            source_sku=source_totals,
            campaign_only=campaign_only,
            document_spend_kopecks=sum(
                document.spend_kopecks for document in documents
            ),
            unknown_spend_kopecks=sum(
                document.spend_kopecks for document in unknown_documents
            ),
            source_sku_by_nm=source_sku_by_nm,
            mapped_catalog_sku_by_nm=mapped,
            unmapped_nm_ids=unmapped,
            ambiguous_nm_ids=tuple(sorted(ambiguous)),
            diagnostics=tuple(diagnostics),
        )

    def get_pnl_source(
        self, marketplace_account_id: int, period: Period
    ) -> AdvertisingPnlSource:
        reconciliation = self.get_raw_reconciliation(marketplace_account_id, period)
        snapshot = reconciliation.snapshot
        if snapshot is None:
            return AdvertisingPnlSource(
                state=reconciliation.state,
                period=period,
                snapshot=None,
                source_kind=None,
                evidence_status=None,
                spend_by_nm={},
                total_spend_kopecks=None,
                unattributed_spend_kopecks=None,
                blocker_ids=(
                    ()
                    if reconciliation.state == "future"
                    else ("WB_PNL_ADS_NOT_CANONICAL",)
                ),
            )
        if reconciliation.state == "partial":
            return AdvertisingPnlSource(
                state="partial",
                period=period,
                snapshot=snapshot,
                source_kind=snapshot.source_kind,
                evidence_status=snapshot.evidence_status,
                spend_by_nm={},
                total_spend_kopecks=None,
                unattributed_spend_kopecks=None,
                blocker_ids=("WB_PNL_ADS_NOT_CANONICAL",),
            )

        source_missing = any(
            totals is None or totals.spend_kopecks is None
            for totals in (reconciliation.campaign, reconciliation.source_sku)
        )
        hierarchy_invalid = (
            reconciliation.campaign_only is None
            or reconciliation.campaign_only.spend_kopecks is None
        )
        if source_missing or hierarchy_invalid:
            return AdvertisingPnlSource(
                state="partial",
                period=period,
                snapshot=snapshot,
                source_kind=snapshot.source_kind,
                evidence_status=snapshot.evidence_status,
                spend_by_nm={},
                total_spend_kopecks=None,
                unattributed_spend_kopecks=None,
                blocker_ids=(
                    "WB_ADS_SOURCE_METRIC_INCOMPLETE"
                    if source_missing
                    else "WB_ADS_HIERARCHY_RECONCILIATION_FAILED",
                ),
            )

        assert reconciliation.source_sku is not None
        assert reconciliation.campaign_only is not None
        source_spend = int(reconciliation.source_sku.spend_kopecks)
        campaign_only_spend = int(reconciliation.campaign_only.spend_kopecks)
        total = source_spend + campaign_only_spend
        mapped = reconciliation.mapped_catalog_sku_by_nm
        spend_by_nm = {
            nm_id: int(metrics.spend_kopecks)
            for nm_id, metrics in reconciliation.source_sku_by_nm.items()
            if nm_id in mapped and metrics.spend_kopecks
        }
        carrying_spend = {
            nm_id
            for nm_id, metrics in reconciliation.source_sku_by_nm.items()
            if metrics.spend_kopecks
        }
        blockers: list[str] = []
        if carrying_spend.intersection(reconciliation.ambiguous_nm_ids):
            blockers.append("WB_ADS_PRODUCT_MAPPING_AMBIGUOUS")
        if carrying_spend.intersection(reconciliation.unmapped_nm_ids):
            blockers.append("WB_ADS_PRODUCT_MAPPING_MISSING")
        unattributed = total - sum(spend_by_nm.values())
        if unattributed:
            blockers.append("WB_PNL_ADVERTISING_UNATTRIBUTED")

        return AdvertisingPnlSource(
            state="empty" if reconciliation.state == "empty" else "ready",
            period=period,
            snapshot=snapshot,
            source_kind=snapshot.source_kind,
            evidence_status=snapshot.evidence_status,
            spend_by_nm=spend_by_nm,
            total_spend_kopecks=total,
            unattributed_spend_kopecks=unattributed,
            blocker_ids=tuple(blockers),
        )


def ingest_legacy_advertising_payload(
    organization_id: int,
    payload: dict[str, Any],
    *,
    source_kind: AdvertisingSourceKind,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
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
                    MarketplaceAccountRow.status == "connected",
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
            snapshot = AdvertisingService(session, organization_id).ingest_payload(
                int(account_ids[0]),
                period,
                source_kind,
                payload,
                source_reference=f"wb_sync:{source_kind}:{period.cache_key}",
                observed_at=observed_at,
            )
            return {
                "state": "ready",
                "marketplaceAccountId": snapshot.marketplace_account_id,
                "syncRunId": snapshot.sync_run_id,
                "snapshotChecksum": snapshot.snapshot_checksum,
                "sourceKind": snapshot.source_kind,
                "sourceTotalSpendKopecks": snapshot.source_total_spend_kopecks,
                "factCount": snapshot.fact_count,
                "evidenceStatus": snapshot.evidence_status,
            }
    except Exception as exc:  # shadow writes must not break the established sync
        logger.exception(
            "canonical WB advertising shadow ingest failed for organization %s",
            organization_id,
        )
        return {"state": "failed", "errorCode": type(exc).__name__}


def shadow_ingest_legacy_advertising_payload(
    organization_id: int,
    payload: dict[str, Any],
    *,
    source_kind: AdvertisingSourceKind,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    if (
        not settings.advertising_shadow_ingest_enabled
        or organization_id not in settings.advertising_shadow_ingest_organization_ids
    ):
        return {"state": "disabled"}
    return ingest_legacy_advertising_payload(
        organization_id,
        payload,
        source_kind=source_kind,
        observed_at=observed_at,
    )
