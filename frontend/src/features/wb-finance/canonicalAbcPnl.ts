import { z } from 'zod'

import { ApiError, apiRequest } from '@/lib/api'

const isoDate = z.string().regex(/^\d{4}-\d{2}-\d{2}$/)
const utcDateTime = z.string().datetime({ offset: true })
const nullableInteger = z.number().int().safe().nullable()

const periodSchema = z.object({
  dateFrom: isoDate,
  dateTo: isoDate,
  timezone: z.literal('Europe/Moscow'),
  startAt: utcDateTime,
  endExclusiveAt: utcDateTime,
  days: z.number().int().safe().min(1).max(90),
  temporalState: z.enum(['complete', 'partial', 'future']),
}).strict()

const snapshotSchema = z.object({
  syncRunId: z.string(),
  snapshotChecksum: z.string(),
  formulaVersion: z.string(),
  operationCount: z.number().int().safe().nonnegative(),
  capturedAt: utcDateTime,
  lastObservedAt: utcDateTime,
}).strict()

const pnlExpenseSchema = z.object({ penaltyKopecks: nullableInteger, acceptanceKopecks: nullableInteger,
  state: z.enum(['confirmed', 'partial', 'unavailable', 'stale']) }).strict()

const rowSchema = z.object({
  pnlExpenses: pnlExpenseSchema.nullable().optional(),
  nmId: z.number().int().safe().positive().nullable(),
  sellerArticle: z.string().nullable(),
  sppPct: z.number().min(0).max(100).nullable().optional(),
  sppBuyerPriceKopecks: nullableInteger.optional(),
  sppObservedOn: isoDate.nullable().optional(),
  sppSource: z.string().nullable().optional(),
  sppHistory: z.array(z.object({ date: isoDate, pct: z.number().min(0).max(100).nullable() }).strict()).optional(),
  catalogSkuId: z.number().int().safe().positive().nullable(),
  operationCount: z.number().int().safe().nonnegative(),
  revenueKopecks: z.number().int().safe(),
  salesRevenueKopecks: z.number().int().safe(),
  returnsRevenueKopecks: z.number().int().safe(),
  mainRevenueKopecks: z.number().int().safe(),
  redemptionsRevenueKopecks: z.number().int().safe(),
  lateCorrectionRevenueKopecks: z.number().int().safe(),
  unknownRevenueKopecks: z.number().int().safe(),
  salesUnits: z.number().int().safe().nonnegative(),
  returnsUnits: z.number().int().safe().nonnegative(),
  netUnits: z.number().int().safe(),
  commissionKopecks: z.number().int().safe(),
  logisticsKopecks: z.number().int().safe(),
  storageKopecks: z.number().int().safe(),
  acceptanceKopecks: z.number().int().safe(),
  penaltyKopecks: z.number().int().safe(),
  deductionKopecks: z.number().int().safe(),
  additionalPaymentKopecks: z.number().int().safe(),
  financeOtherExpensesKopecks: z.number().int().safe().nonnegative(),
  compensationKopecks: z.number().int().safe(),
  acquiringKopecks: z.number().int().safe(),
  financeExpensesKopecks: z.number().int().safe(),
  costValueState: z.enum(['configured', 'assumed', 'missing']),
  costEvidenceStatus: z.enum(['dated', 'undated', 'period_end_fallback']).nullable(),
  cogsKopecks: nullableInteger,
  settlementProfitKopecks: nullableInteger,
  economicsValueState: z.enum(['configured', 'assumed', 'missing']),
  economicsEvidenceStatus: z.enum(['dated', 'undated', 'period_end_fallback']).nullable(),
  taxKopecks: nullableInteger,
  otherExpensesKopecks: nullableInteger,
  profitBeforeAdsAndLoyaltyKopecks: nullableInteger,
  advertisingSpendKopecks: nullableInteger,
  advertisingSpendSource: z.literal('wb_finance_report').optional(),
  profitBeforeLoyaltyKopecks: nullableInteger,
  cashbackAmountKopecks: nullableInteger,
  cashbackDiscountKopecks: nullableInteger,
  cashbackCommissionChangeKopecks: nullableInteger,
  loyaltyNetCostKopecks: nullableInteger,
  profitAfterLoyaltyKopecks: nullableInteger,
  profitBeforeInternalExpensesKopecks: nullableInteger.optional().default(null),
  internalExpensesKopecks: nullableInteger.optional().default(null),
  salesClass: z.enum(['A', 'B', 'C']).nullable(),
  profitClass: z.literal(null),
  abcCode: z.literal(null),
  netProfitKopecks: nullableInteger,
  blockerIds: z.array(z.string()),
}).strict()

