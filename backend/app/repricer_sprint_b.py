from __future__ import annotations

from dataclasses import dataclass, field
from copy import copy
from decimal import Decimal
from contextvars import ContextVar
from threading import RLock
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, Field

from app.contracts.envelopes import UtcDateTime
from app.discovery.repricer_sources import build_repricer_discovery_readiness
from app.repricer_settings import get_repricer_guard_limits, get_repricer_spp_accounting_settings, with_organization_algorithm, organization_algorithm_context, current_algorithm_organization_id
from app.wb22_apply import PriceApplyRequest, PriceApplyResponse, PriceApplyRowRequest, run_price_apply, poll_price_apply
from app.wb23_runtime import (
    MarginPreviewRequest,
    MarginPreviewResponse,
    PminPmaxValidationRequest,
    PminPmaxValidationResponse,
    PriceMetricValue,
    PriceMetricsSnapshot,
    build_margin_preview,
    build_pmin_pmax_validation,
    build_price_metrics_snapshot,
    build_price_metrics_snapshot_from_row,
)
from app.wb_api.client import WbApiClient, RateLimitedWbApiClient
from app.repricer_price_safety import baseline_identity, seller_to_base, seller_price_interval, binding_key, ScopedPriceClient, price_baseline, price_write_permission
from app.wb_api.client import WbApiRequest
from app import repricer_dispatch_store as dispatch_store


GuardSeverity = Literal["info", "warning", "blocker"]
DraftState = Literal["draft", "approved", "queued", "sent", "accepted", "failed", "blocked", "needs_attention", "local_applied"]
ApplyJobState = Literal["queued", "sent", "accepted", "failed", "blocked", "needs_attention", "local_applied"]
ApprovalState = Literal["required", "approved"]


FORMULA_VERSION_RECOMMENDATION = "sprint-b-repricer-v1"
FORMULA_VERSION_GUARDS = "sprint-b-guards-v1"
SOURCE_SNAPSHOT_VERSION = "wb23-runtime-v1"


def _buyer_price_for_accounting(seller_price_kopecks: int, spp_pct: float | None) -> int | None:
    if spp_pct is None:
        return None
    accounting = get_repricer_spp_accounting_settings()
    multiplier = 1 - float(spp_pct) / 100
    if accounting.mode == "spp_plus_wallet" and accounting.wb_wallet_pct is not None:
        multiplier *= 1 - float(accounting.wb_wallet_pct) / 100
    return int(round(seller_price_kopecks * multiplier))


class DraftEconomicsInput(BaseModel):
    cogsKopecks: int | None = Field(default=None, ge=0)
    commissionPct: float | None = Field(default=None, ge=0, le=100)
    logisticsKopecks: int | None = Field(default=None, ge=0)
    storageKopecks: int = Field(default=0, ge=0)
    taxKopecks: int = Field(default=0, ge=0)
    taxPct: float | None = Field(default=None, ge=0, le=100)
    taxConfigured: bool = True
    buyoutPct: float | None = Field(default=None, ge=0, le=100)
    stockUnits: int | None = Field(default=None, ge=0)
    promoActive: bool = False


class PriceRecommendationRequest(BaseModel):
    scenario: str = "complete"
    articleId: str = Field(min_length=1)
    nmId: int = Field(gt=0)
    candidateSellerPriceKopecks: int = Field(gt=0)
    minPriceKopecks: int | None = Field(default=None, gt=0)
    pMaxKopecks: int | None = Field(default=None, gt=0)
    strategyMinPriceKopecks: int | None = Field(default=None, gt=0)
    strategyMaxPriceKopecks: int | None = Field(default=None, gt=0)
    economics: DraftEconomicsInput


class PriceDraftCreateRequest(BaseModel):
    scenario: str = "complete"
    articleId: str = Field(min_length=1)
    nmId: int = Field(gt=0)
    candidateSellerPriceKopecks: int = Field(gt=0)
    minPriceKopecks: int | None = Field(default=None, gt=0)
    pMaxKopecks: int | None = Field(default=None, gt=0)
    strategyMinPriceKopecks: int | None = Field(default=None, gt=0)
    strategyMaxPriceKopecks: int | None = Field(default=None, gt=0)
    economics: DraftEconomicsInput
    reason: str | None = None
    expectedPriceBaseline: dict[str, Any] | None = None
    expectedSettingsRevision: str | None = None
    strategySourceEvidence: list[dict[str, Any]] = Field(default_factory=list)
    nightWindowKey: str | None = None


class ApproveDraftRequest(BaseModel):
    approvalRef: str = Field(min_length=1)
    reason: str | None = None


class ApplyDraftRequest(BaseModel):
    scenario: str = "complete"
    pollMaxAttempts: int = Field(default=6, ge=1, le=60)
    pollDelaySeconds: float = Field(default=0.0, ge=0.0, le=5.0)
    skipStatusPoll: bool = False


class RetryApplyJobRequest(BaseModel):
    scenario: str = "complete"
    pollMaxAttempts: int = Field(default=6, ge=1, le=60)
    pollDelaySeconds: float = Field(default=0.0, ge=0.0, le=5.0)


class GuardTrigger(BaseModel):
    code: str = Field(min_length=1)
    severity: GuardSeverity
    message: str = Field(min_length=1)
    observedValue: float | int | str | None = None
    threshold: float | int | str | None = None


class SourceMetricSnapshot(BaseModel):
    metricId: str = Field(min_length=1)
    sourceStatus: Literal["fresh", "partial", "stale", "blocked"]
    blockerIds: list[str]
    fieldSource: str = Field(min_length=1)
    value: int | float | None = None
    fetchedAt: UtcDateTime
    staleAfter: UtcDateTime
    confidence: Literal["high", "medium", "low", "blocked"]
    warnings: list[str] = Field(default_factory=list)


class SourceSnapshot(BaseModel):
    sourceStatus: Literal["fresh", "partial", "stale", "blocked"]
    blockerIds: list[str]
    capturedAt: UtcDateTime
    snapshotVersion: str = Field(min_length=1)
    metrics: list[SourceMetricSnapshot]
    priceBaseline: dict[str, Any] | None = None


class NormalizedEconomics(BaseModel):
    effectiveStepPct: float | None = None
    strategyMinPriceKopecks: int | None = None
    strategyMaxPriceKopecks: int | None = None
    articleId: str
    nmId: int
    currentSellerPriceKopecks: int | None = None
    currentBuyerPriceKopecks: int | None = None
    currentSppPct: float | None = None
    candidateSellerPriceKopecks: int
    candidateBuyerPriceKopecks: int | None = None
    cogsKopecks: int | None = None
    commissionPct: float | None = None
    logisticsKopecks: int | None = None
    storageKopecks: int = 0
    taxKopecks: int = 0
    buyoutPct: float | None = None
    stockUnits: int | None = None
    promoActive: bool = False
    minPriceKopecks: int | None = None
    pMinKopecks: int | None = None
    pMaxKopecks: int | None = None
    predictedMarginKopecks: int | None = None
    predictedMarginPct: float | None = None


class PriceGuardReport(BaseModel):
    canApply: bool
    blockers: list[str]
    triggers: list[GuardTrigger]


class PriceRecommendationResponse(BaseModel):
    articleId: str
    nmId: int
    formulaVersion: str
    guardFormulaVersion: str
    sourceSnapshot: SourceSnapshot
    normalized: NormalizedEconomics
    guardReport: PriceGuardReport


