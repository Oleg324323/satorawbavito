import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { readFile } from 'node:fs/promises'
import react from '@vitejs/plugin-react'
import { chromium } from 'playwright'
import { build } from 'vite'
import { expect, it } from 'vitest'

it('keeps the catalog visible across loaded/missing/partial/zero periods and ignores late responses', async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const built = await build({ root, configFile: false, envFile: false, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""', 'import.meta.env.VITE_WB_LIVE_ENABLED': '"false"' },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: { entry: fileURLToPath(new URL('./__fixtures__/productsCatalogCountBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'PeriodLoadingRegression' } },
  })
  const outputs = (Array.isArray(built) ? built : [built]).flatMap(item => 'output' in item ? item.output : [])
  const bundle = outputs.find(item => item.type === 'chunk' && item.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing test bundle')
  const css = outputs.filter(item => item.type === 'asset' && item.fileName.endsWith('.css')).map(item => item.type === 'asset' ? String(item.source) : '').join('\n')
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL })
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, serviceWorkers: 'block' })
    const errors: string[] = [], requests: string[] = [], mutations: string[] = []
    let releaseLate: (() => void) | undefined, lateEnabled = false
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', async route => {
      const req = route.request(), url = new URL(req.url())
      if (req.resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (req.resourceType() === 'image') return route.fulfill({ contentType: 'image/gif', body: Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64') })
      if (url.host === 'fonts.googleapis.com') return route.fulfill({ contentType: 'text/css', body: '' })
      if (url.pathname === '/api/v1/wb-repricer/sku') {
        const start = url.searchParams.get('dateFrom')!, end = url.searchParams.get('dateTo')!
        requests.push(`${start}/${end}`)
        if (lateEnabled && start === '2026-10-01') await new Promise<void>(resolve => { releaseLate = resolve })
        const missing = start === '2026-09-01', partial = start === '2026-10-12', zero = start === '2026-10-02'
        const units = missing ? null : zero ? 0 : partial ? 3 : 6
        const amount = units == null ? null : units * 10000
        const days = Math.round((Date.parse(end) - Date.parse(start)) / 86400000) + 1
        const state = missing ? 'no_data' : partial ? 'partial' : 'ok'
        const coverage = { state, coveredDays: missing ? 0 : partial ? 1 : days, requestedDays: days, missingDates: missing ? [start] : [] }
        return route.fulfill({ json: { dateFrom: start, dateTo: end, total: 160, page: Number(url.searchParams.get('page')), pageSize: 150,
          items: [{ meta: { articleId: 'PERIOD-SKU', nmId: 990001, name: 'Synthetic period product', status: 'auto', currentPriceKopecks: 200000, basketsLast7d: 0, basketNorm: 10 },
            settings: { cogsKopecks: 80000 }, analytics: { financeState: missing ? 'no_data' : 'ok', salesUnits: units, grossSalesUnits: units, grossSalesAmountKopecks: amount,
              returnsUnits: missing ? null : 0, baskets: missing ? null : 10, basketsState: state, ordersUnits: missing ? 0 : 999, reportOrdersUnits: missing ? null : 999 } }],
          summary: { skuCount: 160, revenueKopecks: amount, buyoutUnits: units, buyoutAmountKopecks: amount, salesUnits: units, returnsUnits: missing ? null : 0,
            totalBaskets: missing ? null : 10, totalBasketsState: state, adSpendKopecks: null, financialCoverageState: state,
            buyoutComparison: { state: 'ok', currentState: state, units: 4, amountKopecks: 40000, dateFrom: '2026-09-28', dateTo: '2026-10-04', source: 'finance.rrDate.retailAmount.netSalesUnits' } },
          cache: { dateFrom: start, dateTo: end, totalCached: 160, pagesCached: 1, periodStatsFetchedAt: 'now', financeFetchedAt: missing ? null : 'now', basketsFetchedAt: 'now',
            requiredPeriodSources: ['finance', 'orders', 'baskets'], financeFreshnessState: 'fresh',
            periodCoverage: { finance: coverage, orders: { ...coverage, state: missing ? 'no_data' : 'ok' }, baskets: coverage } } } })
      }
      if (url.pathname === '/api/wb/reports/stats/jobs') return route.fulfill({ json: { state: 'completed' } })
      if (url.pathname === '/api/v1/wb-repricer/sync/run') return route.fulfill({ json: { runId: 'isolated', running: true } })
      if (url.pathname === '/api/v1/wb-repricer/sync/status') return route.fulfill({ json: { runId: 'isolated', state: 'completed', running: false, steps: [] } })
      if (req.method() !== 'GET') mutations.push(`${req.method()} ${url.pathname}`)
      return route.fulfill({ json: { items: [], total: 0, available: true, tasks: [] } })
    })
    await page.goto('http://satorna.test/wb/repricer')
    await page.addStyleTag({ content: css }); await page.addScriptTag({ content: bundle.code })
    const row = page.locator('#tbody tr[data-sku="PERIOD-SKU"]')
    await expect.poll(() => row.isVisible(), { timeout: 15000 }).toBe(true)
    const setPeriod = async (from: string, to: string) => {
      await page.getByRole('textbox', { name: 'Начало периода аналитики WB' }).fill(from)
      await page.getByRole('textbox', { name: 'Конец периода аналитики WB' }).fill(to)
      await page.getByLabel('Контекстные действия').getByRole('button', { name: 'Применить', exact: true }).click()
    }
    const ranges = [['2026-10-05', '2026-10-11'], ['2026-09-01', '2026-09-07'], ['2026-10-12', '2026-10-15'],
      ['2026-10-02', '2026-10-02'], ['2026-10-01', '2026-10-01'], ['2026-10-01', '2026-10-06'], ['2026-10-11', '2026-10-14'], ['2026-10-05', '2026-10-11']]
    for (const [from, to] of ranges) {
      await setPeriod(from, to)
      await expect.poll(() => requests.at(-1)).toBe(`${from}/${to}`)
      await expect.poll(() => row.isVisible()).toBe(true)
      if (from === '2026-09-01') {
        await expect.poll(() => page.locator('#kpiOrdersUnits').textContent()).toBe('— шт. / — ₽')
        await expect.poll(() => page.getByText(/За выбранный период показатели ещё не загружены/).isVisible()).toBe(true)
        await page.locator('details.products-kpi-more summary').click()
        await expect.poll(() => page.locator('#kpiBaskets').textContent()).toBe('—')
        await page.locator('details.products-kpi-more summary').click()
        await expect.poll(() => row.locator('[data-column-id="grossSalesUnits"]').innerText()).toBe('—')
      } else if (from === '2026-10-02') await expect.poll(() => page.locator('#kpiOrdersUnits').textContent()).toMatch(/0 шт\. \/ 0,00 ₽/)
      else if (from === '2026-10-12') {
        await expect.poll(() => page.locator('#kpiOrdersUnits').textContent()).toMatch(/3 шт\./)
        expect(await page.locator('#kpiOrdersUnits').locator('..').locator('.stat-delta').textContent()).not.toContain('%')
      } else await expect.poll(() => page.locator('#kpiOrdersUnits').locator('..').locator('.stat-delta').textContent()).toContain('+50%')
    }
    await page.getByRole('button', { name: '›', exact: true }).click()
    await expect.poll(() => page.locator('#kpiOrdersUnits').locator('..').locator('.stat-delta').textContent()).toContain('+50%')
    for (const preset of ['1 день', '7 дней', '14 дней', '30 дней']) {
      const before = requests.length
      await page.getByRole('button', { name: preset, exact: true }).click()
      await expect.poll(() => requests.length).toBeGreaterThan(before)
      const [from, to] = requests.at(-1)!.split('/')
      expect(Math.round((Date.parse(to) - Date.parse(from)) / 86400000) + 1).toBe(parseInt(preset))
      await expect.poll(() => row.isVisible()).toBe(true)
    }
    lateEnabled = true
    await setPeriod('2026-10-01', '2026-10-01')
    await expect.poll(() => Boolean(releaseLate)).toBe(true)
    await setPeriod('2026-10-05', '2026-10-11')
    await expect.poll(() => page.locator('#kpiOrdersUnits').textContent()).toMatch(/6 шт\./)
    releaseLate!()
    await page.waitForTimeout(200)
    expect(await page.getByRole('textbox', { name: 'Начало периода аналитики WB' }).inputValue()).toBe('2026-10-05')
    expect(await row.isVisible()).toBe(true)
    expect(errors).toEqual([])
    expect(mutations).toEqual([])
    if (process.env.SATORNA_REPRICER_PERIOD_FIXTURE) {
      // Optional private evidence: generated from query-only DB reads. Never
      // committed, and no real API/collection request may leave this browser.
      const fixtures = JSON.parse(await readFile(process.env.SATORNA_REPRICER_PERIOD_FIXTURE, 'utf8'))
      await page.route('**/api/**', route => {
        const url = new URL(route.request().url())
        if (url.pathname === '/api/v1/wb-repricer/sku') {
          const payload = fixtures.find((item: { dateFrom: string; dateTo: string }) => item.dateFrom === url.searchParams.get('dateFrom') && item.dateTo === url.searchParams.get('dateTo'))
          return route.fulfill({ json: payload ?? { items: [], total: 0, cache: { totalCached: 0, pagesCached: 0 } } })
        }
        if (url.pathname.endsWith('/sync/run')) return route.fulfill({ json: { runId: 'isolated', running: true } })
        if (url.pathname.endsWith('/sync/status')) return route.fulfill({ json: { runId: 'isolated', running: false, state: 'completed' } })
        if (url.pathname.endsWith('/jobs')) return route.fulfill({ json: { state: 'completed' } })
        return route.fulfill({ json: { items: [], total: 0, available: true, tasks: [] } })
      })
      await page.reload(); await page.addStyleTag({ content: css }); await page.addScriptTag({ content: bundle.code })
      // Existing default type filters exclude shorts/balaclavas. Explicitly
      // select every type for the unfiltered row reconciliation; do not change defaults.
      await page.getByRole('button', { name: /^Фильтры/ }).click()
      await page.locator('#advBar').getByText('Шорты', { exact: true }).click()
      await page.locator('#advBar').getByText('Балаклава', { exact: true }).click()
      await page.locator('#advBar').getByRole('button', { name: 'Применить', exact: true }).click()
      const compact = (value: string | null) => (value ?? '').replace(/\s/g, '')
      for (const fixture of fixtures) {
        await setPeriod(fixture.dateFrom, fixture.dateTo)
        const summary = fixture.summary
        const rubles = (amount: number) => (amount / 100).toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + ' ₽'
        const expected = summary.buyoutUnits == null ? '— шт. / — ₽' : `${summary.buyoutUnits.toLocaleString('ru-RU')} шт. / ${rubles(summary.buyoutAmountKopecks)}`
        await expect.poll(async () => compact(await page.locator('#kpiOrdersUnits').textContent()), { timeout: 10000 }).toBe(compact(expected))
        const revenue = summary.revenueKopecks == null ? '—' : rubles(summary.revenueKopecks)
        expect(compact(await page.locator('#kpiRevenue').textContent())).toBe(compact(revenue))
        expect(await page.locator('#tbody tr').count()).toBe(fixture.items.length)
        expect(await page.locator('#tableWrap').isVisible()).toBe(true)
        for (const nm of [867213853, 1025784485, 486612890]) {
          const item = fixture.items.find((entry: { meta: { nmId: number } }) => entry.meta.nmId === nm)
          if (!item) continue
          const product = page.locator('#tbody tr').filter({ has: page.locator(`a[href="https://www.wildberries.ru/catalog/${nm}/detail.aspx"]`) }).first()
          const quantity = item.analytics.grossSalesUnits
          expect(compact(await product.locator('[data-column-id="grossSalesUnits"]').innerText())).toBe(quantity == null ? '—' : compact(quantity.toLocaleString('ru-RU')))
          const amount = item.analytics.grossSalesAmountKopecks
          expect(compact(await product.locator('[data-column-id="salesRub"]').innerText())).toBe(amount == null ? '—' : compact(rubles(amount)))
          const orders = item.analytics.reportOrdersUnits
          if (orders != null) expect(await product.locator('[data-column-id="ordersPeriod"]').innerText()).toContain(orders.toLocaleString('ru-RU'))
          const orderAmount = item.analytics.ordersAmountKopecks
          if (orderAmount != null) expect(compact(await product.locator('[data-column-id="ordersRub"]').innerText())).toContain(compact(rubles(orderAmount)))
          if (item.analytics.basketsState === 'ok' && item.analytics.baskets != null) {
            expect(compact(await product.locator('[data-column-id="bsk"]').innerText())).toContain(compact(item.analytics.baskets.toLocaleString('ru-RU')))
          }
        }
      }
      expect(errors).toEqual([])
    }
  } finally { await browser.close() }
}, 90000)
