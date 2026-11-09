import { z } from 'zod'
import {
  ConfidenceSchema,
  SourceEvidenceSchema,
  SourceStateBaseSchema,
  SourceStatusSchema,
  validateSourceStateConsistency,
} from '../wb-contracts/sourceState.js'

export const ReportGroupBySchema = z.enum(['sku', 'brand', 'manager', 'category', 'status', 'warehouse', 'campaign'])
export type ReportGroupBy = z.infer<typeof ReportGroupBySchema>

export const PnlReportStateSchema = z.enum(['operative', 'preliminary', 'final', 'blocked'])
export type PnlReportState = z.infer<typeof PnlReportStateSchema>

const MoneyKopecksSchema = z.number().int()
const NonNegativeMoneyKopecksSchema = z.number().int().nonnegative()
const OptionalMoneyKopecksSchema = MoneyKopecksSchema.nullable()
const OptionalNonNegativeMoneyKopecksSchema = NonNegativeMoneyKopecksSchema.nullable()
const OptionalPctSchema = z.number().nullable()

export const PnlFieldMappingSchema = z.object({
  metricId: z.string().min(1),
  metricLabel: z.string().min(1),
  sourceId: z.string().min(1),
  sourceField: z.string().min(1).nullable(),
  formula: z.string().min(1),
  fallback: z.string().min(1).nullable(),
  blockerIds: z.array(z.string().min(1)),
})
export type PnlFieldMapping = z.infer<typeof PnlFieldMappingSchema>

const NonNegativeManualCostSchema = z.object({
  costId: z.string().min(1),
  label: z.string().min(1),
  amountKopecks: OptionalNonNegativeMoneyKopecksSchema,
  allocationBase: z.enum(['sku', 'brand', 'manager', 'marketplace', 'revenue', 'orders', 'units', 'stock_days', 'production_units', 'manual', 'unallocated', 'unknown']),
  sourceStatus: SourceStatusSchema,
  blockerIds: z.array(z.string().min(1)),
})
export const ManualCostSchema = z.union([
  NonNegativeManualCostSchema,
  NonNegativeManualCostSchema.extend({ costId: z.enum(['tax', 'storage', 'overhead']), amountKopecks: OptionalMoneyKopecksSchema }),
])
export type ManualCost = z.infer<typeof ManualCostSchema>

export const ExpenseAllocationBaseSchema = z.enum(['sku', 'brand', 'manager', 'marketplace', 'revenue', 'orders', 'units', 'stock_days', 'production_units', 'manual', 'unallocated', 'unknown'])
export type ExpenseAllocationBase = z.infer<typeof ExpenseAllocationBaseSchema>

export const ExpenseApprovalStatusSchema = z.enum(['draft', 'needs_approval', 'approved', 'rejected', 'blocked'])
export type ExpenseApprovalStatus = z.infer<typeof ExpenseApprovalStatusSchema>

export const ExpenseDefaultPeriodSchema = z.enum(['month'])

export const ExpenseRowSchema = z.object({
  expenseId: z.string().min(1),
  category: z.string().min(1),
  period: z.object({
    dateFrom: z.string().date(),
    dateTo: z.string().date(),
  }),
  amountKopecks: OptionalNonNegativeMoneyKopecksSchema,
  allocationBase: ExpenseAllocationBaseSchema,
  sourceId: z.string().min(1),
  sourceType: z.enum(['wb_api', 'wb_excel', 'one_c', 'manual', 'derived', 'mock']),
  approvalStatus: ExpenseApprovalStatusSchema,
  ownerId: z.string().nullable(),
  comment: z.string().nullable(),
  blockerIds: z.array(z.string().min(1)),
})
export type ExpenseRow = z.infer<typeof ExpenseRowSchema>

