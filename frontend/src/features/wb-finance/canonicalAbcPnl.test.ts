import { afterEach, describe, expect, it, vi } from 'vitest'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { AuthContext, type AuthContextValue } from '@/features/auth/authContext'
import { AbcKpiStripIsland, installAbcLiveDataBridge, mapBackendAbcRowToParity, renderAbcRowHtml } from '@/features/vella-parity/VellaHtmlParityPage'

import {
  adaptCanonicalAbcReport,
  adaptCanonicalPnlReport,
  buildCanonicalAbcPnlPath,
  canonicalAbcPnlEmptyMessage,
  canonicalPnlRowStatus,
  fetchCanonicalAbcPnl,
  parseCanonicalAbcPnlPage,
  resolveCanonicalAbcPnlRollout,
  shouldUseCanonicalAbcPnl,
} from './canonicalAbcPnl'
import { ApiError } from '@/lib/api'

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.unstubAllEnvs()
})

function installBridgeWindow() {
  vi.stubGlobal('window', Object.assign(new EventTarget(), {
    location: { pathname: '/wb/reports/abc', search: '', origin: 'http://localhost' },
    localStorage: { getItem: () => null, setItem: () => undefined },
    setTimeout: (callback: () => void) => { callback(); return 0 },
    __vellaReportPeriods: {
      abc: { days: 7, fromIso: '2026-09-01', toIso: '2026-09-07', label: '7 дней', mode: 'custom' },
    },
    __vellaPublishAbcRowsSnapshot: () => undefined,
  }))
}

function jsonResponse(payload: unknown, status = 200) {
  return new Response(JSON.stringify(payload), { status, headers: { 'content-type': 'application/json' } })
}