const summarySchema = z.object({
  operationCount: z.number().int().safe().nonnegative(),
  skuCount: z.number().int().safe().nonnegative(),
  revenueKopecks: z.number().int().safe(),
  salesRevenueKopecks: z.number().int().safe(),
  returnsRevenueKopecks: z.number().int().safe(),
  salesUnits: z.number().int().safe().nonnegative(),
  returnsUnits: z.number().int().safe().nonnegative(),
  netUnits: z.number().int().safe(),
  commissionKopecks: z.number().int().safe(),
  logisticsKopecks: z.number().int().safe(),
  storageKopecks: z.number().int().safe(),
  acceptanceKopecks: z.number().int().safe(),
  penaltyKopecks: z.number().int().safe(),
  deductionKopecks: z.number().int().safe(),
  financeOtherExpensesKopecks: z.number().int().safe().nonnegative(),
  compensationKopecks: z.number().int().safe(),
  acquiringKopecks: z.number().int().safe(),
  financeExpensesKopecks: z.number().int().safe(),
  cogsKopecks: nullableInteger,
  settlementProfitKopecks: nullableInteger,
  taxKopecks: nullableInteger,
  otherExpensesKopecks: nullableInteger,
  profitBeforeAdsAndLoyaltyKopecks: nullableInteger,
  advertisingSpendKopecks: nullableInteger,
  advertisingSpendSource: z.literal('wb_finance_report').optional(),
  unattributedAdvertisingSpendKopecks: nullableInteger,
  profitBeforeLoyaltyKopecks: nullableInteger,
  cashbackAmountKopecks: nullableInteger,
  cashbackDiscountKopecks: nullableInteger,
  cashbackCommissionChangeKopecks: nullableInteger,
  loyaltyNetCostKopecks: nullableInteger,
  profitAfterLoyaltyKopecks: nullableInteger,
  profitBeforeInternalExpensesKopecks: nullableInteger.optional().default(null),
  internalExpensesKopecks: nullableInteger.optional().default(null),
  netProfitKopecks: nullableInteger,
}).strict()

const metaSchema = z.object({
  pnlExpenseRevision: z.string().nullable().optional(),
  pnlExpenseRows: z.array(pnlExpenseSchema.extend({ nmId: z.number().int().safe().positive().nullable() })).nullable().optional(),
  state: z.enum(['ready', 'partial', 'future', 'empty', 'missing']),
  marketplaceAccountId: z.number().int().safe().positive(),
  period: periodSchema,
  snapshot: snapshotSchema.nullable(),
  formulaVersion: z.literal('wb-abc-pnl-finance-rrdate-v4'),
  costLedgerRevision: z.number().int().safe().nonnegative(),
  economicsRevision: z.number().int().safe().nonnegative(),
  advertisingSource: z.enum(['finance_promotion', 'ads_fullstats']).nullable(),
  advertisingSnapshotChecksum: z.string().nullable(),
  advertisingEvidenceStatus: z.enum(['raw', 'aggregate_only', 'derived_legacy']).nullable(),
  blockerIds: z.array(z.string()),
}).strict()

const pageSchema = z.object({
  items: z.array(rowSchema),
  total: z.number().int().safe().nonnegative(),
  limit: z.number().int().safe().min(1).max(500),
  offset: z.number().int().safe().nonnegative(),
  summary: summarySchema,
  meta: metaSchema,
  timestamp: utcDateTime,
}).strict()

