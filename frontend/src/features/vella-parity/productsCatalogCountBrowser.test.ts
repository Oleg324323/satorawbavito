import path from 'node:path'
import { fileURLToPath } from 'node:url'
import react from '@vitejs/plugin-react'
import { chromium } from 'playwright'
import { build } from 'vite'
import { expect, it } from 'vitest'

const product = (index: number) => ({
  meta: {
    articleId: `SKU-${index}`,
    nmId: 100_000 + index,
    name: `Товар ${index}`,
    status: 'auto',
    currentPriceKopecks: 200_000,
    basketsLast7d: 1,
    basketNorm: 10,
  },
  settings: { wbCommissionPct: 15, minMarginPct: 20, cogsKopecks: 80_000, logisticsKopecks: 10_000 },
  analytics: { baskets: 1, financeState: 'ok' },
})

it('keeps catalog totals in the table without duplicating unscoped navigation counts', async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({
    configFile: false, envFile: false, root, logLevel: 'silent', plugins: [react()],
    define: {
      'process.env.NODE_ENV': JSON.stringify('test'),
      'import.meta.env.VITE_API_BASE_URL': JSON.stringify(''),
      'import.meta.env.VITE_WB_LIVE_ENABLED': JSON.stringify('false'),
    },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: {
      entry: fileURLToPath(new URL('./__fixtures__/productsCatalogCountBrowser.tsx', import.meta.url)),
      formats: ['iife'], name: 'ProductsCatalogCountTest',
    } },
  })
  const outputs = Array.isArray(result) ? result : [result]
  const bundle = outputs.flatMap(output => 'output' in output ? output.output : [])
    .find(output => output.type === 'chunk' && output.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing products count fixture bundle')

  const browser = await chromium.launch({ headless: true })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1440, height: 1000 } })
    const errors: string[] = [], productQueries: string[] = [], unexpected: string[] = []
    let scopedSummary = false
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', route => {
      const request = route.request(), url = new URL(request.url())
      if (url.origin === 'http://satorna.test' && url.pathname === '/wb/repricer') {
        return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      }
      if (request.resourceType() === 'image') {
        return route.fulfill({ contentType: 'image/gif', body: Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64') })
      }
      if (url.origin === 'https://fonts.googleapis.com') return route.fulfill({ contentType: 'text/css', body: '' })
      if (url.pathname === '/api/v1/wb-repricer/sku') {
        productQueries.push(url.search)
        const filtered = Boolean(url.searchParams.get('q'))
        const requestedPage = Number(url.searchParams.get('page') || 1)
        const multiplier = url.searchParams.get('q')?.includes('fallback') ? 2 : 1
        const items = requestedPage > 1 ? [] : filtered ? [product(3410)] : Array.from({ length: 150 }, (_, index) => product(index + 1))
        return route.fulfill({ json: {
          items, total: filtered ? 1 : 3410, totalCached: 3410, itemsReturned: items.length, page: requestedPage, pageSize: 150,
          summary: scopedSummary ? { skuCount: 1, totalBaskets: 1, revenueKopecks: 123400 * multiplier, marginKopecks: 56700 * multiplier, cogsKopecks: 20000 * multiplier, expensesKopecks: 46700 * multiplier, avgMarginPct: 56700 / 123400 * 100 } : { skuCount: filtered ? 1 : 3410, totalBaskets: items.length },
          cache: { pagesCached: 4, totalCached: 3410, nextOffset: 4000, pageLimit: 1000, basketsMatchedNmIds: 3410, ...(scopedSummary ? { summaryScope: 'filtered_skus' } : {}) },
        } })
      }
      if (url.pathname === '/api/v1/wb-repricer/strategies/catalog') return route.fulfill({ json: { items: [], total: 0 } })
      if (url.pathname === '/api/v1/wb-repricer/sku-groups') return route.fulfill({ json: { items: [], total: 0 } })
      if (url.pathname === '/api/v1/wb-repricer/sync/status') return route.fulfill({ json: {
        state: 'partial', running: false, finishedAt: '2026-09-13T16:00:00Z', steps: [],
        syncPlan: [
          { syncProfile: 'onboarding-7', syncProfileLabel: '7 дней', group: 'onboarding', state: 'completed' },
          { syncProfile: 'onboarding-30', syncProfileLabel: '30 дней', group: 'onboarding', state: 'partial' },
        ],
      } })
      if (url.pathname === '/api/v1/wb-repricer/worker/status') return route.fulfill({ json: { available: true, tasks: [] } })
      if (url.pathname === '/api/v1/wb-repricer/changelog') return route.fulfill({ json: { items: [], total: 0 } })
      unexpected.push(`${request.method()} ${url.pathname}`)
      return route.abort()
    })

    await page.goto('http://satorna.test/wb/repricer')
    await page.addScriptTag({ content: bundle.code })
    const sidebarCount = page.locator('#sidebar [data-tab="products"] > .nav-badge')
    const subtabCount = page.locator('.subtabs [data-tab="products"] > .subtab-count')
    await expect.poll(() => productQueries.length, { timeout: 15_000 }).toBeGreaterThan(0)
    await page.waitForFunction(() => window.__vellaProductsCacheMeta?.totalCached === 3410)
    expect(await sidebarCount.count()).toBe(0)
    expect(await subtabCount.count()).toBe(0)
    expect(await page.evaluate(() => window.__vellaProductsCacheMeta?.totalCached)).toBe(3410)
    await expect.poll(() => page.locator('[data-vella-island="products-sticky-pagination"] span').first().innerText()).toBe('1-150 из 3410 SKU')
    expect.soft(await page.locator('.subtabs [data-module-panel="repricer"]').isVisible()).toBe(false)
    const dataStatus = page.locator('[data-vella-island="products-backend-cache-controls"]')
    expect.soft(await dataStatus.count()).toBe(0)
    expect.soft(await page.getByText('Данные загружены частично', { exact: true }).count()).toBe(0)
    expect.soft(await page.getByRole('button', { name: /Скачать настройки XLSX|Импорт настроек XLSX|Статус и диагностика|Этапы загрузки/ }).count()).toBe(0)
    expect.soft(await page.locator('.density-toggle').count()).toBe(0)
    expect.soft(await page.locator('#kpiAdsSpend').isVisible()).toBe(false)
    const extraMetrics = page.locator('details.products-kpi-more')
    expect.soft(await extraMetrics.count()).toBe(1)
    if (await extraMetrics.count()) {
      await extraMetrics.locator('summary').focus()
      await page.keyboard.press('Enter')
      expect.soft(await page.locator('#kpiAdsSpend').isVisible()).toBe(true)
      await page.keyboard.press('Enter')
      expect.soft(await page.locator('#kpiAdsSpend').isVisible()).toBe(false)
    }
    const revenueTip = page.getByRole('button', { name: /^Выручка за период:/ })
    expect.soft(await revenueTip.count()).toBe(1)
    if (await revenueTip.count()) {
      await revenueTip.focus()
      await expect.poll(() => page.locator('#g-tip').innerText()).toContain('retailAmount')
      await page.keyboard.press('Escape')
      expect.soft(await page.locator('#g-tip').isVisible()).toBe(false)
    }

    // Both controls must remain reachable; changing their z-index alone cannot fix the overlap.
    for (const width of [1366, 1440, 1920]) {
      await page.setViewportSize({ width, height: 1000 })
      await page.locator('#bulkBar').waitFor({ state: 'visible' })
      const bulk = (await page.locator('#bulkBar').boundingBox())!
      const pagination = (await page.locator('[data-vella-island="products-sticky-pagination"]').boundingBox())!
      expect.soft(bulk.y + bulk.height, `bulk actions at ${width}px`).toBeLessThanOrEqual(pagination.y)
      expect.soft(bulk.x).toBeGreaterThanOrEqual(0)
      expect.soft(bulk.x + bulk.width).toBeLessThanOrEqual(width)
      const tableWrap = page.locator('#tableWrap')
      await page.locator('#tab-products').evaluate(element => element.scrollTo({ top: element.scrollHeight, behavior: 'instant' }))
      await tableWrap.evaluate(element => { element.scrollTop = element.scrollHeight; element.scrollLeft = element.scrollWidth })
      const lastRow = (await page.locator('#tbody tr').last().boundingBox())!
      expect.soft(lastRow.y + lastRow.height, `last row above controls at ${width}px`).toBeLessThanOrEqual(bulk.y)
      expect.soft(await page.locator('#tab-products').evaluate(element => getComputedStyle(element).paddingBottom)).toBe('0px')
      await tableWrap.evaluate(element => { element.scrollTop = 0; element.scrollLeft = 0 })
      await page.locator('#tab-products').evaluate(element => element.scrollTo({ top: 0, behavior: 'instant' }))
    }
    if (process.env.SATORNA_PRODUCTS_COUNT_SCREENSHOTS) {
      await page.screenshot({ path: '/tmp/satorna-products-count-desktop.png' })
      await page.setViewportSize({ width: 390, height: 844 })
      await expect.poll(async () => Math.round((await page.locator('#sidebar').boundingBox())!.width)).toBe(64)
      await page.screenshot({ path: '/tmp/satorna-products-count-mobile.png' })
      await page.setViewportSize({ width: 1440, height: 1000 })
    }

    await page.evaluate(() => {
      window.__vellaAllProductsForKpi = [{ financeState: 'ok', revenue: 999, netSku: 999, cogsTotal: 999, expenses: 999 }]
      Object.assign(window.__vellaProductsSummary!, { revenueKopecks: 0, marginKopecks: 0, cogsKopecks: -200, expensesKopecks: -300, avgMarginPct: 0 })
      window.dispatchEvent(new CustomEvent('vella:products-kpi-updated'))
    })
    await expect.poll(() => page.locator('#kpiRevenue').innerText()).toBe('0 ₽')
    expect.soft(await page.locator('#kpiMarginRub').innerText()).toBe('0 ₽')
    await expect.poll(() => page.locator('#kpiCogs').innerText()).toBe('-2 ₽')
    expect.soft(await page.locator('#kpiExpenses').innerText()).toBe('-3 ₽')
    expect.soft(await page.locator('#kpiMargin').innerText()).toBe('—')
    await page.evaluate(() => {
      Object.assign(window.__vellaProductsSummary!, { revenueKopecks: -10000, marginKopecks: -5000, avgMarginPct: 99 })
      window.dispatchEvent(new CustomEvent('vella:products-kpi-updated'))
    })
    await expect.poll(() => page.locator('#kpiMargin').innerText()).toBe('50.0%')
    await page.evaluate(() => {
      Object.assign(window.__vellaProductsSummary!, { revenueKopecks: null, marginKopecks: null, cogsKopecks: null, expensesKopecks: null, avgMarginPct: null })
      window.dispatchEvent(new CustomEvent('vella:products-kpi-updated'))
    })
    await expect.poll(() => page.locator('#kpiRevenue').innerText()).toBe('—')
    for (const id of ['kpiMarginRub', 'kpiCogs', 'kpiExpenses', 'kpiMargin']) expect.soft(await page.locator(`#${id}`).innerText()).toBe('—')

    await page.locator('#searchTable').fill('SKU-3410')
    await expect.poll(() => productQueries.some(query => new URLSearchParams(query).get('q') === 'SKU-3410'), { timeout: 15_000 }).toBe(true)
    await expect.poll(() => page.locator('#totalCount').innerText(), { timeout: 15_000 }).toBe('1')
    await expect.poll(() => page.locator('.products-kpi-summary-actions').innerText()).toContain('Сводка каталога · фильтры применены только к таблице')
    scopedSummary = true
    await page.locator('#searchTable').fill('SKU-3410-scoped')
    await expect.poll(() => productQueries.some(query => new URLSearchParams(query).get('q') === 'SKU-3410-scoped')).toBe(true)
    await expect.poll(() => page.locator('.products-kpi-summary-actions').innerText()).toContain('Все товары по фильтру')
    await expect.poll(() => page.locator('#kpiRevenue').innerText()).toBe('1 234 ₽')
    expect(await page.locator('#kpiMarginRub').innerText()).toBe('567 ₽')
    expect(await page.locator('#kpiMargin').innerText()).toBe('45.9%')
    expect(await page.evaluate(() => window.__vellaProductsSummary?.skuCount)).toBe(1)
    await page.evaluate(async () => {
      const input = document.getElementById('searchTable') as HTMLInputElement
      input.value = 'SKU-3410-fallback'
      window.__vellaProductsListState = { ...window.__vellaProductsListState, page: 2 }
      await window.__vellaLoadLiveRepricerProducts?.()
    })
    await expect.poll(() => page.locator('#kpiRevenue').innerText()).toBe('2 468 ₽')
    expect(await page.locator('#kpiMarginRub').innerText()).toBe('1 134 ₽')
    expect(productQueries.filter(query => new URLSearchParams(query).get('q') === 'SKU-3410-fallback').map(query => new URLSearchParams(query).get('page'))).toEqual(['2', '1'])
    expect(await sidebarCount.count()).toBe(0)
    expect(await subtabCount.count()).toBe(0)
    expect(await page.evaluate(() => window.__vellaProductsCacheMeta?.totalCached)).toBe(3410)
    if (process.env.SATORNA_PRODUCTS_COUNT_SCREENSHOTS) {
      await page.screenshot({ path: '/tmp/satorna-products-count-filtered.png' })
    }

    await page.setViewportSize({ width: 390, height: 844 })
    await expect.poll(async () => Math.round((await page.locator('#sidebar').boundingBox())!.width)).toBe(64)
    expect.soft(await page.locator('[data-vella-island="products-sticky-pagination"]').isVisible()).toBe(false)
    expect.soft(await page.locator('#bulkBar').isVisible()).toBe(false)
    expect.soft(await page.getByLabel('Раздел репрайсера').isVisible()).toBe(true)
    await page.getByLabel('Раздел репрайсера').selectOption('history')
    await expect.poll(() => page.locator('#tab-history').getAttribute('class')).toContain('active')
    await page.getByLabel('Раздел репрайсера').selectOption('products')
    await expect.poll(() => page.locator('#tab-products').getAttribute('class')).toContain('active')

    expect(errors).toEqual([])
    expect(unexpected).toEqual([])
  } finally {
    await browser.close()
  }
}, 60_000)
