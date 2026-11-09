import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { expect, it } from 'vitest'

it('filters statistics and compares each visible SKU with the adjacent equal-length period', async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({
    configFile: false, envFile: false, root, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""' },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: {
      entry: fileURLToPath(new URL('./__fixtures__/repricerStatsRaceBrowser.ts', import.meta.url)), formats: ['iife'], name: 'StatsFilterTest',
    } },
  })
  const bundle = (Array.isArray(result) ? result : [result]).flatMap(output => 'output' in output ? output.output : [])
    .find(output => output.type === 'chunk' && output.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing statistics bundle')
  const browser = await chromium.launch({ headless: true })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block' })
    const requests: string[] = [], errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', async route => {
      const url = new URL(route.request().url())
      if (url.pathname === '/wb/repricer/stats') return route.fulfill({ contentType: 'text/html', body: `
        <button id="reload">Reload</button><button id="logout">Logout</button>
        <section id="tab-repricer-stats"><div class="search"><input></div>
        <button id="repricerStatsFiltersTrigger" aria-expanded="false">Фильтры <span class="products-filter-trigger-count">0</span></button>
        <span id="repricerStatsFilterLabel"></span><div id="repricerStatsFilters" hidden></div>
        <button id="repricerStatsReset">Сбросить</button>
        <div class="stats">${Array.from({ length: 4 }, () => '<div class="stat"><span class="stat-val"></span><span class="stat-delta"></span></div>').join('')}</div>
        <div data-filter-summary><span></span></div><table><thead><tr><th data-stats-column="impressions" data-stats-trend="impressions">Показы рекламы</th></tr></thead><tbody id="repricerStatsBody"></tbody></table></section>` })
      if (url.pathname === '/favicon.ico') return route.fulfill({ status: 204 })
      if (url.pathname !== '/api/v1/wb-repricer/stats') return route.abort()
      requests.push(url.search)
      const prior = url.searchParams.get('dateFrom') === '2026-07-25'
      const items = [
        { articleId: 'A', brand: 'Alpha', managerId: 'm1', managerName: 'One', metrics: { impressions: prior ? 50 : 100 }, sources: {}, priceProtection: {}, decision: {} },
        { articleId: 'B', brand: 'Beta', managerId: 'm2', managerName: 'Two', metrics: { impressions: prior ? 20 : 10 }, sources: {}, priceProtection: {}, decision: {} },
        { articleId: 'C', brand: 'Gamma', metrics: { impressions: prior ? 100 : null }, sources: {}, priceProtection: {}, decision: {} },
      ]
      return route.fulfill({ json: { items, total: 3, summary: { baskets: 0, orders: 0 },
        dateFrom: prior ? '2026-07-25' : '2026-08-01', dateTo: prior ? '2026-07-31' : '2026-08-07' } })
    })
    await page.goto('http://satorna.test/wb/repricer/stats')
    await page.evaluate(() => { window.__vellaReportPeriods = { 'repricer-stats': { days: 7, mode: 'custom', fromIso: '2026-08-01', toIso: '2026-08-07', label: '7 дней' } } })
    await page.addScriptTag({ content: bundle.code })
    await page.waitForFunction(() => document.body.dataset.firstSettled === 'true')
    expect(new URLSearchParams(requests[1]).get('dateFrom')).toBe('2026-07-25')
    expect(new URLSearchParams(requests[1]).get('dateTo')).toBe('2026-07-31')
    expect(await page.locator('.stats-trend').innerText()).toBe('↑')
    await page.locator('#repricerStatsFiltersTrigger').click()
    await page.locator('[data-stats-key="brand"][data-stats-value="Beta"]').first().click()
    expect(await page.locator('.stats-trend').innerText()).toBe('↓')
    expect(await page.locator('#repricerStatsBody [data-report-row]:visible').count()).toBe(1)
    await page.locator('[data-stats-column-toggle="impressions"]').uncheck()
    expect(await page.locator('th[data-stats-column="impressions"]').isVisible()).toBe(false)
    await page.locator('#repricerStatsReset').click()
    expect(await page.locator('#repricerStatsBody [data-report-row]:visible').count()).toBe(3)
    expect(await page.locator('.stats-trend').innerText()).toBe('↑')
    expect(errors).toEqual([])
  } finally { await browser.close() }
}, 30_000)
