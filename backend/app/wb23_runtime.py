from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from math import ceil
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.contracts.envelopes import UtcDateTime
from app.wb_api.client import WbApiClient, WbApiRequest
from app.wb_api.price_units import wb_goods_price_to_kopecks
from app.repricer_price_safety import price_baseline, utc_timestamp


SourceStatus = Literal["fresh", "partial", "stale", "blocked"]
Confidence = Literal["high", "medium", "low", "blocked"]


class FreshnessPolicy(BaseModel):
    metricId: str = Field(min_length=1)
    ttlMinutes: int = Field(ge=1)
    staleAction: Literal["allow_partial", "block_auto_apply", "block_report_final"]
    confidenceWhenFresh: Confidence
    confidenceWhenStale: Literal["low", "blocked"]


class MetricFreshness(BaseModel):
    source: str = Field(min_length=1)
    fetchedAt: UtcDateTime
    staleAfter: UtcDateTime
    isStale: bool
    confidence: Confidence


class PriceMetricValue(BaseModel):
    metricId: str = Field(min_length=1)
    label: str = Field(min_length=1)
    value: int | float | None = None
    unit: Literal["kopecks", "percent", "status", "boolean"]
    sourceStatus: SourceStatus
    blockerIds: list[str] = Field(default_factory=list)
    fieldSource: str = Field(min_length=1)
    freshness: MetricFreshness
    warnings: list[str] = Field(default_factory=list)


class PriceMetricsSnapshot(BaseModel):
    articleId: str = Field(min_length=1)
    nmId: int | None = None
    sourceStatus: SourceStatus
    blockerIds: list[str]
    metrics: list[PriceMetricValue]
    priceBaseline: dict[str, Any] | None = None


class MarginPreviewRequest(BaseModel):
    scenario: str = "complete"
    nmId: int | None = Field(default=None, gt=0)
    costPriceKopecks: int = Field(gt=0)
    wbCommissionPct: float = Field(ge=0, le=100)
    logisticsKopecks: int = Field(default=0, ge=0)
    acquiringKopecks: int = Field(default=0, ge=0)
    taxKopecks: int = Field(default=0, ge=0)
    adCostKopecks: int = Field(default=0, ge=0)
    storageKopecks: int = Field(default=0, ge=0)


class MarginPreviewResponse(BaseModel):
    articleId: str = Field(min_length=1)
    sellPriceKopecks: int | None = Field(default=None, gt=0)
    wbCommissionKopecks: int | None = Field(default=None, ge=0)
    marginRubKopecks: int | None = None
    marginPct: float | None = None
    sourceStatus: SourceStatus
    blockerIds: list[str]
    freshness: MetricFreshness
    formula: str = Field(min_length=1)
    warnings: list[str] = Field(default_factory=list)


class PminPmaxValidationRequest(BaseModel):
    scenario: str = "complete"
    nmId: int | None = Field(default=None, gt=0)
    candidateBuyerPriceKopecks: int = Field(gt=0)
    costPriceKopecks: int = Field(gt=0)
    wbCommissionPct: float = Field(ge=0, le=100)
    targetMarginPct: float = Field(ge=0, lt=100)
    logisticsKopecks: int = Field(default=0, ge=0)
    acquiringKopecks: int = Field(default=0, ge=0)
    taxKopecks: int = Field(default=0, ge=0)
    fixedCostsKopecks: int = Field(default=0, ge=0)
    pMaxKopecks: int | None = Field(default=None, gt=0)


class PminPmaxValidationResponse(BaseModel):
    articleId: str = Field(min_length=1)
    pMinKopecks: int | None = Field(default=None, gt=0)
    pMaxKopecks: int | None = Field(default=None, gt=0)
    candidateBuyerPriceKopecks: int = Field(gt=0)
    isValid: bool
    reasons: list[str]
    sourceStatus: SourceStatus
    blockerIds: list[str]
    freshness: MetricFreshness


class ApplyStatusRow(BaseModel):
    nmId: int | None = None
    sizeId: int | None = None
    wbStatus: int | None = None
    statusLabel: Literal["processing", "success", "canceled", "partial_errors", "all_failed", "unknown"]
    errorText: str | None = None


