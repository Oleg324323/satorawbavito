import path from 'node:path'
import { fileURLToPath } from 'node:url'
import react from '@vitejs/plugin-react'
import { chromium, type Browser } from 'playwright'
import { build } from 'vite'
import { afterAll, beforeAll, expect, it } from 'vitest'

let browser: Browser, code: string
beforeAll(async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const built = await build({ root, configFile: false, envFile: false, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""' },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: {
      entry: fileURLToPath(new URL('./__fixtures__/repricerStatsPageBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'StatsPriceRefresh',
    } },
  })
  const bundle = (Array.isArray(built) ? built : [built]).flatMap(item => 'output' in item ? item.output : [])
    .find(item => item.type === 'chunk' && item.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing statistics fixture bundle')
  code = bundle.code
  browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL })
}, 60_000)
afterAll(async () => { await browser?.close() })

it.each(['refresh', 'period', 'account', 'filter'] as const)('isolates saved-price refresh through the actual statistics component: %s', async scenario => {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, serviceWorkers: 'block' })
  let price = 100000, hold = false, fail = false, release = () => {}, heldReads = 0
  const reads: string[] = [], mutations: string[] = [], errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  try {
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      if (request.resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (request.resourceType() === 'image') return route.fulfill({ status: 204 })
      if (url.hostname === 'fonts.googleapis.com') return route.fulfill({ contentType: 'text/css', body: '' })
      if (request.method() !== 'GET') mutations.push(`${request.method()} ${url.pathname}`)
      if (url.pathname.endsWith('/jobs')) return route.fulfill({ json: { state: 'completed' } })
      if (url.pathname !== '/api/v1/wb-repricer/stats') return route.fulfill({ json: { items: [], total: 0 } })
      const from = url.searchParams.get('dateFrom')!, to = url.searchParams.get('dateTo')!
      const changed = from.startsWith('2026-08-') || request.headers().authorization?.includes('other-account')
      reads.push(url.search)
      const observed = price
      if (hold && !changed) { heldReads++; await new Promise<void>(resolve => { release = resolve }) }
      if (fail) return route.fulfill({ status: 503, json: { error: { code: 'SYNTHETIC_READ_FAILED', message: 'Synthetic saved-price error' } } })
      return route.fulfill({ json: { items: [{ articleId: changed ? 'NEW-SCOPE-SKU' : 'PRICE-SKU', nmId: 101, name: 'Synthetic price product',
        metrics: { currentPriceKopecks: observed, marginPct: observed === 100000 ? 20 : 30, revenueKopecks: 900000, netProfitKopecks: 128000 },
        decision: { label: `Цена ${observed}`, reasons: [] }, sources: {}, flags: [], priceProtection: {} }],
        total: 1, itemsReturned: 1, page: 1, pageSize: 500, dateFrom: from, dateTo: to,
        summary: { skuCount: 1, sourceReady: 1, sourcePartial: 0, sourceBlocked: 0 },
        cache: { financeFetchedAt: '2026-10-11T12:00:00Z', periodStatsFetchedAt: '2026-10-11T12:00:00Z',
          basketsFetchedAt: '2026-10-11T12:00:00Z', adsFetchedAt: '2026-10-11T12:00:00Z',
          financeFreshnessState: 'fresh', periodStatsFreshnessState: 'fresh', basketsFreshnessState: 'fresh', adsFreshnessState: 'fresh',
          adsDateFrom: from, adsDateTo: to },
      } })
    })
    await page.goto('http://satorna.test/wb/repricer/stats'); await page.clock.install()
    await page.addScriptTag({ content: code })
    await page.getByText('Цена 100000', { exact: true }).waitFor()
    // Initial source preparation is the normal route load, not a price refresh.
    await page.waitForTimeout(150)
    const initialMutations = mutations.length
    price = 120000
    if (scenario === 'refresh') {
      await page.clock.fastForward(61_000)
      await expect.poll(() => page.getByText('Цена 120000', { exact: true }).count()).toBe(1)
      const before = reads.length
      fail = true
      await page.clock.fastForward(61_000)
      await expect.poll(() => reads.length).toBeGreaterThan(before)
      await page.getByRole('alert').waitFor()
      expect(await page.getByText('Цена 120000', { exact: true }).count()).toBe(1)
      fail = false; price = 130000
      // A verified confirmation bypasses the interval, but performs only a GET.
      await page.evaluate(() => window.dispatchEvent(new Event('vella:wb-price-confirmed')))
      await expect.poll(() => page.getByText('Цена 130000', { exact: true }).count()).toBe(1)
      expect(await page.getByRole('alert').count()).toBe(0)
      expect(mutations.length).toBe(initialMutations)
    } else {
      hold = true
      await page.clock.fastForward(61_000)
      await expect.poll(() => heldReads).toBe(1)
      if (scenario === 'filter') {
        await page.locator('#tab-repricer-stats .search input').fill('NEW-SCOPE')
        await page.clock.fastForward(150)
      } else {
        await page.getByRole('button', { name: scenario === 'period' ? 'Change synthetic period' : 'Switch synthetic account', exact: true }).click()
        await page.getByText('NEW-SCOPE-SKU', { exact: true }).waitFor({ timeout: 5000 }).catch(async error => {
          throw new Error(`${String(error)}; reads=${JSON.stringify(reads)}; body=${await page.locator('#repricerStatsBody').innerText()}`)
        })
      }
      hold = false; release(); await page.waitForTimeout(100)
      expect(await page.getByText('PRICE-SKU', { exact: true }).count()).toBe(0)
      if (scenario !== 'filter') expect(await page.getByText('NEW-SCOPE-SKU', { exact: true }).count()).toBe(1)
    }
    expect(errors).toEqual([])
  } finally { release(); await page.close() }
}, 30_000)
