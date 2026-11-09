import { afterEach, describe, expect, test, vi } from 'vitest'
import { loadLiveBasketsDetailStatus, loadLiveRepricerCacheCoverage, loadLiveRepricerStats, mapLiveRepricerRowToParityProduct, refreshLiveRepricerAllSources, startLiveBasketsDetail, type LiveRepricerSkuRow } from './liveParityData'

afterEach(() => vi.unstubAllGlobals())

function jsonResponse(data: unknown) {
  return new Response(JSON.stringify(data), {
    headers: { 'content-type': 'application/json' },
  })
}

describe('basket detail background job API', () => {
  test('starts daily basket detail for the exact applied range', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({
      runId: 'run-1', state: 'running', cached: false, dateFrom: '2026-06-12', dateTo: '2026-07-12',
      periodDays: 31, requestsCompleted: 0, requestsTotal: 124, progressPercent: 0, updatedAt: 'now',
    }))
    vi.stubGlobal('fetch', fetchMock)

    const result = await startLiveBasketsDetail('token', { dateFrom: '2026-06-12', dateTo: '2026-07-12' })

    expect(result.runId).toBe('run-1')
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining('/api/v1/wb-repricer/baskets/detail/start'), expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ dateFrom: '2026-06-12', dateTo: '2026-07-12', scenario: 'complete', force: false }),
    }))
  })

  test('loads progress only for the requested run id', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({
      runId: 'run with space', state: 'running', cached: false, dateFrom: '2026-06-12', dateTo: '2026-07-12',
      periodDays: 31, requestsCompleted: 17, requestsTotal: 124, progressPercent: 14, updatedAt: 'now',
      phase: 'requesting', day: '2026-06-16', dayIndex: 5, daysTotal: 31, batch: 1, batchesTotal: 4,
    }))
    vi.stubGlobal('fetch', fetchMock)

    const result = await loadLiveBasketsDetailStatus('token', 'run with space')

    expect(result.requestsCompleted).toBe(17)
    expect(fetchMock.mock.calls[0]?.[0]).toContain('runId=run+with+space')
  })
})

describe('repricer stats live API', () => {
  test('loads diagnostics from the stats backend endpoint with the selected period and page', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({
      items: [],
      total: 0,
      summary: { skuCount: 0, canRecalculate: 0, priceBlocked: 0 },
      page: 2,
      pageSize: 50,
      periodDays: 14,
      cache: {},
    }))
    vi.stubGlobal('fetch', fetchMock)

    const result = await loadLiveRepricerStats('token', undefined, {
      periodDays: 14,
      page: 2,
      pageSize: 50,
      q: 'FBBT',
    })

    expect(result.page).toBe(2)
    const requestUrl = String(fetchMock.mock.calls[0]?.[0])
    expect(requestUrl).toContain('/api/v1/wb-repricer/stats?')
    expect(requestUrl).toContain('periodDays=14')
    expect(requestUrl).toContain('page=2')
    expect(requestUrl).toContain('pageSize=50')
    expect(requestUrl).toContain('q=FBBT')
    expect(requestUrl).not.toContain('/api/v1/wb-repricer/sku?')
  })
})

describe('repricer cache coverage API', () => {
  test('loads cache coverage for the selected date range', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({
      dateFrom: '2026-06-10',
      dateTo: '2026-06-12',
      sources: ['period-stats', 'finance', 'ads', 'baskets'],
      days: [],
      summary: { totalDays: 3, completeDays: 0, partialDays: 0, missingDays: 3 },
    }))
    vi.stubGlobal('fetch', fetchMock)

    const result = await loadLiveRepricerCacheCoverage('token', { dateFrom: '2026-06-10', dateTo: '2026-06-12' })

    expect(result?.summary.totalDays).toBe(3)
    const requestUrl = String(fetchMock.mock.calls[0]?.[0])
    expect(requestUrl).toContain('/api/v1/wb-repricer/cache/coverage?')
    expect(requestUrl).toContain('dateFrom=2026-06-10')
    expect(requestUrl).toContain('dateTo=2026-06-12')
  })
})

describe('repricer WB sync API', () => {
  test('routes all-source refresh through cold onboarding sync', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({
      state: 'queued',
      running: true,
      syncProfile: 'onboarding-full',
    }))
    vi.stubGlobal('fetch', fetchMock)

    await refreshLiveRepricerAllSources('token', undefined, { periodDays: 7, dateFrom: '2026-06-10', dateTo: '2026-06-16' }, ['finance'])

    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining('/api/v1/wb-repricer/sync/run'), expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ scenario: 'complete', mode: 'onboarding', force: true }),
    }))
  })
})