class PriceDraftView(BaseModel):
    pendingApprovalId: str | None = None
    settingsRevision: str | None = None
    draftId: str = Field(min_length=1)
    articleId: str = Field(min_length=1)
    nmId: int = Field(gt=0)
    state: DraftState
    approvalState: ApprovalState
    approvalRef: str | None = None
    approvalActorId: str | None = None
    createdByActorId: str = Field(min_length=1)
    createdByRole: str = Field(min_length=1)
    reason: str | None = None
    formulaVersion: str = Field(min_length=1)
    guardFormulaVersion: str = Field(min_length=1)
    sourceSnapshot: SourceSnapshot
    normalized: NormalizedEconomics
    guardReport: PriceGuardReport
    applyJobId: str | None = None
    createdAt: UtcDateTime
    updatedAt: UtcDateTime
    organizationId: int | None = None
    bindingKey: str | None = Field(default=None, exclude=True)
    nightWindowKey: str | None = None
    economics: DraftEconomicsInput | None = None
    dispatchKey: str | None = None
    taxRevision: str | None = None


class ApplyJobRowError(BaseModel):
    nmId: int | None = None
    sizeId: int | None = None
    wbStatus: int | None = None
    errorText: str


class ApplyJobView(BaseModel):
    jobId: str = Field(min_length=1)
    draftId: str = Field(min_length=1)
    articleId: str = Field(min_length=1)
    nmId: int = Field(gt=0)
    state: ApplyJobState
    sourceStatus: Literal["fresh", "partial", "blocked"]
    blockerIds: list[str]
    wbUploadId: int | None = None
    wbStatus: int | None = None
    rowErrors: list[ApplyJobRowError] = Field(default_factory=list)
    retryCount: int = Field(ge=0)
    maxRetryCount: int = Field(ge=0)
    nextRetryAt: UtcDateTime | None = None
    stopRequired: bool
    notes: list[str] = Field(default_factory=list)
    lastApplyResult: PriceApplyResponse
    formulaVersion: str = Field(min_length=1)
    guardFormulaVersion: str = Field(min_length=1)
    sourceSnapshot: SourceSnapshot
    createdAt: UtcDateTime
    updatedAt: UtcDateTime
    dispatchKey: str | None = None


class _PreparedDraft(BaseModel):
    sourceSnapshot: SourceSnapshot
    normalized: NormalizedEconomics
    guardReport: PriceGuardReport


class _PriceHistoryPoint(BaseModel):
    sellerPriceKopecks: int = Field(gt=0)
    appliedAt: UtcDateTime
    oldSellerPriceKopecks: int | None = None


@dataclass
class _SprintBMemoryState:
    drafts: dict[str, PriceDraftView] = field(default_factory=dict)
    jobs: dict[str, ApplyJobView] = field(default_factory=dict)
    price_history: dict[str, list[_PriceHistoryPoint]] = field(default_factory=dict)


_MEMORY = _SprintBMemoryState()
_APPLY_LOCK = RLock()
_CALC_SCOPE: ContextVar[tuple[int, str, int] | None] = ContextVar("repricer_price_scope", default=None)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _metric_by_id(snapshot: PriceMetricsSnapshot, metric_id: str) -> PriceMetricValue | None:
    return next((metric for metric in snapshot.metrics if metric.metricId == metric_id), None)


def _metric_float(metric: PriceMetricValue | None) -> float | None:
    if metric is None:
        return None
    value = metric.value
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _to_source_snapshot(snapshot: PriceMetricsSnapshot) -> SourceSnapshot:
    captured_at = _now_utc()
    items: list[SourceMetricSnapshot] = []
    for metric in snapshot.metrics:
        items.append(
            SourceMetricSnapshot(
                metricId=metric.metricId,
                sourceStatus=metric.sourceStatus,
                blockerIds=metric.blockerIds,
                fieldSource=metric.fieldSource,
                value=metric.value,
                fetchedAt=metric.freshness.fetchedAt,
                staleAfter=metric.freshness.staleAfter,
                confidence=metric.freshness.confidence,
                warnings=metric.warnings,
            )
        )
    return SourceSnapshot(
        sourceStatus=snapshot.sourceStatus,
        blockerIds=snapshot.blockerIds,
        capturedAt=captured_at,
        snapshotVersion=SOURCE_SNAPSHOT_VERSION,
        metrics=items,
        priceBaseline=snapshot.priceBaseline,
    )


def _predict_margin(candidate_buyer_price: int | None, economics: DraftEconomicsInput) -> tuple[int | None, float | None]:
    if candidate_buyer_price is None:
        return None, None
    if economics.cogsKopecks is None:
        return None, None
    if economics.commissionPct is None:
        return None, None
    if economics.logisticsKopecks is None:
        return None, None
    commission_kopecks = int(round(candidate_buyer_price * economics.commissionPct / 100))
    margin_kopecks = (
        candidate_buyer_price
        - commission_kopecks
        - economics.cogsKopecks
        - economics.logisticsKopecks
        - economics.storageKopecks
        - economics.taxKopecks
    )
    margin_pct = (margin_kopecks / candidate_buyer_price * 100) if candidate_buyer_price > 0 else None
    return margin_kopecks, margin_pct


def _daily_reference_price(article_id: str, fallback_price: int | None) -> int | None:
    scope = _CALC_SCOPE.get()
    if scope is not None:
        return dispatch_store.daily_reference(*scope, fallback_price, _now_utc())
    points = _MEMORY.price_history.get(article_id, [])
    cutoff = _now_utc() - timedelta(hours=24)
    recent = [point for point in points if point.appliedAt >= cutoff]
    if recent:
        first = min(recent, key=lambda point: point.appliedAt)
        return getattr(first, "oldSellerPriceKopecks", None) or first.sellerPriceKopecks
    return fallback_price


