import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { readFileSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { chromium, type Page } from 'playwright'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { beforeAll, expect, it } from 'vitest'
import type { CanonicalAbcPnlPage } from '../wb-finance/canonicalAbcPnl'

const row: CanonicalAbcPnlPage['items'][number] = {
  nmId: 1, sellerArticle: 'SKU-0', catalogSkuId: null, operationCount: 1,
  revenueKopecks: -123, salesRevenueKopecks: 0, returnsRevenueKopecks: 123, mainRevenueKopecks: -123,
  redemptionsRevenueKopecks: 0, lateCorrectionRevenueKopecks: 0, unknownRevenueKopecks: 0,
  salesUnits: 0, returnsUnits: 1, netUnits: -1, commissionKopecks: 0, logisticsKopecks: 0,
  storageKopecks: 0, acceptanceKopecks: 0, penaltyKopecks: 0, deductionKopecks: 0,
  additionalPaymentKopecks: 0, financeOtherExpensesKopecks: 0, compensationKopecks: 0, acquiringKopecks: 0,
  financeExpensesKopecks: 0, costValueState: 'missing', costEvidenceStatus: null, cogsKopecks: null,
  settlementProfitKopecks: null, economicsValueState: 'missing', economicsEvidenceStatus: null,
  taxKopecks: null, otherExpensesKopecks: null, profitBeforeAdsAndLoyaltyKopecks: null,
  advertisingSpendKopecks: 0, profitBeforeLoyaltyKopecks: null, cashbackAmountKopecks: 0,
  cashbackDiscountKopecks: 0, cashbackCommissionChangeKopecks: 0, loyaltyNetCostKopecks: 0,
  profitBeforeInternalExpensesKopecks: 0, profitAfterLoyaltyKopecks: 0, salesClass: 'A', profitClass: null, abcCode: null,
  internalExpensesKopecks: null, netProfitKopecks: null, blockerIds: ['WB_PNL_COST_MISSING'],
}
const payload: CanonicalAbcPnlPage = {
  items: Array.from({ length: 65 }, (_, i) => ({ ...row, nmId: 500000000 + i, sellerArticle: `SKU-${i}`, revenueKopecks: i === 64 ? 0 : -123 })),
  total: 65, limit: 500, offset: 0,
  summary: { ...row, skuCount: 65, unattributedAdvertisingSpendKopecks: null },
  meta: { state: 'partial', marketplaceAccountId: 31,
    period: { dateFrom: '2026-09-01', dateTo: '2026-09-07', timezone: 'Europe/Moscow', startAt: '2026-08-31T21:00:00Z', endExclusiveAt: '2026-09-07T21:00:00Z', days: 7, temporalState: 'complete' },
    snapshot: { syncRunId: 'synthetic', snapshotChecksum: 'synthetic-finance-checksum', formulaVersion: 'finance-v1', operationCount: 65, capturedAt: '2026-09-08T00:00:00Z', lastObservedAt: '2026-09-08T01:00:00Z' },
    formulaVersion: 'wb-abc-pnl-finance-rrdate-v4', costLedgerRevision: 2, economicsRevision: 3,
    advertisingSource: null, advertisingSnapshotChecksum: null, advertisingEvidenceStatus: null, blockerIds: ['WB_PNL_COST_MISSING'],
  }, timestamp: '2026-09-08T07:00:00Z',
}
// Summary is a distinct strict API shape, so exclude row-only evidence fields.
for (const key of ['nmId', 'sellerArticle', 'catalogSkuId', 'mainRevenueKopecks', 'redemptionsRevenueKopecks', 'lateCorrectionRevenueKopecks', 'unknownRevenueKopecks', 'additionalPaymentKopecks', 'costValueState', 'costEvidenceStatus', 'economicsValueState', 'economicsEvidenceStatus', 'salesClass', 'profitClass', 'abcCode', 'blockerIds']) delete (payload.summary as Record<string, unknown>)[key]

let bundleCode: string
beforeAll(async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({ configFile: false, envFile: false, root, logLevel: 'silent',
    plugins: [{ name: 'export-auth-fixture', enforce: 'pre', transform(source, id) {
      if (!id.endsWith('/__fixtures__/reportLoadingBrowser.tsx')) return
      return source.replace('cabinetMe: null', `cabinetMe: ({ organization: { organizationId: 7 }, user: { userId: 1, permissions: [] } } as AuthContextValue['cabinetMe'])`)
        .replace('import { MemoryRouter }', 'import { BrowserRouter }')
        .replace('<MemoryRouter initialEntries={[window.location.pathname]}>', '<BrowserRouter>')
        .replace('</MemoryRouter>', '</BrowserRouter>')
        .replace("const [accessToken, setAccessToken]", "const [organizationId, setOrganizationId] = useState(7)\n  const [accessToken, setAccessToken]")
        .replace('<AuthContext.Provider value={{ ...auth, accessToken,', `<button style={{ position: 'fixed', zIndex: 999999, top: 22, right: 0 }} onClick={() => setOrganizationId(8)}>Change synthetic account</button><button style={{ position: 'fixed', zIndex: 999999, top: 44, right: 0 }} onClick={() => setAccessToken('synthetic-replaced')}>Replace synthetic token</button><AuthContext.Provider value={{ ...auth, cabinetMe: { ...auth.cabinetMe!, organization: { ...auth.cabinetMe!.organization, organizationId } }, accessToken,`)
    } }, react()],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""', 'import.meta.env.VITE_CANONICAL_WB_ABC_PNL_ROLLOUT': '"7:31,8:32"' },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: { entry: fileURLToPath(new URL('./__fixtures__/reportLoadingBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'ReportExport' } },
  })
  const chunk = (Array.isArray(result) ? result : [result]).flatMap(result => 'output' in result ? result.output : []).find(output => output.type === 'chunk' && output.isEntry)
  if (!chunk || chunk.type !== 'chunk') throw new Error('Missing fixture bundle')
  bundleCode = chunk.code
}, 60_000)