class ApplyStatusResponse(BaseModel):
    uploadId: int = Field(gt=0)
    sourceStatus: SourceStatus
    blockerIds: list[str]
    wbStatus: int | None = None
    statusLabel: Literal["processing", "success", "canceled", "partial_errors", "all_failed", "unknown"]
    rowErrors: list[ApplyStatusRow]
    freshness: MetricFreshness
    notes: list[str] = Field(default_factory=list)


def wb23_freshness_policies() -> list[FreshnessPolicy]:
    return [
        FreshnessPolicy(
            metricId="current_price_before_spp",
            ttlMinutes=60,
            staleAction="block_auto_apply",
            confidenceWhenFresh="high",
            confidenceWhenStale="blocked",
        ),
        FreshnessPolicy(
            metricId="current_price_after_spp",
            ttlMinutes=60,
            staleAction="block_auto_apply",
            confidenceWhenFresh="high",
            confidenceWhenStale="blocked",
        ),
        FreshnessPolicy(
            metricId="spp_pct",
            ttlMinutes=60,
            staleAction="block_auto_apply",
            confidenceWhenFresh="high",
            confidenceWhenStale="blocked",
        ),
        FreshnessPolicy(
            metricId="margin_rub_pct",
            ttlMinutes=60,
            staleAction="block_auto_apply",
            confidenceWhenFresh="medium",
            confidenceWhenStale="blocked",
        ),
        FreshnessPolicy(
            metricId="pmin_pmax_validation",
            ttlMinutes=15,
            staleAction="block_auto_apply",
            confidenceWhenFresh="high",
            confidenceWhenStale="blocked",
        ),
        FreshnessPolicy(
            metricId="price_apply_status",
            ttlMinutes=15,
            staleAction="block_auto_apply",
            confidenceWhenFresh="high",
            confidenceWhenStale="blocked",
        ),
    ]


def _policy(metric_id: str) -> FreshnessPolicy:
    mapping = {item.metricId: item for item in wb23_freshness_policies()}
    return mapping[metric_id]


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _freshness(metric_id: str, fetched_at: datetime) -> MetricFreshness:
    policy = _policy(metric_id)
    fetched_utc = _to_utc(fetched_at)
    stale_after = fetched_utc + timedelta(minutes=policy.ttlMinutes)
    now = datetime.now(timezone.utc)
    is_stale = now > stale_after
    confidence: Confidence = policy.confidenceWhenFresh if not is_stale else policy.confidenceWhenStale
    return MetricFreshness(
        source=metric_id,
        fetchedAt=fetched_utc,
        staleAfter=stale_after,
        isStale=is_stale,
        confidence=confidence,
    )


def _coerce_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _optional_price_kopecks(value: Any) -> int | None:
    number = _coerce_number(value)
    if number is None:
        return None
    kopecks = wb_goods_price_to_kopecks(number)
    return kopecks if kopecks is not None and kopecks > 0 else None


def _first_price_kopecks(*values: Any) -> int | None:
    for value in values:
        kopecks = _optional_price_kopecks(value)
        if kopecks is not None:
            return kopecks
    return None


def _spp_pct_from_buyer_price(before_spp: int | None, buyer_no_wallet: int | None) -> float | None:
    if before_spp is None or before_spp <= 0 or buyer_no_wallet is None:
        return None
    pct = (Decimal(before_spp) - Decimal(buyer_no_wallet)) / Decimal(before_spp) * Decimal("100")
    return float(pct.quantize(Decimal("1.0000"), rounding=ROUND_HALF_UP))


def _spp_pct_or_none(value: Any) -> float | None:
    number = _coerce_number(value)
    if number is None or number < 0 or number > 100:
        return None
    return float(Decimal(str(number)).quantize(Decimal("1.0000"), rounding=ROUND_HALF_UP))


