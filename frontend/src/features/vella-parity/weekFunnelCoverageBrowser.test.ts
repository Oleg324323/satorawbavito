import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { expect, it } from 'vitest'

it('keeps saved complete funnel rows during refresh, scopes totals, and isolates date responses', async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({ configFile: false, envFile: false, root, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""' },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: { entry: fileURLToPath(new URL('./__fixtures__/reportLoadingBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'WeekCoverage' } },
  })
  const chunk = (Array.isArray(result) ? result : [result]).flatMap(r => 'output' in r ? r.output : []).find(c => c.type === 'chunk' && c.isEntry)
  if (!chunk || chunk.type !== 'chunk') throw Error('Missing fixture bundle')
  const browser = await chromium.launch({ headless: true, channel: 'chrome' })
  try {
    const context = await browser.newContext({ viewport: { width: 1512, height: 982 }, serviceWorkers: 'block' })
    const page = await context.newPage()
    await page.clock.setFixedTime(new Date('2026-10-24T12:00:00Z'))
    await page.addInitScript(() => {
      if (!crypto.randomUUID) Object.defineProperty(crypto, 'randomUUID', { value: () => Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join('') })
    })
    let completed = false, jobs = 0, lateRequested = false, releaseLate = () => {}
    const late = new Promise<void>(resolve => { releaseLate = resolve })
    const errors: string[] = [], unexpected: string[] = [], queries: string[] = []
    page.on('pageerror', e => errors.push(e.message))
    await page.route('**/*', async route => {
      const req = route.request(), url = new URL(req.url())
      if (req.resourceType() === 'image') return route.fulfill({ contentType: 'image/gif', body: Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64') })
      if (url.hostname === 'fonts.googleapis.com') return route.fulfill({ contentType: 'text/css', body: '' })
      if (url.pathname === '/wb/reports/week-over-week') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (url.pathname === '/api/wb/reports/work-demand/release') return route.fulfill({ json: { released: true } })
      if (url.pathname === '/api/v1/wb-repricer/cache/coverage') return route.fulfill({ json: { data: { days: [] } } })
      if (url.pathname === '/api/wb/reports/week-over-week/jobs') {
        if (req.method() === 'POST') jobs++
        return route.fulfill({ json: { state: completed ? 'completed' : 'running', stage: completed ? 'completed' : 'refreshing_sources', percent: completed ? 100 : 20 } })
      }
      if (url.pathname !== '/api/wb/reports/week-over-week/latest-cache') {
        unexpected.push(`${req.method()} ${url.pathname}`); return route.abort()
      }
      queries.push(url.search)
      const to = url.searchParams.get('to')!
      if (to === '2026-10-16') { lateRequested = true; await late }
      const day = to === '2026-10-12', custom = to === '2026-10-15', incomplete = to === '2026-10-17'
      const values = day ? [[8,48,1440000],[1,6,164000],[1,1,164000]] : custom ? [[15,90,2700000],[3,10,492000],[0,1,0]] : [[52,297,9360000],[10,45,1640000],[1,3,164000]]
      const rows = [684820752,462483115,988140099].map((nmId,i) => ({ nmId, sku: `COVERAGE-${nmId}`, productName: `Observed product ${nmId}`, photoUrl: null,
        funnelPopulation: { selected: true, previous: true },
        orders: { units: incomplete && i === 2 ? null : values[i][0], previousUnits: [64,10,4][i], kopecks: incomplete && i === 2 ? null : values[i][2], deltaPct: incomplete && i === 2 ? null : (values[i][0]-[64,10,4][i])/[64,10,4][i]*100 },
        baskets: { units: values[i][1] }, sales: { units: 1, kopecks: 100 },
      }))
      const all = [...rows,
        { nmId: null, isUnallocated: true, productName: 'Account-level financial charges', orders: { units: null, kopecks: null } },
        { nmId: 999, productName: 'Finance-only product', funnelPopulation: { selected: false, previous: false }, orders: { units: null, kopecks: null } },
        ...Array.from({ length: 55 }, (_,i) => ({ nmId:1000+i, productName:`Additional product ${i}`, funnelPopulation:{selected:true,previous:true}, orders:{units:i===54?7:0,previousUnits:0,kopecks:i===54?7000:0,deltaPct:null}, baskets:{units:0} })),
      ]
      return route.fulfill({ json: { rows: all, filters: { dateRange: { from: url.searchParams.get('from'), to } },
        cache: { fresh: completed || to !== '2026-10-18' }, meta: { freshnessState: completed ? 'fresh' : 'partial' },
        metricCoverage: { selected: { funnelPopulationComplete: true }, previous: { funnelPopulationComplete: true } },
      } })
    })
    const mount = async () => {
      await page.goto('http://satorna.test/wb/reports/week-over-week')
      await page.evaluate(() => { window.__vellaReportPeriods = { week: { days: 7, mode: 'custom', fromIso: '2026-10-12', toIso: '2026-10-18', label: 'Test week' } } })
      await page.addScriptTag({ content: chunk.code })
    }
    await mount()
    const surface = page.locator('#tab-week'), table = surface.locator('tr[data-report-row="week"]')
    const product = table.filter({ hasText: 'Observed product 684820752' })
    await product.waitFor({ timeout: 15000 })
    expect(await product.locator('td').nth(1).innerText()).toContain('52 шт')
    expect(await product.locator('td').nth(3).innerText()).toContain('297')
    expect(await table.count()).toBeLessThan(60)
    const card = surface.locator('.week-workbench .stat').first()
    // The last nonzero product is beyond the initial render window.
    expect((await card.innerText()).replace(/\s/g,' ')).toContain('70 шт · 111 710 ₽')
    const search = surface.getByPlaceholder('Артикул, товар или сегмент...')
    await search.fill('684820752')
    await expect.poll(() => table.filter({ visible: true }).count()).toBe(1)
    expect((await card.innerText()).replace(/\s/g,' ')).toContain('70 шт · 111 710 ₽')
    await search.fill('')
    await expect.poll(() => jobs).toBe(1)
    await page.waitForTimeout(250)
    expect(await product.locator('td').nth(1).innerText()).toContain('52 шт')
    completed = true
    await expect.poll(() => queries.length, { timeout:15000 }).toBeGreaterThan(1)
    expect(await product.locator('td').nth(1).innerText()).toContain('52 шт')
    await mount()
    await product.waitFor()
    expect(jobs).toBe(1) // Reopening a fresh exact range does not redownload it.
    const choose = async (to: string) => {
      await page.locator('#globalPeriodRangeTo').fill(to)
      await page.locator('#globalPeriodRangeTo').press('Enter')
    }
    await choose('2026-10-12')
    await expect.poll(() => product.locator('td').nth(1).innerText()).toContain('8 шт')
    expect((await card.innerText()).replace(/\s/g,' ')).toContain('17 шт · 17 750 ₽')
    await choose('2026-10-17')
    await surface.getByText('Сравнение неполное: 2 из 3', { exact:true }).waitFor()
    expect(await card.locator('.stat-val').innerText()).toBe('—')
    await choose('2026-10-16')
    await expect.poll(() => lateRequested).toBe(true)
    await choose('2026-10-15')
    await expect.poll(() => product.locator('td').nth(1).innerText()).toContain('15 шт')
    releaseLate()
    await page.waitForTimeout(150)
    expect(await surface.locator('.week-periods').innerText()).toContain('2026-10-12 — 2026-10-15')
    expect(await product.locator('td').nth(1).innerText()).toContain('15 шт')
    expect(errors).toEqual([])
    expect(unexpected).toEqual([])
    expect(jobs).toBe(1)
  } finally { await browser.close() }
}, 60000)