async function mount(page: Page, tab: 'abc' | 'pnl', responseMode: 'xlsx' | '403' | 'json' | 'delayed' = 'xlsx', report = payload) {
  const errors: string[] = [], unexpected: string[] = [], network: string[] = [], requests: Array<{ headers: string[]; rows: unknown[][]; source: Record<string, unknown> }> = []
  page.on('response', response => { if (response.url().endsWith('table.xlsx')) network.push(`response ${response.status()}`) })
  page.on('requestfinished', request => { if (request.url().endsWith('table.xlsx')) network.push('finished') })
  page.on('requestfailed', request => { if (request.url().endsWith('table.xlsx')) network.push(`failed ${request.failure()?.errorText}`) })
  let release: (() => void) | undefined
  page.on('pageerror', error => errors.push(error.message))
  page.on('console', message => { if (message.type() === 'error' && !message.text().startsWith('An empty string') && !(responseMode === '403' && message.text() === 'Failed to load resource: the server responded with a status of 403 (Forbidden)')) errors.push(message.text()) })
  await page.clock.setFixedTime(new Date('2026-09-11T12:00:00Z'))
  await page.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url())
    if (url.origin === 'http://satorna.test') {
      if (url.pathname === `/wb/reports/${tab}`) return route.fulfill({ contentType: 'text/html', body: '<title>Synthetic report export</title><div id="root"></div>' })
      if (url.pathname === '/api/v2/wb/reports/table.xlsx') {
        const data = request.postDataJSON(); requests.push(data)
        if (responseMode === 'delayed') await new Promise<void>(resolve => { release = resolve })
        let body = Buffer.from('synthetic-xlsx-response')
        if (process.env.SATORNA_EXPORT_RENDERER) body = execFileSync(process.env.SATORNA_EXPORT_PYTHON!, [process.env.SATORNA_EXPORT_RENDERER], { input: JSON.stringify(data), env: process.env })
        return route.fulfill({ status: responseMode === '403' ? 403 : 200, contentType: responseMode === 'json' || responseMode === '403' ? 'application/json' : 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', body }).then(() => { network.push('fulfilled') }).catch(error => { network.push(String(error)) })
      }
      if (url.pathname === '/api/v2/wb/reports/abc-pnl') return route.fulfill({ json: { ...report, meta: { ...report.meta, marketplaceAccountId: Number(url.searchParams.get('marketplaceAccountId')), period: { ...report.meta.period, dateFrom: url.searchParams.get('dateFrom'), dateTo: url.searchParams.get('dateTo') } } } })
      if (url.pathname === '/api/v1/cabinet/team/users') return route.fulfill({ json: { data: [] } })
      if (url.pathname === '/api/v1/cabinet/wb-token') return route.fulfill({ json: { data: { userId: '1', hasToken: false, tokenMasked: null, updatedAt: null } } })
      if (url.pathname === '/api/v1/cabinet/avito-credentials') return route.fulfill({ json: { data: { userId: '1', hasCredentials: false, clientIdMasked: null, clientSecretMasked: null, accessTokenExpiresAt: null, updatedAt: null } } })
      if (url.pathname === '/api/wb/reports/pnl/latest-cache') return route.fulfill({ json: { meta: { dateRange: { from: '2026-09-01', to: '2026-09-07' }, sourceType: 'operational' }, rows: [], cashFlow: null, reportJob: null } })
      if (url.pathname === '/api/wb/reports/abc/latest-cache') return route.fulfill({ json: { rows: [] } })
    }
    if (request.resourceType() === 'image') return route.fulfill({ contentType: 'image/gif', body: Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64') })
    if (url.origin === 'https://fonts.googleapis.com') return route.fulfill({ contentType: 'text/css', body: '' })
    unexpected.push(`${request.method()} ${url.pathname}`); return route.abort()
  })
  await page.goto(`http://satorna.test/wb/reports/${tab}`)
  await page.evaluate(tab => { window.__vellaReportPeriods = { [tab]: { days: 7, mode: 'custom', fromIso: '2026-09-01', toIso: '2026-09-07', label: 'Synthetic period' } } }, tab)
  await page.addScriptTag({ content: bundleCode })
  await page.locator(`#tab-${tab} [data-report-row]`).first().waitFor({ timeout: 15000 })
  await page.evaluate(() => {
    const original = URL.revokeObjectURL
    Object.assign(window, { exportRevoked: 0 })
    URL.revokeObjectURL = url => { Object.assign(window, { exportRevoked: Number(Reflect.get(window, 'exportRevoked')) + 1 }); original(url) }
  })
  return { errors, unexpected, requests, network, release: () => release?.() }
}

async function exportTable(page: Page) {
  await page.locator('#ddExport > button').click()
  await page.locator('#ddExport').getByText('Экспорт XLSX', { exact: true }).click()
}

it.each(['abc', 'pnl'] as const)('downloads every filtered %s row and zero-row results, then revokes URLs', async tab => {
  const browser = await chromium.launch({ headless: true, channel: 'chrome' })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1512, height: 982 } })
    const evidence = await mount(page, tab)
    expect(await page.title()).toBe('Satorna — Отчёты WB')
    expect(page.url()).toContain(`/wb/reports/${tab}`)
    const search = page.locator(`#tab-${tab} .search input`)
    for (const [query, count] of [['', 65], ['SKU-64', 1], ['absent', 0]] as const) {
      await search.fill(query)
      if (tab === 'abc') await expect.poll(() => page.locator('#tab-abc [data-vella-row-count]').first().getAttribute('data-vella-row-count')).toBe(String(count))
      const headerCells = page.locator(`#tab-${tab} .${tab}-table-workspace thead th`)
      await headerCells.first().waitFor()
      const displayedHeaders = await headerCells.evaluateAll(cells => cells.map(cell => cell.childNodes[0].textContent?.trim()))
      const downloadEvent = page.waitForEvent('download', { timeout: 15000 }).catch(() => null)
      await exportTable(page)
      const download = await downloadEvent
      expect(download, `Export must initiate a browser download: ${JSON.stringify({ query, requests: evidence.requests.length, network: evidence.network, errors: evidence.errors, text: (await page.locator('body').innerText()).slice(-500) })}`).not.toBeNull()
      if (!download) throw new Error('No XLSX download')
      const body = readFileSync((await download.path())!)
      expect(body.length).toBeGreaterThan(10)
      expect(download.suggestedFilename()).toBe(`wb-${tab}-2026-09-01-2026-09-07.xlsx`)
      const request = evidence.requests.at(-1)!
      if (process.env.SATORNA_EXPORT_SCREENSHOTS) await download.saveAs(`${process.env.SATORNA_EXPORT_SCREENSHOTS}/${tab}-${count}.xlsx`)
      expect(request.rows).toHaveLength(count)
      if (tab === 'pnl') {
        expect(await page.locator('#tab-pnl .pnl-flow-item b').allTextContents())
          .toEqual([query ? '0 ₽' : '-78,72 ₽', count ? 'нет данных' : '0 ₽', '0 ₽', '0 ₽', '0 ₽', count ? 'нет данных' : '0 ₽'])
      }
      expect(request.headers).toHaveLength(tab === 'abc' ? 23 : 15)
      // The calculation breakdown is a separate table, not part of the SKU export.
      expect(request.headers).toEqual(displayedHeaders)
      expect(request.headers).toContain('Прибыль')
      expect(request.headers).not.toContain('Чистая прибыль')
      expect(request.source).toMatchObject({ state: 'partial', financeSnapshotChecksum: 'synthetic-finance-checksum', blockerIds: ['WB_PNL_COST_MISSING'] })
      if (query === 'SKU-64') {
        expect(JSON.stringify(request.rows[0])).toContain('SKU-64')
        expect(request.rows[0][request.headers.indexOf('Прибыль')]).toBeNull()
        expect(request.rows[0][request.headers.indexOf('Себестоимость')]).toBeNull()
        if (tab === 'pnl') expect(request.rows[0][request.headers.indexOf('Выручка')]).toBe(0)
        if (process.env.SATORNA_EXPORT_SCREENSHOTS) {
          await download.saveAs(`${process.env.SATORNA_EXPORT_SCREENSHOTS}/${tab}.xlsx`)
          await page.screenshot({ path: `${process.env.SATORNA_EXPORT_SCREENSHOTS}/${tab}-desktop.png` })
          await page.setViewportSize({ width: 390, height: 844 })
          await page.screenshot({ path: `${process.env.SATORNA_EXPORT_SCREENSHOTS}/${tab}-mobile.png` })
          await page.setViewportSize({ width: 1512, height: 982 })
        }
      }
    }
    await expect.poll(() => page.evaluate(() => Reflect.get(window, 'exportRevoked'))).toBe(3)
    await page.locator('#ddExport > button').click()
    await page.locator('#ddExport').getByText('История выгрузок', { exact: true }).click()
    await page.getByText('История выгрузок пока недоступна', { exact: true }).waitFor()
    expect(await page.locator('vite-error-overlay').count()).toBe(0)
    expect(evidence.errors).toEqual([]); expect(evidence.unexpected).toEqual([])
  } finally { await browser.close() }
}, 45_000)

