import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { readFile } from 'node:fs/promises'
import react from '@vitejs/plugin-react'
import { chromium } from 'playwright'
import { build } from 'vite'
import { expect, it } from 'vitest'

it('refreshes buyer prices from saved data without changing period profit or submitting prices', async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const output = await build({ root, configFile: false, envFile: false, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""', 'import.meta.env.VITE_WB_LIVE_ENABLED': '"false"' },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: { entry: fileURLToPath(new URL('./__fixtures__/productsCatalogCountBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'WbPricePropagation' } } })
  const outputs = (Array.isArray(output) ? output : [output]).flatMap(item => 'output' in item ? item.output : [])
  const bundle = outputs.find(item => item.type === 'chunk' && item.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing browser fixture')
  const css = outputs.filter(item => item.type === 'asset' && item.fileName.endsWith('.css')).map(item => item.type === 'asset' ? String(item.source) : '').join('\n')
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL })
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, serviceWorkers: 'block' })
    const errors: string[] = [], mutations: string[] = []
    let buyer: number | null = 142600, reads = 0
    page.on('pageerror', error => errors.push(error.message))
    const fact = { financeState: 'ok', salesUnits: 2, returnsUnits: 0, revenueKopecks: 100000,
      settlementFormulaVersion: 'wb-final-payout-cogs-tax-v1', settlementProfitKopecks: 10000,
      settlementBlockers: [], basePriceKopecks: 430000 }
    const payload = (from: string, to: string) => ({ total: 1, page: 1, pageSize: 150, dateFrom: from, dateTo: to,
      items: [{ meta: { articleId: 'PRICE-SKU', nmId: 990001, name: 'Synthetic price product', subject: 'Лонгсливы', currentPriceKopecks: 206400, status: 'manual', basketsLast7d: 0, basketNorm: 1 },
        settings: { cogsKopecks: 80000 }, analytics: { ...fact, buyerPriceNoWalletKopecks: buyer, buyerPriceObservedAt: new Date().toISOString(), sppPct: buyer == null ? null : (206400-buyer)/206400*100 } }],
      cache: { totalCached: 1, pagesCached: 1, financeFreshnessState: 'fresh', periodStatsFreshnessState: 'fresh', basketsFreshnessState: 'fresh',
        adsDateFrom: from, adsDateTo: to, adsFreshnessState: 'fresh', adsFetchedAt: '2026-10-21T10:00:00Z',
        financeFetchedAt: '2026-10-21T10:00:00Z', periodStatsFetchedAt: '2026-10-21T10:00:00Z', basketsFetchedAt: '2026-10-21T10:00:00Z',
        requiredPeriodSources: ['finance'], periodCoverage: { finance: { state: 'ok', coveredDays: 7, requestedDays: 7 } } },
      summary: { skuCount: 1, revenueKopecks: 100000, buyoutUnits: 2, buyoutAmountKopecks: 100000,
        settlementFormulaVersion: fact.settlementFormulaVersion, settlementProfitKopecks: 10000, settlementCogsKopecks: 160000, settlementBlockers: [] } })
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      if (request.resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (request.resourceType() === 'image') return route.fulfill({ status: 204 })
      if (url.hostname === 'fonts.googleapis.com') return route.fulfill({ contentType: 'text/css', body: '' })
      if (url.pathname === '/api/v1/wb-repricer/sku') {
        reads++
        return route.fulfill({ json: payload(url.searchParams.get('dateFrom')!, url.searchParams.get('dateTo')!) })
      }
      if (url.pathname.endsWith('/jobs')) return route.fulfill({ json: { state: 'completed' } })
      if (url.pathname.endsWith('/sync/status')) return route.fulfill({ json: { running: false, state: 'completed', steps: [] } })
      if (request.method() !== 'GET') mutations.push(`${request.method()} ${url.pathname}`)
      return route.fulfill({ json: { items: [], total: 0, tasks: [], available: true } })
    })
    await page.goto('http://satorna.test/wb/repricer')
    await page.clock.install()
    await page.addStyleTag({ content: css }); await page.addScriptTag({ content: bundle.code })
    const row = page.locator('#tbody tr[data-sku="PRICE-SKU"]')
    await row.waitFor()
    await expect.poll(async () => (await row.locator('.price').innerText()).replace(/\s/g,'')).toContain('1426')
    const margin = await row.locator('[data-column-id="mg"]').innerText()
    expect(margin).toContain('10%')
    buyer = 144400
    const before = reads
    await page.clock.fastForward(61_000)
    await expect.poll(() => reads).toBeGreaterThan(before)
    await expect.poll(async () => (await row.locator('.price').innerText()).replace(/\s/g,'')).toContain('1444')
    expect(await row.locator('[data-column-id="mg"]').innerText()).toBe(margin)
    buyer = null
    await page.locator('#searchTable').focus()
    const editingReads = reads
    await page.clock.fastForward(61_000)
    expect(reads).toBe(editingReads)
    await page.locator('#mainTable thead').click()
    await page.clock.fastForward(61_000)
    await expect.poll(() => row.locator('.price').innerText()).toContain('нет данных')
    expect(await row.locator('.price-hint').count()).toBe(0)
    expect(await row.locator('[data-column-id="mg"]').innerText()).toBe(margin)
    expect(mutations).toEqual([])
    expect(errors).toEqual([])

    if (process.env.SATORNA_SPP_AUDIT_FIXTURE) {
      const source = JSON.parse(await readFile(process.env.SATORNA_SPP_AUDIT_FIXTURE, 'utf8'))
      const current = source.periods[0]
      await page.route('**/api/v1/wb-repricer/sku?**', route => route.fulfill({ json: { ...current, items: current.items.slice(0,150), page:1, pageSize:150 } }))
      await page.reload(); await page.addStyleTag({ content: css }); await page.addScriptTag({ content: bundle.code })
      await page.locator('#tbody tr[data-sku]').first().waitFor()
      for (const item of current.items.slice(0,10)) {
        const product = page.locator('#tbody tr').filter({ has: page.locator(`a[href="https://www.wildberries.ru/catalog/${item.meta.nmId}/detail.aspx"]`) }).first()
        if (!await product.count()) continue // Existing type filters remain unchanged.
        const quote = item.analytics.buyerPriceNoWalletKopecks
        const displayed = (await product.locator('.price').innerText()).replace(/\s/g,'')
        expect(displayed).toContain(quote == null ? 'нетданных' : String(Math.round(quote/100)))
        if (item.analytics.settlementAttributedProfitKopecks != null && item.analytics.settlementProfitKopecks == null)
          expect(await product.locator('[data-column-id="mg"]').innerText()).toContain('до общих расходов')
      }
    }
  } finally { await browser.close() }
}, 90_000)
