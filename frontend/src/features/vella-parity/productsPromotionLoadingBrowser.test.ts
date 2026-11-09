import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { chromium } from 'playwright'
import { expect, it } from 'vitest'

it('shows cached products first, completes primary sources, then loads membership without blocking the table', async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({ root, configFile: false, envFile: false, publicDir: false, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""', 'import.meta.env.VITE_WB_LIVE_ENABLED': '"false"' },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: { entry: fileURLToPath(new URL('./__fixtures__/productsCatalogCountBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'PromotionLoadingTest' } },
  })
  const output = (Array.isArray(result) ? result : [result]).flatMap(item => 'output' in item ? item.output : [])
  const entry = output.find(item => item.type === 'chunk' && item.isEntry)
  if (!entry || entry.type !== 'chunk') throw new Error('Missing browser fixture')
  const browser = await chromium.launch({ headless: true })
  let finishPromotions!: () => void
  const promotionsReady = new Promise<void>(resolve => { finishPromotions = resolve })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1600, height: 1000 } })
    const events: string[] = [], errors: string[] = []
    let primaryDone = false, promoted = false, membershipRequests = 0
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', async route => {
      const req = route.request(), url = new URL(req.url())
      if (req.resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (req.resourceType() === 'image') return route.fulfill({ status: 204 })
      if (url.hostname === 'fonts.googleapis.com') return route.fulfill({ body: '' })
      if (url.pathname === '/api/v1/wb-repricer/sku') {
        return route.fulfill({ json: { items: [{
          meta: { articleId: 'PARTICIPANT', nmId: 101, name: 'Synthetic product', status: 'manual', currentPriceKopecks: 150000, basketsLast7d: 1, basketNorm: 1 },
          settings: { cogsKopecks: 50000, logisticsKopecks: 5000, minMarginPct: 15, wbCommissionPct: 15 },
          analytics: { promotionStatus: promoted ? 'yes' : 'unknown', promotionName: promoted ? 'Confirmed promotion' : null },
        }], total: 1, page: 1, pageSize: 150, summary: { skuCount: 1 }, cache: {
          totalCached: 1, pagesCached: 1, periodStatsFetchedAt: 'now', basketsFetchedAt: 'now', financeFetchedAt: 'now',
          periodCoverage: { finance: { state: 'ok' } }, financeFreshnessState: 'fresh',
          adsFetchedAt: primaryDone ? 'now' : null, adsCoverageState: primaryDone ? 'ok' : 'no_data',
          adsDateFrom: url.searchParams.get('dateFrom'), adsDateTo: url.searchParams.get('dateTo'),
          promotionsRefreshRequired: !promoted, promotionsRefreshAt: new Date(Date.now() + 3600000).toISOString(),
        } } })
      }
      if (url.pathname === '/api/v1/wb-repricer/sync/run') {
        expect(req.postDataJSON().sources).toEqual(['ads'])
        events.push('primary-started')
        return route.fulfill({ json: { runId: 'primary', running: true } })
      }
      if (url.pathname === '/api/v1/wb-repricer/sync/status') {
        if (primaryDone) events.push('primary-completed')
        return route.fulfill({ json: { runId: 'primary', running: !primaryDone, state: primaryDone ? 'completed' : 'running' } })
      }
      if (url.pathname === '/api/v1/wb-repricer/sku/refresh-promotions') {
        membershipRequests++
        events.push('promotions-started')
        expect(primaryDone).toBe(true)
        expect(url.searchParams.get('onlyIfStale')).toBe('true')
        await promotionsReady
        promoted = true
        return route.fulfill({ json: { source: 'promotions', count: 1 } })
      }
      return route.fulfill({ json: { items: [], data: [], total: 0, state: 'completed', running: false, steps: [] } })
    })
    await page.goto('https://satorna.test/wb/repricer')
    await page.addScriptTag({ content: entry.code })
    const cell = page.locator('#tbody tr[data-sku="PARTICIPANT"] .promo-state')
    await cell.waitFor({ timeout: 15000 })
    expect(await cell.innerText()).toBe('Нет данных')
    expect(membershipRequests).toBe(0)
    await expect.poll(() => events.includes('primary-started')).toBe(true)
    primaryDone = true
    await expect.poll(() => membershipRequests, { timeout: 12000 }).toBe(1)
    expect(await cell.innerText()).toBe('Нет данных') // Table remains available during the slow optional source.
    expect(events.indexOf('primary-completed')).toBeLessThan(events.indexOf('promotions-started'))
    finishPromotions()
    await expect.poll(() => cell.innerText()).toBe('Confirmed promotion')
    expect(membershipRequests).toBe(1)
    expect(errors).toEqual([])
  } finally {
    finishPromotions()
    await browser.close()
  }
}, 30000)
