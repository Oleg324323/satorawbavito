import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, expect, it, vi } from 'vitest'
import { AdsLiveKpiStripIsland, AdsTableShellIsland, adsCabinetCtrPct, adsCabinetDrrPct, adsCabinetRomiPct, aggregateAdvertisingProducts, buildAdsExportTable, saveAdsSelection, selectAdvertisingRows } from './VellaHtmlParityPage'
import { ADS_STICKY_CSS, ADS_XLSX_MIME, ADS_ROMI_DESCRIPTION, ADS_DRR_DESCRIPTION, adsCampaignIds, advertisingDrrPct, advertisingRomiPct, observeAdsStickyProductWidth, buildAdsTableExport, downloadAdsTableXlsx } from './adsTableExport'

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })
const period = { fromIso: '2026-10-01', toIso: '2026-10-09' }
const report = { filters: { dateRange: { from: period.fromIso, to: period.toIso } }, performanceComplete: true,
  cache: { fresh: true }, meta: { freshnessState: 'fresh' } }
const rows = Array.from({ length: 160 }, (_, index) => ({ nmId: 1000 + index, sku: `SKU-${index}`,
  campaignId: String(index + 42), campaignIds: [String(index + 42), '900'], productName: `Full product name ${index}`,
  adSpendKopecks: index * 100 + 25, impressions: index, clicks: 0, ctrPct: 8.6, baskets: 10,
  orders: { units: 2, kopecks: 100000 }, directCampaignIds: ['900'], attributedCampaignIds: [String(index + 42)] }))

it('shows weighted advertising CTR in the KPI strip without averaging campaign percentages', () => {
  const ctrReport = { ...report, rows: [
    { impressions: 100, adClicks: 10, clicks: 99, ctrPct: 10 },
    { impressions: 900, clicks: 30, ctrPct: 3.33 },
  ] }
  expect(adsCabinetCtrPct(ctrReport)).toBe(4)
  const html = renderToStaticMarkup(createElement(AdsLiveKpiStripIsland, { replacementKey: 'ctr', state: { status: 'ready', report: ctrReport } }))
  expect(html).toMatch(/CTR[\s\S]*?stat-val[^>]*>4%<\/div>/)
  expect(html.match(/class="stat-label"/g)).toHaveLength(7)
  for (const label of ['Расход', 'ДРР кабинета', 'CR', 'ROMI', 'Бюджет РК', 'Баланс кабинета']) expect(html).toContain(label)
})

it('uses WB campaign performance totals including unattributed impressions and clicks', () => {
  expect(adsCabinetCtrPct({ performanceTotals: { impressions: 2000, clicks: 100 },
    rows: [{ impressions: 100, clicks: 100, ctrPct: 100 }] })).toBe(5)
})

it.each([
  { rows: [] },
  { rows: [{ impressions: 0, clicks: 0 }] },
  { rows: [{ impressions: 100, clicks: 5 }, { impressions: 100, clicks: null }] },
  { rows: [{ impressions: -10, clicks: 5 }] },
  { performanceTotals: { impressions: 100, clicks: null }, rows: [{ impressions: 100, clicks: 5 }] },
])('does not manufacture CTR for missing/invalid inputs or a zero denominator (%j)', input => {
  expect(adsCabinetCtrPct(input)).toBeNull()
})

it('distinguishes zero CTR from missing and labels partial performance', () => {
  expect(adsCabinetCtrPct({ performanceTotals: { impressions: 100, clicks: 0 } })).toBe(0)
  const html = renderToStaticMarkup(createElement(AdsLiveKpiStripIsland, { replacementKey: 'partial-ctr',
    state: { status: 'ready', report: { performanceComplete: false, performanceTotals: { impressions: 100, clicks: 0 } } } }))
  expect(html).toMatch(/CTR[\s\S]*?stat-val[^>]*>0%<\/div>/)
  expect(html).toContain('полученная часть статистики WB')
})