describe('mapLiveRepricerRowToParityProduct', () => {
  test.each([10002, 0, -100])('uses financial-report realization %s, not seller-price revenue', value => {
    const product = mapLiveRepricerRowToParityProduct({
      meta: { articleId: 'SYNTHETIC', nmId: 101, name: 'Synthetic', currentPriceKopecks: 999999,
        status: 'auto', basketsLast7d: 0, basketNorm: 1 },
      settings: { cogsKopecks: 1000, logisticsKopecks: 0, minMarginPct: 0, wbCommissionPct: 0 },
      analytics: { financeState: 'ok', revenueKopecks: value, sellerRevenueKopecks: 20004,
        grossSalesUnits: 3, grossSalesAmountKopecks: 15003, salesUnits: 3, returnsUnits: 1 },
    } as LiveRepricerSkuRow, 0)
    expect(product.revenue7d).toBe(value / 100)
    expect(product.salesRub).toBe(150.03)
    expect(product.grossSalesUnits).toBe(3)
  })
  test.each([null, 0, 12])('preserves observed reporting quantities/amounts %s without price estimates', value => {
    const row = {
      meta: { articleId:'REPORTING', nmId:1025784485, name:'Synthetic', currentPriceKopecks:999999,
        status:'auto', basketsLast7d:0, basketNorm:1 },
      settings: { cogsKopecks:1000, logisticsKopecks:0, minMarginPct:0, wbCommissionPct:0 },
      analytics: { reportOrdersUnits:value, ordersUnits:999, grossSalesUnits:value,
        ordersAmountKopecks:value == null ? null : value * 12345,
        grossSalesAmountKopecks:value == null ? null : value * 23456,
        buyoutPct:value, periodStatsState:'no_data' },
    } as LiveRepricerSkuRow
    const product = mapLiveRepricerRowToParityProduct(row,0)
    expect(product.nmId).toBe(1025784485)
    expect(product.ordersPeriod).toBe(value)
    expect(product.grossSalesUnits).toBe(value)
    expect(product.ordersRub).toBe(value == null ? null : value * 12345 / 100)
    expect(product.salesRub).toBe(value == null ? null : value * 23456 / 100)
    expect(product.buyout).toBe(value)
  })
  test.each([24800000, 0, -100, null])('uses versioned settlement profit %s without legacy fallback', (profit) => {
    const product = mapLiveRepricerRowToParityProduct({
      meta: { articleId: 'ABC-TEST', name: 'Synthetic', currentPriceKopecks: 10000, basketsLast7d: 0, basketNorm: 1, status: 'auto' },
      settings: { cogsKopecks: 1000, logisticsKopecks: 0, minMarginPct: 0, wbCommissionPct: 0 },
      analytics: { financeState: 'ok', abcCode: profit === null ? 'A—' : 'AC', abcPolicyVersion: 'wb-units-profit-cumulative-80-95-v1', settlementFormulaVersion: 'wb-final-payout-cogs-tax-v1', settlementProfitKopecks: profit, netProfitKopecks: 999999 },
    }, 0)
    expect(product.abcCode).toBe(profit === null ? 'A—' : 'AC')
    expect(product.netSku).toBe(profit === null ? null : profit / 100)
  })
  test.each(['AA', 'AB', 'BC', 'CC', undefined, '', 'invalid'])('does not present legacy profit rank %s as confirmed net-profit ABC', (abcCode) => {
    const product = mapLiveRepricerRowToParityProduct({
      meta: { articleId: 'ABC-TEST', name: 'Synthetic ABC', currentPriceKopecks: 10000, basketsLast7d: 0, basketNorm: 1, status: 'auto' },
      settings: { cogsKopecks: 1000, logisticsKopecks: 0, minMarginPct: 0, wbCommissionPct: 0 },
      analytics: { abcCode, netProfitKopecks: 1000 },
    }, 0)
    expect(product.abcCode).toBe(abcCode && /^[ABC]{2}$/.test(abcCode) ? `${abcCode[0]}—` : null)
    expect(product.abcReason).toContain('чистая прибыль')
  })
  test('uses direct SPP API buyer price for price after SPP instead of wallet final price', () => {
    const product = mapLiveRepricerRowToParityProduct({
      meta: {
        articleId: 'SKU-1',
        nmId: 123,
        name: 'Test product',
        status: 'auto',
        currentPriceKopecks: 220000,
        basketsLast7d: 0,
        basketNorm: 1,
        subject: 'Футболки',
        brand: 'Brand',
        chrtIds: [],
      },
      strategy: { id: 'baskets_orders', name: 'Корзины', assignmentSource: 'derived' },
      settings: {
        cogsKopecks: 50000,
        logisticsKopecks: 5000,
        minMarginPct: 15,
        wbCommissionPct: 15,
      },
      analytics: {
        buyerPriceNoWalletKopecks: 142500,
        wbStockUnits: 121,
        circulationStockUnits: 613,
        stockInWayToClient: 299,
        stockInWayFromClient: 193,
        avgPriceWithSppKopecks: 150000,
        accountedBuyerPriceKopecks: 165000,
        buyerPriceWithWalletKopecks: 160000,
        sppPct: 35.23,
        basePriceKopecks: 220000,
        sellerDiscountedPriceKopecks: 220000,
        basketsState: 'ok',
        periodStatsState: 'ok',
        stockState: 'ok',
      },
    } as any, 0)

    expect(product.avgPriceSpp).toBe(1500)
    expect(product.stock).toBe(121)
    expect(product.circulationStock).toBe(613)
    expect(product.stockToBuyer).toBe(299)
    expect(product.stockFromBuyer).toBe(193)
    expect(product.buyerPriceNoWallet).toBe(1425)
    expect(product.priceWithSpp).toBe(1425)
    expect(product.priceFinal).toBe(1425)
    expect(product.priceWithWallet).toBe(1600)
    expect(product.spp).toBe(35.23)
  })

  test('does not use period average as current SPP price', () => {
    const product = mapLiveRepricerRowToParityProduct({
      meta: {
        articleId: 'SKU-AVG-ONLY',
        nmId: 124,
        name: 'Average only',
        status: 'auto',
        currentPriceKopecks: 220000,
        basketsLast7d: 1,
        basketNorm: 1,
      },
      strategy: { id: 'auto', name: 'Auto' },
      settings: { cogsKopecks: 50000, logisticsKopecks: 5000, minMarginPct: 15, wbCommissionPct: 15 },
      analytics: {
        buyerPriceNoWalletKopecks: null,
        avgPriceWithSppKopecks: 150000,
        basketsState: 'ok',
        periodStatsState: 'ok',
        stockState: 'ok',
      },
    } as any, 0)

    expect(product.priceWithSpp).toBeNull()
    expect(product.avgPriceSpp).toBe(1500)
  })

  test('preserves partial baskets and actual commission without a planned tariff', () => {
    const product = mapLiveRepricerRowToParityProduct({
      meta: { articleId: 'SKU-PARTIAL', nmId: 125, currentPriceKopecks: 220000, basketsLast7d: 30, basketNorm: 1 },
      settings: { cogsKopecks: 50000 },
      analytics: {
        baskets: 0, basketsState: 'partial', basketsReason: 'Данные за 6 из 7 дней.',
        commissionState: 'no_data', commissionReason: 'wb_commission_tariff_missing',
        financeState: 'ok', commissionKopecks: 10000,
        marginPct: 65, marginKopecks: 143000,
        accountedBuyerPriceKopecks: 150000,
      },
    } as any, 0)
    expect(product.bsk).toBe(0)
    expect(product.basketsState).toBe('partial')
    expect(product.basketsReason).toBe('Данные за 6 из 7 дней.')
    expect(product.commissionRub).toBe(100)
    expect(product.commissionPct).toBeNull()
    expect(product.mg).toBeNull()
    expect(product.mgRub).toBeNull()
    expect(product.priceWithWallet).toBeNull()
  })

  test('recomputes invalid SPP percent from seller and buyer prices', () => {
    const product = mapLiveRepricerRowToParityProduct({
      meta: {
        articleId: 'SKU-BAD-SPP',
        nmId: 456,
        name: 'Bad SPP product',
        status: 'auto',
        currentPriceKopecks: 147000,
        basketsLast7d: 0,
        basketNorm: 1,
        subject: 'Футболки',
        brand: 'Brand',
        chrtIds: [],
      },
      settings: {
        cogsKopecks: 50000,
        logisticsKopecks: 5000,
        minMarginPct: 15,
        wbCommissionPct: 15,
      },
      analytics: {
        buyerPriceNoWalletKopecks: 93900,
        buyerPriceWithWalletKopecks: 90100,
        sppPct: -6287.76,
        basketsState: 'ok',
        periodStatsState: 'ok',
        stockState: 'ok',
      },
    } as any, 0)

    expect(product.price).toBe(1470)
    expect(product.priceWithSpp).toBe(939)
    expect(product.priceWithWallet).toBe(901)
    expect(product.spp).toBe(36.12)
  })
})


