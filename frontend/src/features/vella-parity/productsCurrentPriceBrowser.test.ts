import path from 'node:path'
import { mkdir } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import react from '@vitejs/plugin-react'
import { chromium } from 'playwright'
import { build } from 'vite'
import { expect, it } from 'vitest'

it('keeps current buyer columns separate and shows factual margin without shifting cells', async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({ root, configFile: false, envFile: false, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""', 'import.meta.env.VITE_WB_LIVE_ENABLED': '"false"' },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: { entry: fileURLToPath(new URL('./__fixtures__/productsCatalogCountBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'CurrentProductPriceTest' } },
  })
  const artifacts = (Array.isArray(result) ? result : [result]).flatMap(output => 'output' in output ? output.output : [])
  const bundle = artifacts.find(output => output.type === 'chunk' && output.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing current product price fixture bundle')
  const styles = artifacts.flatMap(output => output.type === 'asset' && output.fileName.endsWith('.css') ? [String(output.source)] : []).join('\n')
  const rows = [
    { sku: 'AVERAGE-ONLY', buyer: null, wallet: null, average: 211400, marginPct: -18.4, marginKopecks: -40500, buyoutPct: 80 },
    { sku: 'BUYER-LOW', buyer: 130000, wallet: null, average: 200000, marginPct: 0, marginKopecks: 0, buyoutPct: 80 },
    { sku: 'BUYER-HIGH', buyer: 180000, wallet: 170000, average: 100000, marginPct: 20, marginKopecks: 36000, buyoutPct: 80 },
    { sku: 'MARGIN-UNKNOWN', buyer: null, wallet: null, average: 213400, marginPct: null, marginKopecks: null, buyoutPct: null },
  ]
  const items = rows.map((row, index) => ({
    meta: { articleId: row.sku, nmId: 100 + index, name: 'Synthetic product', status: 'auto', currentPriceKopecks: 220000, basketsLast7d: 1, basketNorm: 1 },
    settings: { cogsKopecks: 50000, logisticsKopecks: 5000, minMarginPct: 15, wbCommissionPct: 15 },
    analytics: {
      financeState: 'ok', abcCode: 'AA',
      buyerPriceNoWalletKopecks: row.buyer, buyerPriceWithWalletKopecks: row.wallet, avgPriceWithSppKopecks: row.average,
      commissionState: 'ok', commissionSource: 'tariffs.kgvpMarketplace', commissionDisplayPct: 17,
      marginMode: 'planned_indeepa', plannedMarginState: row.buyoutPct === null ? 'missing_inputs' : 'ok',
      marginPct: row.marginPct, marginKopecks: row.marginKopecks, buyoutPct: row.buyoutPct,
      baskets: 1, basketsState: 'ok', ordersUnits: 1, periodStatsState: 'ok', stockState: 'ok', wbStockUnits: 5,
    },
  }))
  const browser = await chromium.launch({ headless: true })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1440, height: 1000 } })
    const errors: string[] = [], unexpected: string[] = []
    let savedCost = 50000
    let costWrites = 0
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', route => {
      const request = route.request(), url = new URL(request.url())
      if (url.origin === 'http://satorna.test' && url.pathname === '/api/v2/wb/products/100/current-cost') return route.fulfill({ json: { data: { catalogSkuId: 1, linkedProductCount: 1, canWrite: true, currentCost: { amountKopecks: savedCost, costVersionId: 1, effectiveFrom: '2026-09-17T00:00:00Z' } } } })
      if (url.origin === 'http://satorna.test' && url.pathname === '/api/v2/catalog/skus/1/cost-history') return route.fulfill({ json: { data: [] } })
      if (url.origin === 'http://satorna.test' && url.pathname === '/api/v2/catalog/skus/1/current-cost' && request.method() === 'POST') {
        const payload = request.postDataJSON()
        expect(payload.expectedCostVersionId).toBe(1)
        expect(payload.sourceReference).toBeTruthy()
        savedCost = payload.amountKopecks; costWrites += 1
        return route.fulfill({ json: { data: { amountKopecks: savedCost, costVersionId: 2, effectiveFrom: '2026-09-17T00:01:00Z' } } })
      }
      if (request.resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (request.resourceType() === 'image') return route.fulfill({ contentType: 'image/gif', body: Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64') })
      if (url.origin === 'https://fonts.googleapis.com') return route.fulfill({ contentType: 'text/css', body: '' })
      if (url.origin === 'http://satorna.test' && request.method() === 'GET') {
        if (url.pathname === '/api/v1/wb-repricer/sku') return route.fulfill({ json: {
          items, total: items.length, itemsReturned: items.length, page: 1, pageSize: 150,
          summary: { skuCount: items.length, buyoutUnits: 6, buyoutAmountKopecks: 160001 }, cache: { totalCached: items.length, pagesCached: 1 },
        } })
        if (['/api/v1/wb-repricer/strategies/catalog', '/api/v1/wb-repricer/sku-groups', '/api/v1/wb-repricer/changelog'].includes(url.pathname)) return route.fulfill({ json: { items: [], total: 0 } })
        if (['/api/v1/wb-repricer/sync/status', '/api/v1/wb-repricer/worker/status'].includes(url.pathname)) return route.fulfill({ json: { state: 'completed', running: false, steps: [] } })
      }
      unexpected.push(`${request.method()} ${url.pathname}`)
      return route.abort()
    })
    await page.goto('http://satorna.test/wb/repricer')
    await page.addStyleTag({ content: styles })
    await page.addScriptTag({ content: bundle.code })
    await page.locator('#tbody tr[data-sku="AVERAGE-ONLY"]').waitFor({ timeout: 15000 })
    const cell = (sku: string, column: string) => page.locator(`#tbody tr[data-sku="${sku}"] [data-column-id="${column}"]`)
    const compact = (value: string) => value.replace(/\s+/g, '')
    expect.soft(compact(await cell('AVERAGE-ONLY', 'avgPriceSpp').innerText())).toBe('—сСПП')
    expect.soft(compact(await cell('BUYER-LOW', 'avgPriceSpp').innerText())).toBe('1300₽сСПП')
    expect.soft(compact(await cell('BUYER-HIGH', 'avgPriceSpp').innerText())).toBe('1800₽сСПП')
    expect.soft(await cell('AVERAGE-ONLY', 'priceWithWallet').innerText()).toBe('нет данных')
    expect.soft(await cell('BUYER-LOW', 'priceWithWallet').innerText()).toBe('нет данных')
    expect.soft(compact(await cell('BUYER-HIGH', 'priceWithWallet').innerText())).toBe('1700₽скошельком')
    expect(await page.locator('#mainTable thead [data-column-id="mg"]').count()).toBe(1)
    expect(await page.locator('#mainTable thead th').count()).toBe(21)
    expect(await page.locator('#mainTable colgroup col').count()).toBe(21)
    for (const row of rows) {
      expect(await page.locator(`#tbody tr[data-sku="${row.sku}"] td`).count()).toBe(21)
      expect(await cell(row.sku, 'mg').innerText()).toContain('—') // Fixture has planned values only.
      expect(await cell(row.sku, 'currentCost').getByRole('textbox').count()).toBe(1)
      expect(await cell(row.sku, 'currentCost').innerText()).not.toContain('Себестоимость…')
      expect(await cell(row.sku, 'currentCost').getByRole('textbox').evaluate(el => getComputedStyle(el).borderTopColor)).toBe('rgb(37, 99, 235)')
      expect(await cell(row.sku, 'commissionPct').innerText()).toContain('17')
      expect(await cell(row.sku, 'comment').count()).toBe(1)
      expect(await cell(row.sku, 'abc').innerText()).toBe('A—')
    }
    const sortValues = await page.evaluate(() => window.eval(
      "PRODUCTS.map(function(p){return {sku:p.sku,spp:productSortValue(p,'avgPriceSpp'),wallet:productSortValue(p,'priceWithWallet')};}).sort(function(a,b){return a.sku.localeCompare(b.sku);})",
    ))
    await cell('AVERAGE-ONLY', 'currentCost').getByRole('textbox').click()
    const costInput = page.getByRole('textbox', { name: 'Себестоимость 100, рублей за штуку' })
    await costInput.fill('350,50')
    expect(costWrites).toBe(0)
    await costInput.press('Enter')
    await expect.poll(() => costWrites).toBe(1)
    expect(savedCost).toBe(35050)
    expect(costWrites).toBe(1)
    expect.soft(sortValues).toEqual([
      { sku: 'AVERAGE-ONLY', spp: 0, wallet: 0 },
      { sku: 'BUYER-HIGH', spp: 1800, wallet: 1700 },
      { sku: 'BUYER-LOW', spp: 1300, wallet: 0 },
      { sku: 'MARGIN-UNKNOWN', spp: 0, wallet: 0 },
    ])
    const prices = await page.evaluate(() => {
      const products = window.PRODUCTS as Array<{ sku: string; avgPriceSpp: number | null; priceWithSpp: number | null }>
      return products.map(product => ({ sku: product.sku, average: product.avgPriceSpp, buyer: product.priceWithSpp })).sort((a, b) => a.sku.localeCompare(b.sku))
    })
    expect(prices).toEqual(rows.map(row => ({ sku: row.sku, average: row.average / 100, buyer: row.buyer === null ? null : row.buyer / 100 })).sort((a, b) => a.sku.localeCompare(b.sku)))
    await page.locator('.products-kpi-more summary').click()
    expect(compact(await page.locator('#kpiOrdersUnits').innerText())).toBe('6шт./1600,01₽')
    expect(await page.locator('#kpiOrdersUnits').locator('..').innerText()).toContain('Выкуплено, шт. / ₽')
    expect(await page.locator('#kpiInSale').innerText()).toBe('—')
    const outputDir = path.join(root, 'output/playwright/all-products')
    await mkdir(outputDir, { recursive: true })
    for (const width of [1440, 390, 720]) {
      await page.setViewportSize({ width, height: 1000 })
      if (width <= 720) {
        // Existing mobile gate is a documented open requirement, not usable-table parity.
        expect(await page.locator('.desktop-only-fallback').isVisible()).toBe(true)
        await page.screenshot({ path: path.join(outputDir, `products-${width}.png`) })
        continue
      }
      await cell('MARGIN-UNKNOWN', 'comment').scrollIntoViewIfNeeded()
      expect(await cell('MARGIN-UNKNOWN', 'comment').isVisible()).toBe(true)
      const commissionHeader = page.locator('#mainTable thead [data-column-id="commissionPct"]')
      await commissionHeader.focus()
      await page.keyboard.press('Enter')
      expect(await commissionHeader.getAttribute('aria-sort')).not.toBe('none')
      await page.screenshot({ path: path.join(outputDir, `products-${width}.png`) })
    }
    await page.setViewportSize({ width: 1440, height: 1000 })
    await cell('MARGIN-UNKNOWN', 'comment').getByRole('button').click()
    expect(await page.locator('#reportCommentDrawer').innerText()).toContain('только в этой вкладке')
    expect(unexpected).toEqual([])
    expect(errors).toEqual([])
  } finally {
    await browser.close()
  }
}, 60000)
