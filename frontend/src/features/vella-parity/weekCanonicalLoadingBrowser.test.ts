import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { expect, it } from 'vitest'

function emptyFinance(from: string, to: string) {
  const zero = 'operationCount skuCount revenueKopecks salesRevenueKopecks returnsRevenueKopecks salesUnits returnsUnits netUnits commissionKopecks logisticsKopecks storageKopecks acceptanceKopecks penaltyKopecks deductionKopecks financeOtherExpensesKopecks compensationKopecks acquiringKopecks financeExpensesKopecks'
  const unknown = 'cogsKopecks settlementProfitKopecks taxKopecks otherExpensesKopecks profitBeforeAdsAndLoyaltyKopecks advertisingSpendKopecks unattributedAdvertisingSpendKopecks profitBeforeLoyaltyKopecks cashbackAmountKopecks cashbackDiscountKopecks cashbackCommissionChangeKopecks loyaltyNetCostKopecks profitAfterLoyaltyKopecks netProfitKopecks'
  return { items: [], total: 0, limit: 500, offset: 0,
    summary: { ...Object.fromEntries(zero.split(' ').map(key => [key, 0])), ...Object.fromEntries(unknown.split(' ').map(key => [key, null])) },
    meta: { state: 'empty', marketplaceAccountId: 31,
      period: { dateFrom: from, dateTo: to, timezone: 'Europe/Moscow', startAt: new Date(`${from}T00:00:00+03:00`).toISOString(), endExclusiveAt: new Date(new Date(`${to}T00:00:00+03:00`).getTime() + 86_400_000).toISOString(), days: 5, temporalState: 'complete' },
      snapshot: null, formulaVersion: 'wb-abc-pnl-finance-rrdate-v4', costLedgerRevision: 0, economicsRevision: 0,
      advertisingSource: null, advertisingSnapshotChecksum: null, advertisingEvidenceStatus: null, blockerIds: [] }, timestamp: '2026-10-17T00:00:00Z' }
}

it('shows selected rows immediately and finishes current canonical reads before starting previous reads', async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({ configFile: false, envFile: false, root, logLevel: 'silent',
    plugins: [{ name: 'synthetic-report-account', enforce: 'pre', transform(source, id) {
      if (id.endsWith('/__fixtures__/reportLoadingBrowser.tsx')) return "import { reportCabinet } from './reportCabinet'\n" + source.replace('cabinetMe: null', 'cabinetMe: reportCabinet')
    } }, react()],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""', 'import.meta.env.VITE_CANONICAL_WB_ABC_PNL_ROLLOUT': '"77:31"' },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: { entry: fileURLToPath(new URL('./__fixtures__/reportLoadingBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'WeekCurrentFirst' } } })
  const bundle = (Array.isArray(result) ? result : [result]).flatMap(item => 'output' in item ? item.output : []).find(item => item.type === 'chunk' && item.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing fixture')
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  let releaseCurrent = () => {}, releasePrevious = () => {}
  const currentGate = new Promise<void>(resolve => { releaseCurrent = resolve })
  const previousGate = new Promise<void>(resolve => { releasePrevious = resolve })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block' })
    page.setDefaultTimeout(5000)
    const periods: string[] = [], errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      if (request.resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (url.pathname === '/api/wb/reports/week-over-week/latest-cache') return route.fulfill({ json: {
        filters: { dateRange: { from: '2026-10-07', to: '2026-10-11' } }, cache: { fresh: true },
        rows: [{ nmId: 101, sku: 'CURRENT-ROW', productName: 'Synthetic product', orders: { units: 61, kopecks: 29_890_000, deltaPct: null }, sales: { units: 11, kopecks: 10000, deltaPct: null }, baskets: { units: 0, deltaPct: null } }], columns: [], kpis: [] } })
      if (url.pathname === '/api/v2/wb/reports/abc-pnl') {
        const from = url.searchParams.get('dateFrom')!, to = url.searchParams.get('dateTo')!
        periods.push(from)
        await (from === '2026-10-07' ? currentGate : previousGate)
        return route.fulfill({ json: emptyFinance(from, to) })
      }
      if (url.pathname === '/api/v1/cabinet/team/users') return route.fulfill({ json: { data: [] } })
      if (url.pathname === '/api/v1/cabinet/wb-token') return route.fulfill({ json: { data: { hasToken: false } } })
      if (url.pathname === '/api/v1/cabinet/avito-credentials') return route.fulfill({ json: { data: { hasCredentials: false } } })
      if (url.origin === 'https://fonts.googleapis.com') return route.fulfill({ contentType: 'text/css', body: '' })
      if (request.resourceType() === 'image') return route.fulfill({ status: 204 })
      errors.push(`${request.method()} ${url.pathname}`)
      return route.abort()
    })
    await page.goto('http://localhost/wb/reports/week-over-week')
    await page.evaluate(() => { window.__vellaReportPeriods = { week: { days: 5, mode: 'custom', fromIso: '2026-10-07', toIso: '2026-10-11', label: 'Synthetic period' } } })
    await page.addScriptTag({ content: bundle.code })
    await page.getByText('Synthetic product', { exact: true }).first().waitFor().catch(async error => {
      throw new Error(`${error.message}\n${JSON.stringify({ errors, periods, text: (await page.locator('body').innerText()).slice(-2000) })}`)
    })
    await expect.poll(() => periods.length).toBe(1)
    expect(periods).toEqual(['2026-10-07'])
    releaseCurrent()
    await expect.poll(() => periods.length).toBe(2)
    expect(periods).toEqual(['2026-10-07', '2026-10-02'])
    expect(await page.getByText('Synthetic product', { exact: true }).first().isVisible()).toBe(true)
    const previousResponse = page.waitForResponse(response => response.url().includes('dateFrom=2026-10-02') && response.status() === 200)
    releasePrevious()
    await previousResponse
    expect(errors).toEqual([])
  } finally { releaseCurrent(); releasePrevious(); await browser.close() }
}, 30_000)
