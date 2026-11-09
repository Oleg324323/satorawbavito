import { z } from 'zod'
import { SourceEvidenceSchema, SourceStatusSchema } from '../wb-contracts/sourceState'

// Источник правды для handoff (Zod → OpenAPI → Pydantic).
// См. docs/api-contracts/README.md и tooling.md.

export const SkuStatus = z.enum(['auto', 'manual', 'warmup', 'liquidation'])
export type SkuStatus = z.infer<typeof SkuStatus>

export const BasketNormMode = z.enum(['auto', 'manual'])
export type BasketNormMode = z.infer<typeof BasketNormMode>

export const BasketNormSource = z.enum(['manual', 'auto', 'fallback'])
export type BasketNormSource = z.infer<typeof BasketNormSource>

export const AbcCode = z.enum(['AA', 'AB', 'AC', 'BA', 'BB', 'BC', 'CA', 'CB', 'CC'])
export type AbcCode = z.infer<typeof AbcCode>

export const PromotionStatus = z.enum(['yes', 'no', 'unknown'])
export type PromotionStatus = z.infer<typeof PromotionStatus>

export const ManagerAssignmentSource = z.enum(['manual', 'xlsx', 'none'])
export type ManagerAssignmentSource = z.infer<typeof ManagerAssignmentSource>

export const AuditActorSchema = z.object({
  id: z.string(),
  name: z.string(),
  role: z.enum(['system', 'manager', 'finance', 'admin']),
})
export type AuditActor = z.infer<typeof AuditActorSchema>

export const ActionSourceSchema = z.enum(['system', 'manager', 'bulk', 'wb_sync'])
export type ActionSource = z.infer<typeof ActionSourceSchema>

export const ActionScopeSchema = z.enum(['sku', 'bulk'])
export type ActionScope = z.infer<typeof ActionScopeSchema>

export const ActionReasonSchema = z.object({
  text: z.string().trim().min(1, 'Укажите причину действия'),
  source: ActionSourceSchema,
})
export type ActionReason = z.infer<typeof ActionReasonSchema>

export const SkuCommentSchema = z.object({
  id: z.string(),
  sku: z.string(),
  author: AuditActorSchema,
  createdAt: z.string().datetime(),
  text: z.string().trim().min(1, 'Комментарий не может быть пустым'),
})
export type SkuComment = z.infer<typeof SkuCommentSchema>

export const CommentSummarySchema = z.object({
  count: z.number().int().nonnegative(),
  latestText: z.string().nullable(),
  latestAuthor: z.string().nullable(),
  latestAt: z.string().datetime().nullable(),
})
export type CommentSummary = z.infer<typeof CommentSummarySchema>

export const SkuAuditEventSchema = z.object({
  id: z.string(),
  sku: z.string(),
  createdAt: z.string().datetime(),
  actor: AuditActorSchema,
  source: ActionSourceSchema,
  scope: ActionScopeSchema,
  action: z.string(),
  reason: z.string().trim().optional(),
  oldValue: z.string().optional(),
  newValue: z.string().optional(),
})
  .refine((event) => event.source === 'system' || Boolean(event.reason?.trim()), {
    message: 'Для ручных и массовых действий нужна причина',
    path: ['reason'],
  })
export type SkuAuditEvent = z.infer<typeof SkuAuditEventSchema>

// Режим ценообразования. Описание стратегий в ТЗ/TZ-WB.md v1.1.
export const RepricerMode = z.enum(['baskets', 'revenue'])
export type RepricerMode = z.infer<typeof RepricerMode>

export const SkuSettingsFormSchema = z
  .object({
    cogsKopecks: z.number().int().positive(),
    wbCommissionPct: z.number().min(0).max(50),
    logisticsKopecks: z.number().int().nonnegative(),
    pMinKopecks: z.number().int().nonnegative().optional(),
    minMarginPct: z.number().min(0),
    pMaxKopecks: z.number().int().positive(),
    priceStepPct: z.number().min(0).max(50).optional(),
    priceStepMinutes: z.number().int().min(5).max(10080).optional(),
    priceStepHours: z.number().int().min(1).max(168).optional(),
    rrpKopecks: z.number().int().nonnegative().optional(),
    allowNegativeMargin: z.boolean(),
    automationEnabled: z.boolean(),
    basketNormMode: BasketNormMode,
    basketNormManual: z.number().int().nonnegative().nullable(),
    repricerMode: RepricerMode.default('baskets'),
    // Период сравнения для режима «Динамика выручки» (в днях). Уточняется с Марией.
    revenueComparisonDays: z.number().int().min(1).max(30).default(7),
    nightMedianEnabled: z.boolean().default(true),
    promoBoostEnabled: z.boolean().default(false),
    promoBoostPct: z.number().min(0).max(100).default(25),
    promoBoostHours: z.number().int().min(1).max(336).default(48),
  })
  .refine((s) => s.wbCommissionPct + s.minMarginPct < 100, {
    message: 'Комиссия + мин. маржа должны быть < 100%',
    path: ['minMarginPct'],
  })
  .refine((s) => s.basketNormMode !== 'manual' || s.basketNormManual !== null, {
    message: 'Для ручной нормы укажите число',
    path: ['basketNormManual'],
  })

