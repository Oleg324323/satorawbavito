from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from app.contracts.envelopes import UtcDateTime
from app.platform.finance.schemas import FinancePeriodView, FinanceSnapshotView


class PnlProductExpensesView(BaseModel):
    penaltyKopecks: int | None
    acceptanceKopecks: int | None
    state: Literal["confirmed", "partial", "unavailable", "stale"]


class PnlExpenseOnlyView(PnlProductExpensesView):
    nmId: int | None = Field(default=None, gt=0)


class AbcPnlRowView(BaseModel):
    # Opt-in P&L display projection, separate from ABC's profit components.
    pnlExpenses: PnlProductExpensesView | None = None
    nmId: int | None = Field(default=None, gt=0)
    sellerArticle: str | None = None
    sppPct: float | None = None
    sppBuyerPriceKopecks: int | None = None
    sppObservedOn: date | None = None
    sppSource: str | None = None
    sppHistory: list[dict[str, date | float | int | str | None]] = Field(default_factory=list)
    catalogSkuId: int | None = Field(default=None, gt=0)
    operationCount: int = Field(ge=0)
    revenueKopecks: int
    salesRevenueKopecks: int
    returnsRevenueKopecks: int
    mainRevenueKopecks: int
    redemptionsRevenueKopecks: int
    lateCorrectionRevenueKopecks: int
    unknownRevenueKopecks: int
    salesUnits: int = Field(ge=0)
    returnsUnits: int = Field(ge=0)
    netUnits: int
    commissionKopecks: int
    logisticsKopecks: int
    storageKopecks: int
    acceptanceKopecks: int
    penaltyKopecks: int
    deductionKopecks: int
    additionalPaymentKopecks: int
    financeOtherExpensesKopecks: int = Field(ge=0)
    compensationKopecks: int
    acquiringKopecks: int
    financeExpensesKopecks: int
    costValueState: Literal["configured", "assumed", "missing"]
    costEvidenceStatus: Literal["dated", "undated", "period_end_fallback"] | None
    cogsKopecks: int | None
    settlementProfitKopecks: int | None
    economicsValueState: Literal["configured", "assumed", "missing"]
    economicsEvidenceStatus: Literal["dated", "undated", "period_end_fallback"] | None
    taxKopecks: int | None
    otherExpensesKopecks: int | None
    profitBeforeAdsAndLoyaltyKopecks: int | None
    advertisingSpendKopecks: int | None
    advertisingSpendSource: str = "wb_finance_report"
    profitBeforeLoyaltyKopecks: int | None
    cashbackAmountKopecks: int | None
    cashbackDiscountKopecks: int | None
    cashbackCommissionChangeKopecks: int | None
    loyaltyNetCostKopecks: int | None
    profitAfterLoyaltyKopecks: int | None
    salesClass: Literal["A", "B", "C"] | None
    profitClass: None
    abcCode: None
    netProfitKopecks: int | None
    profitBeforeInternalExpensesKopecks: int | None
    internalExpensesKopecks: int | None
    blockerIds: list[str]


class AbcPnlSummaryView(BaseModel):
    operationCount: int = Field(ge=0)
    skuCount: int = Field(ge=0)
    revenueKopecks: int
    salesRevenueKopecks: int
    returnsRevenueKopecks: int
    salesUnits: int = Field(ge=0)
    returnsUnits: int = Field(ge=0)
    netUnits: int
    commissionKopecks: int
    logisticsKopecks: int
    storageKopecks: int
    acceptanceKopecks: int
    penaltyKopecks: int
    deductionKopecks: int
    financeOtherExpensesKopecks: int = Field(ge=0)
    compensationKopecks: int
    acquiringKopecks: int
    financeExpensesKopecks: int
    cogsKopecks: int | None
    settlementProfitKopecks: int | None
    taxKopecks: int | None
    otherExpensesKopecks: int | None
    profitBeforeAdsAndLoyaltyKopecks: int | None
    advertisingSpendKopecks: int | None
    advertisingSpendSource: str = "wb_finance_report"
    unattributedAdvertisingSpendKopecks: int | None
    profitBeforeLoyaltyKopecks: int | None
    cashbackAmountKopecks: int | None
    cashbackDiscountKopecks: int | None
    cashbackCommissionChangeKopecks: int | None
    loyaltyNetCostKopecks: int | None
    profitAfterLoyaltyKopecks: int | None
    netProfitKopecks: int | None
    profitBeforeInternalExpensesKopecks: int | None
    internalExpensesKopecks: int | None


class AbcPnlMetaView(BaseModel):
    pnlExpenseRevision: str | None = None
    pnlExpenseRows: list[PnlExpenseOnlyView] | None = None
    state: Literal["ready", "partial", "future", "empty", "missing"]
    marketplaceAccountId: int = Field(gt=0)
    period: FinancePeriodView
    snapshot: FinanceSnapshotView | None = None
    formulaVersion: Literal["wb-abc-pnl-finance-rrdate-v4"]
    costLedgerRevision: int = Field(ge=0)
    economicsRevision: int = Field(ge=0)
    advertisingSource: Literal["finance_promotion", "ads_fullstats"] | None = None
    advertisingSnapshotChecksum: str | None = None
    advertisingEvidenceStatus: (
        Literal["raw", "aggregate_only", "derived_legacy"] | None
    ) = None
    blockerIds: list[str]


class AbcPnlPageView(BaseModel):
    items: list[AbcPnlRowView]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=500)
    offset: int = Field(ge=0)
    summary: AbcPnlSummaryView
    meta: AbcPnlMetaView
    timestamp: UtcDateTime