export const ExpensesReportResponseSchema = SourceStateBaseSchema.extend({
  rows: z.array(ExpenseRowSchema),
  allowedAllocationBases: z.array(ExpenseAllocationBaseSchema),
  defaultExpensePeriod: ExpenseDefaultPeriodSchema,
  defaultAllocationBase: ExpenseAllocationBaseSchema,
  templateCategories: z.array(z.string().min(1)),
  temporaryAssumptions: z.array(z.string().min(1)),
  importPolicy: z.string().min(1),
})
  .superRefine((report, ctx) => {
    validateSourceStateConsistency(report, ctx)

    if ((report.sourceStatus === 'blocked' || report.sourceStatus === 'unknown') && !report.blockerIds.some((id) => ['WB-12', 'WB-13', 'WB-24'].includes(id))) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'Blocked expenses must reference finance/OPEX blockers',
        path: ['blockerIds'],
      })
    }
    if (report.templateCategories.length === 0) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'Expenses response must expose temporary OPEX template categories',
        path: ['templateCategories'],
      })
    }

    report.rows.forEach((row, index) => {
      if (row.approvalStatus === 'approved' && row.blockerIds.length > 0) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: 'Approved expense rows cannot carry blockers',
          path: ['rows', index, 'blockerIds'],
        })
      }
      if (row.category.toLowerCase().includes('налог') && row.approvalStatus === 'blocked' && row.amountKopecks !== null) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: 'Blocked tax rows must not use a zero or estimated amount',
          path: ['rows', index, 'amountKopecks'],
        })
      }
    })
  })
export type ExpensesReportResponse = z.infer<typeof ExpensesReportResponseSchema>

export const ExpenseImportPreviewResponseSchema = SourceStateBaseSchema.extend({
  previewId: z.string().min(1),
  sourceName: z.string().min(1),
  validRows: z.number().int().nonnegative(),
  blockedRows: z.number().int().nonnegative(),
  rows: z.array(ExpenseRowSchema),
  requiredApproval: z.boolean(),
  commitAllowed: z.boolean(),
  nextAction: z.string().min(1),
})
  .superRefine((preview, ctx) => {
    validateSourceStateConsistency(preview, ctx)

    if (preview.commitAllowed && preview.requiredApproval) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'Expense import cannot commit while approval is still required',
        path: ['commitAllowed'],
      })
    }
    if (preview.commitAllowed && preview.blockerIds.length > 0) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'Expense import cannot commit with unresolved blockers',
        path: ['blockerIds'],
      })
    }
  })
export type ExpenseImportPreviewResponse = z.infer<typeof ExpenseImportPreviewResponseSchema>

export const ExpenseImportCommitResponseSchema = SourceStateBaseSchema.extend({
  previewId: z.string().min(1),
  committed: z.boolean(),
  committedRows: z.number().int().nonnegative(),
  auditEventId: z.string().min(1).nullable(),
  nextAction: z.string().min(1),
})
  .superRefine((response, ctx) => {
    validateSourceStateConsistency(response, ctx)

    if (response.committed && response.blockerIds.length > 0) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'Committed expense import cannot carry unresolved blockers',
        path: ['blockerIds'],
      })
    }
    if (response.committed && response.sourceStatus !== 'fresh') {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'Committed expense import must be fresh',
        path: ['sourceStatus'],
      })
    }
    if (!response.committed && response.blockerIds.length === 0) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'Blocked expense import must expose blocker IDs',
        path: ['blockerIds'],
      })
    }
  })
export type ExpenseImportCommitResponse = z.infer<typeof ExpenseImportCommitResponseSchema>

export const SourceStatusRowSchema = z.object({
  sourceId: z.string().min(1),
  label: z.string().min(1),
  surface: z.string().min(1),
  sourceStatus: SourceStatusSchema,
  confidence: ConfidenceSchema,
  freshnessTtlMinutes: z.number().int().positive().nullable(),
  lastSyncedAt: z.string().datetime().nullable(),
  nextRunAt: z.string().datetime().nullable(),
  blockerIds: z.array(z.string().min(1)),
  evidenceRefs: z.array(z.string().min(1)),
  owner: z.string().min(1),
})
export type SourceStatusRow = z.infer<typeof SourceStatusRowSchema>

export const SourceStatusResponseSchema = SourceStateBaseSchema.extend({
  rows: z.array(SourceStatusRowSchema),
  manualUploadAllowed: z.boolean(),
  productionMetricsBlocked: z.boolean(),
})
  .superRefine((status, ctx) => {
    validateSourceStateConsistency(status, ctx)

    const blockedRows = status.rows.filter((row) => row.sourceStatus === 'blocked' || row.sourceStatus === 'unknown')
    if (blockedRows.length > 0 && status.blockerIds.length === 0) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'Source status page must surface blockers from blocked rows',
        path: ['blockerIds'],
      })
    }
    if (status.productionMetricsBlocked && blockedRows.length === 0) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'productionMetricsBlocked requires at least one blocked source row',
        path: ['productionMetricsBlocked'],
      })
    }
  })
export type SourceStatusResponse = z.infer<typeof SourceStatusResponseSchema>