def _evaluate_guards(
    article_id: str,
    snapshot: PriceMetricsSnapshot,
    source_snapshot: SourceSnapshot,
    normalized: NormalizedEconomics,
    pmin_validation: PminPmaxValidationResponse,
    margin_preview: MarginPreviewResponse,
) -> PriceGuardReport:
    triggers: list[GuardTrigger] = []
    blockers: set[str] = set()
    guard_limits = get_repricer_guard_limits()

    discovery = build_repricer_discovery_readiness()
    if not discovery.canUnblockPriceGuard:
        for blocker in discovery.blockingIds:
            if blocker in {"WB-22", "WB-23"}:
                blockers.add(blocker)
                triggers.append(
                    GuardTrigger(
                        code="discovery_not_ready",
                        severity="blocker",
                        message="WB-22/WB-23 readiness is not confirmed for production apply",
                        observedValue=blocker,
                        threshold="confirmed",
                    )
                )

    if source_snapshot.sourceStatus in {"blocked", "stale"}:
        blockers.add("WB-23")
        triggers.append(
            GuardTrigger(
                code="source_stale_or_blocked",
                severity="blocker",
                message="Critical price sources are stale or blocked",
                observedValue=source_snapshot.sourceStatus,
                threshold="fresh|partial",
            )
        )

    if normalized.currentSppPct is None:
        blockers.add("WB-23")
        triggers.append(
            GuardTrigger(
                code="spp_missing",
                severity="blocker",
                message="SPP input is missing, candidate buyer plane cannot be validated",
            )
        )

    if normalized.cogsKopecks is None:
        blockers.add("WB-23")
        triggers.append(
            GuardTrigger(
                code="cogs_missing",
                severity="blocker",
                message="COGS is missing for SKU",
            )
        )

    if normalized.commissionPct is None:
        blockers.add("WB-23")
        triggers.append(
            GuardTrigger(
                code="commission_missing",
                severity="blocker",
                message="Commission input is missing for SKU",
            )
        )

    if normalized.logisticsKopecks is None:
        blockers.add("WB-23")
        triggers.append(
            GuardTrigger(
                code="logistics_missing",
                severity="blocker",
                message="Logistics input is missing for SKU",
            )
        )

    if normalized.buyoutPct is None:
        blockers.add("WB-23")
        triggers.append(
            GuardTrigger(
                code="buyout_missing",
                severity="blocker",
                message="Buyout input is missing for SKU",
            )
        )

    if normalized.stockUnits is None:
        blockers.add("WB-23")
        triggers.append(
            GuardTrigger(
                code="stock_missing",
                severity="blocker",
                message="Stock input is missing for SKU",
            )
        )
    elif normalized.stockUnits <= 0:
        blockers.add("WB-23")
        triggers.append(
            GuardTrigger(
                code="stock_oos",
                severity="blocker",
                message="Stock is empty, auto-apply is blocked",
                observedValue=normalized.stockUnits,
                threshold=1,
            )
        )

    if normalized.promoActive and normalized.minPriceKopecks is None:
        blockers.add("WB-23")
        triggers.append(
            GuardTrigger(
                code="promo_min_price_missing",
                severity="blocker",
                message="Promo flag is active but minPrice is not provided",
            )
        )

    if normalized.minPriceKopecks is not None and normalized.candidateSellerPriceKopecks < normalized.minPriceKopecks:
        blockers.add("WB-23")
        triggers.append(
            GuardTrigger(
                code="candidate_below_min_price",
                severity="blocker",
                message="Candidate seller price is lower than minPrice",
                observedValue=normalized.candidateSellerPriceKopecks,
                threshold=normalized.minPriceKopecks,
            )
        )

    if normalized.pMaxKopecks is not None:
        if normalized.minPriceKopecks is not None and normalized.minPriceKopecks > normalized.pMaxKopecks:
            blockers.add("WB-23")
            triggers.append(GuardTrigger(code="price_bounds_conflict", severity="blocker",
                message="Seller P_MIN exceeds seller P_MAX", observedValue=normalized.minPriceKopecks, threshold=normalized.pMaxKopecks))
        if normalized.candidateSellerPriceKopecks > normalized.pMaxKopecks:
            blockers.add("WB-23")
            triggers.append(GuardTrigger(code="candidate_above_max_price", severity="blocker",
                message="Candidate seller price exceeds seller P_MAX", observedValue=normalized.candidateSellerPriceKopecks, threshold=normalized.pMaxKopecks))

    if normalized.predictedMarginKopecks is not None and normalized.predictedMarginKopecks < 0:
        blockers.add("WB-23")
        triggers.append(
            GuardTrigger(
                code="negative_margin",
                severity="blocker",
                message="Predicted margin is negative",
                observedValue=normalized.predictedMarginKopecks,
                threshold=0,
            )
        )

    if guard_limits.pmin_guard_enabled and not pmin_validation.isValid:
        blockers.add("WB-23")
        triggers.append(
            GuardTrigger(
                code="pmin_pmax_block",
                severity="blocker",
                message="Candidate price failed P_min/P_max validation",
                observedValue=",".join(pmin_validation.reasons),
                threshold="isValid=true",
            )
        )

    current_price = normalized.currentSellerPriceKopecks
    if current_price is not None and current_price > 0:
        delta_update_pct = abs(normalized.candidateSellerPriceKopecks - current_price) / current_price * 100
        step_pct = normalized.effectiveStepPct if normalized.effectiveStepPct is not None else guard_limits.per_update_step_pct
        if abs(normalized.candidateSellerPriceKopecks - current_price) * 100 > Decimal(current_price) * Decimal(str(step_pct)):
            blockers.add("WB-23")
            triggers.append(
                GuardTrigger(
                    code="per_update_step_exceeded",
                    severity="blocker",
                    message=f"Per-update change exceeds {step_pct:.1f}%",
                    observedValue=round(delta_update_pct, 4),
                    threshold=step_pct,
                )
            )

    daily_ref = _daily_reference_price(article_id, current_price)
    if daily_ref is not None and daily_ref > 0:
        delta_day_pct = abs(normalized.candidateSellerPriceKopecks - daily_ref) / daily_ref * 100
        if abs(normalized.candidateSellerPriceKopecks - daily_ref) * 100 > Decimal(daily_ref) * Decimal(str(guard_limits.per_day_step_pct)):
            blockers.add("WB-23")
            triggers.append(
                GuardTrigger(
                    code="per_day_step_exceeded",
                    severity="blocker",
                    message=f"24-hour change exceeds {guard_limits.per_day_step_pct:.1f}%",
                    observedValue=round(delta_day_pct, 4),
                    threshold=guard_limits.per_day_step_pct,
                )
            )

    if margin_preview.sourceStatus in {"blocked"}:
        blockers.add("WB-23")
        triggers.append(
            GuardTrigger(
                code="margin_preview_blocked",
                severity="blocker",
                message="Margin preview source is blocked",
                observedValue=margin_preview.sourceStatus,
                threshold="fresh|partial",
            )
        )

    return PriceGuardReport(
        canApply=not blockers,
        blockers=sorted(blockers),
        triggers=triggers,
    )