export type CanonicalAbcPnlPage = z.infer<typeof pageSchema>
export type CanonicalAbcPnlRow = z.infer<typeof rowSchema>
export type CanonicalAbcPnlState = CanonicalAbcPnlPage['meta']['state']

export type CanonicalAbcPnlRollout = {
  organizationId: number
  marketplaceAccountId: number
}

export type CanonicalAbcPnlSurface = 'abc' | 'pnl'
export type CanonicalPnlMode = 'financial' | 'operational'

export function shouldUseCanonicalAbcPnl(
  rollout: CanonicalAbcPnlRollout | null,
  surface: CanonicalAbcPnlSurface,
  pnlMode: CanonicalPnlMode = 'financial',
) {
  return rollout !== null && (surface === 'abc' || pnlMode === 'financial')
}

export function canonicalPnlRowStatus(row: {
  sourceStatus?: string | null
  confidence?: string | null
  blockerIds?: string[] | null
  expenseState?: string | null
}) {
  if (row.expenseState && row.expenseState !== 'confirmed') return 'частично'
  if (row.blockerIds?.length || row.confidence === 'blocked' || row.sourceStatus === 'partial') return 'частично'
  return row.sourceStatus === 'ready' || row.confidence === 'canonical' ? 'подтверждено' : 'нет данных'
}

export function canonicalAbcPnlEmptyMessage(
  surface: CanonicalAbcPnlSurface,
  state: CanonicalAbcPnlState | null | undefined,
) {
  if (state === 'future') {
    return surface === 'abc'
      ? 'Выбранный период находится в будущем. Canonical API не возвращает расчетные строки.'
      : 'Canonical API не рассчитывает финансовые строки для будущего периода.'
  }
  if (state === 'missing') {
    return surface === 'abc'
      ? 'Для выбранного аккаунта и периода еще нет canonical finance snapshot.'
      : 'Для выбранного аккаунта и периода нет canonical finance snapshot.'
  }
  if (state === 'empty') return 'Canonical snapshot готов, но финансовых операций за период нет.'
  return null
}

export type CanonicalPeriod = { fromIso: string; toIso: string }

export function resolveCanonicalAbcPnlRollout(
  organizationId: number | null | undefined,
  rawFlag = import.meta.env.VITE_CANONICAL_WB_ABC_PNL_ROLLOUT ?? '',
): CanonicalAbcPnlRollout | null {
  if (!Number.isSafeInteger(organizationId) || Number(organizationId) <= 0 || !rawFlag.trim()) return null
  const mappings = new Map<number, number>()
  for (const entry of rawFlag.split(',')) {
    const match = /^([1-9]\d*):([1-9]\d*)$/.exec(entry.trim())
    if (!match) return null
    const [, rawOrganizationId, rawMarketplaceAccountId] = match
    const candidateOrganizationId = Number(rawOrganizationId)
    const marketplaceAccountId = Number(rawMarketplaceAccountId)
    if (!Number.isSafeInteger(candidateOrganizationId) || !Number.isSafeInteger(marketplaceAccountId)) return null
    if (mappings.has(candidateOrganizationId)) return null
    mappings.set(candidateOrganizationId, marketplaceAccountId)
  }
  const marketplaceAccountId = mappings.get(organizationId as number)
  return marketplaceAccountId === undefined ? null : { organizationId: organizationId as number, marketplaceAccountId }
}

export function buildCanonicalAbcPnlPath(input: {
  marketplaceAccountId: number
  period: CanonicalPeriod
  limit: number
  offset: number
  includeSpp?: boolean
  includePnlExpenses?: boolean
}) {
  const params = new URLSearchParams({
    marketplaceAccountId: String(input.marketplaceAccountId),
    dateFrom: input.period.fromIso,
    dateTo: input.period.toIso,
    limit: String(input.limit),
    offset: String(input.offset),
  })
  if (input.includeSpp) params.set('includeSpp', 'true')
  if (input.includePnlExpenses) params.set('includePnlExpenses', 'true')
  return `/api/v2/wb/reports/abc-pnl?${params.toString()}`
}