export type SkuSettingsForm = z.infer<typeof SkuSettingsFormSchema>

export const SkuMetaSchema = z.object({
  articleId: z.string(),
  nmId: z.number().int().positive().optional(),
  name: z.string(),
  status: SkuStatus,
  currentPriceKopecks: z.number().int().positive(),
  basketsLast7d: z.number().int().nonnegative(),
  basketNorm: z.number().int().nonnegative(),
  basketNormSource: BasketNormSource,
  warmupDaysLeft: z.number().int().nonnegative().nullable(),
  lastSavedAt: z.string().datetime().nullable(),
  managerId: z.string().nullable().default(null),
  managerName: z.string().default('Без ответственного'),
  assignmentSource: ManagerAssignmentSource.default('none'),
  assignedAt: z.string().datetime().nullable().default(null),
})

export type SkuMeta = z.infer<typeof SkuMetaSchema>

export const SkuAnalyticsSummarySchema = z.object({
  abcCode: AbcCode,
  productStatus: z.string(),
  promotionStatus: PromotionStatus,
  promotionStatusText: z.string().nullable().optional(),
  promotionName: z.string().nullable().optional(),
  promotionId: z.union([z.string(), z.number()]).nullable().optional(),
  promotionStatusScope: z.literal('regular').nullable().optional(),
  promotionStatusSource: z.string().nullable().optional(),
  promotionStatusReason: z.string().nullable().optional(),
  promotionStatusObservedAt: z.string().nullable().optional(),
  promotionStatusExpiresAt: z.string().nullable().optional(),
  wbStockUnits: z.number().int().nonnegative(),
  buyoutPct: z.number().nonnegative(),
  baskets: z.number().int().nonnegative(),
  ordersUnits: z.number().int().nonnegative(),
  cancelledOrdersUnits: z.number().int().nonnegative().optional(),
  avgPriceWithSppKopecks: z.number().int().nonnegative(),
  marginPct: z.number(),
  marginKopecks: z.number().int(),
  wbCommissionPct: z.number().nonnegative(),
})
export type SkuAnalyticsSummary = z.infer<typeof SkuAnalyticsSummarySchema>

export const SkuSettingsResponseSchema = z.object({
  meta: SkuMetaSchema,
  settings: SkuSettingsFormSchema,
  analytics: SkuAnalyticsSummarySchema.optional(),
  commentSummary: CommentSummarySchema.optional(),
  comments: z.array(SkuCommentSchema).default([]),
  auditEvents: z.array(SkuAuditEventSchema).default([]),
  cachedAt: z.string().datetime().nullable(),
})

export type SkuSettingsResponse = z.infer<typeof SkuSettingsResponseSchema>

export const ErrorResponseSchema = z.object({
  error: z.object({
    code: z.string(),
    message: z.string(),
  }),
})

// ── Журнал изменений цен ──────────────────────────────────────────────────

export const PriceChangeTrigger = z.enum([
  'algorithm',
  'night_median_up',
  'night_median_restore',
  'manual',
  'liquidation',
  'warmup_end',
  'wb_sync',
])
export type PriceChangeTrigger = z.infer<typeof PriceChangeTrigger>