def _build_prepared_draft(
    client: WbApiClient,
    payload: PriceRecommendationRequest,
    *,
    sku_row: dict[str, Any] | None = None,
    orders_client: WbApiClient | None = None,
) -> _PreparedDraft:
    if sku_row is not None:
        price_snapshot = build_price_metrics_snapshot_from_row(payload.articleId, sku_row)
    else:
        price_snapshot = build_price_metrics_snapshot(
            client=client,
            article_id=payload.articleId,
            nm_id=int(payload.nmId) if payload.nmId is not None else None,
            # Historical order SPP is not a current buyer-price observation.
            # Do not issue an account-wide orders read for a selected price.
            orders_client=None,
        )
    source_snapshot = _to_source_snapshot(price_snapshot)
    candidate = payload.candidateSellerPriceKopecks
    baseline = source_snapshot.priceBaseline
    current = _metric_float(_metric_by_id(price_snapshot, "current_price_before_spp"))
    current = int(round(current)) if current is not None else None
    limits = get_repricer_guard_limits()
    step_pct = limits.per_update_step_pct
    sku_step = (sku_row or {}).get("settings", {}).get("priceStepPct")
    if isinstance(sku_step, (int, float)) and 0 < sku_step < step_pct:
        step_pct = float(sku_step)
    lower, upper = seller_price_interval(current, payload.minPriceKopecks, payload.pMaxKopecks,
        step_pct, _daily_reference_price(payload.articleId, current), limits.per_day_step_pct)
    if payload.strategyMinPriceKopecks is not None:
        lower = max(lower, payload.strategyMinPriceKopecks)
    if payload.strategyMaxPriceKopecks is not None:
        upper = min(upper, payload.strategyMaxPriceKopecks) if upper is not None else payload.strategyMaxPriceKopecks
    representation_error = "price_constraints_conflict" if upper is not None and lower > upper else None
    if baseline is not None:
        try:
            # Constrain representation of an eligible candidate; an out-of-limit
            # requested price still reaches the ordinary guards as an error.
            if lower <= candidate and (upper is None or candidate <= upper) and representation_error is None:
                _base_price, candidate = seller_to_base(candidate, baseline["discountPct"], minimum=lower, maximum=upper)
            else:
                _base_price, candidate = seller_to_base(candidate, baseline["discountPct"])
        except ValueError as error:
            representation_error = str(error)
            _base_price, candidate = seller_to_base(candidate, baseline["discountPct"])
    economics = payload.economics
    if economics.taxPct is not None:
        economics = economics.model_copy(update={"taxKopecks": round(candidate * economics.taxPct / 100)})
    payload = payload.model_copy(update={"candidateSellerPriceKopecks": candidate, "economics": economics})

    metric_before = _metric_by_id(price_snapshot, "current_price_before_spp")
    metric_after = _metric_by_id(price_snapshot, "current_price_after_spp")
    metric_spp = _metric_by_id(price_snapshot, "spp_pct")

    current_before = _metric_float(metric_before)
    current_after = _metric_float(metric_after)
    current_spp_pct = _metric_float(metric_spp)

    candidate_buyer_price = _buyer_price_for_accounting(payload.candidateSellerPriceKopecks, current_spp_pct)

    margin_cost = payload.economics.cogsKopecks if payload.economics.cogsKopecks and payload.economics.cogsKopecks > 0 else 1

    margin_preview_request = MarginPreviewRequest(
        scenario=payload.scenario,
        costPriceKopecks=margin_cost,
        wbCommissionPct=payload.economics.commissionPct or 0,
        logisticsKopecks=payload.economics.logisticsKopecks or 0,
        taxKopecks=payload.economics.taxKopecks,
        storageKopecks=payload.economics.storageKopecks,
    )
    margin_preview = build_margin_preview(
        client=client,
        article_id=payload.articleId,
        payload=margin_preview_request,
        price_snapshot=price_snapshot,
    )

    pmin_request = PminPmaxValidationRequest(
        scenario=payload.scenario,
        candidateBuyerPriceKopecks=candidate_buyer_price or payload.candidateSellerPriceKopecks,
        costPriceKopecks=payload.economics.cogsKopecks or 1,
        wbCommissionPct=payload.economics.commissionPct or 0,
        targetMarginPct=10,
        logisticsKopecks=payload.economics.logisticsKopecks or 0,
        taxKopecks=payload.economics.taxKopecks,
        fixedCostsKopecks=payload.economics.storageKopecks,
        pMaxKopecks=_buyer_price_for_accounting(payload.pMaxKopecks, current_spp_pct) if payload.pMaxKopecks is not None else None,
    )
    pmin_validation = build_pmin_pmax_validation(
        client=client,
        article_id=payload.articleId,
        payload=pmin_request,
        price_snapshot=price_snapshot,
    )

    predicted_margin_kopecks, predicted_margin_pct = _predict_margin(candidate_buyer_price, payload.economics)

    normalized = NormalizedEconomics(
        effectiveStepPct=step_pct,
        strategyMinPriceKopecks=payload.strategyMinPriceKopecks,
        strategyMaxPriceKopecks=payload.strategyMaxPriceKopecks,
        articleId=payload.articleId,
        nmId=payload.nmId,
        currentSellerPriceKopecks=int(round(current_before)) if current_before is not None else None,
        currentBuyerPriceKopecks=int(round(current_after)) if current_after is not None else None,
        currentSppPct=round(current_spp_pct, 4) if current_spp_pct is not None else None,
        candidateSellerPriceKopecks=payload.candidateSellerPriceKopecks,
        candidateBuyerPriceKopecks=candidate_buyer_price,
        cogsKopecks=payload.economics.cogsKopecks,
        commissionPct=payload.economics.commissionPct,
        logisticsKopecks=payload.economics.logisticsKopecks,
        storageKopecks=payload.economics.storageKopecks,
        taxKopecks=payload.economics.taxKopecks,
        buyoutPct=payload.economics.buyoutPct,
        stockUnits=payload.economics.stockUnits,
        promoActive=payload.economics.promoActive,
        minPriceKopecks=payload.minPriceKopecks,
        pMinKopecks=pmin_validation.pMinKopecks,
        pMaxKopecks=payload.pMaxKopecks,
        predictedMarginKopecks=predicted_margin_kopecks,
        predictedMarginPct=round(predicted_margin_pct, 4) if predicted_margin_pct is not None else None,
    )

    guard_report = _evaluate_guards(
        article_id=payload.articleId,
        snapshot=price_snapshot,
        source_snapshot=source_snapshot,
        normalized=normalized,
        pmin_validation=pmin_validation,
        margin_preview=margin_preview,
    )
    if representation_error:
        guard_report.canApply = False
        guard_report.blockers = sorted(set(guard_report.blockers + ["WB-23"]))
        guard_report.triggers.append(GuardTrigger(code=representation_error, severity="blocker",
            message="No provider-representable price satisfies the economic, step and daily limits"))
    if (payload.strategyMinPriceKopecks is not None and candidate < payload.strategyMinPriceKopecks
        or payload.strategyMaxPriceKopecks is not None and candidate > payload.strategyMaxPriceKopecks):
        guard_report.canApply = False
        guard_report.blockers = sorted(set(guard_report.blockers + ["WB-23"]))
        guard_report.triggers.append(GuardTrigger(code="strategy_floor_conflict", severity="blocker",
            message="Provider rounding cannot reduce the strategy's minimum ruble change"))
    if not payload.economics.taxConfigured:
        guard_report.canApply = False
        guard_report.blockers = sorted(set(guard_report.blockers + ["WB-23"]))
        guard_report.triggers.append(GuardTrigger(code="tax_configuration_missing", severity="blocker", message="Configured tax input is unavailable"))

    return _PreparedDraft(
        sourceSnapshot=source_snapshot,
        normalized=normalized,
        guardReport=guard_report,
    )


def build_price_recommendation(
    client: WbApiClient,
    payload: PriceRecommendationRequest,
    *,
    sku_row: dict[str, Any] | None = None,
    orders_client: WbApiClient | None = None,
) -> PriceRecommendationResponse:
    org = current_algorithm_organization_id()
    scope_binding = binding_key(client, org)
    token = _CALC_SCOPE.set((org, scope_binding, payload.nmId) if org is not None and scope_binding else None)
    try:
        prepared = _build_prepared_draft(client=client, payload=payload, sku_row=sku_row, orders_client=orders_client)
    finally:
        _CALC_SCOPE.reset(token)
    return PriceRecommendationResponse(
        articleId=payload.articleId,
        nmId=payload.nmId,
        formulaVersion=FORMULA_VERSION_RECOMMENDATION,
        guardFormulaVersion=FORMULA_VERSION_GUARDS,
        sourceSnapshot=prepared.sourceSnapshot,
        normalized=prepared.normalized,
        guardReport=prepared.guardReport,
    )