export function parseCanonicalAbcPnlPage(payload: unknown): CanonicalAbcPnlPage {
  const result = pageSchema.safeParse(payload)
  if (!result.success) {
    throw new ApiError('Invalid canonical ABC/P&L response', 502, 'INVALID_API_RESPONSE', result.error.flatten())
  }
  for (const row of [result.data.summary, ...result.data.items]) {
    if (row.netProfitKopecks !== null && (row.profitBeforeInternalExpensesKopecks === null || row.internalExpensesKopecks === null || row.netProfitKopecks !== row.profitBeforeInternalExpensesKopecks - row.internalExpensesKopecks)) {
      throw new ApiError('Invalid canonical ABC/P&L response', 502, 'INVALID_API_RESPONSE')
    }
  }
  return result.data
}

export function previousCanonicalPeriod(period: CanonicalPeriod): CanonicalPeriod {
  const days = Math.round((Date.parse(period.toIso) - Date.parse(period.fromIso)) / 86400000) + 1
  const end = Date.parse(period.fromIso) - 86400000
  return { fromIso: new Date(end - (days - 1) * 86400000).toISOString().slice(0, 10), toIso: new Date(end).toISOString().slice(0, 10) }
}

type CanonicalRequest = (path: string, init?: RequestInit) => Promise<unknown>

function paginationIdentity(page: CanonicalAbcPnlPage) {
  return JSON.stringify({
    total: page.total,
    summary: page.summary,
    meta: page.meta,
  })
}

export async function fetchCanonicalAbcPnl(input: {
  accessToken: string
  marketplaceAccountId: number
  period: CanonicalPeriod
  signal?: AbortSignal
  pageSize?: number
  includeSpp?: boolean
  includePnlExpenses?: boolean
  request?: CanonicalRequest
}): Promise<CanonicalAbcPnlPage> {
  const request = input.request ?? apiRequest
  const pageSize = Math.min(500, Math.max(1, Math.trunc(input.pageSize ?? 500)))
  const pages: CanonicalAbcPnlPage[] = []
  // Canonical finance groups rows by nmId, including one nullable unknown bucket.
  const seenNmIds = new Set<number | null>()
  let offset = 0
  let identity: string | null = null

  while (pages.length === 0 || offset < pages[0].total) {
    input.signal?.throwIfAborted()
    const path = buildCanonicalAbcPnlPath({
      marketplaceAccountId: input.marketplaceAccountId,
      period: input.period,
      limit: pageSize,
      offset,
      includeSpp: input.includeSpp,
      includePnlExpenses: input.includePnlExpenses,
    })
    const payload = await request(path, {
      signal: input.signal,
      headers: { Authorization: `Bearer ${input.accessToken}` },
    })
    input.signal?.throwIfAborted()
    const page = parseCanonicalAbcPnlPage(payload)
    const periodMatchesRequest = page.meta.period.dateFrom === input.period.fromIso
      && page.meta.period.dateTo === input.period.toIso
    if (
      page.offset !== offset
      || page.limit !== pageSize
      || page.meta.marketplaceAccountId !== input.marketplaceAccountId
      || !periodMatchesRequest
      || page.items.length > pageSize
    ) {
      throw new ApiError('Canonical ABC/P&L pagination drift', 409, 'CANONICAL_PAGINATION_DRIFT')
    }
    const currentIdentity = paginationIdentity(page)
    if (identity !== null && currentIdentity !== identity) {
      throw new ApiError('Canonical ABC/P&L pagination drift', 409, 'CANONICAL_PAGINATION_DRIFT')
    }
    identity = currentIdentity
    for (const item of page.items) {
      if (seenNmIds.has(item.nmId)) {
        throw new ApiError('Canonical ABC/P&L duplicate row', 409, 'CANONICAL_PAGINATION_DRIFT')
      }
      seenNmIds.add(item.nmId)
    }
    pages.push(page)
    if (page.items.length === 0) {
      if (offset < page.total) throw new ApiError('Canonical ABC/P&L pagination stalled', 409, 'CANONICAL_PAGINATION_DRIFT')
      break
    }
    offset += page.items.length
  }

  const first = pages[0]
  if (!first) throw new ApiError('Empty canonical ABC/P&L response', 502, 'INVALID_API_RESPONSE')
  const items = pages.flatMap((page) => page.items)
  if (items.length !== first.total) throw new ApiError('Canonical ABC/P&L pagination incomplete', 409, 'CANONICAL_PAGINATION_DRIFT')
  return { ...first, items, limit: pageSize, offset: 0 }
}