it.each(['allocated', 'missing-cost', 'unattributed'] as const)('ties P&L selected totals to exported rows without inventing %s amounts', async source => {
  const first: CanonicalAbcPnlPage['items'][number] = {
    ...row, nmId: 500000001, sellerArticle: 'Exact-A', revenueKopecks: 10050,
    salesRevenueKopecks: 10050, returnsRevenueKopecks: 0, mainRevenueKopecks: 10050,
    salesUnits: 1, returnsUnits: 0, netUnits: 1, commissionKopecks: 120, logisticsKopecks: 220,
    storageKopecks: 30, penaltyKopecks: 40, deductionKopecks: 50, financeExpensesKopecks: 460,
    costValueState: 'configured', costEvidenceStatus: 'dated', cogsKopecks: 1000, settlementProfitKopecks: 8590,
    economicsValueState: 'configured', economicsEvidenceStatus: 'dated', taxKopecks: 0, otherExpensesKopecks: 0,
    profitBeforeAdsAndLoyaltyKopecks: 8590, advertisingSpendKopecks: 100,
    profitBeforeLoyaltyKopecks: 8490, profitBeforeInternalExpensesKopecks: 8490, profitAfterLoyaltyKopecks: 8490, internalExpensesKopecks: 0, netProfitKopecks: 8490, blockerIds: [],
  }
  const second: CanonicalAbcPnlPage['items'][number] = {
    ...first, nmId: 500000002, sellerArticle: 'Exact-B', revenueKopecks: -125,
    salesRevenueKopecks: 0, returnsRevenueKopecks: 125, mainRevenueKopecks: -125,
    salesUnits: 0, returnsUnits: 1, netUnits: -1, commissionKopecks: 0, logisticsKopecks: 0,
    storageKopecks: 0, penaltyKopecks: 0, deductionKopecks: 0, financeExpensesKopecks: 0,
    cogsKopecks: 0, settlementProfitKopecks: -125, profitBeforeAdsAndLoyaltyKopecks: -125,
    advertisingSpendKopecks: 0, profitBeforeLoyaltyKopecks: -125, profitBeforeInternalExpensesKopecks: -125, profitAfterLoyaltyKopecks: -125, netProfitKopecks: -125,
  }
  const blockerIds = source === 'allocated' ? [] : [source === 'unattributed' ? 'WB_PNL_ADVERTISING_UNATTRIBUTED' : 'WB_PNL_COST_MISSING']
  if (source === 'missing-cost') Object.assign(second, { costValueState: 'missing', costEvidenceStatus: null, cogsKopecks: null, settlementProfitKopecks: null, profitBeforeAdsAndLoyaltyKopecks: null, profitBeforeLoyaltyKopecks: null, profitBeforeInternalExpensesKopecks: null, profitAfterLoyaltyKopecks: null, netProfitKopecks: null, blockerIds })
  // Missing product attribution must not invent advertising or product profit.
  if (source === 'unattributed') for (const item of [first, second]) Object.assign(item, { advertisingSpendKopecks: null, profitBeforeLoyaltyKopecks: null, profitBeforeInternalExpensesKopecks: null, profitAfterLoyaltyKopecks: null, netProfitKopecks: null, blockerIds })
  const report: CanonicalAbcPnlPage = {
    ...payload, items: [first, second], total: 2,
    summary: { ...payload.summary, operationCount: 2, skuCount: 2, revenueKopecks: 9925,
      salesRevenueKopecks: 10050, returnsRevenueKopecks: 125, salesUnits: 1, returnsUnits: 1, netUnits: 0,
      commissionKopecks: 120, logisticsKopecks: 220, storageKopecks: 30, penaltyKopecks: 40, deductionKopecks: 50, financeExpensesKopecks: 460,
      cogsKopecks: source === 'missing-cost' ? null : 1000, settlementProfitKopecks: source === 'missing-cost' ? null : 8465,
      taxKopecks: 0, otherExpensesKopecks: 0, profitBeforeAdsAndLoyaltyKopecks: source === 'missing-cost' ? null : 8465,
      advertisingSpendKopecks: source === 'unattributed' ? 800 : 100, unattributedAdvertisingSpendKopecks: source === 'unattributed' ? 700 : 0,
      profitBeforeLoyaltyKopecks: source === 'missing-cost' ? null : source === 'unattributed' ? 7665 : 8365,
      profitBeforeInternalExpensesKopecks: source === 'allocated' ? 8365 : null, profitAfterLoyaltyKopecks: source === 'missing-cost' ? null : source === 'unattributed' ? 7665 : 8365,
      internalExpensesKopecks: 0, netProfitKopecks: source === 'allocated' ? 8365 : null,
    },
    meta: { ...payload.meta, state: source === 'allocated' ? 'ready' : 'partial', blockerIds,
      advertisingSource: 'finance_promotion', advertisingEvidenceStatus: 'raw', advertisingSnapshotChecksum: 'synthetic-ads-checksum',
      snapshot: { ...payload.meta.snapshot!, operationCount: 2 },
    },
  }
  const browser = await chromium.launch({ headless: true, channel: 'chrome' })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1512, height: 982 } })
    const evidence = await mount(page, 'pnl', 'xlsx', report)
    const surface = page.locator('#tab-pnl'), search = surface.locator('.search input')
    const totals = surface.locator('.pnl-flow-item b')
    for (const [query, selected, expected] of [
      // Storage is its own KPI; penalty and deduction stay in the other-expense
      // columns instead of being relabelled as storage in the selected totals.
      ['Exact-A', [first], ['100,5 ₽', '10 ₽', '1,2 ₽', '2,2 ₽', '0,3 ₽', source === 'unattributed' ? 'нет данных' : '84,9 ₽']],
      ['Exact-B', [second], ['-1,25 ₽', source === 'missing-cost' ? 'нет данных' : '0 ₽', '0 ₽', '0 ₽', '0 ₽', source === 'allocated' ? '-1,25 ₽' : 'нет данных']],
      ['absent', [], Array(6).fill('0 ₽')],
      ['', [first, second], ['99,25 ₽', source === 'missing-cost' ? 'нет данных' : '10 ₽', '1,2 ₽', '2,2 ₽', '0,3 ₽', source === 'allocated' ? '83,65 ₽' : 'нет данных']],
    ] as const) {
      await search.fill(query)
      await expect.poll(() => surface.locator('[data-report-row="pnl"]').count()).toBe(selected.length)
      if (process.env.SATORNA_PNL_TOTALS_SCREENSHOTS && query === 'Exact-A') {
        await page.screenshot({ path: `${process.env.SATORNA_PNL_TOTALS_SCREENSHOTS}/pnl-${source}-desktop.png` })
        await page.setViewportSize({ width: 390, height: 844 })
        await page.screenshot({ path: `${process.env.SATORNA_PNL_TOTALS_SCREENSHOTS}/pnl-${source}-mobile.png` })
        await page.setViewportSize({ width: 1512, height: 982 })
      }
      expect(await totals.allTextContents()).toEqual(expected)
      expect(await surface.locator('.pnl-flow-item').nth(4).innerText()).toContain('Хранение')
      expect(await surface.getByText(`Итоги по строкам таблицы · позиций: ${selected.length}`, { exact: true }).isVisible()).toBe(true)
      // The current workbench intentionally shows selected-row totals, not
      // the removed account-summary panel or its unfiltered profit.
      expect(await surface.locator('[data-vella-island="pnl-account-summary"]').count()).toBe(0)
      const download = page.waitForEvent('download')
      await exportTable(page); await download
      const request = evidence.requests.at(-1)!
      const moneyFields = ['Выручка', 'Себестоимость', 'Комиссия', 'Логистика', 'Хранение', 'Штрафы, ₽', 'Платная приёмка, ₽', 'Реклама', 'Налог', 'Прибыль']
      expect(request.rows.map(cells => moneyFields.map(header => cells[request.headers.indexOf(header)]))).toEqual(selected.map(item => [item.revenueKopecks / 100, item.cogsKopecks == null ? null : item.cogsKopecks / 100, item.commissionKopecks / 100, item.logisticsKopecks / 100, item.storageKopecks / 100, item.penaltyKopecks / 100, item.acceptanceKopecks / 100, item.advertisingSpendKopecks == null ? null : item.advertisingSpendKopecks / 100, 0, item.netProfitKopecks == null ? null : item.netProfitKopecks / 100]))
      expect(request.rows.map(cells => cells[request.headers.indexOf('Финальная маржа')])).toEqual(selected.map(item =>
        item.netProfitKopecks != null && item.revenueKopecks > 0
          ? item.netProfitKopecks / item.revenueKopecks * 100 : null))
      expect(request.source.blockerIds).toEqual(blockerIds)
    }
    await surface.locator('select.adv-select').selectOption('mine')
    await expect.poll(() => totals.allTextContents()).toEqual(Array(6).fill('0 ₽'))
    await surface.locator('select.adv-select').selectOption('unassigned')
    await expect.poll(() => surface.locator('[data-report-row="pnl"]').count()).toBe(2)
    expect((await totals.allTextContents())[0]).toBe('99,25 ₽')
    expect(page.url()).toBe('http://satorna.test/wb/reports/pnl')
    expect(await page.title()).toBe('Satorna — Отчёты WB')
    expect(await page.locator('vite-error-overlay').count()).toBe(0)
    expect(evidence.errors).toEqual([]); expect(evidence.unexpected).toEqual([])
  } finally { await browser.close() }
}, 45_000)