@with_organization_algorithm
def create_price_draft(
    actor_id: str,
    actor_role: str,
    client: WbApiClient,
    payload: PriceDraftCreateRequest,
    *,
    sku_row: dict[str, Any] | None = None,
    orders_client: WbApiClient | None = None,
    organization_id: int | None = None,
    pending_approval_id: str | None = None,
) -> PriceDraftView:
    scope_binding = binding_key(client, organization_id)
    tax_revision = None
    settings_revision = None
    if organization_id is not None and scope_binding:
        rate, tax_revision = dispatch_store.planning_tax(organization_id, _now_utc())
        settings_revision = dispatch_store.pricing_settings_revision(organization_id, payload.articleId)
        payload = payload.model_copy(update={"economics": payload.economics.model_copy(update={"taxPct": rate, "taxConfigured": rate is not None})})
    scope_token = _CALC_SCOPE.set((organization_id, scope_binding, payload.nmId) if organization_id is not None and scope_binding else None)
    try:
        prepared = _build_prepared_draft(
            client=client,
            payload=PriceRecommendationRequest(
                scenario=payload.scenario,
                articleId=payload.articleId,
                nmId=payload.nmId,
                candidateSellerPriceKopecks=payload.candidateSellerPriceKopecks,
                minPriceKopecks=payload.minPriceKopecks,
                pMaxKopecks=payload.pMaxKopecks,
                strategyMinPriceKopecks=payload.strategyMinPriceKopecks,
                strategyMaxPriceKopecks=payload.strategyMaxPriceKopecks,
                economics=payload.economics,
            ),
            sku_row=sku_row,
            orders_client=orders_client,
        )
    finally:
        _CALC_SCOPE.reset(scope_token)
    if payload.expectedSettingsRevision is not None and payload.expectedSettingsRevision != settings_revision:
        prepared.guardReport.canApply = False
        prepared.guardReport.blockers = sorted(set(prepared.guardReport.blockers + ["WB-23"]))
        prepared.guardReport.triggers.append(GuardTrigger(code="pricing_settings_changed_reapproval_required", severity="blocker",
            message="Assignment or settings changed after strategy calculation"))
    for index, evidence in enumerate(payload.strategySourceEvidence):
        metric = SourceMetricSnapshot(metricId=f"strategy_source_{index}", sourceStatus="fresh", blockerIds=[],
            fieldSource=f"{evidence['source']}:{evidence.get('periodFrom', '')}:{evidence.get('periodTo', '')}",
            fetchedAt=evidence["fetchedAt"], staleAfter=evidence["staleAfter"], confidence="high")
        prepared.sourceSnapshot.metrics.append(metric)
        if metric.staleAfter <= _now_utc() or metric.fetchedAt > _now_utc() + timedelta(seconds=30):
            prepared.guardReport.canApply = False
            prepared.guardReport.blockers = sorted(set(prepared.guardReport.blockers + ["WB-23"]))
            prepared.guardReport.triggers.append(GuardTrigger(code="strategy_source_expired", severity="blocker",
                message="Required strategy observation expired before draft creation"))
    if pending_approval_id:
        from app.repricer_persistence.store import get_pending_price_approval
        pending = get_pending_price_approval(organization_id=organization_id, approval_id=pending_approval_id) if organization_id is not None else None
        if pending and pending.get("source") == "worker" and (
            payload.expectedSettingsRevision is None
            or not sku_row or sku_row.get("settings", {}).get("automationEnabled") is False
            or sku_row.get("meta", {}).get("assignmentSource") == "none"
            or sku_row.get("meta", {}).get("status") in {"manual", "warmup"}
        ):
            prepared.guardReport.canApply = False
            prepared.guardReport.blockers = sorted(set(prepared.guardReport.blockers + ["WB-23"]))
            prepared.guardReport.triggers.append(GuardTrigger(code="strategy_recalculation_required", severity="blocker",
                message="Unsubmitted strategy work no longer has a current eligible assignment"))
        from app.repricer_execution import _economics_from_row, _effective_pmin, _settings_pmax_kopecks
        current = _economics_from_row(sku_row) if sku_row else None
        fields = ("cogsKopecks", "commissionPct", "logisticsKopecks", "storageKopecks", "taxPct", "buyoutPct", "stockUnits", "promoActive")
        economics_changed = current is None or any(getattr(current, field) != getattr(payload.economics, field) for field in fields)
        bounds_changed = sku_row is not None and (
            payload.minPriceKopecks != _effective_pmin(sku_row)[0] or payload.pMaxKopecks != _settings_pmax_kopecks(
                sku_row["settings"], int(sku_row["meta"]["currentPriceKopecks"])))
        if economics_changed or bounds_changed:
            prepared.guardReport.canApply = False
            prepared.guardReport.blockers = sorted(set(prepared.guardReport.blockers + ["WB-23"]))
            prepared.guardReport.triggers.append(GuardTrigger(code="approved_economic_inputs_changed", severity="blocker",
                message="Product economics or limits changed; a fresh recommendation and approval are required"))
    if payload.expectedPriceBaseline is not None and (prepared.sourceSnapshot.priceBaseline is None or
            baseline_identity(prepared.sourceSnapshot.priceBaseline) != baseline_identity(payload.expectedPriceBaseline)):
        prepared.guardReport.canApply = False
        prepared.guardReport.blockers = sorted(set(prepared.guardReport.blockers + ["WB-23"]))
        prepared.guardReport.triggers.append(GuardTrigger(code="approved_price_inputs_changed", severity="blocker",
            message="Price or seller discount changed; a fresh recommendation and approval are required"))
    now = _now_utc()
    draft_id = f"drf_{uuid4().hex}"
    state: DraftState = "draft" if prepared.guardReport.canApply else "blocked"
    draft = PriceDraftView(
        draftId=draft_id,
        articleId=payload.articleId,
        nmId=payload.nmId,
        state=state,
        approvalState="required",
        approvalRef=None,
        approvalActorId=None,
        createdByActorId=actor_id,
        createdByRole=actor_role,
        reason=payload.reason,
        formulaVersion=FORMULA_VERSION_RECOMMENDATION,
        guardFormulaVersion=FORMULA_VERSION_GUARDS,
        sourceSnapshot=prepared.sourceSnapshot,
        normalized=prepared.normalized,
        guardReport=prepared.guardReport,
        applyJobId=None,
        createdAt=now,
        updatedAt=now,
        organizationId=organization_id,
        bindingKey=scope_binding,
        nightWindowKey=payload.nightWindowKey,
        economics=payload.economics,
        taxRevision=tax_revision,
        pendingApprovalId=pending_approval_id,
        settingsRevision=settings_revision,
    )
    _persist_draft(draft)
    _MEMORY.drafts[draft_id] = draft
    return draft


def _persist_draft(draft: PriceDraftView) -> None:
    if draft.organizationId is not None and draft.bindingKey:
        dispatch_store.save_draft(draft.organizationId, {**draft.model_dump(mode="json"), "bindingKey": draft.bindingKey})


def get_draft_or_404(draft_id: str, organization_id: int | None = None) -> PriceDraftView:
    draft = _MEMORY.drafts.get(draft_id)
    if organization_id is not None and (draft is None or draft.bindingKey):
        value = dispatch_store.load_draft(organization_id, draft_id)
        if value is not None:
            draft = PriceDraftView.model_validate(value)
            _MEMORY.drafts[draft_id] = draft
    if organization_id is not None and draft is not None and draft.organizationId != organization_id:
        draft = None
    if draft is None:
        raise HTTPException(status_code=404, detail="PRICE_DRAFT_NOT_FOUND")
    return draft