it.each([{ status: 'loading' } as const, { status: 'error', message: 'Source unavailable' } as const])('keeps the CTR card in %s state', state => {
  const html = renderToStaticMarkup(createElement(AdsLiveKpiStripIsland, { replacementKey: 'ctr-state', state }))
  expect(html).toContain('CTR')
  expect(html).not.toContain('>0%<')
})

it('shows deposited cabinet funds and hides verbose successful money snapshots', () => {
  const html = renderToStaticMarkup(createElement(AdsLiveKpiStripIsland, { replacementKey: 'money', state: { status: 'ready', report: {
    ...report, rows: [], cabinetBalance: { netKopecks: 1234500, balanceKopecks: 0, bonusKopecks: 50000 },
    balanceRefresh: { state: 'completed', fresh: true }, budgetRefresh: { state: 'completed', collected: 2, total: 2 },
    kpis: [{ id: 'cabinet_balance', value: '0', hint: 'Текущий снимок; на 2026-10-12; отдельно: счёт' },
      { id: 'campaign_budget', value: '10000', hint: 'Уникальные РК отчёта: 2/2; свежие: 2/2. Снимок от 2026-10-12' }],
  } } }))
  expect(html).toContain((12345).toLocaleString('ru-RU') + ' ₽')
  expect(html).not.toContain('Текущий снимок')
  expect(html).not.toContain('Уникальные РК отчёта')
  expect(html).not.toContain('отдельно: счёт')
})

it.each([0, null])('keeps cabinet zero and missing funds distinct (%s)', netKopecks => {
  const html = renderToStaticMarkup(createElement(AdsLiveKpiStripIsland, { replacementKey: 'money', state: { status: 'ready', report: {
    ...report, rows: [], cabinetBalance: { netKopecks, balanceKopecks: 10000 },
    balanceRefresh: { state: 'failed', fresh: false, error: 'Ошибка обновления счёта' },
    budgetRefresh: { state: 'running', collected: 1, total: 2 },
  } } }))
  const balanceCard = html.slice(html.indexOf('Баланс кабинета'))
  expect(balanceCard).toContain(netKopecks === 0 ? '>0 ₽<' : '>—<')
  expect(balanceCard).not.toContain('>100 ₽<')
  expect(balanceCard).toContain('Ошибка обновления счёта')
  expect(html).toContain('Бюджеты загружены частично или устарели')
})

it('keeps unique real campaign IDs distinct from nmId, articles and unavailable placeholders', () => {
  expect(adsCampaignIds({ campaignId: '42', campaignIds: ['42', '43', 'unknown', '0', 'nmId-101'] })).toEqual(['42', '43'])
  const table = buildAdsExportTable([{ nmId: 101, sku: '42', campaignId: 'unknown' }, rows[0]])
  expect(table.rows[0][3]).toBeNull()
  expect(table.rows[0][2]).toBe('101')
  expect(table.rows[1].slice(3, 7)).toEqual(['42', '42, 900', '900', '42'])
  expect(buildAdsExportTable([{ nmId: 101, sku: '101' }]).rows[0][1]).toBeNull()
  expect(buildAdsExportTable([{ nmId: 101, campaignIds: ['43'], directCampaignIds: ['43'] }]).rows[0][3]).toBe('43')
})

it('resolves catalog text and seller article by nmId rather than a colliding article', () => {
  vi.stubGlobal('window', { __vellaAdsProductLookup: [
    { nmId: 999, sku: '101', name: 'Wrong product' },
    { nmId: 101, sku: 'REAL-ARTICLE', name: 'Correct complete product name' },
  ] })
  const table = buildAdsExportTable([{ nmId: 101, sku: '101', campaignId: '42' }])
  expect(table.rows[0].slice(0, 4)).toEqual(['Correct complete product name', 'REAL-ARTICLE', '101', '42'])
})

it('recovers authoritative IDs from direct/attributed cached mappings without inventing them from nmId', () => {
  const row = { nmId: 101, directCampaignIds: ['43', '43', 'invalid'], attributedCampaignIds: ['42', '0'] }
  expect(adsCampaignIds(row)).toEqual(['42', '43'])
  expect(buildAdsExportTable([row]).rows[0][3]).toBe('42, 43')
  expect(selectAdvertisingRows([row], { query: '42', chip: 'все строки' }, null)).toHaveLength(1)
  expect(adsCampaignIds({ nmId: 101 } as Parameters<typeof adsCampaignIds>[0])).toEqual([])
})