describe('effective backend minimum price', () => {
  test.each([
    { manual: 0, effective: 90000, source: 'calculated', expected: 900 },
    { manual: 110000, effective: 110000, source: 'explicit', expected: 1100 },
    { manual: 50000, effective: 77700, source: 'calculated', expected: 777 },
    { manual: 0, effective: 0, source: 'calculated', expected: 0 },
    { manual: 0, effective: undefined, source: undefined, expected: 786 },
    { manual: 125000, effective: undefined, source: undefined, expected: 1250 },
  ])('displays $expected rubles while preserving raw manual $manual', ({ manual, effective, source, expected }) => {
    const row: LiveRepricerSkuRow = {
      meta: { articleId: 'EFFECTIVE-PMIN', name: 'Synthetic floor', status: 'auto', currentPriceKopecks: 220000, basketsLast7d: 1, basketNorm: 1 },
      settings: { cogsKopecks: 50000, logisticsKopecks: 5000, wbCommissionPct: 15, minMarginPct: 15, pMinKopecks: manual,
        ...effective == null ? {} : { effectivePMinKopecks: effective, effectivePMinSource: source as 'explicit' | 'calculated' } },
    }
    const product = mapLiveRepricerRowToParityProduct(row, 0)
    expect(product.pmin).toBe(expected)
    expect(product.pminBeforeSpp).toBe(expected)
    expect(product.settings.pMinKopecks).toBe(manual)
  })
})