export type CanonicalCompatibilityMeta = {
  marketplaceAccountId: number
  state: CanonicalAbcPnlState
  formulaVersion: CanonicalAbcPnlPage['meta']['formulaVersion']
  advertisingSource: CanonicalAbcPnlPage['meta']['advertisingSource']
  advertisingEvidenceStatus: CanonicalAbcPnlPage['meta']['advertisingEvidenceStatus']
  blockerIds: string[]
  period: CanonicalAbcPnlPage['meta']['period']
  snapshot: CanonicalAbcPnlPage['meta']['snapshot']
  advertisingSnapshotChecksum: string | null
  costLedgerRevision: number
  economicsRevision: number
}

function compatibilityMeta(page: CanonicalAbcPnlPage): CanonicalCompatibilityMeta {
  return {
    marketplaceAccountId: page.meta.marketplaceAccountId,
    state: page.meta.state,
    formulaVersion: page.meta.formulaVersion,
    advertisingSource: page.meta.advertisingSource,
    advertisingEvidenceStatus: page.meta.advertisingEvidenceStatus,
    blockerIds: page.meta.blockerIds,
    period: page.meta.period,
    snapshot: page.meta.snapshot,
    advertisingSnapshotChecksum: page.meta.advertisingSnapshotChecksum,
    costLedgerRevision: page.meta.costLedgerRevision,
    economicsRevision: page.meta.economicsRevision,
  }
}

export type AbcOperationalRow = {
  nmId?: string | number | null
  sku?: string | null
  photoUrl?: string | null
  productName?: string | null
  productStatus?: string | null
  managerId?: string | null
  manager?: string | null
  brand?: string | null
  category?: string | null
  priceBeforeSppKopecks?: number | null
  priceWithSppKopecks?: number | null
  impressions?: number | null
  clicks?: number | null
  clicksDeltaPct?: number | null
  ctrPct?: number | null
  baskets?: number | null
  basketsDeltaPct?: number | null
  cartCrPct?: number | null
  ordersComposite?: { units?: number | null; kopecks?: number | null; deltaPct?: number | null } | null
  ktrIndex?: number | null
  localizationPct?: number | null
  wbStockUnits?: number | null
  wbStockKopecks?: number | null
  promotionStatus?: string | null
  promotionStatusText?: string | null
  promotionName?: string | null
  buyoutPct?: number | null
  ruleEvaluation?: { status?: 'unknown' | 'risk' | 'opportunity' | 'normal' | null } | null
}

