import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { expect, it } from 'vitest'

// Existing WeekBackendRow fields; statuses come from the response, not a derived business rule.
type WeekSearchRow = { sku: string; productName: string; productStatus: string | null; photoUrl: null; nmId: null }
const rows: WeekSearchRow[] = [
  { sku: 'SYNTHETIC-WEEK-A', productName: 'Synthetic Alpha garment', productStatus: 'новинка', photoUrl: null, nmId: null },
  { sku: 'SYNTHETIC-WEEK-B', productName: 'Synthetic Beta garment', productStatus: 'средний', photoUrl: null, nmId: null },
  { sku: 'SYNTHETIC-WEEK-C', productName: 'Synthetic Gamma garment', productStatus: null, photoUrl: null, nmId: null },
  ...Array.from({ length: 48 }, (_, index) => ({
    sku: `SYNTHETIC-WEEK-${index + 4}`, productName: `Synthetic product ${index + 4}`,
    productStatus: null, photoUrl: null, nmId: null,
  })),
]

it('searches Week by SKU, product name and the supplied product segment without inventing unknown segments', async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({
    configFile: false, envFile: false, root, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': JSON.stringify('test'), 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('') },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: {
      entry: fileURLToPath(new URL('./__fixtures__/reportLoadingBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'WeekSearchTest',
    } },
  })
  const outputs = Array.isArray(result) ? result : [result]
  const chunk = outputs.flatMap(output => 'output' in output ? output.output : []).find(output => output.type === 'chunk' && output.isEntry)
  if (!chunk || chunk.type !== 'chunk') throw new Error('Missing Week search browser fixture bundle')
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1512, height: 982 } })
    await page.addInitScript(() => {
      if (!crypto.randomUUID) Object.defineProperty(crypto, 'randomUUID', {
        value: () => Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join(''),
      })
    })
    await page.clock.setFixedTime(new Date('2026-10-24T12:00:00Z'))
    const queries: string[] = [], unexpected: string[] = [], errors: string[] = []
    const exports: Array<{ headers: string[]; rows: string[][] }> = []
    let partialComplete = false
    let missingJobs = 0
    let releaseLate = () => {}
    const lateGate = new Promise<void>(resolve => { releaseLate = resolve })
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      if (request.method() === 'GET' && url.origin === 'http://satorna.test' && url.pathname === '/wb/reports/week-over-week') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (request.method() === 'GET' && request.resourceType() === 'image') return route.fulfill({ contentType: 'image/gif', body: Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64') })
      if (request.method() === 'GET' && url.origin === 'https://fonts.googleapis.com' && url.pathname === '/css2') return route.fulfill({ contentType: 'text/css', body: '' })
      if (request.method() === 'GET' && url.pathname === '/api/v1/wb-repricer/cache/coverage') return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ data: { days: [] } }) })
      if (request.method() === 'POST' && url.pathname === '/api/wb/reports/week-over-week/jobs') {
        missingJobs += 1; partialComplete = true
        return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ state: 'completed', stage: 'completed', percent: 100 }) })
      }
      if (request.method() === 'POST' && url.pathname === '/api/wb/reports/week-over-week/table.xlsx') {
        exports.push(request.postDataJSON())
        return route.fulfill({ contentType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', body: Buffer.from('synthetic-xlsx') })
      }
      if (request.method() !== 'GET' || url.origin !== 'http://satorna.test' || url.pathname !== '/api/wb/reports/week-over-week/latest-cache') {
        unexpected.push(`${request.method()} ${url.pathname}`); return route.abort()
      }
      queries.push(url.search)
      if (url.searchParams.get('to') === '2026-09-17') await lateGate
      const partial = url.searchParams.get('to') === '2026-09-19' && !partialComplete
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({
        meta: { freshnessState: partial ? 'partial' : 'fresh', sourceType: 'operational' }, cache: { fresh: !partial },
        warning: partial ? 'Сравнение неполное: отсутствует предыдущий источник' : null,
        headline: 'Период 2026-08-01 — 2026-08-07; сравнение 2026-07-25 — 2026-07-31.',
        filters: { dateRange: { from: url.searchParams.get('from'), to: url.searchParams.get('to') } },
        rows: rows.map((row, index) => ({ ...row, sales: partial ? null : { units: index + 1, kopecks: (index + 1) * 100, deltaPct: null }, orders: { units: index + 1, kopecks: (index + 1) * 100, deltaPct: 10 }, price: { kopecks: 100 }, baskets: { units: index + 1, deltaPct: -10 } })), reportJob: null,
      }) })
    })
    await page.goto('http://satorna.test/wb/reports/week-over-week')
    await page.evaluate(() => {
      window.__vellaReportPeriods = { week: { days: 7, mode: 'custom', fromIso: '2026-08-01', toIso: '2026-08-07', label: 'Synthetic Week period' } }
    })
    await page.addScriptTag({ content: chunk.code })
    const surface = page.locator('#tab-week')
    const dataRows = surface.locator('tr[data-report-row="week"]')
    await surface.getByText('Synthetic Alpha garment', { exact: true }).waitFor({ timeout: 15_000 })
    expect(await dataRows.count()).toBe(50)
    expect(await surface.getByText('Период 2026-08-01 — 2026-08-07; сравнение 2026-07-25 — 2026-07-31.', { exact: true }).count()).toBe(0)
    expect(await dataRows.first().getByText('↑ +10%', { exact: true }).evaluate(el => getComputedStyle(el).color)).toBe('rgb(5, 150, 105)')
    expect(await dataRows.first().getByText('↓ -10%', { exact: true }).evaluate(el => getComputedStyle(el).color)).toBe('rgb(220, 38, 38)')
    const search = surface.getByPlaceholder('Артикул, товар или сегмент...')
    for (const [query, expectedProduct] of [
      ['SYNTHETIC-WEEK-B', 'Synthetic Beta garment'],
      ['Alpha garment', 'Synthetic Alpha garment'],
      ['новинка', 'Synthetic Alpha garment'],
      ['средний', 'Synthetic Beta garment'],
      ['SYNTHETIC-WEEK-51', 'Synthetic product 51'],
    ]) {
      await search.fill(query)
      const visible = surface.locator('tr[data-report-row="week"]:visible')
      await expect.poll(() => visible.count(), { message: `Visible rows for ${query}` }).toBe(1)
      expect(await visible.innerText()).toContain(expectedProduct)
    }
    await search.fill('absent-synthetic-segment')
    expect(await surface.locator('tr[data-report-row="week"]:visible').count()).toBe(0)
    await search.fill('')
    expect(await surface.locator('tr[data-report-row="week"]:visible').count()).toBe(50)
    expect(queries).toHaveLength(1)
    const query = new URLSearchParams(queries[0])
    expect([query.get('from'), query.get('to'), query.get('groupBy'), query.get('source'), query.get('preset')])
      .toEqual(['2026-08-01', '2026-08-07', 'sku', 'operational', 'custom'])

    // Current WB date fields commit on Enter. The former test clicked a text
    // field and waited for a dialog from an obsolete calendar implementation.
    const choose = async (boundary: 'from' | 'to', iso: string) => {
      const field = page.locator(boundary === 'from' ? '#globalPeriodRange' : '#globalPeriodRangeTo')
      await field.fill(iso)
      await field.press('Enter')
    }
    await choose('from', '2026-09-09')
    await choose('to', '2026-09-16')
    await expect.poll(() => surface.locator('.week-periods').innerText()).toContain('2026-09-01 — 2026-09-08 · 8 дн.')
    await dataRows.first().waitFor()
    await choose('to', '2026-09-17')
    await expect.poll(() => queries.some(q => new URLSearchParams(q).get('to') === '2026-09-17')).toBe(true)
    expect(await dataRows.count()).toBe(0)
    await choose('to', '2026-09-18')
    await expect.poll(() => surface.locator('.week-periods').innerText()).toContain('2026-08-30 — 2026-09-08 · 10 дн.')
    await dataRows.first().waitFor()
    releaseLate()
    await expect.poll(() => surface.locator('.week-periods').innerText()).toContain('2026-09-09 — 2026-09-18 · 10 дн.')
    // Missing previous sales (including a zero comparison base) stays unavailable.
    expect(await surface.locator('.week-workbench .stat').nth(1).locator('.stat-val').innerText()).toBe('—')
    expect((await surface.locator('.week-workbench .stat').first().innerText()).replace(/\s/g, ' ')).toContain('1 326 шт')
    for (const viewport of [{ width: 1512, height: 982 }, { width: 1100, height: 700 }, { width: 900, height: 900 }]) {
      await page.setViewportSize(viewport)
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
      const geometry = await surface.evaluate(tab => {
        const workspace = tab.querySelector<HTMLElement>('.week-table-workspace')!
        workspace.scrollTop = 250; workspace.scrollLeft = 300
        tab.scrollTo({ top: 250, behavior: 'instant' })
        return { height: workspace.clientHeight, scrollTop: workspace.scrollTop, pageScroll: tab.scrollTop,
          scrollLeft: workspace.scrollLeft, pageHeight: tab.clientHeight, pageContent: tab.scrollHeight,
          active: tab.className, overflow: getComputedStyle(tab).overflowY }
      })
      expect(geometry.height).toBeGreaterThan(180)
      expect(geometry.scrollTop).toBe(0)
      expect(geometry.pageScroll, JSON.stringify({ viewport, geometry })).toBeGreaterThan(0)
      expect(geometry.scrollLeft).toBeGreaterThan(0)
      const lastColumn = await surface.evaluate(tab => {
        const wrap = tab.querySelector<HTMLElement>('.week-table-workspace')!
        wrap.scrollTo({ left: wrap.scrollWidth, behavior: 'instant' })
        return wrap.querySelector('thead th:last-child')!.getBoundingClientRect().right
      })
      expect(lastColumn).toBeLessThanOrEqual(viewport.width)
    }
    await page.setViewportSize({ width: 1512, height: 982 })
    await surface.locator('.week-table-workspace').evaluate(el => { el.scrollTop = 0; el.scrollLeft = 0 })
    if (process.env.SATORNA_WEEK_SCREENSHOT) await page.screenshot({ path: process.env.SATORNA_WEEK_SCREENSHOT })
    const salesHeader = surface.locator('thead th').nth(1)
    await salesHeader.click()
    expect(await dataRows.first().innerText()).toContain('Synthetic product 51')
    await surface.getByRole('button', { name: 'Экспорт XLSX', exact: true }).click()
    await expect.poll(() => exports.length).toBe(1)
    expect(exports[0].rows).toHaveLength(51)
    expect(exports[0].rows[0][0]).toBe('2026-09-09 — 2026-09-18 (10 дн.)')
    expect(exports[0].rows[0][1]).toBe('2026-08-30 — 2026-09-08 (10 дн.)')
    // Row 51 was outside the initial render window; the same descending sort
    // must put it first in the complete exported row set.
    expect(exports[0].rows[0][2]).toContain('Synthetic product 51')
    expect(await dataRows.first().innerText()).toContain('Synthetic product 51')
    await search.fill('SYNTHETIC-WEEK-B')
    await surface.getByRole('button', { name: 'Экспорт XLSX', exact: true }).click()
    await expect.poll(() => exports.length).toBe(2)
    expect(exports[1].rows).toHaveLength(1)
    expect(exports[1].rows[0][2]).toContain('Synthetic Beta garment')
    await search.fill('')
    await choose('to', '2026-09-19')
    await expect.poll(() => missingJobs).toBe(1)
    await expect.poll(() => queries.filter(q => new URLSearchParams(q).get('to') === '2026-09-19').length).toBe(2)
    await expect.poll(async () => (await surface.locator('.week-workbench .stat').first().innerText()).replace(/\s/g, ' ')).toContain('1 326 шт')
    await choose('from', '2026-09-02')
    await choose('to', '2026-09-08')
    await expect.poll(() => surface.locator('.week-periods').innerText()).toContain('2026-08-26 — 2026-09-01 · 7 дн.')
    await dataRows.first().waitFor()
    expect(unexpected).toEqual([])
    expect(errors).toEqual([])
  } finally { await browser.close() }
}, 60_000)