def approve_draft(draft_id: str, actor_id: str, request: ApproveDraftRequest) -> PriceDraftView:
    current = get_draft_or_404(draft_id)
    if current.state == "blocked":
        raise HTTPException(status_code=409, detail="PRICE_DRAFT_BLOCKED_BY_GUARDS")
    if current.state not in {"draft", "approved"}:
        raise HTTPException(status_code=409, detail="PRICE_DRAFT_ALREADY_DISPATCHED")
    if current.state == "approved" and current.approvalRef != request.approvalRef:
        raise HTTPException(status_code=409, detail="PRICE_APPROVAL_REF_CONFLICT")
    updated = current.model_copy(
        update={
            "state": "approved",
            "approvalState": "approved",
            "approvalRef": request.approvalRef,
            "approvalActorId": actor_id,
            "updatedAt": _now_utc(),
        }
    )
    _MEMORY.drafts[draft_id] = updated
    _persist_draft(updated)
    return updated


def _map_apply_state(result: PriceApplyResponse) -> tuple[ApplyJobState, bool]:
    if result.applyState == "local_applied":
        return "local_applied", False
    if result.applyState == "accepted":
        return ("accepted", False) if result.priceConfirmed else ("sent", False)
    if result.applyState == "queued":
        return "queued", False
    if result.applyState == "blocked":
        return "blocked", True
    if (not result.wbMutationSent and result.applyState == "requires_attention") or (
            result.statusLabel == "partial_errors" and result.rowErrors):
        return "failed", True
    if result.statusLabel == "processing":
        return "sent", False
    if result.statusLabel in {"all_failed", "canceled"}:
        return "failed", True
    if result.statusLabel in {"partial_errors", "unknown"}:
        return "needs_attention", True
    if result.applyState == "requires_attention":
        return "needs_attention", True
    return "needs_attention", True


def _to_apply_request(draft: PriceDraftView, request: ApplyDraftRequest) -> PriceApplyRequest:
    baseline = draft.sourceSnapshot.priceBaseline
    if baseline is None or baseline.get("nmId") != draft.nmId or baseline.get("articleId") != draft.articleId:
        raise HTTPException(status_code=409, detail="WB_PRICE_BASELINE_REQUIRED")
    if baseline.get("editableSizePrice") is not False:
        raise HTTPException(status_code=409, detail="WB_SIZE_LEVEL_PRICE_REQUIRED")
    base, effective = seller_to_base(draft.normalized.candidateSellerPriceKopecks, baseline["discountPct"])
    if effective != draft.normalized.candidateSellerPriceKopecks:
        raise HTTPException(status_code=409, detail="PRICE_APPROVAL_ROUNDING_CHANGED")
    row = PriceApplyRowRequest(
        nmId=draft.nmId,
        priceKopecks=base,
        discountPct=baseline["discountPct"],
        baseline=baseline,
    )
    return PriceApplyRequest(
        scenario=request.scenario,
        dryRun=False,
        pollMaxAttempts=request.pollMaxAttempts,
        pollDelaySeconds=request.pollDelaySeconds,
        skipStatusPoll=request.skipStatusPoll,
        rows=[row],
    )


def _create_job(draft: PriceDraftView, result: PriceApplyResponse, state: ApplyJobState, stop_required: bool) -> ApplyJobView:
    now = _now_utc()
    retry_count = 1 if state in {"failed", "needs_attention"} else 0
    next_retry_at = now + timedelta(minutes=5) if retry_count > 0 else None
    row_errors = [
        ApplyJobRowError(
            nmId=row.nmId,
            sizeId=row.sizeId,
            wbStatus=row.wbStatus,
            errorText=row.errorText,
        )
        for row in result.rowErrors
    ]
    return ApplyJobView(
        jobId=f"job_{draft.draftId.removeprefix('drf_')}",
        draftId=draft.draftId,
        articleId=draft.articleId,
        nmId=draft.nmId,
        state=state,
        sourceStatus=result.sourceStatus,
        blockerIds=result.blockerIds,
        wbUploadId=result.wbUploadId,
        wbStatus=result.wbStatus,
        rowErrors=row_errors,
        retryCount=retry_count,
        maxRetryCount=3,
        nextRetryAt=next_retry_at,
        stopRequired=stop_required,
        notes=result.notes,
        lastApplyResult=result,
        formulaVersion=draft.formulaVersion,
        guardFormulaVersion=draft.guardFormulaVersion,
        sourceSnapshot=draft.sourceSnapshot,
        createdAt=now,
        updatedAt=now,
        dispatchKey=draft.dispatchKey,
    )


def _store_apply_result(draft: PriceDraftView, result: PriceApplyResponse) -> ApplyJobView:
    if draft.organizationId is not None and draft.bindingKey:
        saved = dispatch_store.load_draft(draft.organizationId, draft.draftId)
        if saved and saved.get("state") in {"accepted", "local_applied"} and saved.get("applyJobId"):
            return get_apply_job_or_404(saved["applyJobId"], draft.organizationId)
    state, stop_required = _map_apply_state(result)
    job = _create_job(draft=draft, result=result, state=state, stop_required=stop_required)
    _MEMORY.jobs[job.jobId] = job

    new_draft_state: DraftState = {
        "accepted": "accepted",
        "local_applied": "local_applied",
        "failed": "failed",
        "blocked": "blocked",
        "needs_attention": "needs_attention",
        "sent": "sent",
        "queued": "queued",
    }.get(state, "needs_attention")
    updated_draft = draft.model_copy(
        update={"state": new_draft_state, "applyJobId": job.jobId, "updatedAt": _now_utc()}
    )
    _MEMORY.drafts[draft.draftId] = updated_draft
    _persist_draft(updated_draft)

    if state in {"accepted", "local_applied"}:
        history = _MEMORY.price_history.setdefault(draft.articleId, [])
        history.append(
            _PriceHistoryPoint(
                sellerPriceKopecks=draft.normalized.candidateSellerPriceKopecks,
                appliedAt=_now_utc(),
                oldSellerPriceKopecks=draft.normalized.currentSellerPriceKopecks,
            )
        )
        _MEMORY.price_history[draft.articleId] = history[-50:]

    return job


def apply_approved_drafts(
    draft_ids: list[str],
    client: WbApiClient,
    real_apply_enabled: bool,
    local_apply_enabled: bool,
    request: ApplyDraftRequest,
) -> list[ApplyJobView]:
    with _APPLY_LOCK:
        organizations = {get_draft_or_404(draft_id).organizationId for draft_id in draft_ids}
        if len(organizations) > 1:
            raise HTTPException(status_code=409, detail="PRICE_DRAFT_ORGANIZATION_SCOPE_MISMATCH")
        with organization_algorithm_context(next(iter(organizations), None)):
            return _apply_approved_drafts_locked(draft_ids, client, real_apply_enabled, local_apply_enabled, request)


def _blocked_result(code: str) -> PriceApplyResponse:
    return PriceApplyResponse(sourceStatus="blocked", applyState="blocked", blockerIds=[code],
        statusLabel="unknown", pollAttempts=0, observedAt=_now_utc(), notes=[code])