export function adaptCanonicalAbcReport(page: CanonicalAbcPnlPage, operationalRows: AbcOperationalRow[] = [], previous?: CanonicalAbcPnlPage) {
  const previousByNm = new Map(previous?.items.map((item) => [item.nmId, item]) ?? [])
  const byNm = new Map(operationalRows.filter((row) => Number(row.nmId) > 0).map((row) => [Number(row.nmId), row]))
  return {
    rows: page.items.map((item) => {
      const operational = item.nmId === null ? undefined : byNm.get(item.nmId)
      const prior = previousByNm.get(item.nmId)
      const delta = (value: number | null, before: number | null | undefined) => value !== null && before != null && before !== 0 ? (value - before) / Math.abs(before) * 100 : null
      const costDelta = (value: number | null, before: number | null | undefined) => value !== null && before != null && prior && prior.revenueKopecks > 0 && item.revenueKopecks > 0 ? (value / item.revenueKopecks - before / prior.revenueKopecks) * 100 : null
      const revenuePct = (value: number | null) => value !== null && item.revenueKopecks > 0 ? value / item.revenueKopecks * 100 : null
      return {
        // Only operational fields may come from the supplemental report; finance stays canonical.
        operationalAvailable: !!operational,
        sku: operational?.sku ?? item.sellerArticle,
        nmId: item.nmId,
        photoUrl: operational?.photoUrl,
        productName: operational?.productName,
        productStatus: operational?.productStatus,
        managerId: operational?.managerId,
        manager: operational?.manager,
        brand: operational?.brand,
        category: operational?.category,
        priceBeforeSppKopecks: operational?.priceBeforeSppKopecks,
        priceWithSppKopecks: item.sppSource === 'current_buyer_price' ? item.sppBuyerPriceKopecks : operational?.priceWithSppKopecks,
        impressions: operational?.impressions,
        clicks: operational?.clicks,
        clicksDeltaPct: operational?.clicksDeltaPct,
        ctrPct: operational?.ctrPct,
        baskets: operational?.baskets,
        basketsDeltaPct: operational?.basketsDeltaPct,
        cartCrPct: operational?.cartCrPct,
        ordersComposite: operational?.ordersComposite,
        ktrIndex: operational?.ktrIndex,
        localizationPct: operational?.localizationPct,
        wbStockUnits: operational?.wbStockUnits,
        wbStockKopecks: operational?.wbStockKopecks ?? (operational?.wbStockUnits != null && item.sppSource === 'current_buyer_price' && item.sppBuyerPriceKopecks != null ? operational.wbStockUnits * item.sppBuyerPriceKopecks : null),
        promotionStatus: operational?.promotionStatus,
        promotionStatusText: operational?.promotionStatusText,
        promotionName: operational?.promotionName,
        buyoutPct: operational?.buyoutPct,
        ruleEvaluation: operational?.ruleEvaluation,
        sppPct: item.sppPct ?? null,
        sppBuyerPriceKopecks: item.sppBuyerPriceKopecks ?? null,
        sppObservedOn: item.sppObservedOn ?? null,
        sppSource: item.sppSource ?? null,
        sppHistory: item.sppHistory ?? [],
        cogsKopecks: item.cogsKopecks,
        cogsPerUnitKopecks: item.cogsKopecks !== null && item.netUnits > 0 ? Math.round(item.cogsKopecks / item.netUnits) : null,
        salesComposite: { units: item.netUnits, kopecks: item.revenueKopecks, deltaPct: delta(item.revenueKopecks, prior?.revenueKopecks) },
        adSpendKopecks: item.advertisingSpendKopecks,
        commissionKopecks: item.commissionKopecks,
        logisticsKopecks: item.logisticsKopecks,
        storageKopecks: item.storageKopecks,
        isUnallocated: item.nmId === null,
        drrSalesPct: revenuePct(item.advertisingSpendKopecks),
        logisticsCostPct: revenuePct(item.logisticsKopecks),
        commissionCostPct: revenuePct(item.commissionKopecks),
        storageCostPct: revenuePct(item.storageKopecks),
        logisticsDeltaPct: costDelta(item.logisticsKopecks, prior?.logisticsKopecks),
        commissionDeltaPct: costDelta(item.commissionKopecks, prior?.commissionKopecks),
        storageDeltaPct: costDelta(item.storageKopecks, prior?.storageKopecks),
        marginDeltaPct: costDelta(item.netProfitKopecks, prior?.netProfitKopecks),
        marginKopecks: item.netProfitKopecks !== null && item.netUnits > 0 ? Math.round(item.netProfitKopecks / item.netUnits) : null,
        marginPct: revenuePct(item.netProfitKopecks),
        profitAfterLoyaltyKopecks: item.profitAfterLoyaltyKopecks,
        profitBeforeInternalExpensesKopecks: item.profitBeforeInternalExpensesKopecks,
        internalExpensesKopecks: item.internalExpensesKopecks,
        netProfitKopecks: item.netProfitKopecks,
        salesClass: item.salesClass,
        abcCode: item.abcCode,
        costValueState: item.costValueState,
        costEvidenceStatus: item.costEvidenceStatus,
        economicsValueState: item.economicsValueState,
        economicsEvidenceStatus: item.economicsEvidenceStatus,
        blockerIds: item.blockerIds,
        canonicalSourceState: page.meta.state,
      }
    }),
    filteredSummary: {
      salesKopecks: page.summary.revenueKopecks,
      salesCount: page.summary.netUnits,
      profitAfterLoyaltyKopecks: page.summary.profitAfterLoyaltyKopecks,
      profitBeforeInternalExpensesKopecks: page.summary.profitBeforeInternalExpensesKopecks,
      internalExpensesKopecks: page.summary.internalExpensesKopecks,
      netProfitKopecks: page.summary.netProfitKopecks,
      attentionCount: page.items.filter((item) => item.blockerIds.length > 0).length,
    },
    sourceStatus: page.meta.state,
    blockerIds: page.meta.blockerIds,
    calculatedAt: page.timestamp,
    canonical: compatibilityMeta(page),
    canonicalSummary: page.summary,
  }
}

