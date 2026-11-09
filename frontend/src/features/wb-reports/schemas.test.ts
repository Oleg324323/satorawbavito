import { describe, expect, test } from 'vitest'
import {
  AbcReportResponseSchema,
  AdsPerformanceResponseSchema,
  AiReviewApprovalSchema,
  ManualCostSchema,
  PnlRowSchema,
  PnlTotalsSchema,
  PnlReportResponseSchema,
  RnpReportResponseSchema,
} from './schemas'

const period = { dateFrom: '2026-05-19', dateTo: '2026-05-19' }
const calculatedAt = '2026-05-21T10:00:00.000Z'
const sourceEvidence = [{
  sourceId: 'mock-wb-financial-report',
  sourceType: 'mock' as const,
  sourceName: 'Draft WB financial report',
  lastSyncedAt: null,
  freshnessTtlMinutes: null,
  fieldsUsed: ['revenue'],
}]

describe('WB 19.05 report contract schemas', () => {
  test.each([[-60, -4, -56], [0, 0, 0], [100000, null, null]])('preserves factual revenue %s, tax %s and profit %s', (revenue, tax, profit) => {
    const row = {
      rowId: 'returns', label: 'Returns', brandId: null, managerId: null, skuId: 'SYNTHETIC',
      revenueKopecks: revenue, taxKopecks: tax, netProfitKopecks: profit, marginPct: null,
      cogsKopecks: 0, commissionKopecks: 0, logisticsKopecks: 0, storageKopecks: 0,
      adSpendKopecks: 0, overheadKopecks: 0, sourceStatus: 'partial', confidence: 'medium',
    }
    expect(PnlRowSchema.parse(row)).toEqual(row)
    expect(PnlTotalsSchema.parse(row).revenueKopecks).toBe(revenue)
    expect(ManualCostSchema.parse({ costId: 'tax', label: 'Tax', amountKopecks: tax,
      allocationBase: 'sku', sourceStatus: 'partial', blockerIds: [] }).amountKopecks).toBe(tax)
    expect(PnlRowSchema.parse({ ...row, cogsKopecks: -1, commissionKopecks: -2,
      logisticsKopecks: -3, storageKopecks: -4, overheadKopecks: -5 }).storageKopecks).toBe(-4)
  })

  test('keeps arbitrary manual costs nonnegative but preserves settlement credits', () => {
    for (const costId of ['storage', 'overhead', 'unverified-manual']) {
      expect(ManualCostSchema.safeParse({ costId, label: 'Cost', amountKopecks: -4,
        allocationBase: 'sku', sourceStatus: 'fresh', blockerIds: [] }).success).toBe(costId !== 'unverified-manual')
    }
  })

  test('blocks P&L closeout when source blockers are unresolved', () => {
    const result = PnlReportResponseSchema.safeParse({
      sourceStatus: 'blocked',
      confidence: 'blocked',
      blockerIds: ['WB-12', 'WB-13'],
      sourceEvidence,
      calculatedAt,
      period,
      reportState: 'final',
      groupBy: 'sku',
      totals: { revenueKopecks: null, netProfitKopecks: null, marginPct: null, sourceStatus: 'blocked', confidence: 'blocked' },
      rows: [],
      fieldMapping: [],
      manualCosts: [],
      dayAllocation: {
        allocationDateField: null,
        expenseDateField: null,
        rule: 'Blocked until Максим confirms allocation',
        sourceStatus: 'blocked',
        blockerIds: ['WB-12'],
      },
    })

    expect(result.success).toBe(false)
  })

  test('rejects P&L profit while finance blockers are unresolved', () => {
    const result = PnlReportResponseSchema.safeParse({
      sourceStatus: 'partial',
      confidence: 'low',
      blockerIds: ['WB-12', 'WB-13', 'WB-24'],
      sourceEvidence,
      calculatedAt,
      period,
      reportState: 'preliminary',
      groupBy: 'sku',
      totals: { revenueKopecks: 100000, netProfitKopecks: 20000, marginPct: 20, sourceStatus: 'partial', confidence: 'low' },
      rows: [{
        rowId: 'sku-1',
        label: 'SKU 1',
        brandId: null,
        managerId: null,
        skuId: 'SKU-1',
        revenueKopecks: 100000,
        cogsKopecks: 40000,
        commissionKopecks: null,
        logisticsKopecks: null,
        storageKopecks: null,
        adSpendKopecks: null,
        taxKopecks: null,
        overheadKopecks: null,
        netProfitKopecks: 20000,
        marginPct: 20,
        sourceStatus: 'partial',
        confidence: 'low',
      }],
      fieldMapping: [],
      manualCosts: [],
      dayAllocation: {
        allocationDateField: 'saleDate',
        expenseDateField: null,
        rule: 'preliminary',
        sourceStatus: 'partial',
        blockerIds: ['WB-23'],
      },
    })

    expect(result.success).toBe(false)
  })

  test('keeps campaign-only ad spend out of exact SKU attribution', () => {
    const result = AdsPerformanceResponseSchema.safeParse({
      sourceStatus: 'partial',
      confidence: 'low',
      blockerIds: ['WB-02'],
      sourceEvidence,
      calculatedAt,
      period,
      groupBy: 'campaign',
      totals: {
        adSpendKopecks: 150000,
        impressions: 1000,
        clicks: 100,
        cartAdds: 20,
        ordersCount: 10,
        ordersKopecks: 500000,
        drrPct: 30,
        romiPct: null,
        roiPct: null,
      },
      rows: [{
        rowId: 'campaign-1',
        campaignId: 'campaign-1',
        skuId: null,
        brandId: null,
        managerId: null,
        adSpendKopecks: 150000,
        impressions: 1000,
        clicks: 100,
        cartAdds: 20,
        ordersCount: 10,
        ordersKopecks: 500000,
        drrPct: 30,
        romiPct: null,
        roiPct: null,
        attributionLevel: 'campaign_only',
        confidence: 'low',
      }],
      attributionPolicy: {
        allowedSkuLevels: ['exact_sku', 'campaign_sku'],
        campaignOnlyCanAllocateToSkuPnl: false,
        notes: ['campaign_only remains report-level'],
      },
    })

    expect(result.success).toBe(true)
  })

  test('rejects blocked report source state without blocker IDs', () => {
    const result = AdsPerformanceResponseSchema.safeParse({
      sourceStatus: 'blocked',
      confidence: 'blocked',
      blockerIds: [],
      sourceEvidence,
      calculatedAt,
      period,
      groupBy: 'campaign',
      totals: {
        adSpendKopecks: null,
        impressions: null,
        clicks: null,
        cartAdds: null,
        ordersCount: null,
        ordersKopecks: null,
        drrPct: null,
        romiPct: null,
        roiPct: null,
      },
      rows: [],
      attributionPolicy: {
        allowedSkuLevels: ['exact_sku'],
        campaignOnlyCanAllocateToSkuPnl: false,
        notes: ['blocked source'],
      },
    })

    expect(result.success).toBe(false)
  })

  test('rejects P&L closeout with nested unresolved blockers', () => {
    const result = PnlReportResponseSchema.safeParse({
      sourceStatus: 'fresh',
      confidence: 'high',
      blockerIds: [],
      sourceEvidence,
      calculatedAt,
      period,
      reportState: 'final',
      groupBy: 'sku',
      totals: { revenueKopecks: 100000, netProfitKopecks: 20000, marginPct: 20, sourceStatus: 'blocked', confidence: 'blocked' },
      rows: [],
      fieldMapping: [{
        metricId: 'storage',
        metricLabel: 'Хранение',
        sourceId: 'manual-costs',
        sourceField: null,
        formula: 'manual allocation',
        fallback: null,
        blockerIds: ['WB-12'],
      }],
      manualCosts: [],
      dayAllocation: {
        allocationDateField: 'saleDate',
        expenseDateField: null,
        rule: 'date of sale',
        sourceStatus: 'fresh',
        blockerIds: [],
      },
    })

    expect(result.success).toBe(false)
  })

  test('rejects SKU-grouped ads rows with campaign-only attribution', () => {
    const result = AdsPerformanceResponseSchema.safeParse({
      sourceStatus: 'partial',
      confidence: 'low',
      blockerIds: ['WB-02'],
      sourceEvidence,
      calculatedAt,
      period,
      groupBy: 'sku',
      totals: {
        adSpendKopecks: 150000,
        impressions: 1000,
        clicks: 100,
        cartAdds: 20,
        ordersCount: 10,
        ordersKopecks: 500000,
        drrPct: 30,
        romiPct: null,
        roiPct: null,
      },
      rows: [{
        rowId: 'sku-campaign-only',
        campaignId: 'campaign-1',
        skuId: 'FBBT_42',
        brandId: null,
        managerId: null,
        adSpendKopecks: 150000,
        impressions: 1000,
        clicks: 100,
        cartAdds: 20,
        ordersCount: 10,
        ordersKopecks: 500000,
        drrPct: 30,
        romiPct: null,
        roiPct: null,
        attributionLevel: 'campaign_only',
        confidence: 'low',
      }],
      attributionPolicy: {
        allowedSkuLevels: ['exact_sku', 'campaign_sku'],
        campaignOnlyCanAllocateToSkuPnl: false,
        notes: ['campaign_only remains report-level'],
      },
    })

    expect(result.success).toBe(false)
  })

  test('requires WB-11 blocker when RNP DRR formula is missing', () => {
    expect(RnpReportResponseSchema.safeParse({
      sourceStatus: 'partial',
      confidence: 'low',
      blockerIds: ['WB-02'],
      sourceEvidence,
      calculatedAt,
      period,
      groupBy: 'sku',
      rows: [],
      adSpendKopecks: null,
      drrPct: null,
      formulaNotes: [],
      adsSourceStatus: 'blocked',
    }).success).toBe(false)
  })

  test('accepts full RNP funnel rows from the backend report endpoint', () => {
    expect(RnpReportResponseSchema.safeParse({
      sourceStatus: 'fresh',
      confidence: 'high',
      blockerIds: [],
      sourceEvidence,
      calculatedAt,
      period,
      groupBy: 'sku',
      rows: [{
        rowId: 'sku-FBBT_01',
        label: 'FBBT_01',
        sku: 'FBBT_01',
        nmId: 123456,
        productName: 'Demo SKU',
        brandName: 'Satorna',
        categoryName: 'Футболки',
        photoUrl: null,
        skuId: 'FBBT_01',
        brandId: 'Satorna',
        managerId: null,
        activeRule: null,
        openCount: 100,
        openCountDeltaPct: 12.5,
        cartCount: 20,
        cartCountDeltaPct: 4.2,
        orderCount: 10,
        orderCountDeltaPct: 1.5,
        orderSumKopecks: 1000000,
        orderSumDeltaPct: 5,
        buyoutCount: 8,
        buyoutSumKopecks: 800000,
        buyoutPct: 80,
        ctrPct: null,
        atcrPct: 20,
        cartToOrderPct: 50,
        adImpressions: 1000,
        adClicks: 100,
        adCtrPct: 10,
        adCartAdds: 12,
        adAtcrPct: 12,
        adOrders: 4,
        adSalesKopecks: 400000,
        adSpendKopecks: 50000,
        adSpendDeltaPct: null,
        drrPct: 5,
        roiPct: 700,
        acooPct: 12.5,
        tacooPct: 5,
        marginPct: null,
        avgPosition: null,
        organicOpenCountEstimated: 0,
        organicCartCountEstimated: 8,
        organicOrderCountEstimated: 6,
        organicSalesKopecksEstimated: 600000,
        organicEstimate: true,
        reasons: ['estimated_organic'],
        comments: [],
        auditEvents: [],
        sourceStatus: 'fresh',
        confidence: 'high',
      }],
      adSpendKopecks: 50000,
      drrPct: 5,
      formulaNotes: ['organicEstimated = total funnel - adAttributed'],
      adsSourceStatus: 'fresh',
    }).success).toBe(true)
  })

  test('accepts ABC filtered summary with finance blockers and null profit', () => {
    expect(AbcReportResponseSchema.safeParse({
      sourceStatus: 'partial',
      confidence: 'low',
      blockerIds: ['WB-02', 'WB-12', 'WB-13', 'WB-24'],
      sourceEvidence,
      calculatedAt,
      period,
      filteredSummary: {
        filterHash: 'locomotives:manager-maria',
        skuCount: 12,
        locomotiveCount: 12,
        ordersCount: 240,
        ordersKopecks: 1200000,
        profitKopecks: null,
        marginPct: null,
        adSpendKopecks: null,
        sourceStatus: 'partial',
        confidence: 'blocked',
      },
      rows: [],
    }).success).toBe(true)
  })

  test('rejects ABC profit while finance blockers are unresolved', () => {
    expect(AbcReportResponseSchema.safeParse({
      sourceStatus: 'partial',
      confidence: 'low',
      blockerIds: ['WB-12', 'WB-13', 'WB-24'],
      sourceEvidence,
      calculatedAt,
      period,
      filteredSummary: {
        filterHash: 'locomotives:manager-maria',
        skuCount: 12,
        locomotiveCount: 12,
        ordersCount: 240,
        ordersKopecks: 1200000,
        profitKopecks: 320000,
        marginPct: 26.6,
        adSpendKopecks: null,
        sourceStatus: 'partial',
        confidence: 'blocked',
      },
      rows: [],
    }).success).toBe(false)
  })

  test('requires approval draft for low-rating AI reviews', () => {
    expect(AiReviewApprovalSchema.safeParse({
      reviewId: 'review-1',
      rating: 3,
      draftId: null,
      approvalState: 'required',
      approvalActorId: null,
      approvedAt: null,
      externalSendAllowed: false,
      sendState: 'draft_only',
      brandVoiceId: 'brand-youth',
      promptTraceId: 'trace-1',
      cacheMetrics: { cachedTokens: 1200, hitRatePct: 80 },
      auditEvidence: sourceEvidence,
    }).success).toBe(false)
  })

  test('allows low-rating review send only after approval evidence', () => {
    expect(AiReviewApprovalSchema.safeParse({
      reviewId: 'review-1',
      rating: 3,
      draftId: 'draft-1',
      approvalState: 'approved',
      approvalActorId: 'manager-maria',
      approvedAt: calculatedAt,
      externalSendAllowed: true,
      sendState: 'ready_to_send',
      brandVoiceId: 'brand-youth',
      promptTraceId: 'trace-1',
      cacheMetrics: { cachedTokens: 1200, hitRatePct: 80 },
      auditEvidence: sourceEvidence,
    }).success).toBe(true)
  })
})