export const PriceChangeEntrySchema = z.object({
  id: z.string(),
  articleId: z.string(),
  skuName: z.string(),
  timestamp: z.string().datetime(),
  oldPriceKopecks: z.number().int().positive(),
  newPriceKopecks: z.number().int().positive(),
  changePct: z.number(),
  trigger: PriceChangeTrigger,
  marginAfterPct: z.number(),
  actor: AuditActorSchema,
  source: ActionSourceSchema,
  scope: ActionScopeSchema,
  reason: z.string().trim().optional(),
  oldValue: z.string().optional(),
  newValue: z.string().optional(),
})
  .refine((entry) => entry.source === 'system' || Boolean(entry.reason?.trim()), {
    message: 'Для ручных и массовых изменений нужна причина',
    path: ['reason'],
  })
export type PriceChangeEntry = z.infer<typeof PriceChangeEntrySchema>

export const ChangelogResponseSchema = z.object({
  items: z.array(PriceChangeEntrySchema),
  total: z.number().int().nonnegative(),
})

export const PricingStatusProgressSchema = z.discriminatedUnion('kind', [
  z.object({
    kind: z.literal('liquidation'),
    currentDay: z.number().int().positive(),
    totalDays: z.number().int().positive(),
    startedAt: z.string().datetime().nullable().optional(),
    nextStepAt: z.string().datetime().nullable().optional(),
    stepPct: z.number(),
    startPriceKopecks: z.number().int().nonnegative(),
    currentPriceKopecks: z.number().int().nonnegative(),
    targetPriceKopecks: z.number().int().nonnegative(),
    requiresNegativeMarginConfirm: z.boolean(),
  }),
  z.object({
    kind: z.literal('warmup'),
    currentDay: z.number().int().positive(),
    totalDays: z.number().int().positive(),
    daysLeft: z.number().int().nonnegative(),
    nextStepAt: z.string().datetime().nullable().optional(),
  }),
  z.object({
    kind: z.literal('pricing'),
    currentDay: z.number().int().positive().nullable(),
    totalDays: z.number().int().positive().nullable(),
    assignedAt: z.string().datetime().nullable().optional(),
    nextStepAt: z.string().datetime().nullable().optional(),
    syncIntervalHours: z.number().int().positive(),
    syncIntervalMinutes: z.number().int().positive().optional(),
  }),
])
export type PricingStatusProgress = z.infer<typeof PricingStatusProgressSchema>

export const PricingStatusResponseSchema = z.object({
  articleId: z.string(),
  skuName: z.string(),
  stage: z.enum(['active_liquidation', 'liquidation_pending', 'warmup', 'manual_hold', 'active_pricing']),
  status: SkuStatus,
  strategy: z.object({
    id: z.string().nullable().optional(),
    name: z.string().nullable().optional(),
    type: z.string().nullable().optional(),
    typedStrategyId: z.string().nullable().optional(),
    assignmentSource: z.string().nullable().optional(),
    assignedAt: z.string().datetime().nullable().optional(),
    rules: z.array(z.object({
      label: z.string(),
      value: z.string(),
    })).default([]),
  }),
  progress: PricingStatusProgressSchema,
  current: z.object({
    priceKopecks: z.number().int().nonnegative(),
    pMinKopecks: z.number().int().nonnegative(),
    pMaxKopecks: z.number().int().nonnegative(),
    marginPct: z.number().nullable().optional(),
    basketsLast7d: z.number().int().nonnegative(),
    basketNorm: z.number().int().nonnegative(),
    ordersUnits: z.number().nullable().optional(),
    stockUnits: z.number().nullable().optional(),
  }),
  lastDecision: z.object({
    timestamp: z.string().datetime().nullable().optional(),
    trigger: PriceChangeTrigger.nullable().optional(),
    reason: z.string().nullable().optional(),
    oldPriceKopecks: z.number().int().positive().nullable().optional(),
    newPriceKopecks: z.number().int().positive().nullable().optional(),
    changePct: z.number().nullable().optional(),
  }),
  recentChanges: z.array(PriceChangeEntrySchema),
  changelogTotal: z.number().int().nonnegative(),
  lastExecution: z.object({
    runId: z.string().nullable().optional(),
    trigger: z.string().nullable().optional(),
    createdAt: z.string().datetime().nullable().optional(),
    status: z.string().nullable().optional(),
    skipReason: z.string().nullable().optional(),
    frontendStrategyId: z.string().nullable().optional(),
    strategyId: z.string().nullable().optional(),
    explanation: z.string().nullable().optional(),
    applyState: z.string().nullable().optional(),
    draftId: z.string().nullable().optional(),
    jobId: z.string().nullable().optional(),
    oldPriceKopecks: z.number().int().nullable().optional(),
    recommendedPriceKopecks: z.number().int().nullable().optional(),
    deltaKopecks: z.number().int().nullable().optional(),
    blockedReasons: z.array(z.string()).default([]),
    blockerDetails: z.array(z.object({
      code: z.string().optional(),
      message: z.string().optional(),
      observedValue: z.unknown().optional(),
      threshold: z.unknown().optional(),
    }).passthrough()).default([]),
  }).nullable().optional(),
})
export type PricingStatusResponse = z.infer<typeof PricingStatusResponseSchema>