describe('dated factual tax contract', () => {
  test.each([12000, 0, -12000])('shows campaign-based product subtotal %s with its allocation qualification', profit => {
    const row = {
      meta: { articleId: 'CAMPAIGN', name: 'Observed', status: 'manual', currentPriceKopecks: 500000 },
      settings: { cogsKopecks: 80000 },
      analytics: { financeState: 'ok', salesUnits: 3, returnsUnits: 1, revenueKopecks: 100000,
        settlementFormulaVersion: 'wb-final-payout-cogs-tax-v1', settlementProfitKopecks: null,
        settlementAttributedProfitKopecks: 999999, campaignAdvertisingVersion: 'wb-repricer-campaign-advertising-v1',
        campaignProfitKopecks: null, campaignAttributedProfitKopecks: profit,
        settlementBlockers: ['settlement_account_expenses_unreconciled'] },
    } as LiveRepricerSkuRow
    const product = mapLiveRepricerRowToParityProduct(row, 0)
    expect(product.mgRub).toBe(profit / 200)
    expect(product.mg).toBe(profit / 1000)
    expect(product.marginIsSubtotal).toBe(true)
    expect(product.marginReason).toContain('не окончательная прибыль')
    expect(product.netSku).toBeNull()
    row.analytics!.settlementBlockers!.push('settlement_dated_cogs_missing')
    expect(mapLiveRepricerRowToParityProduct(row, 0).mgRub).toBeNull()
  })

  test('does not replace unavailable campaign profit with a financial-posting profit', () => {
    const row = {
      meta: { articleId: 'CAMPAIGN-MISSING', name: 'Observed', currentPriceKopecks: 500000 }, settings: {},
      analytics: { financeState: 'ok', salesUnits: 1, returnsUnits: 0, revenueKopecks: 100000,
        settlementFormulaVersion: 'wb-final-payout-cogs-tax-v1', settlementProfitKopecks: 99000,
        campaignAdvertisingVersion: 'wb-repricer-campaign-advertising-v1', campaignProfitKopecks: null },
    } as LiveRepricerSkuRow
    expect(mapLiveRepricerRowToParityProduct(row, 0).mgRub).toBeNull()
  })

  test.each([12000, 0, -12000, null])('uses settlement profit %s even without planned tariffs or buyout forecasts', profit => {
    const row = {
      meta: { articleId: 'SETTLED', name: 'Observed', status: 'auto', currentPriceKopecks: 500000, basketsLast7d: 0, basketNorm: 0 },
      settings: { cogsKopecks: 80000 },
      analytics: { settlementFormulaVersion: 'wb-final-payout-cogs-tax-v1', settlementProfitKopecks: profit,
        settlementCogsState: 'assumed', financeState: 'ok', salesUnits: 3, returnsUnits: 1,
        revenueKopecks: 100000, commissionState: 'no_data', buyoutPct: null, marginKopecks: 999999, marginPct: 99 },
    } as LiveRepricerSkuRow
    const product = mapLiveRepricerRowToParityProduct(row, 0)
    expect(product.mgRub).toBe(profit == null ? null : profit / 200)
    expect(product.mg).toBe(profit == null ? null : profit / 1000)
    expect(product.marginMode).toBe('actual_settlement')
    expect(product.marginAssumed).toBe(true)
  })
  test.each([
    { state: 'missing', fact: null, net: null, expected: null },
    { state: 'missing', fact: 50000, net: 50000, expected: null },
    { state: 'configured', fact: 0, net: 50000, expected: 0 },
    { state: 'configured', fact: null, net: 50000, expected: null },
    { state: 'configured', fact: 25000, net: 50000, expected: 250 },
    { state: undefined, fact: undefined, net: 50000, expected: 500 },
    { state: undefined, fact: undefined, net: null, expected: null },
  ])('keeps factual $state/$fact separate from the current-price plan', ({ state, fact, net, expected }) => {
    const row = {
      meta: { articleId: 'FACT-TAX', name: 'Synthetic tax contract', status: 'auto', currentPriceKopecks: 220000, basketsLast7d: 1, basketNorm: 1 },
      settings: { cogsKopecks: 50000, logisticsKopecks: 5000, wbCommissionPct: 15, minMarginPct: 15 },
      analytics: {
        financeState: 'ok', factTaxState: state, factTaxReason: state === 'missing' ? 'tax_policy_unconfirmed' : null,
        factNetProfitKopecks: fact, netProfitKopecks: net, plannedPeriodMarginKopecks: 120000,
        marginKopecks: 10000, marginPct: 10, ordersUnits: 3, periodStatsState: 'ok',
        commissionState: 'ok', commissionSource: 'tariffs.kgvpMarketplace', commissionDisplayPct: 15,
      },
    } as LiveRepricerSkuRow
    const product = mapLiveRepricerRowToParityProduct(row, 0)
    expect(product.netSku).toBe(expected)
    expect(product.mgRub).toBeNull() // A planned margin must never fill the factual column.
    expect(product.netPerUnit).toBeNull()
    expect(product.factTaxState).toBe(state)
  })
})