it('exports every filtered and sorted row beyond the render window with typed numbers and nulls', () => {
  const selected = selectAdvertisingRows(rows, { query: 'full product', chip: 'все строки' }, { column: 11, direction: -1 })
  const table = buildAdsExportTable(selected.map(item => item.row))
  const payload = buildAdsTableExport(table, report, period, period, 'Расход по убыванию')
  expect(payload.rows).toHaveLength(160)
  expect(payload.rows[0][0]).toBe('Full product name 159')
  expect(payload.rows[0][payload.headers.indexOf('Расход')]).toBe(159.25)
  expect(payload.rows[159][payload.headers.indexOf('Клики')]).toBe(0)
  expect(payload.rows[159][payload.headers.indexOf('CTR')]).toBe(0.086)
  expect(payload.headers).not.toContain('Продажи, шт.')
  expect(payload.headers).not.toContain('Продажи, ₽')
  expect(payload.rows.every(row => row.length === payload.headers.length)).toBe(true)
  const filtered = selectAdvertisingRows(rows, { query: 'SKU-159', chip: 'все строки' }, null)
  expect(filtered).toHaveLength(1)
  expect(buildAdsExportTable(filtered.map(item => item.row)).rows[0][2]).toBe('1159')
})

it('binds export to the exact applied inclusive period and rejects old-period or unbound reports', () => {
  const selected = { fromIso: '2026-09-02', toIso: '2026-09-03' }
  const table = buildAdsExportTable(rows)
  const payload = buildAdsTableExport(table, { ...report, filters: { dateRange: { from: selected.fromIso, to: selected.toIso } } }, selected, selected, '')
  expect(payload).toMatchObject({ reportKind: 'ads', sourceKind: 'loaded-ads', dateFrom: selected.fromIso, dateTo: selected.toIso })
  expect(() => buildAdsTableExport(table, report, selected, period, '')).toThrow('выбранный период')
  expect(() => buildAdsTableExport(table, report, selected, selected, '')).toThrow('выбранный период')
  expect(() => buildAdsTableExport(table, {}, period, period, '')).toThrow('выбранный период')
})

it('marks partial/stale data and keeps an unavailable metric distinct from zero', () => {
  const table = buildAdsExportTable([{ nmId: 101, adSpendKopecks: 0 }, { nmId: 102 }])
  const payload = buildAdsTableExport(table, { ...report, cache: { fresh: false }, warning: 'Missing source days' }, period, period, '')
  expect(payload.sourceState).toBe('stale')
  expect(payload.warning).toContain('Missing source days')
  expect(payload.rows[0][payload.headers.indexOf('Расход')]).toBe(0)
  expect(payload.rows[1][payload.headers.indexOf('Расход')]).toBeNull()
  expect(buildAdsTableExport(table, { ...report, performanceComplete: false }, period, period, '').sourceState).toBe('partial')
})

it('uses the authenticated advertising endpoint without canonical/account payload', async () => {
  const fetcher = vi.fn(async () => new Response('xlsx', { headers: { 'content-type': ADS_XLSX_MIME } }))
  vi.stubGlobal('fetch', fetcher)
  const payload = buildAdsTableExport(buildAdsExportTable(rows), report, period, period, '')
  expect(await (await downloadAdsTableXlsx('synthetic', payload)).text()).toBe('xlsx')
  expect(fetcher.mock.calls[0]).toEqual(['/api/wb/reports/ads/table.xlsx', expect.objectContaining({
    method: 'POST', body: JSON.stringify(payload), headers: expect.objectContaining({ Authorization: 'Bearer synthetic' }),
  })])
  expect(payload).not.toHaveProperty('marketplaceAccountId')
})

