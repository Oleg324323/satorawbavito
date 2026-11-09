import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { chromium } from 'playwright'
import { expect, it } from 'vitest'

it('renders evidence states, sorts them separately and excludes unknown products from negative filters', async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({ root, configFile: false, envFile: false, publicDir: false, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""', 'import.meta.env.VITE_WB_LIVE_ENABLED': '"false"' },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: { entry: fileURLToPath(new URL('./__fixtures__/productsCatalogCountBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'PromotionStatusTest' } },
  })
  const output = (Array.isArray(result) ? result : [result]).flatMap(item => 'output' in item ? item.output : [])
  const entry = output.find(item => item.type === 'chunk' && item.isEntry)
  if (!entry || entry.type !== 'chunk') throw new Error('Missing promotion status fixture')
  const css = output.filter(item => item.type === 'asset' && item.fileName.endsWith('.css')).map(item => item.type === 'asset' ? String(item.source) : '').join('\n')
  const cases = [
    { sku: 'YES', status: 'yes', name: 'Synthetic promo' },
    { sku: 'NO', status: 'no', name: undefined },
    { sku: 'MISSING', status: undefined, name: 'A name is not participation evidence' },
    { sku: 'UNKNOWN', status: 'unknown', name: undefined },
  ]
  const items = cases.map((item, index) => ({
    meta: { articleId: item.sku, nmId: 101 + index, name: 'Synthetic product', status: 'manual', currentPriceKopecks: 150000, basketsLast7d: 1, basketNorm: 1 },
    settings: { cogsKopecks: 50000, logisticsKopecks: 5000, minMarginPct: 15, wbCommissionPct: 15 },
    analytics: { promotionStatus: item.status, promotionName: item.name, promotionStatusScope: 'regular',
      promotionStatusReason: item.status === 'no' ? 'Нет в проверенных обычных акциях. Автоакции не проверены.' : undefined,
      financeState: 'ok', baskets: 1, basketsState: 'ok', ordersUnits: 1, periodStatsState: 'ok', stockState: 'ok', wbStockUnits: 5 },
  }))
  const browser = await chromium.launch({ headless: true })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1600, height: 1000 } })
    const errors: string[] = [], queries: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', route => {
      const req = route.request(), url = new URL(req.url())
      if (req.resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (req.resourceType() === 'image') return route.fulfill({ status: 204 })
      if (url.hostname === 'fonts.googleapis.com') return route.fulfill({ body: '' })
      if (url.pathname === '/api/v1/wb-repricer/sku') {
        queries.push(url.search)
        const filters = JSON.parse(url.searchParams.get('filters') || '{}')
        const selected = items.filter(item => !filters.promo || filters.promo === 'all' || item.analytics.promotionStatus === filters.promo)
        // Exercise the frontend's local fallback too: the server omits the
        // serverFiltersApplied marker on this synthetic response.
        return route.fulfill({ json: { items: selected, total: selected.length, page: 1, pageSize: 150,
          summary: { skuCount: selected.length }, cache: { totalCached: items.length, pagesCached: 1 } } })
      }
      return route.fulfill({ json: { items: [], data: [], total: 0, state: 'completed', running: false, steps: [] } })
    })
    await page.goto('http://satorna.test/wb/repricer')
    if (css) await page.addStyleTag({ content: css })
    await page.addScriptTag({ content: entry.code })
    const cell = (sku: string) => page.locator(`#tbody tr[data-sku="${sku}"] .promo-state`)
    await cell('MISSING').waitFor({ timeout: 15000 })
    expect(await cell('YES').innerText()).toBe('Synthetic promo')
    expect(await cell('YES').evaluate(el => getComputedStyle(el, '::before').backgroundColor)).toBe('rgb(245, 158, 11)')
    expect(await cell('NO').innerText()).toBe('Нет в обычных')
    expect(await cell('NO').getAttribute('data-tip')).toContain('Автоакции не проверены')
    expect(await cell('NO').evaluate(el => getComputedStyle(el, '::before').backgroundColor)).toBe('rgb(16, 185, 129)')
    for (const sku of ['MISSING', 'UNKNOWN']) {
      expect(await cell(sku).innerText()).toBe('Нет данных')
      expect(await cell(sku).getAttribute('data-tip')).not.toContain('Не участвует')
      expect(await cell(sku).evaluate(el => getComputedStyle(el, '::before').backgroundColor)).toBe('rgb(148, 163, 184)')
    }
    const ids = () => page.locator('#tbody tr[data-sku]').evaluateAll(rows => rows.map(row => row.getAttribute('data-sku')))
    const header = page.locator('#mainTable thead [data-column-id="promo"]')
    await header.click({ position: { x: 8, y: 10 } }) // Avoid the help icon, which deliberately does not sort.
    await expect.poll(ids).toEqual(['MISSING', 'UNKNOWN', 'YES', 'NO'])
    await header.click({ position: { x: 8, y: 10 } })
    await expect.poll(ids).toEqual(['NO', 'YES', 'MISSING', 'UNKNOWN'])
    await page.locator('[data-vella-island="products-filter-chips"] button').filter({ hasText: 'Фильтры' }).first().click()
    await page.locator('[data-promo="no"]').click()
    await expect.poll(ids).toEqual(['NO'])
    await page.locator('[data-promo="all"]').click()
    await expect.poll(async () => (await ids()).length).toBe(4)
    await page.locator('[data-promo="yes"]').click()
    await expect.poll(ids).toEqual(['YES'])
    expect(queries.length).toBeLessThan(8)
    expect(errors).toEqual([])
  } finally {
    await browser.close()
  }
}, 60000)