test.each([undefined, null, 0, 45025])('uses only saved unit cost for the editor: %s', saved => {
  const row = { meta: { articleId: 'TW55', nmId: 123, currentPriceKopecks: 100000 },
    settings: { cogsKopecks: 45000, savedCogsKopecks: saved }, analytics: {} } as LiveRepricerSkuRow
  const product = mapLiveRepricerRowToParityProduct(row, 0)
  expect(product.savedCogs).toBe(saved == null ? null : saved / 100)
  // The existing planning fallback stays independent from the saved editor.
  expect(product.cogs).toBe(450)
})

test('renders the second ABC letter from confirmed product campaign profit despite separate cash-report blockers', () => {
  const product = mapLiveRepricerRowToParityProduct({
    meta: { articleId: 'WB-PRODUCT', nmId: 123, name: 'WB product', status: 'manual', currentPriceKopecks: 100000, basketsLast7d: 0, basketNorm: 0 },
    settings: { cogsKopecks: 38000, logisticsKopecks: 0, minMarginPct: 0, wbCommissionPct: 0 },
    analytics: { financeState: 'ok', salesUnits: 2, returnsUnits: 0, revenueKopecks: 200000,
      settlementFormulaVersion: 'wb-final-payout-cogs-tax-v1', settlementProfitKopecks: null,
      settlementBlockers: ['settlement_account_expenses_unreconciled'],
      campaignAdvertisingVersion: 'wb-repricer-campaign-advertising-v1',
      repricerEconomicsVersion: 'wb-stock-campaign-no-deduction-v1',
      campaignProfitKopecks: 50000, campaignBlockers: [],
      abcPolicyVersion: 'wb-units-profit-cumulative-80-95-v1', abcCode: 'AB' },
  } as LiveRepricerSkuRow, 0)
  expect(product.abcCode).toBe('AB')
  expect(product.marginIsSubtotal).toBe(false)
  expect(product.abcReason).toContain('Удержания исключены')
})
