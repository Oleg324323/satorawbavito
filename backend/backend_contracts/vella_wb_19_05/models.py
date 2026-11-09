from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator


SourceStatus = Literal["fresh", "partial", "stale", "blocked", "unknown"]
Confidence = Literal["high", "medium", "low", "blocked"]
SourceType = Literal["wb_api", "wb_excel", "manual", "derived", "mock"]
ReportGroupBy = Literal["sku", "brand", "manager", "category", "status", "warehouse", "campaign"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DatePeriod(BaseModel):
    dateFrom: date
    dateTo: date

    @model_validator(mode="after")
    def validate_order(self) -> DatePeriod:
        if self.dateTo < self.dateFrom:
            raise ValueError("dateTo must be greater than or equal to dateFrom")
        return self


class SourceEvidence(BaseModel):
    sourceId: str = Field(min_length=1)
    sourceType: SourceType
    sourceName: str = Field(min_length=1)
    lastSyncedAt: Optional[datetime]
    freshnessTtlMinutes: Optional[int] = Field(default=None, gt=0)
    fieldsUsed: List[str]


class SourceStateBase(BaseModel):
    sourceStatus: SourceStatus
    confidence: Confidence
    blockerIds: List[str]
    sourceEvidence: List[SourceEvidence]
    calculatedAt: datetime
    period: DatePeriod

    @model_validator(mode="after")
    def validate_source_state(self) -> SourceStateBase:
        if self.sourceStatus in {"blocked", "unknown"} and not self.blockerIds:
            raise ValueError("Blocked or unknown source state must reference at least one blocker ID")
        if self.sourceStatus == "blocked" and self.confidence != "blocked":
            raise ValueError("Blocked source state must expose blocked confidence")
        return self


class PnlFieldMapping(BaseModel):
    metricId: str = Field(min_length=1)
    metricLabel: str = Field(min_length=1)
    sourceId: str = Field(min_length=1)
    sourceField: Optional[str]
    formula: str = Field(min_length=1)
    fallback: Optional[str]
    blockerIds: List[str]


class ManualCost(BaseModel):
    costId: str = Field(min_length=1)
    label: str = Field(min_length=1)
    amountKopecks: Optional[int] = None
    allocationBase: Literal["sku", "brand", "manager", "marketplace", "manual", "unknown"]
    sourceStatus: SourceStatus
    blockerIds: List[str]

    @model_validator(mode="after")
    def validate_signed_tax(self) -> ManualCost:
        if self.costId not in {"tax", "storage", "overhead"} and self.amountKopecks is not None and self.amountKopecks < 0:
            raise ValueError("Only settlement tax, storage and overhead may include credits")
        return self


class DayAllocationSummary(BaseModel):
    allocationDateField: Optional[str]
    expenseDateField: Optional[str]
    rule: str = Field(min_length=1)
    sourceStatus: SourceStatus
    blockerIds: List[str]


class PnlRow(BaseModel):
    comment: Optional[str] = None
    financialExpenseSubtotalKopecks: Optional[int] = None
    costReasons: list[str] = Field(default_factory=list)
    costOperationDates: list[str] = Field(default_factory=list)
    rowId: str = Field(min_length=1)
    label: str = Field(min_length=1)
    brandId: Optional[str]
    managerId: Optional[str]
    skuId: Optional[str]
    nmId: Optional[int] = None
    sku: Optional[str] = None
    productName: Optional[str] = None
    category: Optional[str] = None
    photoUrl: Optional[str] = None
    revenueKopecks: Optional[int] = None
    # Net settlement cost can be negative when returns exceed sales.
    cogsKopecks: Optional[int] = None
    commissionKopecks: Optional[int] = None
    logisticsKopecks: Optional[int] = None
    storageKopecks: Optional[int] = None
    penaltyKopecks: Optional[int] = None
    acceptanceKopecks: Optional[int] = None
    expenseState: Literal["confirmed", "partial", "unavailable", "stale"] = "unavailable"
    adSpendKopecks: Optional[int] = None
    taxKopecks: Optional[int] = None
    overheadKopecks: Optional[int] = None
    netProfitKopecks: Optional[int]
    marginPct: Optional[float]
    sourceStatus: SourceStatus
    confidence: Confidence


class PnlTotals(BaseModel):
    revenueKopecks: Optional[int] = None
    netProfitKopecks: Optional[int]
    marginPct: Optional[float]
    sourceStatus: SourceStatus
    confidence: Confidence


class PnlReportResponse(SourceStateBase):
    financialCoverage: Dict[str, Any] = Field(default_factory=dict)
    unallocatedFinancials: Dict[str, Any] = Field(default_factory=dict)
    storageAllocation: Dict[str, Any] = Field(default_factory=dict)
    reportState: Literal["operative", "preliminary", "final", "blocked"]
    groupBy: ReportGroupBy
    totals: PnlTotals
    rows: List[PnlRow]
    fieldMapping: List[PnlFieldMapping]
    manualCosts: List[ManualCost]
    dayAllocation: DayAllocationSummary

    @model_validator(mode="after")
    def validate_final_state(self) -> PnlReportResponse:
        if self.reportState != "final":
            return self
        if self.sourceStatus != "fresh":
            raise ValueError("Final P&L requires fresh source state")
        if self.blockerIds:
            raise ValueError("Final P&L cannot carry unresolved blocker IDs")
        if self.totals.sourceStatus != "fresh" or self.totals.confidence == "blocked":
            raise ValueError("Final P&L totals must be fresh and usable")
        if any(row.sourceStatus != "fresh" or row.confidence == "blocked" for row in self.rows):
            raise ValueError("Final P&L rows must be fresh and usable")
        if any(cost.sourceStatus != "fresh" or cost.blockerIds for cost in self.manualCosts):
            raise ValueError("Final P&L manual costs must be confirmed")
        if self.dayAllocation.sourceStatus != "fresh" or self.dayAllocation.blockerIds:
            raise ValueError("Final P&L day allocation must be confirmed")
        if any(mapping.blockerIds for mapping in self.fieldMapping):
            raise ValueError("Final P&L field mapping cannot carry blockers")
        return self


AdsAttributionLevel = Literal["exact_sku", "campaign_sku", "campaign_only", "unknown"]


class AdsPerformanceRow(BaseModel):
    rowId: str = Field(min_length=1)
    campaignId: Optional[str]
    skuId: Optional[str]
    brandId: Optional[str]
    managerId: Optional[str]
    adSpendKopecks: Optional[int] = Field(default=None, ge=0)
    impressions: Optional[int] = Field(default=None, ge=0)
    clicks: Optional[int] = Field(default=None, ge=0)
    cartAdds: Optional[int] = Field(default=None, ge=0)
    ordersCount: Optional[int] = Field(default=None, ge=0)
    ordersKopecks: Optional[int] = Field(default=None, ge=0)
    drrPct: Optional[float]
    romiPct: Optional[float]
    roiPct: Optional[float]
    attributionLevel: AdsAttributionLevel
    confidence: Confidence


class AdsTotals(BaseModel):
    adSpendKopecks: Optional[int] = Field(default=None, ge=0)
    impressions: Optional[int] = Field(default=None, ge=0)
    clicks: Optional[int] = Field(default=None, ge=0)
    cartAdds: Optional[int] = Field(default=None, ge=0)
    ordersCount: Optional[int] = Field(default=None, ge=0)
    ordersKopecks: Optional[int] = Field(default=None, ge=0)
    drrPct: Optional[float]
    romiPct: Optional[float]
    roiPct: Optional[float]


class AdsAttributionPolicy(BaseModel):
    allowedSkuLevels: List[AdsAttributionLevel]
    campaignOnlyCanAllocateToSkuPnl: bool
    notes: List[str]

    @model_validator(mode="after")
    def validate_policy(self) -> AdsAttributionPolicy:
        if self.campaignOnlyCanAllocateToSkuPnl:
            raise ValueError("campaign_only spend must not be allocated to SKU P&L as exact profit")
        if "campaign_only" in self.allowedSkuLevels or "unknown" in self.allowedSkuLevels:
            raise ValueError("SKU attribution policy cannot allow campaign-only or unknown levels")
        return self


class AdsPerformanceResponse(SourceStateBase):
    groupBy: ReportGroupBy
    totals: AdsTotals
    rows: List[AdsPerformanceRow]
    attributionPolicy: AdsAttributionPolicy

    @model_validator(mode="after")
    def validate_ads(self) -> AdsPerformanceResponse:
        if self.sourceStatus in {"blocked", "unknown"} and "WB-02" not in self.blockerIds:
            raise ValueError("Blocked or unknown ads source must reference WB-02")
        for row in self.rows:
            if self.groupBy == "sku" and row.attributionLevel in {"campaign_only", "unknown"}:
                raise ValueError("SKU-grouped ads rows cannot use campaign-only or unknown attribution")
            if row.attributionLevel in {"campaign_only", "unknown"} and row.confidence == "high":
                raise ValueError("Weak ads attribution cannot be marked high confidence")
        return self


class RnpRow(BaseModel):
    rowId: str = Field(min_length=1)
    label: str = Field(min_length=1)
    sku: Optional[str] = None
    nmId: Optional[int] = Field(default=None, ge=0)
    productName: Optional[str] = None
    brandName: Optional[str] = None
    categoryName: Optional[str] = None
    photoUrl: Optional[str] = None
    colorName: Optional[str] = None
    managerName: Optional[str] = None
    strategyName: Optional[str] = None
    warehouseId: Optional[int] = Field(default=None, ge=0)
    warehouseName: Optional[str] = None
    warehouses: List[Dict[str, Any]] = Field(default_factory=list)
    skuId: Optional[str]
    brandId: Optional[str]
    managerId: Optional[str]
    activeRule: Optional[str] = None
    openCount: Optional[int] = Field(default=None, ge=0)
    impressions: Optional[int] = Field(default=None, ge=0)
    openCountDeltaPct: Optional[float] = None
    cartCount: Optional[int] = Field(default=None, ge=0)
    wishlistCount: Optional[int] = Field(default=None, ge=0)
    cartCountDeltaPct: Optional[float] = None
    orderCount: Optional[int] = Field(default=None, ge=0)
    orderCountDeltaPct: Optional[float] = None
    orderSumKopecks: Optional[int] = Field(default=None, ge=0)
    orderSumDeltaPct: Optional[float] = None
    buyoutCount: Optional[int] = None
    buyoutCountDeltaPct: Optional[float] = None
    buyoutSumKopecks: Optional[int] = None
    buyoutSumDeltaPct: Optional[float] = None
    funnelBuyoutCount: Optional[int] = Field(default=None, ge=0)
    funnelBuyoutSumKopecks: Optional[int] = Field(default=None, ge=0)
    financialBuyoutState: Literal["confirmed", "partial", "unavailable", "forbidden"] = "unavailable"
    financialBuyoutSource: Optional[str] = None
    financialSalesUnits: Optional[int] = None
    financialReturnsUnits: Optional[int] = None
    buyoutPct: Optional[float] = None
    ctrPct: Optional[float] = None
    atcrPct: Optional[float] = None
    cartToOrderPct: Optional[float] = None
    adImpressions: Optional[int] = Field(default=None, ge=0)
    adClicks: Optional[int] = Field(default=None, ge=0)
    adCtrPct: Optional[float] = None
    adCartAdds: Optional[int] = Field(default=None, ge=0)
    adAtcrPct: Optional[float] = None
    adOrders: Optional[int] = Field(default=None, ge=0)
    adSalesKopecks: Optional[int] = Field(default=None, ge=0)
    adSpendKopecks: Optional[int] = Field(default=None, ge=0)
    adSpendDeltaPct: Optional[float] = None
    drrPct: Optional[float]
    roiPct: Optional[float]
    acooPct: Optional[float] = None
    tacooPct: Optional[float] = None
    marginPct: Optional[float]
    avgPosition: Optional[float] = None
    organicOpenCountEstimated: Optional[int] = Field(default=None, ge=0)
    organicCartCountEstimated: Optional[int] = Field(default=None, ge=0)
    organicOrderCountEstimated: Optional[int] = Field(default=None, ge=0)
    organicSalesKopecksEstimated: Optional[int] = Field(default=None, ge=0)
    organicEstimate: bool = False
    reasons: List[str] = Field(default_factory=list)
    comments: List[Dict[str, Any]] = Field(default_factory=list)
    auditEvents: List[Dict[str, Any]] = Field(default_factory=list)
    sourceStatus: SourceStatus
    confidence: Confidence


class RnpReportResponse(SourceStateBase):
    groupBy: ReportGroupBy
    rows: List[RnpRow]
    adSpendKopecks: Optional[int] = Field(default=None, ge=0)
    drrPct: Optional[float]
    formulaNotes: List[str]
    adsSourceStatus: SourceStatus
    diagnostics: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_rnp(self) -> RnpReportResponse:
        if self.drrPct is None and "WB-11" not in self.blockerIds:
            raise ValueError("Missing DRR formula must reference WB-11")
        if self.adsSourceStatus in {"blocked", "unknown"} and "WB-02" not in self.blockerIds:
            raise ValueError("Blocked or unknown ads source must reference WB-02")
        return self


class AbcFilteredSummary(BaseModel):
    filterHash: str = Field(min_length=1)
    skuCount: int = Field(ge=0)
    locomotiveCount: Optional[int] = Field(default=None, ge=0)
    basketsCount: Optional[int] = Field(default=None, ge=0)
    ordersCount: Optional[int] = Field(default=None, ge=0)
    ordersKopecks: Optional[int] = Field(default=None, ge=0)
    salesKopecks: Optional[int] = None
    returnsKopecks: Optional[int] = Field(default=None, ge=0)
    profitKopecks: Optional[int]
    marginPct: Optional[float]
    adSpendKopecks: Optional[int] = Field(default=None, ge=0)
    sourceStatus: SourceStatus
    confidence: Confidence


class AbcReportResponse(SourceStateBase):
    financialCoverage: Dict[str, Any] = Field(default_factory=dict)
    unallocatedFinancials: Dict[str, Any] = Field(default_factory=dict)
    storageAllocation: Dict[str, Any] = Field(default_factory=dict)
    filteredSummary: AbcFilteredSummary
    rows: List[Dict[str, Any]]

    @model_validator(mode="after")
    def validate_abc(self) -> AbcReportResponse:
        if self.filteredSummary.sourceStatus in {"blocked", "unknown"} and not self.blockerIds:
            raise ValueError("Blocked or unknown ABC filtered summary must reference report blockers")
        return self


class SppSnapshot(BaseModel):
    sourceStatus: SourceStatus
    sppPct: Optional[float] = Field(default=None, ge=0, le=100)
    buyerPriceKopecks: Optional[int] = Field(default=None, gt=0)
    capturedAt: Optional[datetime]
    blockerIds: List[str]

    @model_validator(mode="after")
    def validate_snapshot(self) -> SppSnapshot:
        if self.sourceStatus in {"blocked", "unknown"} and not self.blockerIds:
            raise ValueError("Blocked or unknown SPP snapshot must reference blocker IDs")
        return self


class PriceGuardTrigger(BaseModel):
    type: Literal["stock_jump", "spp_jump", "cogs_jump", "seller_price_jump", "source_stale"]
    severity: Literal["info", "warning", "blocker"]
    observedValue: Optional[Union[int, float, str]]
    previousValue: Optional[Union[int, float, str]]
    threshold: Optional[Union[int, float, str]]
    message: str = Field(min_length=1)


class PriceGuardResponse(BaseModel):
    articleId: str = Field(min_length=1)
    currentPriceKopecks: Optional[int] = Field(default=None, gt=0)
    buyerPriceKopecks: Optional[int] = Field(default=None, gt=0)
    sppSnapshot: Optional[SppSnapshot]
    recommendedSellerPriceKopecks: Optional[int] = Field(default=None, gt=0)
    lastKnownGoodPriceKopecks: Optional[int] = Field(default=None, gt=0)
    canApply: bool
    blockedReason: Optional[str]
    freezeState: Literal["none", "frozen", "requires_review", "source_blocked"]
    guardTriggers: List[PriceGuardTrigger]
    sourceEvidence: List[SourceEvidence]
    blockerIds: List[str]

    @model_validator(mode="after")
    def validate_guard(self) -> PriceGuardResponse:
        if not self.canApply and self.blockedReason is None and not self.blockerIds:
            raise ValueError("Blocked price guard must explain why apply is unavailable")
        if not self.canApply and self.freezeState == "none":
            raise ValueError("Blocked price guard must expose a non-none freeze state")
        if self.canApply and self.blockedReason is not None:
            raise ValueError("Applicable price guard cannot carry blockedReason")
        if self.canApply and self.freezeState != "none":
            raise ValueError("Applicable price guard must have freezeState none")
        if self.canApply and self.blockerIds:
            raise ValueError("Applicable price guard cannot carry blocker IDs")
        if self.canApply and any(trigger.severity == "blocker" for trigger in self.guardTriggers):
            raise ValueError("Applicable price guard cannot carry blocker triggers")
        if self.canApply and self.sppSnapshot is not None:
            if self.sppSnapshot.sourceStatus != "fresh" or self.sppSnapshot.blockerIds:
                raise ValueError("Applicable SPP-aware guard requires fresh SPP snapshot without blockers")
        return self


class AiReviewCacheMetrics(BaseModel):
    cachedTokens: int = Field(ge=0)
    hitRatePct: float = Field(ge=0, le=100)


class AiReviewApproval(BaseModel):
    reviewId: str = Field(min_length=1)
    rating: int = Field(ge=1, le=5)
    draftId: Optional[str]
    approvalState: Literal["not_required", "required", "approved", "rejected", "expired"]
    approvalActorId: Optional[str]
    approvedAt: Optional[datetime]
    externalSendAllowed: bool
    sendState: Literal["draft_only", "ready_to_send", "sent", "blocked"]
    brandVoiceId: Optional[str]
    promptTraceId: Optional[str]
    cacheMetrics: Optional[AiReviewCacheMetrics]
    auditEvidence: List[SourceEvidence]

    @model_validator(mode="after")
    def validate_review_approval(self) -> AiReviewApproval:
        if self.rating < 4 and self.approvalState == "not_required":
            raise ValueError("Reviews below 4 stars require approval")
        if self.rating < 4 and self.draftId is None:
            raise ValueError("Low-rating review approval requires draftId")
        if self.rating < 4 and self.externalSendAllowed and self.approvalState != "approved":
            raise ValueError("Low-rating review send requires approved state")
        if self.approvalState == "approved" and (self.approvalActorId is None or self.approvedAt is None):
            raise ValueError("Approved review requires approval actor and timestamp")
        if self.sendState in {"ready_to_send", "sent"} and not self.externalSendAllowed:
            raise ValueError("Ready or sent reviews must pass external send gate")
        return self