it('keeps a financial account charge visible and exported without assigning it to a product', async () => {
  const accountCharge = { ...row, nmId: null, sellerArticle: null, revenueKopecks: 0,
    returnsRevenueKopecks: 0, mainRevenueKopecks: 0, returnsUnits: 0, netUnits: 0,
    storageKopecks: 10000, advertisingSpendKopecks: 700, financeExpensesKopecks: 10000 }
  const report = { ...payload, items: [accountCharge], total: 1 }
  const browser = await chromium.launch({ headless: true, channel: 'chrome' })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block' })
    const evidence = await mount(page, 'pnl', 'xlsx', report)
    const surface = page.locator('#tab-pnl'), tableRow = surface.locator('[data-report-row="pnl"]')
    expect(await tableRow.innerText()).toContain('Нераспределённые расходы WB')
    expect(await tableRow.innerText()).toContain('100 ₽')
    expect(await tableRow.locator('a[href*="wildberries.ru/catalog"]').count()).toBe(0)
    const download = page.waitForEvent('download')
    await exportTable(page); await download
    const exported = evidence.requests[0].rows[0]
    const headers = evidence.requests[0].headers
    expect(exported[headers.indexOf('Хранение')]).toBe(100)
    expect(exported[headers.indexOf('Реклама')]).toBe(7)
    expect(exported[headers.indexOf('Прибыль')]).toBeNull()
    expect(evidence.errors).toEqual([])
  } finally { await browser.close() }
}, 45_000)

