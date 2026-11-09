import path from 'node:path'
import { fileURLToPath } from 'node:url'
import react from '@vitejs/plugin-react'
import { chromium } from 'playwright'
import { build } from 'vite'
import { expect, it } from 'vitest'

it('updates only current ABC prices from saved data and retains historical profit on refresh or failure', async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const built = await build({ root, configFile: false, envFile: false, logLevel: 'silent', plugins: [
    { name: 'abc-synthetic-identity', enforce: 'pre', transform(source, id) {
      if (!id.endsWith('/__fixtures__/reportLoadingBrowser.tsx')) return
      return source.replace('cabinetMe: null', "cabinetMe: ({ organization: { organizationId: 9 }, user: { userId: 1, permissions: [] } } as AuthContextValue['cabinetMe'])")
    } }, react(),
  ], define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""' },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: {
      entry: fileURLToPath(new URL('./__fixtures__/reportLoadingBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'AbcPriceRefresh',
    } },
  })
  const bundle = (Array.isArray(built) ? built : [built]).flatMap(item => 'output' in item ? item.output : [])
    .find(item => item.type === 'chunk' && item.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing ABC fixture')
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL })
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, serviceWorkers: 'block' })
    let price = 100000, fail = false, reads = 0, calculationRevision = 'first', financialProfit = 128000
    const mutations: string[] = [], errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      if (request.resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (request.resourceType() === 'image') return route.fulfill({ status: 204 })
      if (url.hostname === 'fonts.googleapis.com') return route.fulfill({ contentType: 'text/css', body: '' })
      if (request.method() !== 'GET') mutations.push(`${request.method()} ${url.pathname}`)
      if (url.pathname === '/api/wb/reports/abc/latest-cache') {
        reads++
        if (fail) return route.fulfill({ status: 503, json: { error: { message: 'Synthetic ABC price read failure' } } })
        return route.fulfill({ json: { meta: { id: 'abc' }, filters: { groupBy: 'sku', dateRange: { from: url.searchParams.get('from'), to: url.searchParams.get('to') } },
          rows: [{ sku: 'ABC-PRICE', nmId: 101, name: 'Synthetic ABC product', priceBeforeSppKopecks: price,
            priceWithSppKopecks: null, salesComposite: { units: 10, kopecks: 900000 },
            revenueKopecks: 900000, netTotalKopecks: financialProfit, marginKopecks: 128000, marginPct: 14.2 }], cache: { fresh: true, calculationRevision } } })
      }
      return route.fulfill({ json: { data: [], items: [], rows: [], total: 0 } })
    })
    await page.goto('http://localhost/wb/reports/abc'); await page.clock.install()
    await page.addScriptTag({ content: bundle.code })
    await page.getByText('ABC-PRICE', { exact: true }).first().waitFor({ timeout: 5000 }).catch(async error => {
      throw new Error(`${String(error)}; reads=${reads}; errors=${JSON.stringify(errors)}; body=${(await page.locator('body').innerText()).slice(-3000)}`)
    })
    const row = () => page.evaluate(() => window.__vellaAbcLiveReport?.rows?.[0])
    await page.clock.fastForward(5000)
    await page.waitForTimeout(100)
    expect((await row())?.netTotalKopecks).toBe(128000)
    const initialMutations = mutations.length
    price = 120000; financialProfit = 999999
    await page.clock.fastForward(61_000)
    await expect.poll(async () => (await row())?.priceBeforeSppKopecks).toBe(120000)
    expect((await row())?.netTotalKopecks).toBe(128000)
    expect((await row())?.salesComposite?.kopecks).toBe(900000)
    calculationRevision = 'second'; financialProfit = 150000
    await page.evaluate(() => window.dispatchEvent(new Event('satorna:costs-saved')))
    await expect.poll(async () => (await row())?.netTotalKopecks).toBe(150000)
    expect((await row())?.salesComposite?.kopecks).toBe(900000)
    fail = true
    await page.clock.fastForward(61_000)
    await page.getByText('Не удалось обновить текущие цены:', { exact: false }).waitFor({ timeout: 5000 })
    expect((await row())?.priceBeforeSppKopecks).toBe(120000)
    expect((await row())?.netTotalKopecks).toBe(150000)
    expect(mutations.length).toBe(initialMutations)
    expect(errors).toEqual([])
  } finally { await browser.close() }
}, 60_000)