export function financeBlockerReasons(blockers: string[]) {
  const costs: Record<string, string> = {
    WB_PNL_COST_MISSING: 'Нет себестоимости для части дат продаж или возвратов',
    WB_PNL_DATED_COST_MISSING: 'Не найдена подтверждённая себестоимость для части операций',
    WB_PNL_COST_ASSUMED: 'Себестоимость отмечена как примерная, а не фактическая',
    WB_PNL_COST_EVIDENCE_UNDATED: 'Не подтверждён период действия себестоимости',
    WB_PNL_COST_MAPPING_MISSING: 'Товар не связан с записью себестоимости',
    WB_PNL_COST_MAPPING_AMBIGUOUS: 'У товара несколько неоднозначных привязок себестоимости',
    WB_PNL_COST_PRODUCT_UNATTRIBUTED: 'Часть продаж не привязана к товару — себестоимость определить нельзя',
    WB_PNL_COST_DAILY_COVERAGE_MISMATCH: 'Дневные количества операций не сходятся с итогом финансового отчёта',
  }
  return [...new Set(blockers.map((blocker) => {
    if (blocker === 'WB_USER_EXAMPLE_COSTS') return 'Примерная себестоимость — замените своей таблицей'
    if (costs[blocker]) return costs[blocker]
    if (blocker.includes('COST')) return 'Не подтверждена себестоимость части операций'
    if (blocker.includes('TAX')) return 'Не подтверждён налог за период'
    if (blocker.includes('ECONOMICS')) return 'Не подтверждён налог за период'
    if (blocker.includes('OPERATIONS_UNRECONCILED')) return 'Нужна сверка финансовых операций'
    if (blocker.includes('ADVERTISING_UNATTRIBUTED')) return 'Часть рекламы не связана с товаром'
    if (blocker.includes('ADS')) return 'Не загружена реклама за период'
    return 'Не все исходные данные подтверждены'
  }))]
}