def _product_apply_result(result: PriceApplyResponse, nm_id: int) -> PriceApplyResponse:
    """Split a batch receipt without attributing another product's success."""
    goods = [good for good in result.confirmedGoods if good.get("nmID") == nm_id]
    errors = [row for row in result.rowErrors if row.nmId in {None, nm_id}]
    if goods and not errors:
        return result.model_copy(update={"applyState": "accepted", "sourceStatus": "fresh", "priceConfirmed": True,
            "confirmedGoods": goods, "rowErrors": [], "statusLabel": "success"})
    return result.model_copy(update={"priceConfirmed": False, "confirmedGoods": [], "rowErrors": errors})


def _receipt_state(result: PriceApplyResponse) -> str:
    if result.priceConfirmed:
        return "confirmed"
    if not result.wbMutationSent or result.statusLabel in {"canceled", "all_failed"} or (
            result.statusLabel == "partial_errors" and result.rowErrors):
        return "rejected"
    return "processing"


def _dispatch_guard(draft: PriceDraftView) -> str | None:
    if not draft.guardReport.canApply:
        return "PRICE_DRAFT_BLOCKED_BY_GUARDS"
    if getattr(draft, "nightWindowKey", None):
        from app.repricer_execution import _night_previous_window_key, _night_median_collecting_now
        if draft.nightWindowKey != _night_previous_window_key() or _night_median_collecting_now():
            return "night_window_expired_or_collecting"
    if any(metric.sourceStatus in {"stale", "blocked"} or metric.staleAfter <= _now_utc()
           for metric in draft.sourceSnapshot.metrics):
        return "PRICE_DRAFT_SOURCE_EXPIRED"
    value = draft.normalized
    if value.minPriceKopecks is not None and value.pMaxKopecks is not None and value.minPriceKopecks > value.pMaxKopecks:
        return "price_bounds_conflict"
    if value.minPriceKopecks is not None and value.candidateSellerPriceKopecks < value.minPriceKopecks:
        return "candidate_below_min_price"
    if value.pMaxKopecks is not None and value.candidateSellerPriceKopecks > value.pMaxKopecks:
        return "candidate_above_max_price"
    reference = value.currentSellerPriceKopecks
    limits = get_repricer_guard_limits()
    draft_step = getattr(value, "effectiveStepPct", None)
    step_pct = min(limits.per_update_step_pct, draft_step) if draft_step is not None else limits.per_update_step_pct
    if reference and abs(value.candidateSellerPriceKopecks - reference) * 100 > Decimal(reference) * Decimal(str(step_pct)):
        return "per_update_step_exceeded"
    if draft.organizationId is not None and draft.bindingKey:
        _rate, current_tax_revision = dispatch_store.planning_tax(draft.organizationId, _now_utc())
        if draft.taxRevision != current_tax_revision:
            return "tax_settings_changed_reapproval_required"
        if draft.settingsRevision != dispatch_store.pricing_settings_revision(draft.organizationId, draft.articleId):
            return "pricing_settings_changed_reapproval_required"
        reference = dispatch_store.daily_reference(draft.organizationId, draft.bindingKey, draft.nmId, reference, _now_utc())
    else:
        reference = _daily_reference_price(draft.articleId, reference)
    if reference and abs(value.candidateSellerPriceKopecks - reference) * 100 > Decimal(reference) * Decimal(str(limits.per_day_step_pct)):
        return "per_day_step_exceeded"
    return None


def _apply_approved_drafts_locked(draft_ids, client, real_apply_enabled, local_apply_enabled, request):
    drafts = [get_draft_or_404(draft_id) for draft_id in draft_ids]
    if not drafts:
        return []
    if any(draft.approvalState != "approved" or draft.approvalRef is None for draft in drafts):
        raise HTTPException(status_code=409, detail="PRICE_DRAFT_NOT_APPROVED")

    jobs_by_draft: dict[str, ApplyJobView] = {}
    ready: list[PriceDraftView] = []
    for draft in drafts:
        if draft.dispatchKey and draft.organizationId is not None:
            jobs_by_draft[draft.draftId] = get_apply_job_or_404(f"job_{draft.draftId.removeprefix('drf_')}", draft.organizationId)
            continue
        if draft.applyJobId is not None and draft.applyJobId in _MEMORY.jobs:
            jobs_by_draft[draft.draftId] = _MEMORY.jobs[draft.applyJobId]
            continue
        if draft.state != "approved":
            raise HTTPException(status_code=409, detail="PRICE_DRAFT_ALREADY_DISPATCHED")
        error = _dispatch_guard(draft)
        if error is None:
            ready.append(draft)
            continue
        result = _blocked_result(error)
        jobs_by_draft[draft.draftId] = _store_apply_result(draft, result)

    if ready:
        if not real_apply_enabled:
            for draft in ready:
                result = run_price_apply(client, PriceApplyRequest(rows=[PriceApplyRowRequest(nmId=draft.nmId,
                    priceKopecks=draft.normalized.candidateSellerPriceKopecks, discountPct=0)]), False, local_apply_enabled)
                jobs_by_draft[draft.draftId] = _store_apply_result(draft, result)
            return [jobs_by_draft[draft.draftId] for draft in drafts]
        scope_binding = binding_key(client, ready[0].organizationId)
        if scope_binding and (ready[0].organizationId is None or any(draft.bindingKey != scope_binding
                or draft.organizationId != ready[0].organizationId for draft in ready)):
            raise HTTPException(status_code=409, detail="PRICE_DISPATCH_SCOPE_CHANGED")
        permission_error = price_write_permission(client)
        if permission_error:
            for draft in ready:
                jobs_by_draft[draft.draftId] = _store_apply_result(draft, _blocked_result(permission_error))
            return [jobs_by_draft[draft.draftId] for draft in drafts]
        apply_request = _to_apply_request(draft=ready[0], request=request).model_copy(
            update={"rows": [_to_apply_request(draft=draft, request=request).rows[0] for draft in ready]}
        )
        scoped_client = ScopedPriceClient(client, {draft.nmId for draft in ready},
            {draft.nmId: draft.sourceSnapshot.priceBaseline for draft in ready})
        request_client = scoped_client
        if isinstance(client, RateLimitedWbApiClient):
            # Preserve the existing limiter, sleep function and lock. The final
            # fence runs after its wait, immediately before the raw transport.
            request_client = copy(client)
            scoped_client = ScopedPriceClient(client.inner, {draft.nmId for draft in ready},
                {draft.nmId: draft.sourceSnapshot.priceBaseline for draft in ready})
            request_client.inner = scoped_client
        observed = request_client.request(WbApiRequest(method="POST", path="/api/v2/list/goods/filter", jsonBody={"nmList": [draft.nmId for draft in ready]}))
        data = observed.data.get("data", observed.data) if observed.ok and isinstance(observed.data, dict) else {}
        goods = data.get("listGoods") or []
        actual = {good.get("nmID"): price_baseline(good) for good in goods if isinstance(good, dict)}
        if set(actual) != {draft.nmId for draft in ready} or len(goods) != len(ready) or any(actual.get(draft.nmId) is None or
                baseline_identity(actual[draft.nmId]) != baseline_identity(draft.sourceSnapshot.priceBaseline) for draft in ready):
            for draft in ready:
                jobs_by_draft[draft.draftId] = _store_apply_result(draft, _blocked_result("WB_PRICE_BASELINE_CHANGED_OR_UNAVAILABLE"))
            return [jobs_by_draft[draft.draftId] for draft in drafts]
        # Preflight can be rate-limited or slow. It does not refresh the SPP,
        # tax revision or daily reference captured by the approved intent.
        errors = [_dispatch_guard(draft) for draft in ready]
        if any(errors):
            for draft, error in zip(ready, errors, strict=True):
                jobs_by_draft[draft.draftId] = _store_apply_result(draft, _blocked_result(error or "PRICE_BATCH_GUARD_FAILED"))
            return [jobs_by_draft[draft.draftId] for draft in drafts]
        keys = None
        dispatch_errors = []
        org = ready[0].organizationId
        if scope_binding is not None:
            keys = dispatch_store.claim(org, scope_binding, [{**draft.model_dump(mode="json"), "bindingKey": draft.bindingKey} for draft in ready], now=_now_utc())
            if keys is None:
                raise HTTPException(status_code=409, detail="PRICE_INTENT_ALREADY_CLAIMED_OR_STORAGE_UNAVAILABLE")
            def begin_submission():
                try:
                    if binding_key(client, org) != scope_binding:
                        raise dispatch_store.DispatchPersistenceError("WB_ACCOUNT_BINDING_CHANGED")
                    if any(draft.nightWindowKey for draft in ready):
                        from app.repricer_execution import _night_previous_window_key, _night_median_collecting_now
                        if _night_median_collecting_now() or any(draft.nightWindowKey and
                            draft.nightWindowKey != _night_previous_window_key() for draft in ready):
                            raise dispatch_store.DispatchPersistenceError("night_window_expired_or_collecting")
                    dispatch_store.transition(org, keys, "dispatched", now=_now_utc())
                except dispatch_store.DispatchPersistenceError as error:
                    dispatch_errors.append(str(error))
                    raise
            scoped_client.before_upload = begin_submission
            def remember_upload(upload_id):
                pending = PriceApplyResponse(sourceStatus="partial", applyState="processing", blockerIds=[],
                    wbUploadId=upload_id, statusLabel="processing", pollAttempts=0, observedAt=_now_utc(), wbMutationSent=True,
                    notes=["WB accepted the request; current price is not confirmed yet"])
                dispatch_store.transition(org, keys, "processing", result=pending.model_dump(mode="json"))
            scoped_client.on_upload = remember_upload
        for index, draft in enumerate(ready):
            ready[index] = draft.model_copy(update={"state": "sent", "dispatchKey": keys[index] if keys else None})
            _MEMORY.drafts[draft.draftId] = ready[index]
            _persist_draft(ready[index])
        try:
            result = run_price_apply(request_client, apply_request, True, False)
        except Exception:
            result = PriceApplyResponse(sourceStatus="partial", applyState="requires_attention", blockerIds=["wb_submission_outcome_unknown"],
                statusLabel="unknown", pollAttempts=0, observedAt=_now_utc(), wbMutationSent=True,
                wbUploadId=next(iter(scoped_client.upload_ids)) if len(scoped_client.upload_ids) == 1 else None,
                notes=["Do not resend; reconcile the existing intent first"])
        if dispatch_errors and not scoped_client.mutation_attempted:
            result = _blocked_result(dispatch_errors[0])
        for draft in ready:
            draft_result = _product_apply_result(result, draft.nmId)
            if draft.dispatchKey:
                dispatch_store.transition(org, [draft.dispatchKey], _receipt_state(draft_result), result=draft_result.model_dump(mode="json"))
            jobs_by_draft[draft.draftId] = _store_apply_result(draft, draft_result)

    return [jobs_by_draft[draft.draftId] for draft in drafts]