describe('canonical ABC bridge lifecycle', () => {
  it('does not fall back to legacy profit while the cabinet identity is unavailable', async () => {
    installBridgeWindow()
    const request = vi.fn()
    vi.stubGlobal('fetch', request)
    installAbcLiveDataBridge('pending-cabinet-token', null, false)
    await window.__vellaLoadLiveAbcReport?.()
    expect(request).not.toHaveBeenCalled()
    expect(window.__vellaAbcLiveReport).toBeNull()
    expect(window.__vellaAbcLiveRows).toEqual([])
    expect(window.__vellaAbcLiveError).toContain('Данные кабинета ещё не загружены')
  })

  it.each(['session', 'account', 'period'] as const)('hides old ABC KPI on the first %s render before bridge effects', async (changed) => {
    installBridgeWindow()
    vi.stubEnv('VITE_CANONICAL_WB_ABC_PNL_ROLLOUT', '7:31,8:32')
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(page())))
    const token = `render-test-${changed}`
    installAbcLiveDataBridge(token, { organizationId: 7, marketplaceAccountId: 31 })
    await window.__vellaLoadLiveAbcReport?.()
    function render(accessToken: string, organizationId: number) {
      // Only the read-only auth fields used by this island are needed for SSR.
      const auth = { accessToken, cabinetMe: { organization: { organizationId } } } as AuthContextValue
      return renderToStaticMarkup(createElement(AuthContext.Provider, { value: auth },
        createElement(AbcKpiStripIsland, { replacementKey: 'scope-test' })))
    }
    const previous = render(token, 7)
    expect(previous).toContain('1 208 ₽')
    if (changed === 'period') window.__vellaReportPeriods!.abc!.fromIso = '2026-09-02'
    const next = render(changed === 'session' ? 'new-session' : token, changed === 'account' ? 8 : 7)
    expect(next).not.toContain('1 208 ₽')
    expect(next).toBe('')
  })

  it('publishes the warm table and revalidates on every opening, including before five minutes', async () => {
    installBridgeWindow()
    let now = 1_000_000
    vi.spyOn(Date, 'now').mockImplementation(() => now)
    const request = vi.fn(async () => jsonResponse(page()))
    vi.stubGlobal('fetch', request)
    installAbcLiveDataBridge('ttl-test-token', { organizationId: 7, marketplaceAccountId: 31 })
    await window.__vellaLoadLiveAbcReport?.()
    now += 299_999
    await window.__vellaLoadLiveAbcReport?.()
    expect(request).toHaveBeenCalledTimes(6)
    now += 1
    await window.__vellaLoadLiveAbcReport?.()
    expect(request).toHaveBeenCalledTimes(9)
    expect(window.__vellaAbcLiveError).toBeUndefined()
  })

  it.each([
    ['401', () => jsonResponse({ detail: 'Unauthenticated' }, 401)],
    ['403', () => jsonResponse({ detail: 'Forbidden' }, 403)],
    ['429', () => jsonResponse({ detail: 'Rate limited' }, 429)],
    ['503', () => jsonResponse({ detail: 'Unavailable' }, 503)],
    ['HTML', () => new Response('<html>SPA shell</html>', { headers: { 'content-type': 'text/html' } })],
    ['invalid JSON', () => new Response('{', { headers: { 'content-type': 'application/json' } })],
    ['invalid contract', () => jsonResponse({ items: [] })],
    ['network', () => { throw new TypeError('Failed to fetch') }],
  ])('keeps %s failures explicit without legacy or demo fallback', async (label, response) => {
    installBridgeWindow()
    vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const request = vi.fn(async (_input: RequestInfo | URL) => response())
    vi.stubGlobal('fetch', request)
    installAbcLiveDataBridge(`error-test-${label}`, { organizationId: 7, marketplaceAccountId: 31 })
    await window.__vellaLoadLiveAbcReport?.()
    expect(request).toHaveBeenCalledTimes(1)
    expect(String(request.mock.calls[0]?.[0])).toContain('/api/v2/wb/reports/abc-pnl?')
    expect(window.__vellaAbcLiveRows).toEqual([])
    expect(window.__vellaAbcLiveReport).toBeNull()
    expect(window.__vellaAbcLiveLoading).toBe(false)
    expect(window.__vellaAbcLiveError).toBeTruthy()
    if (label === '401') expect(window.__vellaAbcLiveAuthExpired).toBe(true)
    if (label === '403') expect(window.__vellaAbcLiveAccessDenied).toBe(true)
  })

  it.each(['account', 'period'] as const)('ignores a late %s response even when transport ignores abort', async (changed) => {
    installBridgeWindow()
    const resolvers: Array<(response: Response) => void> = []
    const request = vi.fn((input: RequestInfo | URL) => String(input).includes('/latest-cache?')
      ? Promise.resolve(jsonResponse({ rows: [] }))
      : !String(input).includes('includeSpp=true')
        ? Promise.resolve(jsonResponse(page()))
        : new Promise<Response>((resolve) => resolvers.push(resolve)))
    vi.stubGlobal('fetch', request)
    installAbcLiveDataBridge(`race-test-${changed}`, { organizationId: 7, marketplaceAccountId: 31 })
    const oldLoad = window.__vellaLoadLiveAbcReport?.()
    const next = page()
    if (changed === 'account') {
      next.meta.marketplaceAccountId = 32
      installAbcLiveDataBridge(`race-test-${changed}`, { organizationId: 8, marketplaceAccountId: 32 })
    } else {
      window.__vellaReportPeriods!.abc!.fromIso = '2026-09-02'
      next.meta.period.dateFrom = '2026-09-02'
      next.meta.period.startAt = '2026-09-01T21:00:00Z'
      next.meta.period.days = 6
    }
    const nextLoad = window.__vellaLoadLiveAbcReport?.()
    expect(window.__vellaAbcLiveRows).toEqual([])
    expect(window.__vellaAbcLiveLoading).toBe(true)
    resolvers[1]!(jsonResponse(next))
    await nextLoad
    const currentReport = window.__vellaAbcLiveReport
    expect(currentReport).not.toBeNull()
    resolvers[0]!(jsonResponse(page()))
    await oldLoad
    expect(window.__vellaAbcLiveReport).toBe(currentReport)
    expect(window.__vellaAbcLiveLoading).toBe(false)
    expect(window.__vellaAbcLiveError).toBeUndefined()
    expect(request).toHaveBeenCalledTimes(4)
  })
})

