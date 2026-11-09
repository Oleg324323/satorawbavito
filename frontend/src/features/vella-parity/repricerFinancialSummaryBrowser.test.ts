import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { mkdir, readFile } from 'node:fs/promises'
import react from '@vitejs/plugin-react'
import { chromium } from 'playwright'
import { build } from 'vite'
import { expect, it } from 'vitest'

it('renders period-scoped advertising by its declared source and preserves genuine gaps', async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const built = await build({ root, configFile: false, envFile: false, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""', 'import.meta.env.VITE_WB_LIVE_ENABLED': '"false"' },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: { entry: fileURLToPath(new URL('./__fixtures__/productsCatalogCountBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'RepricerFinancialSummary' } } })
  const output = (Array.isArray(built) ? built : [built]).flatMap(item => 'output' in item ? item.output : [])
  const bundle = output.find(item => item.type === 'chunk' && item.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing browser fixture')
  const css = output.filter(item => item.type === 'asset' && item.fileName.endsWith('.css')).map(item => item.type === 'asset' ? String(item.source) : '').join('\n')
  // These fixtures replay financial definitions, not live WB responses.
  const samples = [
    { dateFrom: '2026-10-05', dateTo: '2026-10-11', revenueKopecks: 292025355, buyoutUnits: 1859, adSpendKopecks: 13492300, settlementCogsKopecks: 148720000, expensesKopecks: 103480619 },
    { dateFrom: '2026-10-12', dateTo: '2026-10-16', revenueKopecks: 213660929, buyoutUnits: 1329, adSpendKopecks: 13441185, settlementCogsKopecks: null, expensesKopecks: 92273540, campaignAdvertisingVersion: 'wb-repricer-campaign-advertising-v1' },
    { dateFrom: '2026-10-11', dateTo: '2026-10-11', revenueKopecks: 123456, buyoutUnits: 1, adSpendKopecks: 13492300, settlementCogsKopecks: 80000, expensesKopecks: 13492300 },
    { dateFrom: '2026-10-01', dateTo: '2026-10-04', revenueKopecks: 123456, buyoutUnits: 1, adSpendKopecks: null, settlementCogsKopecks: null, expensesKopecks: null },
    { dateFrom: '2026-10-20', dateTo: '2026-10-20', revenueKopecks: 123456, buyoutUnits: 1, adSpendKopecks: 0, settlementCogsKopecks: 80000, expensesKopecks: 43456, campaignAdvertisingVersion: 'wb-repricer-campaign-advertising-v1', campaignProfitKopecks: 0 },
  ]
  type Sample = (typeof samples)[number] & { financialCoverageState?: string; salesUnits?: number; returnsUnits?: number; campaignAdvertisingVersion?: string; campaignProfitKopecks?: number | null; adSpendState?: string }
  const sourceBacked: Sample[] = process.env.SATORNA_REPRICER_SUMMARY_FIXTURE
    ? JSON.parse(await readFile(process.env.SATORNA_REPRICER_SUMMARY_FIXTURE, 'utf8')) : samples
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL })
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, serviceWorkers: 'block' })
    const errors: string[] = [], forbidden: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', route => {
      const request = route.request(), url = new URL(request.url())
      if (request.resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (request.resourceType() === 'image') return route.fulfill({ status: 204 })
      if (url.hostname === 'fonts.googleapis.com') return route.fulfill({ contentType: 'text/css', body: '' })
      if (url.pathname === '/api/v1/wb-repricer/sku') {
        const from = url.searchParams.get('dateFrom')!, to = url.searchParams.get('dateTo')!
        const sample = sourceBacked.find(item => item.dateFrom === from && item.dateTo === to) ?? sourceBacked[0]
        const days = Math.round((Date.parse(to) - Date.parse(from)) / 86400000) + 1
        const full = sample.adSpendKopecks !== null
        return route.fulfill({ json: { total: 3704, page: 1, pageSize: 150, dateFrom: from, dateTo: to,
          items: [{ meta: { articleId: 'FINANCE-SKU', nmId: 990001, name: 'Synthetic product', subject: 'Лонгсливы', status: 'manual', currentPriceKopecks: 206400 }, settings: { cogsKopecks: 80000 },
            analytics: { financeState: 'ok', basePriceKopecks: 430000, buyerPriceNoWalletKopecks: null, salesUnits: 2, returnsUnits: 0, revenueKopecks: 10000,
              settlementFormulaVersion: 'wb-final-payout-cogs-tax-v1', settlementProfitKopecks: null,
              campaignAdvertisingVersion: sample.campaignAdvertisingVersion, campaignProfitKopecks: null,
              campaignAttributedProfitKopecks: sample.adSpendState === 'partial' ? null : 10000, settlementBlockers: ['settlement_account_expenses_unreconciled'] } }],
          cache: { totalCached: 3704, pagesCached: 1, financeFetchedAt: '2026-10-21T10:00:00Z', periodStatsFetchedAt: '2026-10-21T10:00:00Z', basketsFetchedAt: '2026-10-21T10:00:00Z',
            financeFreshnessState: 'fresh', periodStatsFreshnessState: 'fresh', basketsFreshnessState: 'fresh', requiredPeriodSources: ['finance'],
            periodCoverage: { finance: { state: sample.financialCoverageState ?? 'ok', coveredDays: sample.financialCoverageState === 'partial' ? Math.min(3, days) : days, requestedDays: days }, baskets: { state: 'ok', coveredDays: days, requestedDays: days } } },
          summary: { ...sample, salesUnits: sample.salesUnits ?? sample.buyoutUnits, returnsUnits: sample.returnsUnits ?? 0,
            skuCount: 3704, buyoutAmountKopecks: sample.revenueKopecks, settlementFormulaVersion: 'wb-final-payout-cogs-tax-v1', settlementProfitKopecks: null,
            adSpendSource: sample.campaignAdvertisingVersion ? 'wb_ads_fullstats' : full ? 'finance_deduction_wb_promotion' : null, adSkuCount: 394, totalBaskets: 10 } } })
      }
      if (url.pathname.endsWith('/sync/status')) return route.fulfill({ json: { running: false, state: 'completed', steps: [] } })
      if (url.pathname.endsWith('/jobs')) return route.fulfill({ json: { state: 'completed' } })
      if (request.method() !== 'GET' || /refresh-ads|fullstats|\/campaigns/.test(url.pathname)) forbidden.push(`${request.method()} ${url.pathname}`)
      return route.fulfill({ json: { items: [], total: 0, tasks: [], available: true } })
    })
    await page.goto('http://localhost/wb/repricer')
    await page.addStyleTag({ content: css }); await page.addScriptTag({ content: bundle.code })
    await page.locator('#tbody tr[data-sku="FINANCE-SKU"]').waitFor()
    await page.locator('details.products-kpi-more summary').click()
    const compact = (value: string | null) => (value ?? '').replace(/\s/g, '')
    for (const sample of sourceBacked) {
      await page.getByRole('textbox', { name: 'Начало периода аналитики WB' }).fill(sample.dateFrom)
      await page.getByRole('textbox', { name: 'Конец периода аналитики WB' }).fill(sample.dateTo)
      await page.getByLabel('Контекстные действия').getByRole('button', { name: 'Применить', exact: true }).click()
      const expected = sample.adSpendKopecks === null ? '—' : `${Math.round(sample.adSpendKopecks / 100).toLocaleString('ru-RU')} ₽`
      await expect.poll(async () => compact(await page.locator('#kpiAdsSpend').textContent())).toBe(compact(expected))
      const rubles = (amount: number) => (amount / 100).toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' ₽'
      await expect.poll(async () => compact(await page.locator('#kpiRevenue').textContent())).toBe(compact(rubles(sample.revenueKopecks)))
      await expect.poll(async () => compact(await page.locator('#kpiOrdersUnits').textContent())).toBe(compact(`${sample.buyoutUnits.toLocaleString('ru-RU')} шт. / ${rubles(sample.revenueKopecks)}`))
      expect(compact(await page.locator('#kpiSalesUnits').textContent())).toBe(compact((sample.salesUnits ?? sample.buyoutUnits).toLocaleString('ru-RU')))
      if (sample.returnsUnits != null) expect(compact(await page.locator('#kpiReturnsUnits').textContent())).toBe(compact(sample.returnsUnits.toLocaleString('ru-RU')))
      if (sample.adSpendKopecks !== null) {
        expect(await page.locator('#kpiAdsSpend').locator('..').innerText()).toContain(sample.campaignAdvertisingVersion ? 'Расход кампаний' : 'финансовый отчёт')
        expect(await page.locator('#kpiAdsSpend').locator('..').innerText()).not.toContain('камп.')
      }
      await expect.poll(async () => compact(await page.locator('#kpiExpenses').textContent())).toBe(sample.expensesKopecks === null ? '—' : compact(`${Math.round(sample.expensesKopecks / 100).toLocaleString('ru-RU')} ₽`))
      if (sample.campaignAdvertisingVersion && sample.adSpendState !== 'partial') {
        expect(await page.locator('#tbody tr[data-sku="FINANCE-SKU"]').innerText()).toContain('до общих расходов')
        expect(await page.locator('#tbody tr[data-sku="FINANCE-SKU"]').innerText()).toContain('50')
      }
      await expect.poll(async () => compact(await page.locator('#kpiCogs').textContent())).toBe(sample.settlementCogsKopecks === null ? '—' : compact(`${Math.round(sample.settlementCogsKopecks / 100).toLocaleString('ru-RU')} ₽`))
      expect(compact(await page.locator('#kpiMarginRub').textContent())).toBe(
        sample.campaignAdvertisingVersion && Number.isSafeInteger(sample.campaignProfitKopecks)
          ? compact(`${Math.round(sample.campaignProfitKopecks! / 100).toLocaleString('ru-RU')} ₽`) : '—')
      expect(await page.locator('#kpiInSale').textContent()).toBe('—')
      expect(await page.locator('#tbody tr[data-sku="FINANCE-SKU"] .price').innerText()).toContain('нет данных')
      expect(await page.locator('#tbody tr[data-sku="FINANCE-SKU"] .price-hint').count()).toBe(0)
      if (process.env.SATORNA_REPRICER_SUMMARY_SCREENSHOTS) {
        await mkdir(process.env.SATORNA_REPRICER_SUMMARY_SCREENSHOTS, { recursive: true })
        await page.screenshot({ path: path.join(process.env.SATORNA_REPRICER_SUMMARY_SCREENSHOTS, `repricer-summary-${sample.dateFrom}-${sample.dateTo}.png`) })
      }
    }
    expect(errors).toEqual([])
    expect(forbidden).toEqual([])
  } finally { await browser.close() }
}, 90000)