def apply_approved_draft(
    draft_id: str,
    client: WbApiClient,
    real_apply_enabled: bool,
    local_apply_enabled: bool,
    request: ApplyDraftRequest,
) -> ApplyJobView:
    return apply_approved_drafts(
        draft_ids=[draft_id],
        client=client,
        real_apply_enabled=real_apply_enabled,
        local_apply_enabled=local_apply_enabled,
        request=request,
    )[0]


def get_apply_job_or_404(job_id: str, organization_id: int | None = None) -> ApplyJobView:
    job = _MEMORY.jobs.get(job_id)
    if organization_id is not None:
        draft = get_draft_or_404(f"drf_{job_id.removeprefix('job_')}", organization_id)
        receipt = dispatch_store.load_receipt(organization_id, draft.dispatchKey) if draft.dispatchKey else None
        if receipt:
            value = receipt.get("result")
            result = PriceApplyResponse.model_validate(value) if value else PriceApplyResponse(sourceStatus="partial", applyState="requires_attention",
                blockerIds=["wb_submission_outcome_unknown"], statusLabel="unknown", pollAttempts=0, observedAt=_now_utc(), wbMutationSent=True)
            state, stop = _map_apply_state(result)
            job = _create_job(draft, result, state, stop)
            _MEMORY.jobs[job_id] = job
    if job is None:
        raise HTTPException(status_code=404, detail="PRICE_APPLY_JOB_NOT_FOUND")
    return job


def retry_apply_job(
    job_id: str,
    client: WbApiClient,
    real_apply_enabled: bool,
    local_apply_enabled: bool,
    request: RetryApplyJobRequest,
) -> ApplyJobView:
    with _APPLY_LOCK:
        return _reconcile_apply_job(job_id, client, real_apply_enabled, local_apply_enabled, request)


def _reconcile_apply_job(job_id, client, real_apply_enabled, local_apply_enabled, request):
    job = get_apply_job_or_404(job_id)
    if job.state in {"accepted", "local_applied"}:
        return job
    if job.state not in {"failed", "needs_attention", "sent", "queued"}:
        raise HTTPException(status_code=409, detail="PRICE_APPLY_JOB_RETRY_NOT_ALLOWED")
    draft = get_draft_or_404(job.draftId)
    if draft.dispatchKey:
        receipt = dispatch_store.load_receipt(draft.organizationId, draft.dispatchKey)
        if receipt and receipt.get("state") in {"confirmed", "rejected"}:
            return get_apply_job_or_404(job_id, draft.organizationId)
    if draft.bindingKey != binding_key(client, draft.organizationId):
        raise HTTPException(status_code=409, detail="PRICE_DISPATCH_SCOPE_CHANGED")
    if job.wbUploadId is None:
        raise HTTPException(status_code=409, detail="PRICE_OUTCOME_UNCERTAIN_OR_FRESH_APPROVAL_REQUIRED")
    scoped_client = ScopedPriceClient(client, {draft.nmId}, {draft.nmId: draft.sourceSnapshot.priceBaseline})
    scoped_client.upload_ids.add(job.wbUploadId)
    payload = _to_apply_request(draft, ApplyDraftRequest(scenario=request.scenario,
        pollMaxAttempts=request.pollMaxAttempts, pollDelaySeconds=request.pollDelaySeconds))
    result = poll_price_apply(scoped_client, payload, job.wbUploadId)
    if draft.dispatchKey:
        dispatch_store.transition(draft.organizationId, [draft.dispatchKey], _receipt_state(result), result=result.model_dump(mode="json"))
    return _store_apply_result(draft, result)