it('P&L expense projection uses signed observed fees, not clipped profit components', () => {
  const payload = page()
  const parsed = parseCanonicalAbcPnlPage({ ...payload, items: [{ ...row,
    penaltyKopecks: -123, acceptanceKopecks: 0,
    pnlExpenses: { penaltyKopecks: 999, acceptanceKopecks: null, state: 'partial' },
  }] })
  const report = adaptCanonicalPnlReport(parsed)
  expect(report.rows[0]).toMatchObject({ penaltyKopecks: -123, acceptanceKopecks: 0,
    expenseState: 'confirmed', netProfitKopecks: row.netProfitKopecks })
  const withoutEnrichment = adaptCanonicalPnlReport(parseCanonicalAbcPnlPage(payload)).rows[0]
  expect(withoutEnrichment.penaltyKopecks).toBe(row.penaltyKopecks)
  expect(withoutEnrichment.acceptanceKopecks).toBe(row.acceptanceKopecks)
  expect(withoutEnrichment.expenseState).toBe('confirmed')
  expect(buildCanonicalAbcPnlPath({ marketplaceAccountId: 31,
    period: { fromIso: '2026-09-01', toIso: '2026-09-07' }, limit: 500, offset: 0,
    includePnlExpenses: true })).toContain('includePnlExpenses=true')
  expect(buildCanonicalAbcPnlPath({ marketplaceAccountId: 31,
    period: { fromIso: '2026-09-01', toIso: '2026-09-07' }, limit: 500, offset: 0 })).not.toContain('includePnlExpenses')
})

it('P&L expense projection keeps fee-only and unallocated rows through pagination without fake profit', () => {
  const payload = page()
  const parsed = parseCanonicalAbcPnlPage({ ...payload, meta: { ...payload.meta,
    pnlExpenseRows: [
      { nmId: null, penaltyKopecks: 700, acceptanceKopecks: 800, state: 'confirmed' },
      { nmId: row.nmId, penaltyKopecks: -123, acceptanceKopecks: 0, state: 'confirmed' },
      { nmId: 101, penaltyKopecks: 9, acceptanceKopecks: 10, state: 'partial' },
    ],
  }, items: [{ ...row, pnlExpenses: { penaltyKopecks: -123, acceptanceKopecks: 0, state: 'confirmed' } }] })
  const report = adaptCanonicalPnlReport(parsed)
  expect(report.rows).toHaveLength(3)
  expect(report.rows.find(item => item.nmId === null)).toMatchObject({
    label: 'Нераспределённые расходы WB', penaltyKopecks: 700, acceptanceKopecks: 800, netProfitKopecks: null,
  })
  expect(report.rows.filter(item => item.nmId === row.nmId)).toHaveLength(1)
  expect(report.rows.find(item => item.nmId === 101)?.revenueKopecks).toBeUndefined()
})

const row = {
  nmId: 453200669,
  sellerArticle: 'FBBT_1133',
  catalogSkuId: 11,
  operationCount: 2,
  revenueKopecks: 120_800,
  salesRevenueKopecks: 125_000,
  returnsRevenueKopecks: -4_200,
  mainRevenueKopecks: 120_800,
  redemptionsRevenueKopecks: 0,
  lateCorrectionRevenueKopecks: 0,
  unknownRevenueKopecks: 0,
  salesUnits: 2,
  returnsUnits: 1,
  netUnits: 1,
  commissionKopecks: 10_000,
  logisticsKopecks: 2_000,
  storageKopecks: 500,
  acceptanceKopecks: 100,
  penaltyKopecks: 0,
  deductionKopecks: 0,
  additionalPaymentKopecks: 0,
  financeOtherExpensesKopecks: 300,
  compensationKopecks: 0,
  acquiringKopecks: 800,
  financeExpensesKopecks: 13_700,
  costValueState: 'assumed' as const,
  costEvidenceStatus: 'undated' as const,
  cogsKopecks: null,
  settlementProfitKopecks: null,
  economicsValueState: 'assumed' as const,
  economicsEvidenceStatus: 'undated' as const,
  taxKopecks: null,
  otherExpensesKopecks: null,
  profitBeforeAdsAndLoyaltyKopecks: null,
  advertisingSpendKopecks: 2_283,
  profitBeforeLoyaltyKopecks: null,
  cashbackAmountKopecks: 1_000,
  cashbackDiscountKopecks: 250,
  cashbackCommissionChangeKopecks: -50,
  loyaltyNetCostKopecks: 700,
  profitAfterLoyaltyKopecks: null,
  profitBeforeInternalExpensesKopecks: null,
  internalExpensesKopecks: null,
  salesClass: 'A' as const,
  profitClass: null,
  abcCode: null,
  netProfitKopecks: null,
  blockerIds: ['WB_PNL_COST_ASSUMED'],
}