export const DayAllocationSummarySchema = z.object({
  allocationDateField: z.string().min(1).nullable(),
  expenseDateField: z.string().min(1).nullable(),
  rule: z.string().min(1),
  sourceStatus: SourceStatusSchema,
  blockerIds: z.array(z.string().min(1)),
})
export type DayAllocationSummary = z.infer<typeof DayAllocationSummarySchema>

export const PnlRowSchema = z.object({
  rowId: z.string().min(1),
  label: z.string().min(1),
  brandId: z.string().nullable(),
  managerId: z.string().nullable(),
  skuId: z.string().nullable(),
  revenueKopecks: OptionalMoneyKopecksSchema,
  cogsKopecks: OptionalMoneyKopecksSchema,
  commissionKopecks: OptionalMoneyKopecksSchema,
  logisticsKopecks: OptionalMoneyKopecksSchema,
  storageKopecks: OptionalMoneyKopecksSchema,
  adSpendKopecks: OptionalNonNegativeMoneyKopecksSchema,
  taxKopecks: OptionalMoneyKopecksSchema,
  overheadKopecks: OptionalMoneyKopecksSchema,
  netProfitKopecks: OptionalMoneyKopecksSchema,
  marginPct: OptionalPctSchema,
  sourceStatus: SourceStatusSchema,
  confidence: ConfidenceSchema,
})
export type PnlRow = z.infer<typeof PnlRowSchema>

export const PnlTotalsSchema = z.object({
  revenueKopecks: OptionalMoneyKopecksSchema,
  netProfitKopecks: OptionalMoneyKopecksSchema,
  marginPct: OptionalPctSchema,
  sourceStatus: SourceStatusSchema,
  confidence: ConfidenceSchema,
})
export type PnlTotals = z.infer<typeof PnlTotalsSchema>

export const PnlReportResponseSchema = SourceStateBaseSchema.extend({
  reportState: PnlReportStateSchema,
  groupBy: ReportGroupBySchema,
  totals: PnlTotalsSchema,
  rows: z.array(PnlRowSchema),
  fieldMapping: z.array(PnlFieldMappingSchema),
  manualCosts: z.array(ManualCostSchema),
  dayAllocation: DayAllocationSummarySchema,
})
  .superRefine((report, ctx) => {
    validateSourceStateConsistency(report, ctx)

    const hasFinanceBlockers = ['WB-12', 'WB-13', 'WB-24'].some((blockerId) => report.blockerIds.includes(blockerId))
    if (hasFinanceBlockers && (report.totals.netProfitKopecks !== null || report.totals.marginPct !== null)) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'P&L profit and margin must stay null while finance blockers are unresolved',
        path: ['totals'],
      })
    }
    if (hasFinanceBlockers) {
      report.rows.forEach((row, index) => {
        if (row.netProfitKopecks !== null || row.marginPct !== null) {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            message: 'P&L row profit and margin must stay null while finance blockers are unresolved',
            path: ['rows', index],
          })
        }
      })
    }

    if (report.reportState !== 'final') return

    if (report.sourceStatus !== 'fresh') {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'Final P&L requires fresh source state',
        path: ['sourceStatus'],
      })
    }
    if (report.blockerIds.length > 0) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'Final P&L cannot carry unresolved blocker IDs',
        path: ['blockerIds'],
      })
    }
    if (report.totals.sourceStatus !== 'fresh' || report.totals.confidence === 'blocked') {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'Final P&L totals must be fresh and usable',
        path: ['totals'],
      })
    }
    report.rows.forEach((row, index) => {
      if (row.sourceStatus !== 'fresh' || row.confidence === 'blocked') {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: 'Final P&L rows must be fresh and usable',
          path: ['rows', index],
        })
      }
    })
    report.manualCosts.forEach((cost, index) => {
      if (cost.sourceStatus !== 'fresh' || cost.blockerIds.length > 0) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: 'Final P&L manual costs must be confirmed',
          path: ['manualCosts', index],
        })
      }
    })
    if (report.dayAllocation.sourceStatus !== 'fresh' || report.dayAllocation.blockerIds.length > 0) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'Final P&L day allocation must be confirmed',
        path: ['dayAllocation'],
      })
    }
    report.fieldMapping.forEach((mapping, index) => {
      if (mapping.blockerIds.length > 0) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: 'Final P&L field mapping cannot carry blockers',
          path: ['fieldMapping', index, 'blockerIds'],
        })
      }
    })
  })
export type PnlReportResponse = z.infer<typeof PnlReportResponseSchema>