def _buyer_price_from_spp_pct(before_spp: int | None, spp_pct: float | None) -> int | None:
    if before_spp is None or before_spp <= 0 or spp_pct is None:
        return None
    multiplier = Decimal("1") - Decimal(str(spp_pct)) / Decimal("100")
    return int((Decimal(before_spp) * multiplier).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _orders_spp_fallback(
    client: WbApiClient | None,
    nm_id: int | None,
    fetched_at: datetime,
) -> tuple[float | None, int | None, str | None]:
    if client is None or nm_id is None or nm_id <= 0:
        return None, None, None
    envelope = client.request(
        WbApiRequest(
            method="GET",
            path="/api/v1/supplier/orders",
            query={"dateFrom": f"{(fetched_at - timedelta(days=90)).date().isoformat()}T00:00:00"},
        )
    )
    if not envelope.ok:
        return None, None, None
    rows = envelope.data if isinstance(envelope.data, list) else []
    selected: dict[str, Any] | None = None
    selected_key = ""
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_nm_id = int(row.get("nmId") or row.get("nmID") or 0)
        if row_nm_id != nm_id or _spp_pct_or_none(row.get("spp")) is None:
            continue
        observed_at = str(row.get("lastChangeDate") or row.get("date") or "")
        if selected is None or observed_at >= selected_key:
            selected = row
            selected_key = observed_at
    if selected is None:
        return None, None, None
    spp_pct = _spp_pct_or_none(selected.get("spp"))
    buyer_price = _first_price_kopecks(selected.get("finishedPrice"))
    return spp_pct, buyer_price, "supplier.orders.spp"


def _extract_price_item(payload: dict[str, Any]) -> dict[str, Any] | None:
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    list_goods = data.get("listGoods")
    if not isinstance(list_goods, list) or not list_goods:
        return None
    item = list_goods[0]
    return item if isinstance(item, dict) else None


def _extract_first_size(item: dict[str, Any]) -> dict[str, Any] | None:
    sizes = item.get("sizes")
    if not isinstance(sizes, list) or not sizes:
        return None
    first = sizes[0]
    return first if isinstance(first, dict) else None


def _price_metrics_from_payload(
    article_id: str,
    payload: dict[str, Any],
    fetched_at: datetime,
    *,
    requested_nm_id: int | None = None,
    orders_client: WbApiClient | None = None,
) -> PriceMetricsSnapshot:
    item = _extract_price_item(payload) or {}
    if requested_nm_id is not None and item.get("nmID") != requested_nm_id:
        item = {}
    size = _extract_first_size(item) or {}
    nm_id = requested_nm_id or (item.get("nmID") if isinstance(item.get("nmID"), int) else None)

    before_spp_raw = _coerce_number(size.get("discountedPrice"))
    before_spp = wb_goods_price_to_kopecks(before_spp_raw) if before_spp_raw is not None else None
    before_spp = before_spp if before_spp is not None and before_spp > 0 else None

    after_spp = _first_price_kopecks(
        size.get("buyerPriceNoWallet"),
        size.get("buyerPriceNoWalletKopecks"),
        size.get("buyerPriceKopecks"),
        size.get("buyerPrice"),
        size.get("clientPrice"),
        item.get("buyerPriceNoWallet"),
        item.get("buyerPriceNoWalletKopecks"),
        item.get("buyerPriceKopecks"),
        item.get("buyerPrice"),
        item.get("clientPrice"),
    )
    spp_pct = _spp_pct_from_buyer_price(before_spp, after_spp)
    spp_source = "fallback: (seller discounted price - live buyer price without wallet) / seller discounted price * 100"
    after_source = "live_price.buyerPriceNoWallet"
    after_warning = "Cannot derive buyer price: missing live buyer price without WB wallet"
    spp_warning = "SPP percent cannot be resolved without live buyer price"
    if spp_pct is None:
        orders_spp_pct, orders_buyer_price, orders_source = _orders_spp_fallback(orders_client, nm_id, fetched_at)
        if orders_spp_pct is not None:
            spp_pct = orders_spp_pct
            spp_source = orders_source or "supplier.orders.spp"
            after_spp = _buyer_price_from_spp_pct(before_spp, spp_pct) or orders_buyer_price
            after_source = f"{spp_source} applied to sizes[].discountedPrice"
            after_warning = ""
            spp_warning = ""

    price_before_freshness = _freshness("current_price_before_spp", fetched_at)
    price_after_freshness = _freshness("current_price_after_spp", fetched_at)
    spp_freshness = _freshness("spp_pct", fetched_at)

    metric_before = PriceMetricValue(
        metricId="current_price_before_spp",
        label="Current price before SPP",
        value=before_spp,
        unit="kopecks",
        sourceStatus="fresh" if before_spp is not None else "blocked",
        blockerIds=[] if before_spp is not None else ["WB-23"],
        fieldSource="sizes[].discountedPrice",
        freshness=price_before_freshness,
        warnings=[] if before_spp is not None else ["discountedPrice is missing in WB response"],
    )

    metric_after = PriceMetricValue(
        metricId="current_price_after_spp",
        label="Current price after SPP",
        value=after_spp,
        unit="kopecks",
        sourceStatus="fresh" if after_spp is not None else "blocked",
        blockerIds=[] if after_spp is not None else ["WB-23"],
        fieldSource=after_source,
        freshness=price_after_freshness,
        warnings=[] if after_spp is not None else [after_warning],
    )

    metric_spp = PriceMetricValue(
        metricId="spp_pct",
        label="SPP percent by SKU",
        value=round(spp_pct, 4) if spp_pct is not None else None,
        unit="percent",
        sourceStatus="fresh" if spp_pct is not None else "blocked",
        blockerIds=[] if spp_pct is not None else ["WB-23"],
        fieldSource=spp_source,
        freshness=spp_freshness,
        warnings=[] if spp_pct is not None else [spp_warning],
    )

    statuses = [metric_before.sourceStatus, metric_after.sourceStatus, metric_spp.sourceStatus]
    overall_status: SourceStatus
    if "blocked" in statuses:
        overall_status = "blocked"
    elif "stale" in statuses:
        overall_status = "stale"
    elif "partial" in statuses:
        overall_status = "partial"
    else:
        overall_status = "fresh"

    blocker_ids = sorted({blocker for metric in [metric_before, metric_after, metric_spp] for blocker in metric.blockerIds})

    return PriceMetricsSnapshot(
        articleId=article_id,
        nmId=nm_id,
        sourceStatus=overall_status,
        blockerIds=blocker_ids,
        metrics=[metric_before, metric_after, metric_spp],
        priceBaseline=price_baseline(item, fetched_at.isoformat()),
    )


def build_price_metrics_snapshot_from_row(article_id: str, row: dict[str, Any]) -> PriceMetricsSnapshot:
    meta = row.get("meta") or {}
    analytics = row.get("analytics") or {}
    nm_id = meta.get("nmId") if isinstance(meta.get("nmId"), int) else None
    before_spp = int(meta.get("currentPriceKopecks") or analytics.get("sellerDiscountedPriceKopecks") or 0) or None
    after_spp = int(analytics.get("buyerPriceNoWalletKopecks") or 0) or None
    spp_pct = analytics.get("sppPct")
    if isinstance(spp_pct, (int, float)):
        spp_pct = float(spp_pct)
    else:
        spp_pct = None
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    observed = utc_timestamp(meta.get("priceObservedAt") or (meta.get("wbPriceBaseline") or {}).get("observedAt"))
    buyer_observed = utc_timestamp(analytics.get("buyerPriceObservedAt"))
    buyer_expiry = utc_timestamp(analytics.get("buyerPriceExpiresAt"))
    now = datetime.now(timezone.utc)

    def row_freshness(metric_id: str, stamp: datetime | None, expiry: datetime | None = None) -> MetricFreshness:
        result = _freshness(metric_id, stamp or epoch)
        if expiry is not None and expiry < result.staleAfter:
            result.staleAfter = expiry
        result.isStale = stamp is None or stamp > now + timedelta(seconds=30) or now >= result.staleAfter
        if result.isStale:
            result.confidence = "blocked"
        return result

    metric_before = PriceMetricValue(
        metricId="current_price_before_spp",
        label="Current price before SPP",
        value=before_spp,
        unit="kopecks",
        sourceStatus="fresh" if before_spp is not None else "blocked",
        blockerIds=[] if before_spp is not None else ["WB-23"],
        fieldSource="repricer_cache.currentPriceKopecks",
        freshness=row_freshness("current_price_before_spp", observed),
        warnings=[],
    )
    metric_after = PriceMetricValue(
        metricId="current_price_after_spp",
        label="Current price after SPP",
        value=after_spp,
        unit="kopecks",
        sourceStatus="fresh" if after_spp is not None else "blocked",
        blockerIds=[] if after_spp is not None else ["WB-23"],
        fieldSource="repricer_cache.buyerPriceNoWalletKopecks",
        freshness=row_freshness("current_price_after_spp", buyer_observed, buyer_expiry),
        warnings=[],
    )
    metric_spp = PriceMetricValue(
        metricId="spp_pct",
        label="SPP percent by SKU",
        value=round(spp_pct, 4) if spp_pct is not None else None,
        unit="percent",
        sourceStatus="fresh" if spp_pct is not None else "blocked",
        blockerIds=[] if spp_pct is not None else ["WB-23"],
        fieldSource="repricer_cache.sppPct",
        freshness=row_freshness("spp_pct", buyer_observed, buyer_expiry),
        warnings=[],
    )
    statuses = [metric_before.sourceStatus, metric_after.sourceStatus, metric_spp.sourceStatus]
    for metric in (metric_before, metric_after, metric_spp):
        if metric.value is not None and metric.freshness.isStale:
            metric.sourceStatus = "stale"
            metric.blockerIds = ["WB-23"]
            metric.warnings = ["Source observation is missing, expired or in the future"]
    statuses = [metric_before.sourceStatus, metric_after.sourceStatus, metric_spp.sourceStatus]
    if "blocked" in statuses:
        overall_status: SourceStatus = "blocked"
    elif "stale" in statuses:
        overall_status = "stale"
    elif "partial" in statuses:
        overall_status = "partial"
    else:
        overall_status = "fresh"
    blocker_ids = sorted({blocker for metric in [metric_before, metric_after, metric_spp] for blocker in metric.blockerIds})
    return PriceMetricsSnapshot(
        articleId=article_id,
        nmId=nm_id,
        sourceStatus=overall_status,
        blockerIds=blocker_ids,
        metrics=[metric_before, metric_after, metric_spp],
        priceBaseline=meta.get("wbPriceBaseline"),
    )


def build_price_metrics_snapshot(
    client: WbApiClient,
    article_id: str,
    *,
    nm_id: int | None = None,
    orders_client: WbApiClient | None = None,
) -> PriceMetricsSnapshot:
    fetched_at = datetime.now(timezone.utc)
    if nm_id is None or nm_id <= 0:
        return build_price_metrics_snapshot_from_row(article_id, {"meta": {"articleId": article_id}, "analytics": {}})
    envelope = client.request(
        WbApiRequest(
            method="POST",
            path="/api/v2/list/goods/filter",
            query={"limit": 1},
            jsonBody={"nmList": [int(nm_id)]},
        )
    )
    if not envelope.ok or not isinstance(envelope.data, dict):
        freshness = _freshness("current_price_before_spp", fetched_at)
        blocked_metric = PriceMetricValue(
            metricId="current_price_before_spp",
            label="Current price before SPP",
            value=None,
            unit="kopecks",
            sourceStatus="blocked",
            blockerIds=["WB-23"],
            fieldSource="sizes[].discountedPrice",
            freshness=freshness,
            warnings=["WB API response is unavailable for price snapshot"],
        )
        return PriceMetricsSnapshot(
            articleId=article_id,
            nmId=None,
            sourceStatus="blocked",
            blockerIds=["WB-23"],
            metrics=[blocked_metric],
        )
    return _price_metrics_from_payload(
        article_id=article_id,
        payload=envelope.data,
        fetched_at=fetched_at,
        requested_nm_id=nm_id,
        orders_client=orders_client,
    )


def build_margin_preview(
    client: WbApiClient,
    article_id: str,
    payload: MarginPreviewRequest,
    *,
    price_snapshot: PriceMetricsSnapshot | None = None,
) -> MarginPreviewResponse:
    snapshot = price_snapshot or build_price_metrics_snapshot(
        client=client,
        article_id=article_id,
        nm_id=payload.nmId,
    )
    after_metric = next((metric for metric in snapshot.metrics if metric.metricId == "current_price_after_spp"), None)
    freshness = _freshness("margin_rub_pct", datetime.now(timezone.utc))
    if after_metric is None or not isinstance(after_metric.value, (int, float)):
        return MarginPreviewResponse(
            articleId=article_id,
            sellPriceKopecks=None,
            wbCommissionKopecks=None,
            marginRubKopecks=None,
            marginPct=None,
            sourceStatus="blocked",
            blockerIds=["WB-23"],
            freshness=freshness,
            formula="sellPrice - wbCommission - logistics - acquiring - storage - adCost - costPrice",
            warnings=["Cannot calculate margin without current price after SPP"],
        )

    sell_price = int(round(float(after_metric.value)))
    commission = int(round(sell_price * payload.wbCommissionPct / 100))
    margin_rub = (
        sell_price
        - commission
        - payload.logisticsKopecks
        - payload.acquiringKopecks
        - payload.storageKopecks
        - payload.adCostKopecks
        - payload.costPriceKopecks
    )
    margin_pct = (margin_rub / sell_price * 100) if sell_price > 0 else None

    status: SourceStatus = "fresh" if snapshot.sourceStatus == "fresh" else "partial"
    blockers = snapshot.blockerIds
    warnings = list(after_metric.warnings)
    if snapshot.sourceStatus in {"blocked", "stale"}:
        status = "blocked"
        blockers = ["WB-23"]
        warnings.append("Price source is stale or blocked; margin should not be used for auto-apply")

    return MarginPreviewResponse(
        articleId=article_id,
        sellPriceKopecks=sell_price,
        wbCommissionKopecks=commission,
        marginRubKopecks=margin_rub,
        marginPct=round(margin_pct, 4) if margin_pct is not None else None,
        sourceStatus=status,
        blockerIds=blockers,
        freshness=freshness,
        formula="sellPrice - wbCommission - logistics - acquiring - storage - adCost - costPrice",
        warnings=warnings,
    )


def build_pmin_pmax_validation(
    client: WbApiClient,
    article_id: str,
    payload: PminPmaxValidationRequest,
    *,
    price_snapshot: PriceMetricsSnapshot | None = None,
) -> PminPmaxValidationResponse:
    snapshot = price_snapshot or build_price_metrics_snapshot(
        client=client,
        article_id=article_id,
        nm_id=payload.nmId,
    )
    freshness = _freshness("pmin_pmax_validation", datetime.now(timezone.utc))

    denominator = 1 - payload.wbCommissionPct / 100 - payload.targetMarginPct / 100
    reasons: list[str] = []
    if denominator <= 0:
        reasons.append("invalid_formula_inputs: commissionPct + targetMarginPct must be below 100")
        return PminPmaxValidationResponse(
            articleId=article_id,
            pMinKopecks=None,
            pMaxKopecks=payload.pMaxKopecks,
            candidateBuyerPriceKopecks=payload.candidateBuyerPriceKopecks,
            isValid=False,
            reasons=reasons,
            sourceStatus="blocked",
            blockerIds=["WB-23"],
            freshness=freshness,
        )

    total_costs = (
        payload.costPriceKopecks
        + payload.logisticsKopecks
        + payload.acquiringKopecks
        + payload.fixedCostsKopecks
    )
    p_min = int(ceil(total_costs / denominator))

    if payload.candidateBuyerPriceKopecks < p_min:
        reasons.append("candidate_buyer_price_below_p_min")
    if payload.pMaxKopecks is not None and payload.candidateBuyerPriceKopecks > payload.pMaxKopecks:
        reasons.append("candidate_buyer_price_above_p_max")

    before_metric = next((metric for metric in snapshot.metrics if metric.metricId == "current_price_before_spp"), None)
    baseline_price = int(round(float(before_metric.value))) if before_metric and isinstance(before_metric.value, (int, float)) else None
    if baseline_price is not None and payload.candidateBuyerPriceKopecks < int(baseline_price / 3):
        reasons.append("wb_quarantine_risk_new_price_below_one_third_of_current")

    if snapshot.sourceStatus in {"blocked", "stale"}:
        reasons.append("source_stale_or_blocked_for_validation")

    return PminPmaxValidationResponse(
        articleId=article_id,
        pMinKopecks=p_min,
        pMaxKopecks=payload.pMaxKopecks,
        candidateBuyerPriceKopecks=payload.candidateBuyerPriceKopecks,
        isValid=not reasons,
        reasons=reasons,
        sourceStatus="fresh" if snapshot.sourceStatus == "fresh" else ("partial" if snapshot.sourceStatus == "partial" else "blocked"),
        blockerIds=snapshot.blockerIds if reasons else [],
        freshness=freshness,
    )


def _status_label(value: int | None) -> Literal["processing", "success", "canceled", "partial_errors", "all_failed", "unknown"]:
    mapping = {
        1: "processing",
        3: "success",
        4: "canceled",
        5: "partial_errors",
        6: "all_failed",
    }
    return mapping.get(value, "unknown")


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_apply_status(client: WbApiClient, upload_id: int) -> ApplyStatusResponse:
    fetched_at = datetime.now(timezone.utc)
    freshness = _freshness("price_apply_status", fetched_at)
    state = client.request(WbApiRequest(method="GET", path="/api/v2/history/tasks", query={"uploadID": upload_id}))
    details = client.request(
        WbApiRequest(method="GET", path="/api/v2/history/goods/task", query={"uploadID": upload_id, "limit": 100, "offset": 0})
    )

    if not state.ok or not isinstance(state.data, dict):
        return ApplyStatusResponse(
            uploadId=upload_id,
            sourceStatus="blocked",
            blockerIds=["WB-22"],
            wbStatus=None,
            statusLabel="unknown",
            rowErrors=[],
            freshness=freshness,
            notes=["WB processed upload state is unavailable"],
        )

    state_data = state.data.get("data") if isinstance(state.data.get("data"), dict) else state.data
    wb_status = _as_int(state_data.get("status")) if isinstance(state_data, dict) else None
    status_label = _status_label(wb_status)

    row_errors: list[ApplyStatusRow] = []
    if details.ok and isinstance(details.data, dict):
        details_data = details.data.get("data") if isinstance(details.data.get("data"), dict) else details.data
        history_rows = details_data.get("historyGoods") if isinstance(details_data, dict) else None
        if isinstance(history_rows, list):
            for row in history_rows:
                if not isinstance(row, dict):
                    continue
                errors = row.get("errors")
                first_error = None
                if isinstance(errors, list) and errors:
                    first_error = str(errors[0])
                elif isinstance(row.get("errorText"), str):
                    first_error = row.get("errorText")
                row_status = _as_int(row.get("status"))
                if first_error:
                    row_errors.append(
                        ApplyStatusRow(
                            nmId=_as_int(row.get("nmID")),
                            sizeId=_as_int(row.get("sizeID")),
                            wbStatus=row_status,
                            statusLabel=_status_label(row_status),
                            errorText=first_error,
                        )
                    )

    needs_attention = status_label in {"unknown", "processing", "partial_errors", "all_failed", "canceled"} or bool(row_errors)
    return ApplyStatusResponse(
        uploadId=upload_id,
        sourceStatus="fresh" if not needs_attention else "partial",
        blockerIds=[],
        wbStatus=wb_status,
        statusLabel=status_label,
        rowErrors=row_errors,
        freshness=freshness,
        notes=[
            "Status mapping uses WB processed upload states: 1/3/4/5/6",
            "Use row-level errors and quarantine signals to decide retry/manual review",
        ],
    )