it('rejects downloading a snapshot when the selection changes during export', async () => {
  const payload = buildAdsTableExport(buildAdsExportTable(rows), report, period, period, '')
  let selected = payload
  vi.stubGlobal('fetch', vi.fn(async () => {
    selected = { ...payload, filterDescription: 'Changed filter' }
    return new Response('xlsx', { headers: { 'content-type': ADS_XLSX_MIME } })
  }))
  // This must fail before any DOM/download API is used (the test is Node-only).
  await expect(saveAdsSelection('synthetic', () => selected, new AbortController().signal))
    .rejects.toThrow('Период, фильтры или данные изменились')
})

it('renders the sticky product header/cells and IDs consistently; CSS is advertising-only', () => {
  vi.spyOn(console, 'error').mockImplementation(() => undefined)
  const html = renderToStaticMarkup(createElement(AdsTableShellIsland, { replacementKey: 'ads-test', state: { status: 'ready', report: { ...report, rows } } }))
  expect(html).toContain('<th class="report-sticky">Товар')
  expect(html).toContain('<th class="report-sticky">ID рекламы')
  expect(html).toContain('<td class="ads-entity-cell report-sticky">')
  expect(html).toContain('ID рекламы')
  expect(html).toContain('Все ID товара: 42, 900')
  expect(html).toContain('атрибуция за период')
  expect(html).not.toMatch(/>РК \/ источник|>Тип РК|>Дней/)
  expect(html).not.toContain('>Продажи')
  expect(html).toContain('Full product name 0')
  expect(html.match(/data-report-row="ads"/g)).toHaveLength(50)
  expect(ADS_STICKY_CSS).toContain('position: sticky; left: 0; transform: none')
  expect(ADS_STICKY_CSS).toContain('table.report-mid { overflow: visible; }') // hidden table overflow trapped sticky cells
  expect(ADS_STICKY_CSS).toContain('td:nth-child(2)')
  expect(ADS_STICKY_CSS).toContain('left: var(--ads-product-width, 360px)')
  expect(ADS_STICKY_CSS).toContain('[data-vella-sticky-tab="tab-ads"] table.report-mid th:nth-child(2)')
  expect(ADS_STICKY_CSS).not.toContain('overflow-y: hidden')
  expect(ADS_STICKY_CSS).not.toContain('#tab-pnl')
})

it('shows product totals across campaigns instead of the first zero-activity conversion row', () => {
  const campaigns = [
    { nmId: 101, campaignId: '42', productName: 'Complete product title', impressions: 0, clicks: 0,
      adSpendKopecks: 0, baskets: 1, orders: { units: 0, kopecks: 0 }, directCampaignIds: ['42', '43'], attributedCampaignIds: ['42'], budgetTotalKopecks: 1000 },
    { nmId: 101, campaignId: '43', productName: 'Complete product title', impressions: 200, clicks: 20,
      adSpendKopecks: 600, baskets: 9, orders: { units: 2, kopecks: 24000 }, directCampaignIds: ['42', '43'], attributedCampaignIds: ['43'], budgetTotalKopecks: 2000 },
  ]
  const selected = selectAdvertisingRows(campaigns, { query: '', chip: 'все строки' }, null)
  expect(selected).toHaveLength(1)
  expect(selected[0].row).toMatchObject({ nmId: 101, campaignId: null, campaignIds: ['42', '43'],
    directCampaignIds: ['42', '43'], attributedCampaignIds: ['42', '43'], impressions: 200, clicks: 20,
    adSpendKopecks: 600, baskets: 10, ctrPct: 10, drrPct: 2.5, budgetTotalKopecks: 3000 })
  const table = buildAdsExportTable(selected.map(item => item.row))
  const get = (label: string) => table.rows[0][table.headers.indexOf(label)]
  expect(get('Товар')).toBe('Complete product title')
  expect(get('ID рекламы')).toBe('42, 43')
  expect(get('Показы')).toBe(200)
  expect(get('Клики')).toBe(20)
  expect(get('CTR')).toBe(0.1)
  expect(get('CR')).toBe(0.2)
  expect(get('ДРР')).toBe(0.025)
  for (const removed of ['РК / источник', 'Тип РК', 'Дней']) expect(table.headers).not.toContain(removed)
  vi.spyOn(console, 'error').mockImplementation(() => undefined)
  const html = renderToStaticMarkup(createElement(AdsTableShellIsland, { replacementKey: 'totals', state: { status: 'ready', report: { ...report, rows: campaigns } } }))
  expect(html.match(/data-report-row="ads"/g)).toHaveLength(1)
  expect(html).toContain('Complete product title')
  expect(html).toContain('200</td>')
  expect(html).toContain('итог по кампаниям за период')
  expect(selectAdvertisingRows(campaigns, { query: '43', chip: 'все строки' }, null)[0].row.impressions).toBe(200)
})

