import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium, type Page } from 'playwright'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { expect, it } from 'vitest'

const knownVellaEmptyImageWarning = 'An empty string ("") was passed to the %s attribute. This may cause the browser to download the whole page again over the network. To fix this, either do not render the element at all or pass null to %s instead of an empty string. src src'
const metricRows = [
  { articleId: 'UNKNOWN-STATS-SKU', baskets: null, orders: 1, cartToOrderCrPct: null, promotionStatus: 'promo' },
  { articleId: 'ZERO-STATS-SKU', baskets: 0, orders: 0, cartToOrderCrPct: 0, promotionStatus: 'none' },
  { articleId: 'NULL-CR-STATS-SKU', baskets: 5, orders: 3, cartToOrderCrPct: null, promotionStatus: 'promo' },
  { articleId: 'POSITIVE-STATS-SKU', baskets: 7, orders: 3, cartToOrderCrPct: 42.9, promotionStatus: 'promo' },
]

function statsItem(row: typeof metricRows[number]) {
  return {
    articleId: row.articleId, nmId: 101, name: 'Synthetic statistics product', brand: 'Synthetic',
    managerId: null, managerName: '', promotionStatus: row.promotionStatus, promotionStatusText: null,
    metrics: {
      impressions: 10, clicks: 2, ctrPct: 20, baskets: row.baskets, orders: row.orders,
      cartToOrderCrPct: row.cartToOrderCrPct, stockUnits: 5, medianPriceKopecks: 100_000,
    },
    decision: { id: 'can_recalculate', label: 'готово', tone: 'ok', reasons: [] },
    sources: { status: 'ready', missing: [], fresh: ['baskets'], states: {} },
    priceProtection: { status: 'can_recalculate', blockerIds: [], message: null }, flags: [],
  }
}

function response(rows: typeof metricRows, summary: Record<string, unknown>) {
  return {
    items: rows.map(statsItem), total: rows.length, itemsReturned: rows.length, page: 1,
    pageSize: 500, periodDays: 7, dateFrom: '2026-08-01', dateTo: '2026-08-07', summary, cache: {},
  }
}

async function expectRow(page: Page, articleId: string, basketIndex: number, crIndex: number, basket: string, cr: string) {
  const row = page.getByRole('row').filter({ hasText: articleId })
  expect(await row.locator('td').nth(basketIndex).innerText()).toBe(basket)
  expect(await row.locator('td').nth(crIndex).innerText()).toBe(cr)
}

