import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { beforeAll, expect, it } from 'vitest'

let bundleCode: string
beforeAll(async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({
    configFile: false, envFile: false, root, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': JSON.stringify('test'), 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('') },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false,
      lib: { entry: fileURLToPath(new URL('./__fixtures__/reportLoadingBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'StockFiltersTest' } },
  })
  const outputs = Array.isArray(result) ? result : [result]
  const bundle = outputs.flatMap(output => 'output' in output ? output.output : [])
    .find(output => output.type === 'chunk' && output.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing stock filters bundle')
  bundleCode = bundle.code
}, 60_000)

// Source fields follow wb_reports_bff._stock_product_rows_from_snapshot and StockBackendRow.
// Deliberately no manager, promotion flag, tags or fabricated UI dataset fields.
// Values are away from policy boundaries: 4 days is risk, 90 is excess, 20 is neither.
const photoUrl = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7'
const rows = [
  {
    sku: 'SYNTH-STOCK-RISK', nmId: 990101, productName: 'Синтетический товар с низким запасом',
    photoUrl, brand: 'Synthetic', category: 'Тестовые товары',
    warehouseName: 'Коледино', clusterName: 'Центр', warehouseCount: 1,
    wbStockUnits: 8, marketplaceStockUnits: 0, totalStockUnits: 8,
    fromClientUnits: 0, toClientUnits: 0, availableUnits: 8, ordersPerDay: 2, daysToOos: 4,
    ktrIndex: null, localizationPct: null, logisticsPerUnitKopecks: null,
    decision: 'дозагрузить', decisionStatus: 'source_partial', comment: 'Синтетическая строка',
    historyCoverageDays: 0, historySource: 'backfilled',
    warehouses: [{ warehouseName: 'Коледино', clusterName: 'Центр', wbStockUnits: 8, fromClientUnits: 0, toClientUnits: 0, availableUnits: 8 }],
  },
  {
    sku: 'SYNTH-STOCK-EXCESS', nmId: 990102, productName: 'Синтетический товар с большим запасом',
    photoUrl, brand: 'Synthetic', category: 'Тестовые товары',
    warehouseName: 'Екатеринбург', clusterName: 'Урал', warehouseCount: 1,
    wbStockUnits: 180, marketplaceStockUnits: 0, totalStockUnits: 180,
    fromClientUnits: 0, toClientUnits: 0, availableUnits: 180, ordersPerDay: 2, daysToOos: 90,
    ktrIndex: null, localizationPct: null, logisticsPerUnitKopecks: null,
    decision: 'снизить поставку', decisionStatus: 'source_partial', comment: 'Синтетическая строка',
    historyCoverageDays: 0, historySource: 'backfilled',
    warehouses: [{ warehouseName: 'Екатеринбург', clusterName: 'Урал', wbStockUnits: 180, fromClientUnits: 0, toClientUnits: 0, availableUnits: 180 }],
  },
  {
    sku: 'SYNTH-STOCK-NORMAL', nmId: 990103, productName: 'Синтетический товар с обычным запасом',
    photoUrl, brand: 'Synthetic', category: 'Тестовые товары',
    warehouseName: 'Казань', clusterName: 'Поволжье', warehouseCount: 1,
    wbStockUnits: 40, marketplaceStockUnits: 0, totalStockUnits: 40,
    fromClientUnits: 0, toClientUnits: 0, availableUnits: 40, ordersPerDay: 2, daysToOos: 20,
    ktrIndex: null, localizationPct: null, logisticsPerUnitKopecks: null,
    decision: 'норма', decisionStatus: 'source_partial', comment: 'Синтетическая строка',
    historyCoverageDays: 0, historySource: 'backfilled',
    warehouses: [{ warehouseName: 'Казань', clusterName: 'Поволжье', wbStockUnits: 40, fromClientUnits: 0, toClientUnits: 0, availableUnits: 40 }],
  },
]
rows.push(...Array.from({ length: 48 }, (_, index) => ({
  ...rows[2], sku: `SYNTH-STOCK-EXTRA-${index + 4}`, nmId: 990104 + index,
  warehouseName: 'Синтетический склад', warehouses: [],
})))

it('filters the full stock response and restores its first display window on reset', async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1440, height: 1000 } })
    await page.addInitScript(() => {
      if (!crypto.randomUUID) Object.defineProperty(crypto, 'randomUUID', { value: () => '00000000-0000-4000-8000-000000000001' })
    })
    await page.clock.setFixedTime(new Date('2026-09-09T12:00:00Z'))
    const requests: string[] = [], unexpected: string[] = [], errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', route => {
      const request = route.request(), url = new URL(request.url())
      if (request.method() === 'GET' && url.origin === 'http://satorna.test' && url.pathname === '/wb/reports/stock') {
        return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      }
      if (request.method() === 'GET' && url.origin === 'http://satorna.test' &&
        ['/wb/reports/brand/satorna-logo-white.svg', '/wb/reports/brand/satorna-icon.svg'].includes(url.pathname)) {
        return route.fulfill({ contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1" />' })
      }
      if (request.method() === 'GET' && url.origin === 'https://fonts.googleapis.com' && url.pathname === '/css2') {
        return route.fulfill({ contentType: 'text/css', body: '' })
      }
      if (request.method() !== 'GET' || url.origin !== 'http://satorna.test' || url.pathname !== '/api/wb/reports/stock/latest-cache') {
        unexpected.push(`${request.method()} ${url.origin}${url.pathname}`)
        return route.abort()
      }
      requests.push(url.search)
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({
        meta: { title: 'Остатки WB', sourceType: 'operational', freshnessState: 'fresh' },
        headline: 'Синтетические остатки для локальной проверки фильтров',
        filters: { dateRange: { from: url.searchParams.get('from'), to: url.searchParams.get('to') }, groupBy: 'sku' },
        rows, kpis: [], sourceCoverage: [], managementCards: [], reportJob: null,
      }) })
    })
    await page.goto('http://satorna.test/wb/reports/stock')
    await page.addScriptTag({ content: bundleCode })
    const surface = page.locator('#tab-stock')
    const visibleSkus = () => surface.locator('tbody tr[data-report-row="stock"]:visible').evaluateAll(elements =>
      elements.map(element => {
        const text = element.querySelector('td')?.textContent ?? ''
        // An unknown rendered row remains visible in the assertion, never silently filtered out.
        return text.match(/SYNTH-STOCK-(?:RISK|EXCESS|NORMAL|EXTRA-\d+)/)?.[0] ?? text
      }).sort(),
    )
    const allSkus = ['SYNTH-STOCK-EXCESS', 'SYNTH-STOCK-NORMAL', 'SYNTH-STOCK-RISK', ...Array.from({ length: 47 }, (_, index) => `SYNTH-STOCK-EXTRA-${index + 4}`)].sort()
    const expectSkus = async (expected: string[]) => {
      await expect.poll(visibleSkus, { timeout: 15_000 }).toEqual(expected)
    }
    await expectSkus(allSkus)
    expect(await surface.getByRole('columnheader', { name: /^КТР/ }).count()).toBe(0)
    expect(await surface.getByRole('columnheader', { name: /^Решение/ }).count()).toBe(0)
    expect(await surface.locator('tr[data-report-row="stock"]').first().locator('td').count()).toBe(12)
    expect(requests).toHaveLength(1)
    const query = new URLSearchParams(requests[0])
    expect([query.get('groupBy'), query.get('source'), query.get('preset')]).toEqual(['sku', 'operational', 'custom'])
    const chip = (label: string) => surface.locator('.toolbar .chips .chip').filter({ hasText: new RegExp(`^${label}$`) })
    await chip('Риск нехватки').click()
    await expectSkus(['SYNTH-STOCK-RISK'])
    await chip('Избыток').click()
    await expectSkus(['SYNTH-STOCK-EXCESS'])

    // No source row supplies promotion evidence: never invent a positive match.
    // This does not claim positive promotion filtering parity or add a new backend field.
    await chip('В акции').click()
    await expectSkus([])
    expect(await surface.locator('[data-report-empty]').isVisible()).toBe(true)
    await chip('Все').click()
    await expectSkus(allSkus)

    await chip('Топ-склады').click()
    await expectSkus(['SYNTH-STOCK-NORMAL', 'SYNTH-STOCK-RISK'])
    const search = surface.locator('.toolbar .search input')
    await search.fill('Казань')
    await expectSkus(['SYNTH-STOCK-NORMAL'])
    // Екатеринбург exists in the response, but must not escape the active top-warehouse chip.
    await search.fill('Екатеринбург')
    await expectSkus([])
    expect(await surface.locator('[data-report-empty]').isVisible()).toBe(true)
    await surface.locator('[data-report-empty]').getByRole('button', { name: 'Сбросить фильтры' }).click()
    await expectSkus(allSkus)
    expect(await search.inputValue()).toBe('')
    expect(await chip('Все').getAttribute('class')).toContain('active')
    expect(await surface.locator('[data-report-empty]').isVisible()).toBe(false)

    await search.fill('SYNTH-STOCK-EXCESS')
    await expectSkus(['SYNTH-STOCK-EXCESS'])
    await search.fill('SYNTH-STOCK-EXTRA-51')
    await expectSkus(['SYNTH-STOCK-EXTRA-51'])
    await search.fill('')
    await expectSkus(allSkus)
    expect(requests, 'Local filter interactions must not refetch or trigger provider actions').toHaveLength(1)
    expect(unexpected, JSON.stringify(unexpected)).toEqual([])
    expect(errors).toEqual([])
  } finally { await browser.close() }
}, 45_000)