it('does not replace missing campaign metrics with zero or assign unattributed amounts to a product', () => {
  const source = [
    { nmId: 101, campaignId: '42', impressions: 0, clicks: 0, adSpendKopecks: 0, sales: { kopecks: 500 } },
    { nmId: 101, campaignId: '43', impressions: null, clicks: 0, adSpendKopecks: null },
    { nmId: 0, campaignId: '42', impressions: 7, adSpendKopecks: 200 },
    { nmId: 0, campaignId: '43', impressions: 9, adSpendKopecks: 300 },
  ]
  const original = JSON.stringify(source)
  const grouped = aggregateAdvertisingProducts(source)
  expect(grouped).toHaveLength(3)
  expect(grouped[0]).toMatchObject({ impressions: null, clicks: 0, adSpendKopecks: null, sales: { kopecks: null } })
  const table = buildAdsExportTable(grouped)
  expect(table.rows[0][table.headers.indexOf('ROMI')]).toBeNull()
  expect(grouped.slice(1).map(row => row.adSpendKopecks)).toEqual([200, 300])
  expect(JSON.stringify(source)).toBe(original)
})

it('calculates order-based ROMI without unavailable buyouts or unrelated financial fields', () => {
  expect(advertisingRomiPct(24000, 600)).toBe(3900)
  expect(advertisingRomiPct(600, 600)).toBe(0)
  expect(advertisingRomiPct(0, 600)).toBe(-100)
  expect(advertisingRomiPct(300, 600)).toBe(-50)
  for (const [revenue, spend] of [[null, 600], [24000, null], [24000, 0], [0, 0], [24000, -1], [NaN, 600], [24000, Infinity]]) {
    expect(advertisingRomiPct(revenue, spend)).toBeNull()
  }
  const campaignRows = [
    { nmId: 101, campaignId: '42', productName: 'Test product', adSpendKopecks: 100, orders: { kopecks: 3000 } },
    { nmId: 101, campaignId: '43', productName: 'Test product', adSpendKopecks: 500, orders: { kopecks: 1000 },
      sales: { kopecks: 999999 }, cogsKopecks: 999999, commissionKopecks: 999999 },
  ]
  const grouped = aggregateAdvertisingProducts(campaignRows)
  const table = buildAdsExportTable(grouped)
  const ratio = table.rows[0][table.headers.indexOf('ROMI')]
  expect(ratio).toBeCloseTo((4000 - 600) / 600, 10) // weighted totals, not the mean of campaign ROMIs
  const payload = buildAdsTableExport(table, report, period, period, '')
  expect(payload.warning).toContain(ADS_ROMI_DESCRIPTION)
  vi.spyOn(console, 'error').mockImplementation(() => undefined)
  const html = renderToStaticMarkup(createElement(AdsTableShellIsland, { replacementKey: 'romi', state: { status: 'ready', report: { ...report, rows: campaignRows } } }))
  expect(html).toContain('566,67%</td>')
  expect(html).not.toContain('>Продажи')
  expect(table.rows.every(row => row.length === table.headers.length)).toBe(true)
})