// ── Реальные дневные ряды SKU для drawer ─────────────────────────────────

const NullableNumberSchema = z.number().nullable().optional()
const NullableIntSchema = z.number().int().nullable().optional()

export const SkuTimeseriesSourceSchema = z.object({
  source: z.string(),
  status: z.string(),
  rows: z.number().int().nonnegative(),
  message: z.string().nullable().optional(),
})
export type SkuTimeseriesSource = z.infer<typeof SkuTimeseriesSourceSchema>

export const SkuDailyTimeseriesPointSchema = z.object({
  date: z.string(),
  ordersUnits: NullableIntSchema,
  cancelledOrdersUnits: NullableIntSchema,
  salesUnits: NullableIntSchema,
  returnsUnits: NullableIntSchema,
  revenueKopecks: NullableIntSchema,
  avgPriceWithSppKopecks: NullableIntSchema,
  avgSellerPriceKopecks: NullableIntSchema,
  buyoutPct: NullableNumberSchema,
  openCount: NullableIntSchema,
  cartCount: NullableIntSchema,
  funnelOrderCount: NullableIntSchema,
  crPct: NullableNumberSchema,
  funnelBuyoutPct: NullableNumberSchema,
  cartToOrderPct: NullableNumberSchema,
  adSpendKopecks: NullableIntSchema,
  adImpressions: NullableIntSchema,
  adClicks: NullableIntSchema,
  adCartAdds: NullableIntSchema,
  adOrders: NullableIntSchema,
  adRevenueKopecks: NullableIntSchema,
  drrPct: NullableNumberSchema,
  stockUnits: NullableIntSchema,
  inWayToClient: NullableIntSchema,
  inWayFromClient: NullableIntSchema,
  warehouses: NullableIntSchema,
  stockSourceGranularity: z.string().nullable().optional(),
  priceKopecks: NullableIntSchema,
  adsSourceGranularity: z.string().nullable().optional(),
})
export type SkuDailyTimeseriesPoint = z.infer<typeof SkuDailyTimeseriesPointSchema>

export const SkuHourlyOrdersPointSchema = z.object({
  hour: z.number().int().min(0).max(23),
  ordersUnits: z.number().int().nonnegative(),
  revenueKopecks: z.number().int().nonnegative(),
})
export type SkuHourlyOrdersPoint = z.infer<typeof SkuHourlyOrdersPointSchema>

export const SkuPriceEventSchema = z.object({
  timestamp: z.string().nullable().optional(),
  date: z.string().nullable().optional(),
  oldPriceKopecks: NullableIntSchema,
  newPriceKopecks: NullableIntSchema,
  trigger: z.string().nullable().optional(),
  reason: z.string().nullable().optional(),
})
export type SkuPriceEvent = z.infer<typeof SkuPriceEventSchema>

export const SkuTimeseriesResponseSchema = z.object({
  articleId: z.string(),
  nmId: z.number().int().nullable().optional(),
  skuName: z.string().nullable().optional(),
  period: z.object({
    days: z.number().int().positive().optional(),
    startDate: z.string().optional(),
    endDate: z.string().optional(),
  }).passthrough(),
  sourceStatus: z.string(),
  sources: z.array(SkuTimeseriesSourceSchema),
  daily: z.array(SkuDailyTimeseriesPointSchema),
  hourlyOrders: z.array(SkuHourlyOrdersPointSchema),
  priceEvents: z.array(SkuPriceEventSchema),
  currentPriceKopecks: z.number().int().nonnegative().nullable().optional(),
  rawRows: z.unknown().nullable().optional(),
})
export type SkuTimeseriesResponse = z.infer<typeof SkuTimeseriesResponseSchema>

export const AddSkuCommentRequestSchema = z.object({
  text: z.string().trim().min(1, 'Комментарий не может быть пустым'),
})
export type AddSkuCommentRequest = z.infer<typeof AddSkuCommentRequestSchema>

