import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { beforeAll, expect, it } from 'vitest'

let bundleCode: string
beforeAll(async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const mutation = process.env.SATORNA_WB_REPORT_PERIOD_MUTATION ?? ''
  if (mutation && !['ads', 'stock'].includes(mutation)) throw new Error('Unknown WB report period mutation')
  const result = await build({
    configFile: false, envFile: false, root, logLevel: 'silent',
    plugins: [{ name: 'scoped-period-guard-test-mutation', enforce: 'pre', transform(source, id) {
      if (!mutation || !id.endsWith('/VellaHtmlParityPage.tsx')) return
      const owner = mutation === 'ads' ? 'function AdsReportActiveIsland(' : 'function useStockReportState('
      const start = source.indexOf(owner), end = source.indexOf('\nfunction ', start + owner.length)
      if (start < 0 || end < 0) throw new Error('Missing period mutation owner')
      const section = source.slice(start, end)
      const guard = `if (!reportPeriodEventMatches(event, '${mutation}')) return`
      if (section.split(guard).length !== 2) throw new Error('Expected exactly one owner period guard')
      return source.slice(0, start) + section.replace(guard, '') + source.slice(end)
    } }, react()],
    define: { 'process.env.NODE_ENV': JSON.stringify('test'), 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('') },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false,
      lib: { entry: fileURLToPath(new URL('./__fixtures__/reportLoadingBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'ReportPeriodScopeTest' } },
  })
  const outputs = Array.isArray(result) ? result : [result]
  const bundle = outputs.flatMap(output => 'output' in output ? output.output : [])
    .find(output => output.type === 'chunk' && output.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing report period scope bundle')
  bundleCode = bundle.code
}, 60_000)

it.each([
  { tab: 'ads', groupBy: 'campaign', empty: 'За выбранный период нет рекламы' },
  { tab: 'stock', groupBy: 'sku', empty: 'За выбранный период нет остатков' },
])('reloads only $tab for its own period event, ignoring RNP', async ({ tab, groupBy, empty }) => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1440, height: 1000 } })
    await page.addInitScript(() => {
      if (!crypto.randomUUID) Object.defineProperty(crypto, 'randomUUID', {
        value: () => Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join(''),
      })
    })
    await page.clock.setFixedTime(new Date('2026-09-09T12:00:00Z'))
    const requests: string[] = [], unexpected: string[] = [], errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', route => {
      const request = route.request(), url = new URL(request.url())
      if (request.method() === 'GET' && url.origin === 'http://satorna.test' && url.pathname === `/wb/reports/${tab}`) {
        return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      }
      if (request.method() === 'GET' && request.resourceType() === 'image') return route.fulfill({ contentType: 'image/gif', body: Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64') })
      if (request.method() === 'GET' && url.origin === 'https://fonts.googleapis.com' && url.pathname === '/css2') return route.fulfill({ contentType: 'text/css', body: '' })
      if (request.method() !== 'GET' || url.origin !== 'http://satorna.test' || url.pathname !== `/api/wb/reports/${tab}/latest-cache`) {
        unexpected.push(`${request.method()} ${url.pathname}`); return route.abort()
      }
      requests.push(url.search)
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({
        filters: { dateRange: { from: url.searchParams.get('from'), to: url.searchParams.get('to') } },
        rows: [], kpis: [], formulaNotes: [], sourceEvidence: [], reportJob: null,
      }) })
    })
    await page.goto(`http://satorna.test/wb/reports/${tab}`)
    await page.addScriptTag({ content: bundleCode })
    const surface = page.locator(`#tab-${tab}`)
    await surface.getByText(empty, { exact: true }).waitFor({ timeout: 15_000 })
    expect(requests).toHaveLength(1)
    expect(new URLSearchParams(requests[0]).get('groupBy')).toBe(groupBy)
    const setPeriodAndDispatch = async (fromIso: string, toIso: string, reportKey: string) => {
      await page.evaluate(({ tab, fromIso, toIso, reportKey }) => {
        const runtime = window as unknown as { __vellaReportPeriods?: Record<string, unknown> }
        runtime.__vellaReportPeriods = { ...runtime.__vellaReportPeriods,
          [tab]: { days: 7, mode: 'custom', fromIso, toIso, label: 'Synthetic report period' } }
        window.dispatchEvent(new CustomEvent('vella:report-period-updated', { detail: { reportKey } }))
      }, { tab, fromIso, toIso, reportKey })
    }
    // Changed own state makes deleting the unrelated-event guard observable as an extra GET.
    await setPeriodAndDispatch('2026-08-01', '2026-08-07', 'rnp')
    await page.waitForTimeout(200)
    expect(requests, 'Unrelated RNP event must not reload this report').toHaveLength(1)
    await setPeriodAndDispatch('2026-08-08', '2026-08-14', tab)
    await expect.poll(() => requests.length, { timeout: 15_000 }).toBe(2)
    await surface.getByText(empty, { exact: true }).waitFor()
    await page.waitForTimeout(200)
    expect(requests).toHaveLength(2)
    const query = new URLSearchParams(requests[1])
    expect([query.get('from'), query.get('to'), query.get('groupBy'), query.get('source'), query.get('preset')])
      .toEqual(['2026-08-08', '2026-08-14', groupBy, 'operational', 'custom'])
    expect(await surface.locator('[data-report-row]').count()).toBe(0)
    expect(unexpected).toEqual([])
    expect(errors).toEqual([])
  } finally { await browser.close() }
}, 45_000)