export const AdsAttributionLevelSchema = z.enum(['exact_sku', 'campaign_sku', 'campaign_only', 'unknown'])
export type AdsAttributionLevel = z.infer<typeof AdsAttributionLevelSchema>

export const AdsPerformanceRowSchema = z.object({
  rowId: z.string().min(1),
  campaignId: z.string().nullable(),
  skuId: z.string().nullable(),
  brandId: z.string().nullable(),
  managerId: z.string().nullable(),
  adSpendKopecks: OptionalNonNegativeMoneyKopecksSchema,
  impressions: z.number().int().nonnegative().nullable(),
  clicks: z.number().int().nonnegative().nullable(),
  cartAdds: z.number().int().nonnegative().nullable(),
  ordersCount: z.number().int().nonnegative().nullable(),
  ordersKopecks: OptionalNonNegativeMoneyKopecksSchema,
  drrPct: OptionalPctSchema,
  romiPct: OptionalPctSchema,
  roiPct: OptionalPctSchema,
  attributionLevel: AdsAttributionLevelSchema,
  confidence: ConfidenceSchema,
})
export type AdsPerformanceRow = z.infer<typeof AdsPerformanceRowSchema>

export const AdsTotalsSchema = z.object({
  adSpendKopecks: OptionalNonNegativeMoneyKopecksSchema,
  impressions: z.number().int().nonnegative().nullable(),
  clicks: z.number().int().nonnegative().nullable(),
  cartAdds: z.number().int().nonnegative().nullable(),
  ordersCount: z.number().int().nonnegative().nullable(),
  ordersKopecks: OptionalNonNegativeMoneyKopecksSchema,
  drrPct: OptionalPctSchema,
  romiPct: OptionalPctSchema,
  roiPct: OptionalPctSchema,
})
export type AdsTotals = z.infer<typeof AdsTotalsSchema>

export const AdsAttributionPolicySchema = z.object({
  allowedSkuLevels: z.array(AdsAttributionLevelSchema),
  campaignOnlyCanAllocateToSkuPnl: z.boolean(),
  notes: z.array(z.string().min(1)),
})
  .refine((policy) => !policy.campaignOnlyCanAllocateToSkuPnl, {
    message: 'campaign_only spend must not be allocated to SKU P&L as exact profit',
    path: ['campaignOnlyCanAllocateToSkuPnl'],
  })
export type AdsAttributionPolicy = z.infer<typeof AdsAttributionPolicySchema>

export const AdsPerformanceResponseSchema = SourceStateBaseSchema.extend({
  groupBy: ReportGroupBySchema,
  totals: AdsTotalsSchema,
  rows: z.array(AdsPerformanceRowSchema),
  attributionPolicy: AdsAttributionPolicySchema,
})
  .superRefine((report, ctx) => {
    validateSourceStateConsistency(report, ctx)

    if ((report.sourceStatus === 'blocked' || report.sourceStatus === 'unknown') && !report.blockerIds.includes('WB-02')) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'Blocked or unknown ads source must reference WB-02',
        path: ['blockerIds'],
      })
    }

    if (report.attributionPolicy.allowedSkuLevels.includes('campaign_only') || report.attributionPolicy.allowedSkuLevels.includes('unknown')) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'SKU attribution policy cannot allow campaign-only or unknown levels',
        path: ['attributionPolicy', 'allowedSkuLevels'],
      })
    }

    report.rows.forEach((row, index) => {
      if (report.groupBy === 'sku' && (row.attributionLevel === 'campaign_only' || row.attributionLevel === 'unknown')) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: 'SKU-grouped ads rows cannot use campaign-only or unknown attribution',
          path: ['rows', index, 'attributionLevel'],
        })
      }
      if ((row.attributionLevel === 'campaign_only' || row.attributionLevel === 'unknown') && row.confidence === 'high') {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: 'Weak ads attribution cannot be marked high confidence',
          path: ['rows', index, 'confidence'],
        })
      }
    })
  })
export type AdsPerformanceResponse = z.infer<typeof AdsPerformanceResponseSchema>

