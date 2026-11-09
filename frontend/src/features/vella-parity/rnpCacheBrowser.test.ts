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
      lib: { entry: fileURLToPath(new URL('./__fixtures__/reportLoadingBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'RnpCacheTest' } },
  })
  const outputs = Array.isArray(result) ? result : [result]
  const bundle = outputs.flatMap(output => 'output' in output ? output.output : [])
    .find(output => output.type === 'chunk' && output.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing RNP cache browser bundle')
  bundleCode = bundle.code
}, 60_000)

it.each(['empty', 'failure', 'populated'])('renders RNP cache loading → %s and clears on session loss', async outcome => {
  const browser = await chromium.launch({ headless: true })
  let release = () => {}
  const gate = new Promise<void>(resolve => { release = resolve })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1440, height: 1000 } })
    await page.clock.setFixedTime(new Date('2026-09-09T12:00:00Z'))
    const requests: string[] = [], unexpected: string[] = [], errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      if (request.method() === 'GET' && url.origin === 'http://satorna.test' && url.pathname === '/wb/reports/rnp') {
        return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      }
      if (request.method() === 'GET' && request.resourceType() === 'image') return route.fulfill({ contentType: 'image/gif', body: Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64') })
      if (request.method() === 'GET' && url.origin === 'https://fonts.googleapis.com' && url.pathname === '/css2') return route.fulfill({ contentType: 'text/css', body: '' })
      if (request.method() !== 'GET' || url.origin !== 'http://satorna.test' || url.pathname !== '/api/wb/reports/rnp/latest-cache') {
        unexpected.push(`${request.method()} ${url.pathname}`); return route.abort()
      }
      requests.push(url.search)
      await gate
      if (outcome === 'failure') return route.fulfill({ status: 503, contentType: 'application/json', body: '{"error":{"code":"SYNTHETIC_RNP_UNAVAILABLE","message":"Синтетический источник РНП недоступен"}}' })
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({
        filters: { dateRange: { from: url.searchParams.get('from'), to: url.searchParams.get('to') } },
        // These optional product fields are the existing RnpBackendRow contract used by rnpPeriodBrowser.
        rows: outcome === 'populated' ? [{ sku: 'SYNTHETIC-CACHE-RNP', productName: 'Synthetic cached RNP product', nmId: 900101,
          reasons: ['Synthetic source explanation'],
          photoUrl: 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7' }] : [],
        kpis: [], formulaNotes: [], sourceEvidence: [], reportJob: null,
      }) })
    })
    await page.goto('http://satorna.test/wb/reports/rnp')
    await page.evaluate(() => {
      window.__vellaReportPeriods = { rnp: { days: 7, mode: 'custom', fromIso: '2026-08-01', toIso: '2026-08-07', label: 'Synthetic RNP period' } }
    })
    await page.addScriptTag({ content: bundleCode })
    await expect.poll(() => requests.length, { timeout: 15_000 }).toBe(1)
    const surface = page.locator('#tab-rnp')
    // With no rows the current island renders ReportDataStateIsland, not the KPI-strip skeleton.
    await surface.getByText('Загружаем РНП', { exact: true }).waitFor({ timeout: 15_000 })
    expect(await surface.locator('[data-report-row]').count()).toBe(0)
    expect(await surface.locator('tbody tr').count()).toBe(0)
    const query = new URLSearchParams(requests[0])
    expect([query.get('from'), query.get('to'), query.get('groupBy'), query.get('source'), query.get('preset')])
      .toEqual(['2026-08-01', '2026-08-07', 'sku', 'operational', 'custom'])
    release()
    if (outcome === 'populated') {
      await surface.getByText('Артикул: SYNTHETIC-CACHE-RNP', { exact: true }).waitFor({ timeout: 15_000 })
      expect(await surface.locator('[data-vella-runtime-binding="backend-rnp"]').isVisible()).toBe(true)
      // The filter bridge also creates a hidden empty-state row; count actual report data rows.
      expect(await surface.locator('[data-vella-island="rnp-live-table-body"] > tr[data-report-row="rnp"]').count()).toBe(1)
      expect(await surface.locator('[data-report-empty="rnp-filter"]').isVisible()).toBe(false)
      const table = surface.locator('[data-vella-runtime-binding="backend-rnp"] table')
      const commentColumn = await table.getByRole('columnheader', { name: /^Комментарий/ })
        .evaluate((header: HTMLTableCellElement) => header.cellIndex)
      expect(await table.locator('tr[data-report-row="rnp"] td').nth(commentColumn).innerText())
        .toBe('Synthetic source explanation')
      expect(await table.locator('.report-comment-btn').count()).toBe(0)
    } else {
      await surface.getByText(outcome === 'empty' ? 'За выбранный период нет данных' : 'Не удалось загрузить РНП', { exact: true }).waitFor({ timeout: 15_000 })
      expect(await surface.locator('tbody tr').count()).toBe(0)
      if (outcome === 'failure') expect(await surface.innerText()).toContain('Синтетический источник РНП недоступен')
    }
    expect(await surface.getByText('Загружаем РНП', { exact: true }).count()).toBe(0)
    await page.getByRole('button', { name: 'Clear synthetic report session', exact: true }).click()
    await surface.getByText('Сессия истекла. Войдите снова, чтобы открыть РНП.', { exact: true }).waitFor({ timeout: 15_000 })
    expect(await surface.getByText('Не удалось загрузить РНП', { exact: true }).isVisible()).toBe(true)
    expect(await surface.locator('tbody tr').count()).toBe(0)
    expect(await surface.getByText('Артикул: SYNTHETIC-CACHE-RNP', { exact: true }).count()).toBe(0)
    expect(requests).toHaveLength(1)
    expect(unexpected).toEqual([])
    expect(errors).toEqual([])
  } finally { release(); await browser.close() }
}, 45_000)