it('shows historical ads before optional budgets and rejects the old-period budget response', async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  let releaseOld!: () => void
  const oldPending = new Promise<void>(resolve => { releaseOld = resolve })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1440, height: 1000 } })
    await page.addInitScript(() => {
      if (!crypto.randomUUID) Object.defineProperty(crypto, 'randomUUID', { value: () => '12345678901234567890123456789012' })
    })
    const budgetRequests: string[] = [], unexpected: string[] = []
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      if (url.pathname === '/wb/reports/ads') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (request.resourceType() === 'image') return route.abort()
      if (url.origin === 'https://fonts.googleapis.com') return route.fulfill({ contentType: 'text/css', body: '' })
      if (request.method() === 'POST' && url.pathname === '/api/wb/reports/work-demand/release') return route.fulfill({ contentType: 'application/json', body: '{}' })
      const from = url.searchParams.get('from'), to = url.searchParams.get('to')
      if (url.pathname === '/api/wb/reports/ads/latest-cache') return route.fulfill({ contentType: 'application/json', body: JSON.stringify({
        filters: { dateRange: { from, to } }, rows: [{ campaignId: '11', campaignName: 'Synthetic campaign', adSpendKopecks: from === '2026-08-08' ? 22200 : 11100 }],
        kpis: [{ id: 'ad_spend', value: from === '2026-08-08' ? '22200' : '11100' }, { id: 'campaign_budget', value: '—' }], reportJob: null,
      }) })
      if (url.pathname === '/api/wb/reports/ads/budgets') {
        budgetRequests.push(String(from))
        const nextPeriod = from === '2026-08-08'
        if (!nextPeriod) await oldPending
        try {
          return await route.fulfill({ contentType: 'application/json', body: JSON.stringify({ dateFrom: from, dateTo: to,
            rows: [{ campaignId: '11', budgetTotalKopecks: nextPeriod ? 250000 : 999000 }],
            kpi: { id: 'campaign_budget', value: nextPeriod ? '250000' : '999000' }, budgetRefresh: { state: 'completed', collected: 1, total: 1 },
          }) })
        } catch { return } // The old request was correctly aborted during navigation.
      }
      unexpected.push(url.pathname); return route.abort()
    })
    await page.goto('http://satorna.test/wb/reports/ads')
    await page.addScriptTag({ content: bundleCode })
    const surface = page.locator('#tab-ads')
    await surface.locator('[data-report-row="ads"]').first().waitFor({ timeout: 15000 })
    await expect.poll(() => budgetRequests.length).toBe(1)
    expect(await surface.innerText()).toContain('111')
    await page.evaluate(() => {
      const runtime = window as unknown as { __vellaReportPeriods?: Record<string, unknown> }
      runtime.__vellaReportPeriods = { ...runtime.__vellaReportPeriods, ads: { days: 7, mode: 'custom', fromIso: '2026-08-08', toIso: '2026-08-14', label: 'New period' } }
      window.dispatchEvent(new CustomEvent('vella:report-period-updated', { detail: { reportKey: 'ads' } }))
    })
    await expect.poll(() => budgetRequests.length).toBe(2)
    await expect.poll(async () => (await surface.innerText()).includes('2 500') || (await surface.innerText()).includes('2 500')).toBe(true)
    releaseOld()
    await page.waitForTimeout(100)
    expect(await surface.innerText()).not.toMatch(/9[\s\u00a0]990/)
    expect(unexpected).toEqual([])
  } finally { releaseOld(); await browser.close() }
}, 45000)