export const RnpRowSchema = z.object({
  rowId: z.string().min(1),
  label: z.string().min(1),
  sku: z.string().nullable().optional(),
  nmId: z.number().int().nonnegative().nullable().optional(),
  productName: z.string().nullable().optional(),
  brandName: z.string().nullable().optional(),
  categoryName: z.string().nullable().optional(),
  photoUrl: z.string().nullable().optional(),
  skuId: z.string().nullable(),
  brandId: z.string().nullable(),
  managerId: z.string().nullable(),
  activeRule: z.string().nullable().optional(),
  openCount: z.number().int().nonnegative().nullable().optional(),
  openCountDeltaPct: OptionalPctSchema.optional(),
  cartCount: z.number().int().nonnegative().nullable().optional(),
  cartCountDeltaPct: OptionalPctSchema.optional(),
  orderCount: z.number().int().nonnegative().nullable().optional(),
  orderCountDeltaPct: OptionalPctSchema.optional(),
  orderSumKopecks: OptionalNonNegativeMoneyKopecksSchema.optional(),
  orderSumDeltaPct: OptionalPctSchema.optional(),
  buyoutCount: z.number().int().nonnegative().nullable().optional(),
  buyoutSumKopecks: OptionalNonNegativeMoneyKopecksSchema.optional(),
  buyoutPct: OptionalPctSchema.optional(),
  ctrPct: OptionalPctSchema.optional(),
  atcrPct: OptionalPctSchema.optional(),
  cartToOrderPct: OptionalPctSchema.optional(),
  adImpressions: z.number().int().nonnegative().nullable().optional(),
  adClicks: z.number().int().nonnegative().nullable().optional(),
  adCtrPct: OptionalPctSchema.optional(),
  adCartAdds: z.number().int().nonnegative().nullable().optional(),
  adAtcrPct: OptionalPctSchema.optional(),
  adOrders: z.number().int().nonnegative().nullable().optional(),
  adSalesKopecks: OptionalNonNegativeMoneyKopecksSchema.optional(),
  adSpendKopecks: OptionalNonNegativeMoneyKopecksSchema,
  adSpendDeltaPct: OptionalPctSchema.optional(),
  drrPct: OptionalPctSchema,
  roiPct: OptionalPctSchema,
  acooPct: OptionalPctSchema.optional(),
  tacooPct: OptionalPctSchema.optional(),
  marginPct: OptionalPctSchema,
  avgPosition: OptionalPctSchema.optional(),
  organicOpenCountEstimated: z.number().int().nonnegative().nullable().optional(),
  organicCartCountEstimated: z.number().int().nonnegative().nullable().optional(),
  organicOrderCountEstimated: z.number().int().nonnegative().nullable().optional(),
  organicSalesKopecksEstimated: OptionalNonNegativeMoneyKopecksSchema.optional(),
  organicEstimate: z.boolean().optional(),
  reasons: z.array(z.string().min(1)).optional(),
  comments: z.array(z.record(z.unknown())).optional(),
  auditEvents: z.array(z.record(z.unknown())).optional(),
  sourceStatus: SourceStatusSchema,
  confidence: ConfidenceSchema,
})
export type RnpRow = z.infer<typeof RnpRowSchema>

export const RnpReportResponseSchema = SourceStateBaseSchema.extend({
  groupBy: ReportGroupBySchema,
  rows: z.array(RnpRowSchema),
  adSpendKopecks: OptionalNonNegativeMoneyKopecksSchema,
  drrPct: OptionalPctSchema,
  formulaNotes: z.array(z.string().min(1)),
  adsSourceStatus: SourceStatusSchema,
})
  .superRefine((report, ctx) => {
    validateSourceStateConsistency(report, ctx)

    if (report.drrPct === null && !report.blockerIds.includes('WB-11')) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'Missing DRR formula must reference WB-11',
        path: ['blockerIds'],
      })
    }
    if ((report.adsSourceStatus === 'blocked' || report.adsSourceStatus === 'unknown') && !report.blockerIds.includes('WB-02')) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'Blocked or unknown ads source must reference WB-02',
        path: ['blockerIds'],
      })
    }
  })
export type RnpReportResponse = z.infer<typeof RnpReportResponseSchema>

export const AbcFilteredSummarySchema = z.object({
  filterHash: z.string().min(1),
  skuCount: z.number().int().nonnegative(),
  locomotiveCount: z.number().int().nonnegative().nullable(),
  ordersCount: z.number().int().nonnegative().nullable(),
  ordersKopecks: OptionalNonNegativeMoneyKopecksSchema,
  profitKopecks: OptionalMoneyKopecksSchema,
  marginPct: OptionalPctSchema,
  adSpendKopecks: OptionalNonNegativeMoneyKopecksSchema,
  sourceStatus: SourceStatusSchema,
  confidence: ConfidenceSchema,
})
export type AbcFilteredSummary = z.infer<typeof AbcFilteredSummarySchema>

