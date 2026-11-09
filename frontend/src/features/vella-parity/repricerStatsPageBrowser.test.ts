import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { expect, it } from 'vitest'

it('loads live statistics through the actual React route and clears them on session loss', async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const mutation = process.env.SATORNA_STATS_TEST_MUTATION
  if (mutation && !['route', 'period', 'strategies-scope'].includes(mutation)) throw new Error('Unknown statistics test mutation')
  const result = await build({
    configFile: false, envFile: false, root, logLevel: 'silent', plugins: [
      { name: 'statistics-test-mutation', enforce: 'pre', transform(code, id) {
        if (!mutation || !id.endsWith('/VellaHtmlParityPage.tsx')) return null
        const original = mutation === 'route'
          ? "if (effectiveActiveParityTab === 'repricer-stats') void window.__vellaLoadLiveRepricerStats?.()"
          : mutation === 'period' ? "if (!reportPeriodEventMatches(event, 'repricer-stats')) return"
          : 'if (!runtime || !accessToken || !productsTabActive) return\n\n    const controller = new AbortController()\n    void loadLiveRepricerStrategies(accessToken, controller.signal)'
        if (!code.includes(original)) throw new Error('Statistics mutation target absent')
        return code.replace(original, mutation === 'route' ? 'void 0' : mutation === 'period' ? 'if (false) return' : original.replace(' || !productsTabActive', ''))
      } },
      react(),
    ],
    define: { 'process.env.NODE_ENV': JSON.stringify('test'), 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('') },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false,
      lib: { entry: fileURLToPath(new URL('./__fixtures__/repricerStatsPageBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'StatsPageTest' } },
  })
  const outputs = Array.isArray(result) ? result : [result]
  const bundle = outputs.flatMap(output => 'output' in output ? output.output : [])
    .find(output => output.type === 'chunk' && output.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing synthetic stats page bundle')
  const browser = await chromium.launch({ headless: true })
  let release = () => {}
  const firstPage = new Promise<void>(resolve => { release = resolve })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1440, height: 1000 } })
    const unexpected: string[] = [], errors: string[] = [], requests: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      if (url.origin === 'http://satorna.test' && request.method() === 'GET' && url.pathname === '/wb/repricer/stats') {
        return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      }
      if (request.resourceType() === 'image') return route.fulfill({ contentType: 'image/gif', body: Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64') })
      if (request.method() === 'GET' && url.origin === 'https://fonts.googleapis.com' && url.pathname === '/css2') {
        return route.fulfill({ contentType: 'text/css', body: '/* Offline font fixture: use system fallback. */' })
      }
      if (url.origin !== 'http://satorna.test' || request.method() !== 'GET' || url.pathname !== '/api/v1/wb-repricer/stats') {
        unexpected.push(`${request.method()} ${url.pathname}`); return route.abort()
      }
      requests.push(url.search)
      if (requests.length === 1) await firstPage
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({
        items: [{ articleId: url.searchParams.get('dateFrom') === '2026-08-01' ? 'NEW-PERIOD-REACT-STATS-SKU' : 'ACTUAL-REACT-STATS-SKU', nmId: 101, name: 'Synthetic route product', brand: 'Synthetic', managerName: '',
          metrics: { baskets: 902, orders: 10, impressions: 123, clicks: 10 }, decision: {}, sources: {}, priceProtection: {}, flags: [] }],
        total: 1, itemsReturned: 1, page: 1, pageSize: 500, summary: {
          skuCount: 1, baskets: 902, orders: 10, sourceReady: 0, sourcePartial: 0, sourceBlocked: 1,
          metricTotals: {
            baskets: { value: 902, coveredSku: 1, partialSku: 0 },
            impressions: { value: 123, coveredSku: 1, partialSku: 0 },
            ctrPct: { value: 8.1, coveredSku: 1, partialSku: 0 },
            totalImpressions: { value: null, coveredSku: 0, partialSku: 0 },
          },
        }, cache: { basketsFetchedAt: '2026-08-07T12:00:00Z' },
      }) })
    })
    await page.goto('http://satorna.test/wb/repricer/stats')
    await page.addScriptTag({ content: bundle.code })
    await expect.poll(() => requests.length).toBe(1)
    await page.getByText('Загружаю статистику товаров...', { exact: true }).waitFor()
    expect(await page.locator('#repricerStatsBody [data-report-row]').count()).toBe(0)
    expect(await page.locator('#tab-repricer-stats [data-filter-summary]').innerText()).not.toContain('1 из 1')
    release()
    await page.getByText('ACTUAL-REACT-STATS-SKU', { exact: true }).waitFor({ state: 'visible', timeout: 15000 })
    expect(await page.locator('#tab-repricer-stats').isVisible()).toBe(true)
    expect(await page.title()).toBe('Satorna — Репрайсер WB')
    expect(await page.locator('#tab-repricer-stats .stat-val').first().innerText()).toBe('902')
    expect(await page.locator('#repricerStatsCards .stat').count()).toBe(16)
    expect(await page.locator('#repricerStatsCards [data-stats-card="totalImpressions"] .stat-val').innerText()).toBe('—')
    expect(await page.locator('#repricerStatsCards [data-stats-card="impressions"] .stat-val').innerText()).toBe('123')
    expect(await page.locator('#repricerStatsCards [data-stats-card="ctrPct"] .stat-val').innerText()).toBe('8,1%')
    expect(await page.locator('#repricerStatsFreshness').innerText()).toContain('Воронка:')
    expect(requests).toHaveLength(1)
    expect(new URLSearchParams(requests[0]).get('pageSize')).toBe('500')
    await page.locator('#repricerStatsFiltersTrigger').click()
    await page.screenshot({ path: '/tmp/satorna-stats-filters-panel.png' })
    await page.locator('#repricerStatsFilters [data-stats-action="apply"]').click()
    await page.getByRole('button', { name: 'Change synthetic period', exact: true }).click()
    await page.getByText('NEW-PERIOD-REACT-STATS-SKU', { exact: true }).waitFor({ state: 'visible' })
    expect(requests).toHaveLength(2)
    expect(new URLSearchParams(requests[1]).get('dateFrom')).toBe('2026-08-01')
    expect(new URLSearchParams(requests[1]).get('dateTo')).toBe('2026-08-07')
    expect(await page.getByText('ACTUAL-REACT-STATS-SKU', { exact: true }).count()).toBe(0)
    await page.screenshot({ path: '/tmp/satorna-t4-stats-actual-react-page.png' })
    await page.getByRole('button', { name: 'Clear synthetic session', exact: true }).click()
    await page.locator('#repricerStatsBody').getByText('Статистика репрайсера недоступна', { exact: false }).waitFor()
    expect(await page.getByText('ACTUAL-REACT-STATS-SKU', { exact: true }).count()).toBe(0)
    expect(await page.getByText('NEW-PERIOD-REACT-STATS-SKU', { exact: true }).count()).toBe(0)
    expect(await page.locator('#tab-repricer-stats .stat-val').allTextContents()).toEqual(Array(16).fill('—'))
    expect(requests).toHaveLength(2)
    expect(unexpected, `Unexpected fixture requests: ${JSON.stringify(unexpected)}`).toEqual([])
    expect(errors).toEqual([])
  } finally { release(); await browser.close() }
}, 60_000)