it('sorts ROMI by recalculated product totals and keeps missing values last', () => {
  const source = [
    { nmId: 101, adSpendKopecks: 100, orders: { kopecks: 100 } },
    { nmId: 102, adSpendKopecks: 100, orders: { kopecks: 200 } },
    { nmId: 103, adSpendKopecks: 0, orders: { kopecks: 99999 } },
  ]
  expect(selectAdvertisingRows(source, { query: '', chip: 'все строки' }, { column: 14, direction: -1 }).map(item => item.row.nmId)).toEqual([102, 101, 103])
})

it('keeps kopeck precision and uses weighted attributed orders for table, KPI and export ratios', () => {
  const example = { nmId: 101, orders: { kopecks: 3920000 }, adSpendKopecks: 300576, drrPct: 99 }
  expect(advertisingRomiPct(3920000, 300576)).toBeCloseTo(1204.16, 2)
  expect(advertisingRomiPct(3920000, 300600)).toBeCloseTo(1204.06, 2)
  expect(advertisingDrrPct(3920000, 300576)).toBeCloseTo(7.66775, 4)
  expect(advertisingDrrPct(1000, 0)).toBe(0)
  for (const [revenue, spend] of [[0, 100], [null, 100], [100, null], [NaN, 100], [100, Infinity]]) {
    expect(advertisingDrrPct(revenue, spend)).toBeNull()
  }
  const table = buildAdsExportTable([example])
  expect(table.rows[0][table.headers.indexOf('ROMI')]).toBeCloseTo((3920000 - 300576) / 300576, 8)
  expect(table.rows[0][table.headers.indexOf('ДРР')]).toBeCloseTo(0.0766775, 6)
  const dataset = [example, { nmId: 102, orders: { kopecks: 100000 }, adSpendKopecks: 100000 }]
  expect(adsCabinetDrrPct({ rows: dataset })).toBeCloseTo(400576 / 4020000 * 100, 8)
  expect(adsCabinetRomiPct({ rows: dataset })).toBeCloseTo((4020000 - 400576) / 400576 * 100, 8)
  expect(adsCabinetDrrPct({ rows: [...dataset, { nmId: 103 }] })).toBeNull()
  expect(adsCabinetRomiPct({ rows: [...dataset, { nmId: 103 }] })).toBeNull()
  expect(buildAdsTableExport(table, report, period, period, '').warning).toContain(ADS_DRR_DESCRIPTION)
  vi.spyOn(console, 'error').mockImplementation(() => undefined)
  const html = renderToStaticMarkup(createElement(AdsTableShellIsland, { replacementKey: 'precision', state: { status: 'ready', report: { ...report, rows: [example] } } }))
  expect(html).toContain('1\u00a0204,16%</td>')
  expect(html).toContain('7,67%</td>')
})

it('tracks the actual product-column width for sticky ID offsets and disconnects on unmount', () => {
  let width = 384
  let resized: (() => void) | undefined
  const header = { getBoundingClientRect: () => ({ width }) }
  const values = new Map<string, string>()
  const style = { getPropertyValue: (key: string) => values.get(key), setProperty: vi.fn((key: string, value: string) => values.set(key, value)) }
  const wrap = { querySelector: () => header, style } as unknown as HTMLElement
  const observe = vi.fn(), disconnect = vi.fn(), addEventListener = vi.fn(), removeEventListener = vi.fn()
  vi.stubGlobal('window', { addEventListener, removeEventListener })
  vi.stubGlobal('ResizeObserver', class {
    constructor(callback: () => void) { resized = callback }
    observe = observe
    disconnect = disconnect
  })
  const cleanup = observeAdsStickyProductWidth(wrap)
  expect(values.get('--ads-product-width')).toBe('384px')
  expect(observe).toHaveBeenCalledWith(header)
  resized?.()
  expect(style.setProperty).toHaveBeenCalledTimes(1) // no style/mutation loop
  width = 432
  resized?.()
  expect(values.get('--ads-product-width')).toBe('432px')
  width = 0
  resized?.()
  expect(values.get('--ads-product-width')).toBe('432px') // hidden tab must not reset the offset
  cleanup()
  expect(disconnect).toHaveBeenCalledOnce()
  expect(removeEventListener).toHaveBeenCalledWith('resize', expect.any(Function))
})