export const AbcReportResponseSchema = SourceStateBaseSchema.extend({
  filteredSummary: AbcFilteredSummarySchema,
  rows: z.array(z.unknown()),
})
  .superRefine((report, ctx) => {
    validateSourceStateConsistency(report, ctx)

    if ((report.filteredSummary.sourceStatus === 'blocked' || report.filteredSummary.sourceStatus === 'unknown') && report.blockerIds.length === 0) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'Blocked or unknown ABC filtered summary must reference report blockers',
        path: ['blockerIds'],
      })
    }
    const hasFinanceBlockers = ['WB-12', 'WB-13', 'WB-24'].some((blockerId) => report.blockerIds.includes(blockerId))
    if (hasFinanceBlockers && (report.filteredSummary.profitKopecks !== null || report.filteredSummary.marginPct !== null)) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'ABC profit and margin must stay null while finance blockers are unresolved',
        path: ['filteredSummary'],
      })
    }
  })
export type AbcReportResponse = z.infer<typeof AbcReportResponseSchema>

export const PlanFactDimensionSchema = z.enum(['company', 'manager', 'brand'])
export type PlanFactDimension = z.infer<typeof PlanFactDimensionSchema>

export const PlanFactBrandDimensionSchema = z.object({
  dimension: PlanFactDimensionSchema,
  brandId: z.string().nullable(),
  managerId: z.string().nullable(),
  responsibilityPct: z.number().min(0).max(100).nullable(),
  sourceStatus: SourceStatusSchema,
  blockerIds: z.array(z.string().min(1)),
})
  .refine((row) => row.dimension !== 'brand' || row.brandId !== null, {
    message: 'Brand dimension requires brandId',
    path: ['brandId'],
  })
  .refine((row) => row.dimension !== 'brand' || row.sourceStatus !== 'blocked' || (Array.isArray(row.blockerIds) && row.blockerIds.includes('WB-14A')), {
    message: 'Blocked brand plan dimension must reference WB-14A',
    path: ['blockerIds'],
  })
export type PlanFactBrandDimension = z.infer<typeof PlanFactBrandDimensionSchema>

export const AiReviewApprovalStateSchema = z.enum(['not_required', 'required', 'approved', 'rejected', 'expired'])
export type AiReviewApprovalState = z.infer<typeof AiReviewApprovalStateSchema>

export const AiReviewApprovalSchema = z.object({
  reviewId: z.string().min(1),
  rating: z.number().int().min(1).max(5),
  draftId: z.string().nullable(),
  approvalState: AiReviewApprovalStateSchema,
  approvalActorId: z.string().min(1).nullable(),
  approvedAt: z.string().datetime().nullable(),
  externalSendAllowed: z.boolean(),
  sendState: z.enum(['draft_only', 'ready_to_send', 'sent', 'blocked']),
  brandVoiceId: z.string().nullable(),
  promptTraceId: z.string().nullable(),
  cacheMetrics: z.object({
    cachedTokens: z.number().int().nonnegative(),
    hitRatePct: z.number().min(0).max(100),
  }).nullable(),
  auditEvidence: z.array(SourceEvidenceSchema),
})
  .refine((review) => review.rating >= 4 || review.approvalState !== 'not_required', {
    message: 'Reviews below 4 stars require approval',
    path: ['approvalState'],
  })
  .refine((review) => review.rating >= 4 || review.draftId !== null, {
    message: 'Low-rating review approval requires draftId',
    path: ['draftId'],
  })
  .refine((review) => review.rating >= 4 || !review.externalSendAllowed || review.approvalState === 'approved', {
    message: 'Low-rating review send requires approved state',
    path: ['externalSendAllowed'],
  })
  .refine((review) => review.approvalState !== 'approved' || (review.approvalActorId !== null && review.approvedAt !== null), {
    message: 'Approved review requires approval actor and timestamp',
    path: ['approvalActorId'],
  })
  .refine((review) => review.sendState !== 'ready_to_send' && review.sendState !== 'sent' || review.externalSendAllowed, {
    message: 'Ready or sent reviews must pass external send gate',
    path: ['sendState'],
  })
export type AiReviewApproval = z.infer<typeof AiReviewApprovalSchema>

export const ReportSourceStateEnvelopeSchema = SourceStateBaseSchema.superRefine(validateSourceStateConsistency)
export type ReportSourceStateEnvelope = z.infer<typeof ReportSourceStateEnvelopeSchema>
