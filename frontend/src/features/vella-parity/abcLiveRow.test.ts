import { describe, expect, it, vi } from 'vitest'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import { AbcKpiStripIsland, ABC_TABLE_COLUMNS, buildAbcExportTable, installAbcLiveDataBridge, mapBackendAbcRowToParity, renderAbcRowHtml } from './VellaHtmlParityPage'

describe('ABC live row', () => {
  it('keeps precise ruble expenses, zero, deltas and campaign spend separate in cells and export', () => {
    const source = { sku: 'TEST', nmId: 101, commissionKopecks: 12345, logisticsKopecks: -100,
      storageKopecks: 0, commissionCostPct: 12.3, logisticsCostPct: -0.1, storageCostPct: 0,
      commissionDeltaPct: 77, adSpendKopecks: 500, campaignAdSpendKopecks: 9900, drrSalesPct: 0.5 }
    const row = mapBackendAbcRowToParity(source)
    expect(row.costs).toBe('−1 ₽ · -0,1% / 123,45 ₽ · 12,3% / 0 ₽ · 0%')
    expect(row.ads).toBe('5 ₽ / 0,5%')
    expect(row.campaignAds).toBe('Кампании: 99 ₽')
    const exported = buildAbcExportTable([source])
    const values = Object.fromEntries(exported.headers.map((key, index) => [key, exported.rows[0][index]]))
    expect(values['Комиссия, ₽']).toBe('123,45 ₽')
    expect(values['Хранение, ₽']).toBe('0 ₽')
    expect(values['Реклама: фин. списания, ₽']).toBe('5 ₽')
    expect(values['Реклама: кампании, ₽']).toBe('99 ₽')
    expect(mapBackendAbcRowToParity({ commissionKopecks: null }).costs).toContain('—')
  })

  it('does not create product links or drawer actions for an unallocated cabinet entry', () => {
    Object.defineProperty(globalThis, 'window', { configurable: true, value: {
      reportSkuOpenAttrs: () => 'data-unsafe-product-action', reportThumb: () => '',
    } })
    const source = { isUnallocated: true, sku: 'Нераспределённые расходы WB', storageKopecks: 10000 }
    const html = renderAbcRowHtml(mapBackendAbcRowToParity(source), 0)
    expect(html).toContain('100 ₽')
    expect(html).not.toContain('wildberries.ru/catalog/')
    expect(html).not.toContain('data-unsafe-product-action')
    expect(buildAbcExportTable([source]).rows[0].at(-1)).toBe('Нераспределённые начисления кабинета')
  })

  it('renders WB-backed values in the same order as the table headers', () => {
    Object.defineProperty(globalThis, 'window', {
      configurable: true,
      value: {
        reportSkuOpenAttrs: () => '',
        reportThumb: () => '',
        abcBadgeClass: () => 'aa',
        promoStateHTML: (value: string) => value,
        managerCellHTML: () => 'manager',
        pairMetricCell: (value: string) => value,
        reportCommentCell: () => 'comment',
        warehouseMetrics: () => ({ ktr: 9.99, localization: 77, tag: 'warn' }),
      },
    })

    const row = mapBackendAbcRowToParity({
      sku: 'FBBT_11',
      nmId: 111,
      productName: 'Футболка',
      productStatus: 'locomotive',
      abcCode: 'AA',
      promotionStatus: 'no',
      priceBeforeSppKopecks: 130_000,
      priceWithSppKopecks: 110_000,
      cogsPerUnitKopecks: 45_000,
      cogsKopecks: 90_000,
      marginKopecks: 65_000,
      marginPct: 50,
      marginDeltaPct: null,
      impressions: 1_000,
      clicks: 200,
      clicksDeltaPct: 10,
      ctrPct: 20,
      baskets: 90,
      basketsDeltaPct: 30,
      cartCrPct: 44.44,
      ordersComposite: { units: 40, kopecks: 4_000_000, deltaPct: 25 },
      salesComposite: { units: 25, kopecks: 2_500_000, deltaPct: -10 },
      adSpendKopecks: 50_000,
      drrSalesPct: 2,
      netTotalKopecks: 650_000,
      netPerUnitKopecks: 65_000,
      logisticsCostPct: 5,
      logisticsDeltaPct: null,
      commissionCostPct: 10,
      commissionDeltaPct: null,
      storageCostPct: 1,
      storageDeltaPct: null,
      ktrIndex: null,
      localizationPct: 46,
      wbStockUnits: 15,
      wbStockKopecks: 1_650_000,
      ruleEvaluation: { status: 'risk' },
    })
    const html = renderAbcRowHtml(row, 0)
    const cells = Array.from(html.matchAll(/data-vella-cell="([^"]+)"/g), (match) => match[1])

    expect(ABC_TABLE_COLUMNS.map((column) => column.label)).not.toContain('Показы')
    expect(ABC_TABLE_COLUMNS.map((column) => column.label)).not.toContain('КТР / локализация')
    expect(ABC_TABLE_COLUMNS.find((column) => column.column === 'clicks')?.label).toBe('Переходы')
    expect(ABC_TABLE_COLUMNS.find((column) => column.column === 'cr')?.label).toBe('Конверсия в заказ')
    expect(cells).toEqual([
      'abc-product-cell', 'abc-wb-cell', 'abc-status-cell', 'abc-code-cell', 'abc-action-cell', 'abc-promo-cell',
      'abc-manager-cell', 'abc-price-cell', 'abc-spp-cell', 'abc-cogs-cell', 'abc-margin-cell', 'abc-clicks-cell',
      'abc-baskets-cell', 'abc-cr-cell', 'abc-orders-cell', 'abc-sales-cell', 'abc-ads-cell', 'abc-net-cell',
      'abc-logistics-cell', 'abc-commission-cell', 'abc-storage-cell', 'abc-stock-cell', 'abc-comment-cell',
    ])
    expect(cells).toHaveLength(ABC_TABLE_COLUMNS.length)
    expect(ABC_TABLE_COLUMNS.slice(0, 2).every((column) => column.className?.includes('report-sticky'))).toBe(true)
    expect(html).toContain('class="report-sticky abc-sticky-wb" data-vella-cell="abc-wb-cell"')
    expect(row).toMatchObject({
      price: '1 300 ₽',
      spp: 'с СПП 1 100 ₽',
      cogs: '450 ₽',
      margin: '650 ₽ / 50%',
      views: '1 000',
      clicks: '200',
      clicksSub: 'CTR 20% · ↑ +10%',
      basketDelta: '↑ +30%',
      cr: '44,4%',
      warehouse: '—',
      localization: '46%',
    })
    expect(String(row.orders)).toContain('+25%')
    expect(String(row.sales)).toContain('−10%')
    expect(row.action).toBe('аудит')
    expect(html).not.toContain('9.99')
    expect(html).not.toContain('лок. 77%')
    expect(mapBackendAbcRowToParity({ priceWithSppKopecks: 110_000 }).price).toBe('—')
    expect(mapBackendAbcRowToParity({ abcCode: 'CC', ruleEvaluation: { status: 'normal' } }).action).toBe('наблюдать')
  })

  it('renders the backend attention count instead of a local ABC heuristic', () => {
    Object.defineProperty(globalThis, 'window', {
      configurable: true,
      value: {
        __vellaAbcLiveLoading: false,
        __vellaAbcLiveRows: [],
        __vellaAbcLiveReport: {
          filteredSummary: { attentionCount: 2, ordersCount: 0, profitKopecks: 0 },
          rows: [
            { abcCode: 'AA', ruleEvaluation: { status: 'risk' }, salesComposite: { kopecks: 0 } },
            { abcCode: 'BB', ruleEvaluation: { status: 'risk' }, salesComposite: { kopecks: 0 } },
          ],
        },
      },
    })

    const html = renderToStaticMarkup(createElement(AbcKpiStripIsland, { replacementKey: 'abc-test' }))

    expect(html).toContain('Требуют внимания')
    expect(html).toContain('<div class="stat-val">2</div>')
  })

  it('renders canonical sales class and preliminary profit without inventing final values', () => {
    Object.defineProperty(globalThis, 'window', {
      configurable: true,
      value: {
        reportSkuOpenAttrs: () => '',
        reportThumb: () => '',
        abcBadgeClass: () => 'neutral',
        pairMetricCell: (value: string) => value,
      },
    })
    const unavailable = mapBackendAbcRowToParity({
      sku: 'FBBT_11',
      nmId: 111,
      salesClass: 'A',
      abcCode: null,
      profitAfterLoyaltyKopecks: null,
      adSpendKopecks: 0,
      canonicalSourceState: 'partial',
      blockerIds: ['WB_PNL_COST_ASSUMED'],
      salesComposite: { units: 1, kopecks: 10_000, deltaPct: null },
    })

    expect(unavailable).toMatchObject({
      abc: 'A·—',
      net: '—',
      netCls: '',
      status: 'нет данных',
      action: 'нет данных',
      orders: '—',
      ordersKnown: false,
      profitKnown: false,
      adsKnown: true,
    })
    expect(renderAbcRowHtml(unavailable, 0)).toContain('Прибыль не подтверждена')

    const zero = mapBackendAbcRowToParity({
      salesClass: null,
      abcCode: null,
      netProfitKopecks: 0,
      canonicalSourceState: 'ready',
    })
    expect(zero).toMatchObject({ abc: '—', net: '0 ₽', profitKnown: true })
  })

  it('uses canonical no-access copy only while the canonical rollout is active', async () => {
    function installWindow() {
      Object.defineProperty(globalThis, 'window', {
        configurable: true,
        value: Object.assign(new EventTarget(), {
          location: { pathname: '/wb/reports/abc', search: '', origin: 'http://localhost' },
          localStorage: { getItem: () => null, setItem: () => undefined },
          setTimeout: (callback: () => void) => { callback(); return 0 },
          __vellaReportPeriods: {
            abc: { days: 7, fromIso: '2026-09-01', toIso: '2026-09-07', label: '7 дней', mode: 'custom' as const },
          },
          __vellaPublishAbcRowsSnapshot: () => undefined,
        }),
      })
    }

    const deniedResponse = () => new Response(JSON.stringify({ detail: 'legacy forbidden' }), {
      status: 403,
      headers: { 'content-type': 'application/json' },
    })
    vi.spyOn(console, 'warn').mockImplementation(() => undefined)

    installWindow()
    const canonicalFetch = vi.fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>(async () => deniedResponse())
    vi.stubGlobal('fetch', canonicalFetch)
    installAbcLiveDataBridge('token', { organizationId: 7, marketplaceAccountId: 31 })
    await window.__vellaLoadLiveAbcReport?.()
    expect(String(canonicalFetch.mock.calls[0]?.[0])).toContain('/api/v2/wb/reports/abc-pnl?')
    expect(window.__vellaAbcLiveAccessDenied).toBe(true)
    expect(window.__vellaAbcLiveError).toContain('canonical ABC/P&L')

    installWindow()
    const legacyFetch = vi.fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>(async () => deniedResponse())
    vi.stubGlobal('fetch', legacyFetch)
    installAbcLiveDataBridge('token')
    await window.__vellaLoadLiveAbcReport?.()
    expect(String(legacyFetch.mock.calls[0]?.[0])).toContain('/api/wb/reports/abc/latest-cache?')
    expect(window.__vellaAbcLiveAccessDenied).toBe(false)
    expect(window.__vellaAbcLiveError).toBe('legacy forbidden')

    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('publishes canonical loading and generic error states without inventing no-access', async () => {
    Object.defineProperty(globalThis, 'window', {
      configurable: true,
      value: Object.assign(new EventTarget(), {
        location: { pathname: '/wb/reports/abc', search: '', origin: 'http://localhost' },
        localStorage: { getItem: () => null, setItem: () => undefined },
        setTimeout: (callback: () => void) => { callback(); return 0 },
        __vellaReportPeriods: {
          abc: { days: 7, fromIso: '2026-09-01', toIso: '2026-09-07', label: '7 дней', mode: 'custom' as const },
        },
        __vellaPublishAbcRowsSnapshot: () => undefined,
      }),
    })
    let rejectRequest!: (reason: Error) => void
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>((_resolve, reject) => {
      rejectRequest = reject
    })))
    vi.spyOn(console, 'warn').mockImplementation(() => undefined)

    installAbcLiveDataBridge('token', { organizationId: 7, marketplaceAccountId: 31 })
    const load = window.__vellaLoadLiveAbcReport?.()
    expect(window.__vellaAbcLiveLoading).toBe(true)
    rejectRequest(new Error('network down'))
    await load

    expect(window.__vellaAbcLiveLoading).toBe(false)
    expect(window.__vellaAbcLiveAccessDenied).toBe(false)
    expect(window.__vellaAbcLiveError).toBe('network down')

    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('revalidates a warm period from DB and ignores a late response for a cancelled period', async () => {
    vi.stubGlobal('document', { getElementById: () => null })
    const periods = {
      first: { days: 7, fromIso: '2026-08-18', toIso: '2026-08-24', label: '7 дней', mode: 'custom' as const },
      second: { days: 14, fromIso: '2026-08-11', toIso: '2026-08-24', label: '14 дней', mode: 'custom' as const },
    }
    const storage = new Map<string, string>()
    Object.defineProperty(globalThis, 'window', {
      configurable: true,
      value: Object.assign(new EventTarget(), {
        location: { pathname: '/wb/reports/abc', search: '', origin: 'http://localhost' },
        localStorage: {
          getItem: (key: string) => storage.get(key) ?? null,
          setItem: (key: string, value: string) => storage.set(key, value),
        },
        setTimeout: (callback: () => void) => { callback(); return 0 },
        __vellaReportPeriods: { abc: periods.first },
        __vellaPublishAbcRowsSnapshot: () => undefined,
        applyAbcFilter: () => undefined,
      }),
    })
    let resolveSecond!: (response: Response) => void
    let firstVisits = 0
    const fetchMock = vi.fn((input: string | URL | Request) => {
      const url = String(input)
      const sku = url.includes('2026-08-11') ? 'SECOND' : ++firstVisits === 1 ? 'FIRST' : 'UPDATED-FIRST'
      const response = new Response(JSON.stringify({ rows: [{ sku }], filteredSummary: {}, cache: { fresh: true } }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })
      return sku === 'SECOND'
        ? new Promise<Response>((resolve) => { resolveSecond = resolve })
        : Promise.resolve(response)
    })
    vi.stubGlobal('fetch', fetchMock)

    try {
      installAbcLiveDataBridge('token')
      await window.__vellaLoadLiveAbcReport?.()
      window.__vellaReportPeriods = { abc: periods.second }
      const secondLoad = window.__vellaLoadLiveAbcReport?.()
      await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2))
      window.__vellaReportPeriods = { abc: periods.first }
      await window.__vellaLoadLiveAbcReport?.()
      resolveSecond(new Response(JSON.stringify({ rows: [{ sku: 'SECOND' }], filteredSummary: {} }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }))
      await secondLoad

      expect(fetchMock).toHaveBeenCalledTimes(3)
      expect(window.__vellaAbcLiveRows?.[0]?.sku).toBe('UPDATED-FIRST')
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('explains every SPP bar on hover instead of colouring it by direction', () => {
    Object.defineProperty(globalThis, 'window', { configurable: true, value: {
      reportSkuOpenAttrs: () => '', reportThumb: () => '', abcBadgeClass: () => 'aa',
      promoStateHTML: (value: string) => value, managerCellHTML: () => 'manager',
      pairMetricCell: (value: string) => value, reportCommentCell: () => 'comment',
      warehouseMetrics: () => ({ ktr: null, localization: null, tag: '' }),
    } })
    const history = Array.from({ length: 14 }, (_, index) => ({
      date: `2026-10-${String(index + 1).padStart(2, '0')}`, pct: null as number | null,
      buyerPriceKopecks: null as number | null, source: null as string | null,
    }))
    history[11] = { date: '2026-10-12', pct: 20, buyerPriceKopecks: 99_000, source: 'orders' }
    history[13] = { date: '2026-10-14', pct: 25.5, buyerPriceKopecks: 95_000, source: 'extension' }
    const html = renderAbcRowHtml(mapBackendAbcRowToParity({
      sku: 'FBBT_1', nmId: 443894764, sppPct: 25.5, sppBuyerPriceKopecks: 95_000,
      sppObservedOn: '2026-10-14', sppSource: 'current_buyer_price', sppHistory: history,
    }), 0)
    const bars = Array.from(html.matchAll(/<i [^>]*>/g), (match) => match[0])

    expect(bars).toHaveLength(14)
    expect(bars.every((bar) => bar.includes('data-chart-tip="1"'))).toBe(true)
    expect(html).not.toMatch(/class="(up|down|flat)"/)
    expect(bars[0]).toContain('data-chart-title="01.10.2026, чт"')
    expect(bars[0]).toContain('нет наблюдения')
    expect(bars[0]).not.toContain('нет заказов')
    expect(bars[11]).toContain('СПП|20%')
    expect(bars[11]).toContain('Цена покупателя|990 ₽')
    expect(bars[11]).toContain('заказы WB')
    expect(bars[13]).toContain('class="last"')
    expect(bars[13]).toContain('К 12.10|▲ +5,5 пп')
    expect(bars[13]).toContain('расширение')
    expect(html).toContain('14.10')
  })
})