const summary = {
  operationCount: 2,
  skuCount: 1,
  revenueKopecks: 120_800,
  salesRevenueKopecks: 125_000,
  returnsRevenueKopecks: -4_200,
  salesUnits: 2,
  returnsUnits: 1,
  netUnits: 1,
  commissionKopecks: 10_000,
  logisticsKopecks: 2_000,
  storageKopecks: 500,
  acceptanceKopecks: 100,
  penaltyKopecks: 0,
  deductionKopecks: 0,
  financeOtherExpensesKopecks: 300,
  compensationKopecks: 0,
  acquiringKopecks: 800,
  financeExpensesKopecks: 13_700,
  cogsKopecks: null,
  settlementProfitKopecks: null,
  taxKopecks: null,
  otherExpensesKopecks: null,
  profitBeforeAdsAndLoyaltyKopecks: null,
  advertisingSpendKopecks: 2_283,
  unattributedAdvertisingSpendKopecks: 0,
  profitBeforeLoyaltyKopecks: null,
  cashbackAmountKopecks: 1_000,
  cashbackDiscountKopecks: 250,
  cashbackCommissionChangeKopecks: -50,
  loyaltyNetCostKopecks: 700,
  profitAfterLoyaltyKopecks: null,
  profitBeforeInternalExpensesKopecks: null,
  internalExpensesKopecks: null,
  netProfitKopecks: null,
}

function page(state: 'ready' | 'partial' | 'future' | 'empty' | 'missing' = 'partial') {
  return {
    items: state === 'future' || state === 'empty' || state === 'missing' ? [] : [row],
    total: state === 'future' || state === 'empty' || state === 'missing' ? 0 : 1,
    limit: 500,
    offset: 0,
    summary: state === 'future' || state === 'empty' || state === 'missing'
      ? { ...summary, operationCount: 0, skuCount: 0, revenueKopecks: 0 }
      : summary,
    meta: {
      state,
      marketplaceAccountId: 31,
      period: {
        dateFrom: '2026-09-01',
        dateTo: '2026-09-07',
        timezone: 'Europe/Moscow' as const,
        startAt: '2026-08-31T21:00:00Z',
        endExclusiveAt: '2026-09-07T21:00:00Z',
        days: 7,
        temporalState: state === 'future' ? 'future' as const : 'complete' as const,
      },
      snapshot: null,
      formulaVersion: 'wb-abc-pnl-finance-rrdate-v4' as const,
      costLedgerRevision: 4,
      economicsRevision: 2,
      advertisingSource: 'ads_fullstats' as const,
      advertisingSnapshotChecksum: 'ads-checksum',
      advertisingEvidenceStatus: 'raw' as const,
      blockerIds: ['WB_PNL_COST_ASSUMED', 'WB_PNL_ECONOMICS_ASSUMED'],
    },
    timestamp: '2026-09-08T07:00:00Z',
  }
}

describe('canonical ABC/P&L rollout', () => {
  it.each(['7:44,broken', 'broken,7:44', '7:44,7:45', '7:44,7:44', '7:4e1', '7:0x2c', '7:44,', '7:9007199254740993'])('fails closed for malformed or ambiguous rollout %s', (flag) => {
    expect(resolveCanonicalAbcPnlRollout(7, flag)).toBeNull()
  })
  it('is default-off and resolves an explicit organization/account mapping', () => {
    expect(resolveCanonicalAbcPnlRollout(7, '')).toBeNull()
    expect(resolveCanonicalAbcPnlRollout(7, ' 2:31, 7:44 ')).toEqual({ organizationId: 7, marketplaceAccountId: 44 })
    expect(resolveCanonicalAbcPnlRollout(7, '7:not-a-number,7:0,broken')).toBeNull()
  })

  it('always serializes the selected preset or custom range as exact dates', () => {
    const path = buildCanonicalAbcPnlPath({
      marketplaceAccountId: 31,
      period: { fromIso: '2026-09-01', toIso: '2026-09-07' },
      limit: 500,
      offset: 0,
    })
    const url = new URL(path, 'https://frontend.test')
    expect(url.pathname).toBe('/api/v2/wb/reports/abc-pnl')
    expect(Object.fromEntries(url.searchParams)).toEqual({
      marketplaceAccountId: '31',
      dateFrom: '2026-09-01',
      dateTo: '2026-09-07',
      limit: '500',
      offset: '0',
    })
    expect(url.searchParams.has('periodDays')).toBe(false)
  })

  it('uses canonical only for an allowlisted organization and financial P&L', () => {
    const rollout = resolveCanonicalAbcPnlRollout(7, '7:44')
    expect(shouldUseCanonicalAbcPnl(rollout, 'abc')).toBe(true)
    expect(shouldUseCanonicalAbcPnl(rollout, 'pnl', 'financial')).toBe(true)
    expect(shouldUseCanonicalAbcPnl(rollout, 'pnl', 'operational')).toBe(false)
    expect(shouldUseCanonicalAbcPnl(null, 'abc')).toBe(false)
    expect(shouldUseCanonicalAbcPnl(null, 'pnl', 'financial')).toBe(false)
  })
})