it.each([['pnl', 'period'], ['pnl', 'token'], ['pnl', 'account'], ['pnl', 'logout'], ['pnl', 'tab'], ['abc', 'period'], ['abc', 'account'], ['abc', 'token']] as const)('cancels a pending %s export after %s changes', async (tab, change) => {
  const browser = await chromium.launch({ headless: true, channel: 'chrome' })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block' })
    const evidence = await mount(page, tab, 'delayed')
    const downloads: string[] = []; page.on('download', download => downloads.push(download.suggestedFilename()))
    await exportTable(page)
    await expect.poll(() => evidence.requests.length).toBe(1)
    expect(await page.locator('#ddExport').getByRole('button', { name: 'Формируем XLSX…' }).isDisabled()).toBe(true)
    if (change === 'logout') await page.getByText('Clear synthetic report session', { exact: true }).click()
    if (change === 'token') await page.getByText('Replace synthetic token', { exact: true }).click()
    if (change === 'account') await page.getByText('Change synthetic account', { exact: true }).click()
    if (change === 'tab') {
      await page.locator('#sidebar [data-tab="abc"]').click()
      await expect.poll(() => page.url()).toContain('/wb/reports/abc')
    }
    if (change === 'period') await page.evaluate(tab => { window.__vellaReportPeriods![tab]!.fromIso = '2026-09-02'; window.dispatchEvent(new CustomEvent('vella:report-period-updated', { detail: { reportKey: tab } })) }, tab)
    evidence.release()
    await expect.poll(() => page.locator('#ddExport').getByRole('button', { name: 'Формируем XLSX…' }).count()).toBe(0)
    await page.evaluate(() => new Promise(resolve => setTimeout(resolve, 100)))
    expect(downloads).toEqual([])
    expect(evidence.requests).toHaveLength(1)
    expect(await page.evaluate(() => Reflect.get(window, 'exportRevoked'))).toBe(0)
  } finally { await browser.close() }
}, 45_000)

it.each(['403', 'json'] as const)('does not download a %s response', async mode => {
  const browser = await chromium.launch({ headless: true, channel: 'chrome' })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block' }), evidence = await mount(page, 'abc', mode)
    const downloads: string[] = []; page.on('download', download => downloads.push(download.suggestedFilename()))
    await exportTable(page)
    await expect.poll(() => evidence.requests.length).toBe(1)
    await expect.poll(() => page.locator('#ddExport').getByRole('button', { name: 'Экспорт XLSX', exact: true }).isEnabled()).toBe(true)
    expect(downloads).toEqual([])
  } finally { await browser.close() }
}, 45_000)