export function adaptCanonicalPnlReport(page: CanonicalAbcPnlPage, operationalRows: AbcOperationalRow[] = []) {
  const byNm = new Map(operationalRows.filter((row) => Number(row.nmId) > 0).map((row) => [Number(row.nmId), row]))
  const rows = page.items.map((item) => ({
      label: item.nmId === null ? 'Нераспределённые расходы WB' : item.sellerArticle,
      articleId: item.sellerArticle,
      sku: item.sellerArticle,
      nmId: item.nmId,
      productName: item.nmId === null ? undefined : byNm.get(item.nmId)?.productName,
      photoUrl: item.nmId === null ? undefined : byNm.get(item.nmId)?.photoUrl,
      category: item.nmId === null ? undefined : byNm.get(item.nmId)?.category,
      managerId: item.nmId === null ? undefined : byNm.get(item.nmId)?.managerId,
      revenueKopecks: item.revenueKopecks,
      cogsKopecks: item.cogsKopecks,
      commissionKopecks: item.commissionKopecks,
      logisticsKopecks: item.logisticsKopecks,
      storageKopecks: item.storageKopecks,
      // The v4 immutable Finance row already carries these signed amounts.
      // Optional legacy enrichment must not hide or replace its evidence.
      acceptanceKopecks: item.acceptanceKopecks as number | null,
      penaltyKopecks: item.penaltyKopecks as number | null,
      expenseState: 'confirmed' as z.infer<typeof pnlExpenseSchema>['state'],
      deductionKopecks: item.deductionKopecks,
      financeOtherExpensesKopecks: item.financeOtherExpensesKopecks,
      compensationKopecks: item.compensationKopecks,
      acquiringKopecks: item.acquiringKopecks,
      financeExpensesKopecks: item.financeExpensesKopecks,
      settlementProfitKopecks: item.settlementProfitKopecks,
      taxKopecks: item.taxKopecks,
      otherExpensesKopecks: item.otherExpensesKopecks,
      profitBeforeAdsAndLoyaltyKopecks: item.profitBeforeAdsAndLoyaltyKopecks,
      profitBeforeLoyaltyKopecks: item.profitBeforeLoyaltyKopecks,
      loyaltyNetCostKopecks: item.loyaltyNetCostKopecks,
      adSpendKopecks: item.advertisingSpendKopecks,
      overheadKopecks: item.internalExpensesKopecks,
      profitBeforeInternalExpensesKopecks: item.profitBeforeInternalExpensesKopecks,
      internalExpensesKopecks: item.internalExpensesKopecks,
      returnsPenaltyKopecks: item.penaltyKopecks + item.deductionKopecks,
      netProfitKopecks: item.netProfitKopecks,
      profitAfterLoyaltyKopecks: item.profitAfterLoyaltyKopecks,
      marginPct: item.netProfitKopecks !== null && item.revenueKopecks > 0 ? item.netProfitKopecks / item.revenueKopecks * 100 : null,
      sourceStatus: item.netProfitKopecks !== null && item.blockerIds.length === 0 ? 'ready' : 'partial',
      confidence: item.blockerIds.length > 0 ? 'blocked' : 'canonical',
      comment: financeBlockerReasons(item.blockerIds).join(' · '),
      blockerIds: item.blockerIds,
    }))
  const existing = new Set(page.items.map(item => item.nmId))
  const feeOnlyRows: Partial<(typeof rows)[number]>[] = (page.meta.pnlExpenseRows ?? [])
    .filter(item => !existing.has(item.nmId)).map(item => ({
      nmId: item.nmId, label: item.nmId === null ? 'Нераспределённые расходы WB' : `WB ${item.nmId}`,
      penaltyKopecks: item.penaltyKopecks, acceptanceKopecks: item.acceptanceKopecks,
      expenseState: item.state, netProfitKopecks: null, sourceStatus: 'partial', confidence: 'blocked',
      comment: 'Расходы получены из операций WB; строка ещё не включена в основной финансовый снимок. Прибыль не рассчитана.',
    }))
  const expensesIncomplete = rows.some(item => item.expenseState !== 'confirmed') || feeOnlyRows.length > 0
  return {
    meta: {
      title: 'Canonical WB ABC/P&L',
      generatedAt: page.timestamp,
      freshnessState: page.meta.state,
      sourceType: page.meta.advertisingSource,
      dateRange: { from: page.meta.period.dateFrom, to: page.meta.period.dateTo },
    },
    headline: page.meta.state === 'partial' ? 'Для расчёта прибыли нужны дополнительные данные' : 'Финансовый отчёт WB',
    warning: [...financeBlockerReasons(page.meta.blockerIds), ...(expensesIncomplete ? ['Штрафы и приёмка: данные неполные, устарели или недоступны; отсутствующие суммы не означают ноль.'] : [])].join(' · ') || null,
    rows: [...rows, ...feeOnlyRows],
    canonical: compatibilityMeta(page),
    canonicalSummary: page.summary,
  }
}