describe('canonical ABC/P&L contract', () => {
  it.each(['row', 'summary'] as const)('rejects unsafe money in the %s before adapting the report', (scope) => {
    const source = page()
    const values = scope === 'row' ? source.items[0]! : source.summary
    for (const key of Object.keys(values).filter((key) => key.endsWith('Kopecks') && key !== 'netProfitKopecks')) {
      const invalid = { ...values, [key]: Number.MAX_SAFE_INTEGER + 1 }
      const payload = scope === 'row' ? { ...source, items: [invalid] } : { ...source, summary: invalid }
      expect(() => parseCanonicalAbcPnlPage(payload), key).toThrow(ApiError)
    }
  })

  it('preserves exact safe integer money boundaries and unknown values', () => {
    const source = page()
    const parsed = parseCanonicalAbcPnlPage({ ...source, items: [{ ...row,
      revenueKopecks: Number.MAX_SAFE_INTEGER,
      settlementProfitKopecks: Number.MIN_SAFE_INTEGER,
      taxKopecks: 0, otherExpensesKopecks: null,
    }] })
    expect(parsed.items[0]?.revenueKopecks).toBe(Number.MAX_SAFE_INTEGER)
    expect(parsed.items[0]?.settlementProfitKopecks).toBe(Number.MIN_SAFE_INTEGER)
    expect(parsed.items[0]?.taxKopecks).toBe(0)
    expect(parsed.items[0]?.otherExpensesKopecks).toBeNull()
  })

  it.each([false, true])('rejects repeated financial identities across pages (changed row=%s)', async (changed) => {
    const first = { ...page(), total: 2, limit: 1 }
    const second = { ...first, offset: 1, items: [{ ...row, revenueKopecks: changed ? 0 : row.revenueKopecks }] }
    await expect(fetchCanonicalAbcPnl({
      accessToken: 'synthetic', marketplaceAccountId: 31,
      period: { fromIso: '2026-09-01', toIso: '2026-09-07' }, pageSize: 1,
      request: async (path) => path.includes('offset=0') ? first : second,
    })).rejects.toMatchObject({ code: 'CANONICAL_PAGINATION_DRIFT' })
  })

  it('rejects an oversized page instead of accepting a broken pagination contract', async () => {
    await expect(fetchCanonicalAbcPnl({
      accessToken: 'synthetic', marketplaceAccountId: 31,
      period: { fromIso: '2026-09-01', toIso: '2026-09-07' }, pageSize: 1,
      request: async () => ({ ...page(), total: 2, limit: 1, items: [row, { ...row, nmId: 42 }] }),
    })).rejects.toMatchObject({ code: 'CANONICAL_PAGINATION_DRIFT' })
  })

  it('does not publish a late response when transport ignores cancellation', async () => {
    const controller = new AbortController()
    await expect(fetchCanonicalAbcPnl({
      accessToken: 'synthetic', marketplaceAccountId: 31,
      period: { fromIso: '2026-09-01', toIso: '2026-09-07' }, signal: controller.signal,
      request: async () => { controller.abort(); return page() },
    })).rejects.toMatchObject({ name: 'AbortError' })
  })
  it.each(['ready', 'partial', 'future', 'empty', 'missing'] as const)('preserves the %s source state', (state) => {
    expect(parseCanonicalAbcPnlPage(page(state)).meta.state).toBe(state)
  })

  it('distinguishes zero from unavailable final fields', () => {
    const parsed = parseCanonicalAbcPnlPage({
      ...page('ready'),
      items: [{ ...row, advertisingSpendKopecks: 0, profitAfterLoyaltyKopecks: 0 }],
      summary: { ...summary, advertisingSpendKopecks: 0, profitAfterLoyaltyKopecks: 0 },
    })
    expect(parsed.items[0].advertisingSpendKopecks).toBe(0)
    expect(parsed.items[0].profitAfterLoyaltyKopecks).toBe(0)
    expect(parsed.items[0].netProfitKopecks).toBeNull()
    expect(parsed.items[0].abcCode).toBeNull()
  })

  it('rejects a response that invents final profit or a second ABC letter', () => {
    expect(() => parseCanonicalAbcPnlPage({ ...page(), items: [{ ...row, netProfitKopecks: 1 }] })).toThrow(/canonical ABC\/P&L response/i)
    expect(() => parseCanonicalAbcPnlPage({ ...page(), items: [{ ...row, abcCode: 'AA' }] })).toThrow(/canonical ABC\/P&L response/i)
  })

  it('preserves approved profit, internal expenses and final profit without using the old subtotal', () => {
    const amounts = { profitBeforeInternalExpensesKopecks: 24_800_000, internalExpensesKopecks: 5_000_000, netProfitKopecks: 19_800_000 }
    const parsed = parseCanonicalAbcPnlPage({ ...page(), items: [{ ...row, ...amounts }], summary: { ...summary, ...amounts } })
    expect(adaptCanonicalAbcReport(parsed).rows[0]).toMatchObject(amounts)
    expect(adaptCanonicalPnlReport(parsed).rows[0]).toMatchObject(amounts)
    expect(adaptCanonicalPnlReport(parsed).canonicalSummary).toMatchObject(amounts)
    const missing = parseCanonicalAbcPnlPage(page())
    expect(adaptCanonicalAbcReport(missing).rows[0].profitBeforeInternalExpensesKopecks).toBeNull()
    expect(adaptCanonicalPnlReport(missing).rows[0].internalExpensesKopecks).toBeNull()
  })

  it('keeps source evidence and blockers in both compatibility adapters', () => {
    const parsed = parseCanonicalAbcPnlPage(page())
    const abc = adaptCanonicalAbcReport(parsed)
    const pnl = adaptCanonicalPnlReport(parsed)

    expect(abc.canonical).toMatchObject({
      state: 'partial',
      advertisingSource: 'ads_fullstats',
      advertisingEvidenceStatus: 'raw',
      blockerIds: ['WB_PNL_COST_ASSUMED', 'WB_PNL_ECONOMICS_ASSUMED'],
    })
    expect(abc.rows[0]).toMatchObject({
      abcCode: null,
      profitAfterLoyaltyKopecks: null,
      blockerIds: ['WB_PNL_COST_ASSUMED'],
    })
    expect(abc.rows[0]).not.toHaveProperty('netTotalKopecks')
    expect(pnl.rows[0]).toMatchObject({
      netProfitKopecks: null,
      profitAfterLoyaltyKopecks: null,
      sourceStatus: 'partial',
      blockerIds: ['WB_PNL_COST_ASSUMED'],
    })
    expect(pnl.canonicalSummary.netProfitKopecks).toBeNull()
  })

  it('enriches canonical ABC by WB id without hiding operations or replacing missing finance', () => {
    installBridgeWindow()
    const supplemental = [
      { nmId: 99, sku: row.sellerArticle, baskets: 999 },
      {
        nmId: String(row.nmId), sku: 'Actual article', productName: 'Товар WB', productStatus: 'new',
        managerId: 'manager-1', manager: 'Менеджер', promotionStatus: 'yes', promotionName: 'Акция WB',
        priceBeforeSppKopecks: 150_000, priceWithSppKopecks: 100_000, clicks: 12, baskets: 0,
        ordersComposite: { units: 0, kopecks: 0 }, wbStockUnits: 8,
        ruleEvaluation: { status: 'risk' as const },
        // Supplemental financial values must never override canonical nulls or amounts.
        cogsKopecks: 999, netTotalKopecks: 999, marginPct: 999, adSpendKopecks: 999,
      },
    ]
    const report = adaptCanonicalAbcReport(parseCanonicalAbcPnlPage(page()), supplemental)
    expect(report.rows[0]).toMatchObject({
      sku: 'Actual article', productName: 'Товар WB', baskets: 0, clicks: 12, wbStockUnits: 8,
      adSpendKopecks: row.advertisingSpendKopecks, cogsKopecks: null, marginPct: null,
      profitBeforeInternalExpensesKopecks: null, netProfitKopecks: null,
    })
    expect(report.rows[0].commissionCostPct).toBeCloseTo(row.commissionKopecks / row.revenueKopecks * 100)
    const mapped = mapBackendAbcRowToParity(report.rows[0])
    expect(mapped).toMatchObject({
      status: 'новинка', action: 'аудит', baskets: '0', clicks: '12', stock: '8 шт',
      price: '1 500 ₽', spp: 'с СПП 1 000 ₽', ordersKnown: true, profitKnown: false, net: '—',
    })
    const html = renderAbcRowHtml(mapped, 0)
    expect(html).toContain('Акция WB')
    expect(html).toContain('Менеджер')
    expect(html).toContain('Прибыль не подтверждена')
    expect(html).toContain('Себестоимость отмечена как примерная, а не фактическая')
    expect(html).toContain('Хранение, ₽ и доля в реализации')
    expect(html).not.toContain('КТР не получен') // The unsupported KTR column was removed.
    expect(renderAbcRowHtml({ ...mapped, adsKnown: true, blockerIds: ['WB_PNL_ADVERTISING_UNATTRIBUTED'] }, 0)).toContain('без нераспределённых расходов')
    const absent = mapBackendAbcRowToParity(adaptCanonicalAbcReport(parseCanonicalAbcPnlPage(page())).rows[0])
    expect(absent).toMatchObject({ baskets: '—', orders: '—', stock: '—', ordersKnown: false })
  })

  it('renders partial and blocked canonical P&L rows as partial', () => {
    expect(canonicalPnlRowStatus({ sourceStatus: 'partial', confidence: 'canonical', blockerIds: [] })).toBe('частично')
    expect(canonicalPnlRowStatus({ sourceStatus: 'ready', confidence: 'blocked', blockerIds: [] })).toBe('частично')
    expect(canonicalPnlRowStatus({ sourceStatus: 'ready', confidence: 'canonical', blockerIds: [] })).toBe('подтверждено')
  })

  it('gives future, missing, and empty states distinct customer-facing copy', () => {
    expect(canonicalAbcPnlEmptyMessage('abc', 'future')).toContain('будущем')
    expect(canonicalAbcPnlEmptyMessage('abc', 'missing')).toContain('еще нет canonical finance snapshot')
    expect(canonicalAbcPnlEmptyMessage('pnl', 'empty')).toContain('операций за период нет')
    expect(canonicalAbcPnlEmptyMessage('pnl', 'partial')).toBeNull()
    expect(canonicalAbcPnlEmptyMessage('abc', 'ready')).toBeNull()
  })

  it('loads all pages and keeps one stable snapshot', async () => {
    const first = { ...page('ready'), total: 2, limit: 1, items: [row] }
    const second = { ...page('ready'), total: 2, limit: 1, offset: 1, items: [{ ...row, nmId: 453200670, sellerArticle: 'SECOND' }] }
    const request = vi.fn(async (path: string) => path.includes('offset=0') ? first : second)

    const result = await fetchCanonicalAbcPnl({
      accessToken: 'token',
      marketplaceAccountId: 31,
      period: { fromIso: '2026-09-01', toIso: '2026-09-07' },
      pageSize: 1,
      request,
    })

    expect(request).toHaveBeenCalledTimes(2)
    expect(request).toHaveBeenNthCalledWith(
      1,
      expect.stringContaining('marketplaceAccountId=31'),
      expect.objectContaining({ headers: { Authorization: 'Bearer token' } }),
    )
    expect(result.items.map((item) => item.sellerArticle)).toEqual(['FBBT_1133', 'SECOND'])
    expect(result.total).toBe(2)
  })

  it('rejects pagination drift instead of combining different snapshots', async () => {
    const first = { ...page('ready'), total: 2, limit: 1, items: [row] }
    const second = {
      ...page('ready'),
      total: 2,
      limit: 1,
      offset: 1,
      items: [{ ...row, nmId: 453200670 }],
      meta: { ...page('ready').meta, economicsRevision: 3 },
    }
    const request = vi.fn(async (path: string) => path.includes('offset=0') ? first : second)

    await expect(fetchCanonicalAbcPnl({
      accessToken: 'token',
      marketplaceAccountId: 31,
      period: { fromIso: '2026-09-01', toIso: '2026-09-07' },
      pageSize: 1,
      request,
    })).rejects.toMatchObject({ code: 'CANONICAL_PAGINATION_DRIFT' })
  })

  it.each([
    ['summary', { summary: { ...summary, revenueKopecks: 1 } }],
    ['total', { total: 3 }],
    ['repeated offset', { offset: 0 }],
    ['stalled page', { items: [] }],
    ['snapshot', { meta: { ...page().meta, advertisingSnapshotChecksum: 'different' } }],
    ['cost revision', { meta: { ...page().meta, costLedgerRevision: 99 } }],
    ['evidence', { meta: { ...page().meta, advertisingEvidenceStatus: 'aggregate_only' } }],
    ['account', { meta: { ...page().meta, marketplaceAccountId: 32 } }],
  ])('rejects %s drift on a later page without returning partial rows', async (_label, override) => {
    const first = { ...page(), total: 2, limit: 1 }
    const second = { ...first, offset: 1, items: [{ ...row, nmId: 453200670 }], ...override }
    const request = vi.fn().mockResolvedValueOnce(first).mockResolvedValueOnce(second)
    await expect(fetchCanonicalAbcPnl({
      accessToken: 'token', marketplaceAccountId: 31,
      period: { fromIso: '2026-09-01', toIso: '2026-09-07' }, pageSize: 1, request,
    })).rejects.toMatchObject({ code: 'CANONICAL_PAGINATION_DRIFT' })
    expect(request).toHaveBeenCalledTimes(2)
  })

  it('allows distinct nmIds with the same article and one unmapped bucket', async () => {
    const items = [row, { ...row, nmId: 453200670 }, { ...row, nmId: null }]
    const result = await fetchCanonicalAbcPnl({
      accessToken: 'token', marketplaceAccountId: 31,
      period: { fromIso: '2026-09-01', toIso: '2026-09-07' },
      request: vi.fn().mockResolvedValue({ ...page(), total: 3, items }),
    })
    expect(result.items).toHaveLength(3)
  })

  it('rejects an unknown formula instead of calculating a substitute in the UI', () => {
    expect(() => parseCanonicalAbcPnlPage({ ...page(), meta: { ...page().meta, formulaVersion: 'v2' } })).toThrow(ApiError)
  })

  it('rejects an older payable formula whose date and advertising definitions differ', () => {
    const payload = page()
    expect(() => parseCanonicalAbcPnlPage({
      ...payload,
      meta: { ...payload.meta, formulaVersion: 'wb-abc-pnl-payable-v2' },
    })).toThrow(ApiError)
  })

  it('uses GET with bearer authorization and no mutation body through the real API client', async () => {
    const request = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => jsonResponse(page()))
    vi.stubGlobal('fetch', request)
    await fetchCanonicalAbcPnl({
      accessToken: 'fake-read-token', marketplaceAccountId: 31,
      period: { fromIso: '2026-09-01', toIso: '2026-09-07' },
    })
    const [url, init] = request.mock.calls[0]!
    expect(String(url)).toContain('/api/v2/wb/reports/abc-pnl?')
    expect(init?.method ?? 'GET').toBe('GET')
    expect(init?.body).toBeUndefined()
    expect(new Headers(init?.headers).get('Authorization')).toBe('Bearer fake-read-token')
  })

  it.each([
    ['response period', { meta: { ...page('ready').meta, period: { ...page('ready').meta.period, dateTo: '2026-09-06' } } }],
    ['response page size', { limit: 499 }],
  ])('rejects a mismatched %s', async (_label, override) => {
    const request = vi.fn(async () => ({ ...page('ready'), ...override }))

    await expect(fetchCanonicalAbcPnl({
      accessToken: 'token',
      marketplaceAccountId: 31,
      period: { fromIso: '2026-09-01', toIso: '2026-09-07' },
      request,
    })).rejects.toMatchObject({ code: 'CANONICAL_PAGINATION_DRIFT' })
  })

  it('preserves a scoped no-access response for the consumer state', async () => {
    const denied = new ApiError('WB account is outside the actor scope', 403, 'WB_ACCOUNT_SCOPE_FORBIDDEN')
    const request = vi.fn(async () => { throw denied })

    await expect(fetchCanonicalAbcPnl({
      accessToken: 'token',
      marketplaceAccountId: 31,
      period: { fromIso: '2026-09-01', toIso: '2026-09-07' },
      request,
    })).rejects.toBe(denied)
  })

  it('propagates aborts so stale period requests cannot publish', async () => {
    const aborted = new DOMException('The operation was aborted', 'AbortError')
    const request = vi.fn(async () => { throw aborted })

    await expect(fetchCanonicalAbcPnl({
      accessToken: 'token',
      marketplaceAccountId: 31,
      period: { fromIso: '2026-09-01', toIso: '2026-09-07' },
      request,
    })).rejects.toBe(aborted)
  })
})