it('renders unknown and zero baskets honestly in both actual statistics pages', async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({
    configFile: false, envFile: false, root, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': JSON.stringify('test'), 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('') },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false,
      lib: { entry: fileURLToPath(new URL('./__fixtures__/repricerStatsNullableBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'NullableStatsTest' } },
  })
  const outputs = Array.isArray(result) ? result : [result]
  const bundle = outputs.flatMap(output => 'output' in output ? output.output : [])
    .find(output => output.type === 'chunk' && output.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing nullable statistics bundle')
  const styles = outputs.flatMap(output => 'output' in output ? output.output : [])
    .flatMap(output => output.type === 'asset' && output.fileName.endsWith('.css') ? [String(output.source)] : [])
    .join('\n')
  if (!styles) throw new Error('Missing nullable statistics styles')
  const browser = await chromium.launch({ headless: true })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1440, height: 1000 } })
    const unexpected: string[] = [], errors: string[] = [], requests: string[] = []
    let phase = 'public'
    page.on('pageerror', error => errors.push(error.message))
    page.on('console', message => {
      if (message.type() !== 'error') return
      if (phase === 'public' && message.text() === knownVellaEmptyImageWarning) return
      errors.push(`${phase}: ${message.text()} ${JSON.stringify(message.location())}`)
    })
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      if (url.origin === 'http://satorna.test' && request.method() === 'GET' && url.pathname === '/wb/repricer/stats') {
        return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      }
      if (request.resourceType() === 'image') return route.fulfill({ contentType: 'image/gif', body: Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64') })
      if (request.method() === 'GET' && url.origin === 'https://fonts.googleapis.com' && url.pathname === '/css2') {
        return route.fulfill({ contentType: 'text/css', body: '/* Offline font fixture. */' })
      }
      if (url.origin !== 'http://satorna.test' || request.method() !== 'GET' || url.pathname !== '/api/v1/wb-repricer/stats') {
        unexpected.push(`${request.method()} ${url.pathname}`)
        return route.abort()
      }
      requests.push(url.search)
      const internalRequest = url.searchParams.get('pageSize') === '150'
      const internalRequestNumber = requests.filter(search => new URLSearchParams(search).get('pageSize') === '150').length
      const body = !internalRequest || internalRequestNumber === 1
        ? response(metricRows, { baskets: null, orders: 4, cartToOrderCrPct: null })
        : internalRequestNumber === 2
          ? response(metricRows.slice(1), { orders: 3, cartToOrderCrPct: 42.9 })
          : response([metricRows[0], metricRows[2]], { orders: 4, cartToOrderCrPct: null })
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) })
    })

    await page.goto('http://satorna.test/wb/repricer/stats')
    await page.addStyleTag({ content: styles })
    await page.addScriptTag({ content: bundle.code })
    await page.getByText('UNKNOWN-STATS-SKU', { exact: true }).waitFor({ state: 'visible', timeout: 15_000 })
    expect(page.url()).toBe('http://satorna.test/wb/repricer/stats')
    expect(await page.title()).toBe('Satorna — Репрайсер WB')
    expect(await page.locator('#repricerStatsBody tr').first().locator('td').nth(8).innerText()).toBe('—')
    expect(await page.locator('#repricerStatsBody tr').first().locator('td').nth(10).innerText()).toBe('—')
    expect(await page.locator('#tab-repricer-stats .stat-val').first().innerText()).toBe('—')
    expect(await page.locator('#tab-repricer-stats .stat-delta').first().innerText()).toContain('нет данных WB')
    await expectRow(page, 'ZERO-STATS-SKU', 8, 10, '0', '0%')
    await expectRow(page, 'NULL-CR-STATS-SKU', 8, 10, '5', '—')
    await expectRow(page, 'POSITIVE-STATS-SKU', 8, 10, '7', '42,9%')
    expect(await page.locator('vite-error-overlay, [data-vite-dev-id]').count()).toBe(0)
    const internalSwitcher = page.getByRole('button', { name: 'Open internal statistics', exact: true, includeHidden: true })
    await internalSwitcher.evaluate(element => { element.style.visibility = 'hidden' })
    await page.screenshot({ path: '/tmp/satorna-task2-public-desktop.png' })
    await internalSwitcher.evaluate(element => { element.style.visibility = 'visible' })

    phase = 'internal'
    await internalSwitcher.click()
    await page.getByRole('heading', { name: 'Статистика репрайсера', exact: true }).waitFor()
    await expectRow(page, 'UNKNOWN-STATS-SKU', 6, 8, '—', '—')
    await expectRow(page, 'ZERO-STATS-SKU', 6, 8, '0', '0.0%')
    await expectRow(page, 'NULL-CR-STATS-SKU', 6, 8, '5', '—')
    await expectRow(page, 'POSITIVE-STATS-SKU', 6, 8, '7', '42.9%')
    const basketsKpi = page.locator('.vella-kpi').filter({ hasText: 'Корзины' }).locator('.vella-kpi-value')
    expect(await basketsKpi.innerText()).toBe('—')

    await page.getByRole('button', { name: 'Обновить', exact: true }).click()
    await expect.poll(() => basketsKpi.innerText()).toBe('12')
    await page.getByRole('button', { name: 'Обновить', exact: true }).click()
    await page.getByText('UNKNOWN-STATS-SKU', { exact: true }).waitFor()
    expect(await basketsKpi.innerText()).toBe('—')
    await page.getByRole('button', { name: 'В акции', exact: true }).click()
    expect(await page.locator('.vella-table tbody tr').count()).toBe(2)
    expect(await basketsKpi.innerText()).toBe('—')
    await page.screenshot({ path: '/tmp/satorna-task2-internal-desktop.png' })
    await page.setViewportSize({ width: 390, height: 844 })
    expect(await page.getByRole('heading', { name: 'Статистика репрайсера', exact: true }).isVisible()).toBe(true)
    await page.screenshot({ path: '/tmp/satorna-task2-internal-mobile.png' })

    expect(requests.some(search => new URLSearchParams(search).get('pageSize') === '500')).toBe(true)
    expect(requests.filter(search => new URLSearchParams(search).get('pageSize') === '150')).toHaveLength(3)
    expect(unexpected, `Unexpected fixture requests: ${JSON.stringify(unexpected)}`).toEqual([])
    expect(errors, `Browser errors: ${JSON.stringify(errors)}`).toEqual([])
  } finally { await browser.close() }
}, 60_000)