export const ManualPriceActionRequestSchema = z.object({
  newPriceKopecks: z.number().int().positive(),
  reason: z.string().trim().min(1, 'Укажите причину изменения цены'),
})
export type ManualPriceActionRequest = z.infer<typeof ManualPriceActionRequestSchema>

export const BulkRepricerActionRequestSchema = z.object({
  skuIds: z.array(z.string()).min(1),
  action: z.enum(['strategy', 'p_min', 'manual_mode', 'liquidation']),
  reason: z.string().trim().min(1, 'Укажите причину массового действия'),
})
export type BulkRepricerActionRequest = z.infer<typeof BulkRepricerActionRequestSchema>

// ── SPP guard / freeze-state по итогам созвона 19.05 ─────────────────────

export const SppSnapshotSchema = z.object({
  sourceStatus: SourceStatusSchema,
  sppPct: z.number().min(0).max(100).nullable(),
  buyerPriceKopecks: z.number().int().positive().nullable(),
  capturedAt: z.string().datetime().nullable(),
  blockerIds: z.array(z.string().min(1)),
})
  .refine((snapshot) => snapshot.sourceStatus !== 'blocked' && snapshot.sourceStatus !== 'unknown' || snapshot.blockerIds.length > 0, {
    message: 'Blocked or unknown SPP snapshot must reference blocker IDs',
    path: ['blockerIds'],
  })
export type SppSnapshot = z.infer<typeof SppSnapshotSchema>

export const PriceGuardTriggerSchema = z.object({
  type: z.enum(['stock_jump', 'spp_jump', 'cogs_jump', 'seller_price_jump', 'source_stale']),
  severity: z.enum(['info', 'warning', 'blocker']),
  observedValue: z.union([z.number(), z.string()]).nullable(),
  previousValue: z.union([z.number(), z.string()]).nullable(),
  threshold: z.union([z.number(), z.string()]).nullable(),
  message: z.string().min(1),
})
export type PriceGuardTrigger = z.infer<typeof PriceGuardTriggerSchema>

export const PriceGuardResponseSchema = z.object({
  articleId: z.string().min(1),
  currentPriceKopecks: z.number().int().positive().nullable(),
  buyerPriceKopecks: z.number().int().positive().nullable(),
  sppSnapshot: SppSnapshotSchema.nullable(),
  recommendedSellerPriceKopecks: z.number().int().positive().nullable(),
  lastKnownGoodPriceKopecks: z.number().int().positive().nullable(),
  canApply: z.boolean(),
  blockedReason: z.string().nullable(),
  freezeState: z.enum(['none', 'frozen', 'requires_review', 'source_blocked']),
  guardTriggers: z.array(PriceGuardTriggerSchema),
  sourceEvidence: z.array(SourceEvidenceSchema),
  blockerIds: z.array(z.string().min(1)),
})
  .refine((guard) => guard.canApply || guard.blockedReason !== null || guard.blockerIds.length > 0, {
    message: 'Blocked price guard must explain why apply is unavailable',
    path: ['blockedReason'],
  })
  .refine((guard) => guard.canApply || guard.freezeState !== 'none', {
    message: 'Blocked price guard must expose a non-none freeze state',
    path: ['freezeState'],
  })
  .refine((guard) => !guard.canApply || guard.blockedReason === null, {
    message: 'Applicable price guard cannot carry blockedReason',
    path: ['blockedReason'],
  })
  .refine((guard) => !guard.canApply || guard.freezeState === 'none', {
    message: 'Applicable price guard must have freezeState none',
    path: ['freezeState'],
  })
  .refine((guard) => !guard.canApply || guard.blockerIds.length === 0, {
    message: 'Applicable price guard cannot carry blocker IDs',
    path: ['blockerIds'],
  })
  .refine((guard) => !guard.canApply || guard.guardTriggers.every((trigger) => trigger.severity !== 'blocker'), {
    message: 'Applicable price guard cannot carry blocker triggers',
    path: ['guardTriggers'],
  })
  .refine((guard) => !guard.canApply || guard.sppSnapshot === null || (guard.sppSnapshot.sourceStatus === 'fresh' && guard.sppSnapshot.blockerIds.length === 0), {
    message: 'Applicable SPP-aware guard requires fresh SPP snapshot without blockers',
    path: ['sppSnapshot'],
  })
export type PriceGuardResponse = z.infer<typeof PriceGuardResponseSchema>
