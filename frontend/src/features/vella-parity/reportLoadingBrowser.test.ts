import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { expect, it } from 'vitest'

async function prepareBrowser(page: import('playwright').Page) {
  await page.addInitScript(() => {
    if (!crypto.randomUUID) Object.defineProperty(crypto, 'randomUUID', {
      value: () => Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join(''),
    })
  })
}

const reports = [
  { report: 'stock', tab: 'stock', empty: 'За выбранный период нет остатков', error: 'Остатки не загрузились' },
  { report: 'week-over-week', tab: 'week', empty: 'За выбранный период нет сравнения', error: 'Сравнение недель не загрузилось' },
]
it.each(reports.flatMap(report => ['empty', 'failure'].map(outcome => ({ ...report, outcome }))))(
  'renders $report loading → $outcome without mock rows', async ({ report, tab, empty, error, outcome }) => {
    const root = fileURLToPath(new URL('../../../', import.meta.url))
    const result = await build({
      configFile: false, envFile: false, root, logLevel: 'silent', plugins: [react()],
      define: { 'process.env.NODE_ENV': JSON.stringify('test'), 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('') },
      resolve: { alias: { '@': path.join(root, 'src') } },
      build: { write: false, minify: false,
        lib: { entry: fileURLToPath(new URL('./__fixtures__/reportLoadingBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'ReportStateTest' } },
    })
    const outputs = Array.isArray(result) ? result : [result]
    const bundle = outputs.flatMap(output => 'output' in output ? output.output : [])
      .find(output => output.type === 'chunk' && output.isEntry)
    if (!bundle || bundle.type !== 'chunk') throw new Error('Missing synthetic report state bundle')
    const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
    let release = () => {}
    const gate = new Promise<void>(resolve => { release = resolve })
    try {
      const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1440, height: 1000 } })
      await prepareBrowser(page)
      const unexpected: string[] = [], errors: string[] = [], queries: string[] = []
      page.on('pageerror', event => errors.push(event.message))
      await page.route('**/*', async route => {
        const request = route.request(), url = new URL(request.url())
        if (request.method() === 'GET' && url.origin === 'http://satorna.test' && url.pathname === `/wb/reports/${report}`) {
          return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
        }
        if (request.method() === 'GET' && request.resourceType() === 'image') return route.fulfill({ contentType: 'image/gif', body: Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64') })
        if (request.method() === 'GET' && url.origin === 'https://fonts.googleapis.com' && url.pathname === '/css2') return route.fulfill({ contentType: 'text/css', body: '' })
        if (request.method() !== 'GET' || url.origin !== 'http://satorna.test' || url.pathname !== `/api/wb/reports/${report}/latest-cache`) {
          unexpected.push(`${request.method()} ${url.pathname}`); return route.abort()
        }
        queries.push(url.search)
        await gate
        if (outcome === 'failure') return route.fulfill({ status: 503, contentType: 'application/json', body: '{"error":{"code":"SYNTHETIC_UNAVAILABLE","message":"Источник временно недоступен"}}' })
        return route.fulfill({ contentType: 'application/json', body: JSON.stringify({
          filters: { dateRange: { from: url.searchParams.get('from'), to: url.searchParams.get('to') } },
          rows: [], kpis: [], formulaNotes: [], sourceEvidence: [], reportJob: null,
        }) })
      })
      await page.goto(`http://satorna.test/wb/reports/${report}`)
      await page.addScriptTag({ content: bundle.code })
      // Wait for the real React effect, not a one-second machine-speed assumption.
      await expect.poll(() => queries.length, { timeout: 15_000 }).toBe(1)
      const surface = page.locator(`#tab-${tab}`)
      await surface.locator('.report-progress-panel[role="status"]').waitFor({ state: 'visible' })
      expect(await surface.innerText()).toContain('Собираем показатели за выбранный период')
      expect(await surface.locator('[data-report-row]').count()).toBe(0)
      if (outcome === 'empty') await page.screenshot({ path: `/tmp/satorna-t4-${tab}-loading.png` })
      release()
      await surface.getByText(outcome === 'empty' ? empty : error, { exact: true }).waitFor({ state: 'visible' })
      expect(await surface.locator('.report-progress-panel').count()).toBe(0)
      expect(await surface.locator('[data-report-row]').count()).toBe(0)
      if (outcome === 'failure') expect(await surface.innerText()).toContain('Источник временно недоступен')
      if (outcome === 'empty') await page.screenshot({ path: `/tmp/satorna-t4-${tab}-empty.png` })
      await page.getByRole('button', { name: 'Clear synthetic report session', exact: true }).click()
      await surface.getByText(error, { exact: true }).waitFor({ state: 'visible' })
      // Failure already displayed this heading before logout. Observe the actual
      // session transition rather than racing the still-visible source error.
      await expect.poll(() => surface.innerText()).toContain('Сессия истекла')
      expect(queries).toHaveLength(1)
      const query = new URLSearchParams(queries[0])
      expect([query.get('groupBy'), query.get('source'), query.get('preset')]).toEqual(['sku', 'operational', 'custom'])
      expect(await page.title()).toBe('Satorna — Отчёты WB')
      expect(unexpected, `Unexpected fixture requests: ${JSON.stringify(unexpected)}`).toEqual([])
      expect(errors).toEqual([])
    } finally { release(); await browser.close() }
  }, 60_000,
)

it('does not report an empty P&L when polling expires without a ready cache', async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({
    configFile: false, envFile: false, root, logLevel: 'silent', plugins: [{
      name: 'synthetic-cabinet', enforce: 'pre', transform(code, id) {
        if (!id.endsWith('/__fixtures__/reportLoadingBrowser.tsx')) return
        return code.replace('cabinetMe: null', `cabinetMe: ({ organization: { organizationId: 7 }, user: { userId: 1, permissions: [] } } as AuthContextValue['cabinetMe'])`)
      },
    }, react()],
    define: { 'process.env.NODE_ENV': JSON.stringify('test'), 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('') },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: {
      entry: fileURLToPath(new URL('./__fixtures__/reportLoadingBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'ReportPollingTest',
    } },
  })
  const outputs = Array.isArray(result) ? result : [result]
  const bundle = outputs.flatMap(output => 'output' in output ? output.output : [])
    .find(output => output.type === 'chunk' && output.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing report polling fixture bundle')
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1440, height: 1000 } })
    await prepareBrowser(page)
    // Only accelerate the real loader's two-second waits; keep its poll budget unchanged.
    await page.addInitScript(() => {
      const browserWindow: Window = window
      const schedule = browserWindow.setTimeout.bind(browserWindow)
      browserWindow.setTimeout = (handler, timeout, ...args) => schedule(handler, timeout === 2000 ? 0 : timeout, ...args)
    })
    let starts = 0, polls = 0, cacheReads = 0, readyEmpty = false
    const unexpected: string[] = [], errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', route => {
      const request = route.request(), url = new URL(request.url())
      if (request.method() === 'GET' && url.origin === 'http://satorna.test' && url.pathname === '/wb/reports/pnl') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (request.method() === 'POST' && url.origin === 'http://satorna.test' && url.pathname === '/api/wb/reports/work-demand/release') return route.fulfill({ json: {} })
      if (request.method() === 'GET' && request.resourceType() === 'image') return route.fulfill({ contentType: 'image/gif', body: Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64') })
      if (request.method() === 'GET' && url.origin === 'https://fonts.googleapis.com' && url.pathname === '/css2') return route.fulfill({ contentType: 'text/css', body: '' })
      if (url.pathname === '/api/v1/cabinet/team/users') return route.fulfill({ json: { data: [] } })
      if (url.pathname === '/api/v1/cabinet/wb-token') return route.fulfill({ json: { data: { hasToken: false } } })
      if (url.pathname === '/api/v1/cabinet/avito-credentials') return route.fulfill({ json: { data: { hasCredentials: false } } })
      if (url.origin === 'http://satorna.test' && url.pathname === '/api/wb/reports/pnl/latest-cache' && request.method() === 'GET') {
        cacheReads += 1
        return readyEmpty
          ? route.fulfill({ contentType: 'application/json', body: JSON.stringify({ rows: [], kpis: [], cashFlow: null, reportJob: { state: 'completed' } }) })
          : route.fulfill({ status: 404, contentType: 'application/json', body: '{"error":{"code":"HTTP_404","message":"REPORT_LATEST_CACHE_MISSING"}}' })
      }
      if (url.origin === 'http://satorna.test' && url.pathname === '/api/wb/reports/pnl/jobs' && ['GET', 'POST'].includes(request.method())) {
        if (request.method() === 'POST') starts += 1
        else polls += 1
        return route.fulfill({ contentType: 'application/json', body: JSON.stringify({
          state: request.method() === 'POST' ? 'queued' : 'running', stage: 'refreshing_sources', kind: 'report_source_refresh',
          taskId: 'synthetic-pending-report', reportId: 'pnl', dateFrom: url.searchParams.get('from'), dateTo: url.searchParams.get('to'),
          groupBy: 'sku', source: 'financial', percent: 13, label: 'Получаем данные WB',
        }) })
      }
      unexpected.push(`${request.method()} ${url.pathname}`)
      return route.abort()
    })
    await page.goto('http://satorna.test/wb/reports/pnl')
    await page.addScriptTag({ content: bundle.code })
    const surface = page.locator('#tab-pnl')
    await expect.poll(() => polls, { timeout: 20_000 }).toBe(600)
    await surface.getByText(/^(Не удалось загрузить P&L|За выбранный период нет данных)$/).waitFor()
    expect([starts, polls]).toEqual([1, 600])
    expect(await surface.innerText()).toContain('Не удалось загрузить P&L')
    expect(await surface.innerText()).not.toContain('За выбранный период нет данных')
    expect(await surface.locator('[data-report-row]').count()).toBe(0)

    readyEmpty = true
    await page.reload()
    await page.addScriptTag({ content: bundle.code })
    await surface.getByText('За выбранный период нет данных', { exact: true }).waitFor()
    expect([starts, polls]).toEqual([1, 600])
    expect(unexpected).toEqual([])
    expect(errors).toEqual([])
  } finally { await browser.close() }
}, 60_000)

it.each(['abc', 'pnl', 'ads'])('stops a cold %s report poll after navigating to statistics', async report => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({
    configFile: false, envFile: false, root, logLevel: 'silent', plugins: [{
      name: 'hidden-report-browser-router', enforce: 'pre',
      transform(code, id) {
        if (!id.endsWith('/__fixtures__/reportLoadingBrowser.tsx')) return null
        return code.replaceAll('MemoryRouter', 'BrowserRouter').replace(' initialEntries={[window.location.pathname]}', '')
          .replace('cabinetMe: null', `cabinetMe: ({ organization: { organizationId: 7 }, user: { userId: 1, permissions: [] } } as AuthContextValue['cabinetMe'])`)
      },
    }, react()],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""' },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: {
      entry: fileURLToPath(new URL('./__fixtures__/reportLoadingBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'HiddenReportPollingTest',
    } },
  })
  const bundle = (Array.isArray(result) ? result : [result]).flatMap(output => 'output' in output ? output.output : [])
    .find(output => output.type === 'chunk' && output.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing hidden report polling bundle')
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1440, height: 1000 } })
    await prepareBrowser(page)
    let starts = 0, polls = 0
    const unexpected: string[] = [], errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', route => {
      const request = route.request(), url = new URL(request.url())
      if (request.resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (request.resourceType() === 'image') return route.fulfill({ contentType: 'image/gif', body: Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64') })
      if (url.origin === 'https://fonts.googleapis.com') return route.fulfill({ contentType: 'text/css', body: '' })
      if (url.pathname === '/api/v1/cabinet/team/users') return route.fulfill({ json: { data: [] } })
      if (url.pathname === '/api/v1/cabinet/wb-token') return route.fulfill({ json: { data: { hasToken: false } } })
      if (url.pathname === '/api/v1/cabinet/avito-credentials') return route.fulfill({ json: { data: { hasCredentials: false } } })
      if (url.origin === 'http://satorna.test' && url.pathname === `/api/wb/reports/${report}/latest-cache` && request.method() === 'GET') {
        return route.fulfill({ status: 404, json: { error: { code: 'HTTP_404', message: 'REPORT_LATEST_CACHE_MISSING' } } })
      }
      if (url.origin === 'http://satorna.test' && url.pathname === `/api/wb/reports/${report}/jobs`) {
        if (request.method() === 'POST') starts++
        else polls++
        return route.fulfill({ json: { state: 'queued', percent: 0, label: 'Получаем данные WB', dateFrom: url.searchParams.get('from'), dateTo: url.searchParams.get('to') } })
      }
      if (url.origin === 'http://satorna.test' && url.pathname === '/api/v1/wb-repricer/stats' && request.method() === 'GET') return route.fulfill({ json: {
        items: [{ articleId: 'VISIBLE-STATS-SKU', name: 'Synthetic statistics', metrics: {}, sources: {}, decision: {}, priceProtection: {}, flags: [] }], total: 1, itemsReturned: 1, page: 1, pageSize: 500,
      } })
      unexpected.push(`${request.method()} ${url.href}`)
      return route.abort()
    })
    await page.goto(`http://satorna.test/wb/reports/${report}`)
    await page.addScriptTag({ content: bundle.code })
    await expect.poll(() => starts, { timeout: 15000 }).toBe(1)
    // Exercise the existing route handler; its parent navigation group is collapsed on reports.
    await page.locator('#sidebar [data-tab="repricer-stats"]').dispatchEvent('click')
    await page.getByText('VISIBLE-STATS-SKU', { exact: true }).waitFor({ timeout: 5000 })
    const before = polls
    await page.waitForTimeout(2200) // One real two-second poll boundary while the report is hidden.
    expect(polls).toBe(before)
    expect(starts).toBe(1)
    expect(errors).toEqual([])
    expect(unexpected).toEqual([])
  } finally { await browser.close() }
}, 60_000)

it('reloads the requested Digest period after leaving during a cold refresh', async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({
    configFile: false, envFile: false, root, logLevel: 'silent', plugins: [{
      name: 'digest-browser-router', enforce: 'pre',
      transform(code, id) {
        if (!id.endsWith('/__fixtures__/reportLoadingBrowser.tsx')) return null
        return code.replaceAll('MemoryRouter', 'BrowserRouter').replace(' initialEntries={[window.location.pathname]}', '')
      },
    }, react()],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""' },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: { entry: fileURLToPath(new URL('./__fixtures__/reportLoadingBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'DigestResumeTest' } },
  })
  const bundle = (Array.isArray(result) ? result : [result]).flatMap(output => 'output' in output ? output.output : [])
    .find(output => output.type === 'chunk' && output.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing Digest resume bundle')
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block' })
    await prepareBrowser(page)
    let starts = 0, cacheReadsB = 0, readyB = false
    const errors: string[] = [], unexpected: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', route => {
      const request = route.request(), url = new URL(request.url())
      if (request.resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (request.resourceType() === 'image') return route.fulfill({ contentType: 'image/gif', body: Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64') })
      if (url.origin === 'https://fonts.googleapis.com') return route.fulfill({ contentType: 'text/css', body: '' })
      if (url.origin === 'http://satorna.test' && url.pathname === '/api/wb/reports/work-demand/release') return route.fulfill({ json: {} })
      if (url.origin === 'http://satorna.test' && url.pathname === '/api/wb/reports/sales-funnel') {
        const from = url.searchParams.get('dateFrom'), to = url.searchParams.get('dateTo')
        const isB = from === '2026-08-01'
        if (isB) cacheReadsB++
        return route.fulfill({ json: { items: [{ nmId: '1', sellerArticle: 'RESUME-SKU', title: 'Synthetic', imageUrl: null, metrics: { orders: isB ? 3 : 7 } }], totals: { orders: isB ? 3 : 7 }, total: 1, page: 1, pageSize: 50, complete: !isB || readyB, coveredProducts: !isB || readyB ? 1 : 0, brands: [], dateFrom: from, dateTo: to, unavailable: {}, job: isB && !readyB ? { status: 'queued' } : null } })
      }
      if (url.origin === 'http://satorna.test' && url.pathname === '/api/wb/reports/sales-funnel/refresh') {
        starts++
        return route.fulfill({ json: { status: 'queued' } })
      }
      if (url.origin === 'http://satorna.test' && url.pathname === '/api/v1/wb-repricer/stats') return route.fulfill({ json: { items: [{ articleId: 'DIGEST-NAV-STATS', name: 'Synthetic', metrics: {}, decision: {}, sources: {}, flags: [] }], total: 1 } })
      unexpected.push(`${request.method()} ${url.href}`)
      return route.abort()
    })
    await page.goto('http://satorna.test/wb/reports')
    await page.evaluate(() => {
      window.__vellaReportPeriods = { digest: { days: 7, mode: 'custom', fromIso: '2026-09-02', toIso: '2026-09-08', label: 'Synthetic A' } }
      window.__vellaReportPeriodManual = { digest: true }
    })
    await page.addScriptTag({ content: bundle.code })
    await expect.poll(() => page.locator('.sf-filters').innerText(), { timeout: 15000 }).toContain('02.09.2026')
    await page.evaluate(() => {
      const period = { days: 3, mode: 'custom' as const, fromIso: '2026-08-01', toIso: '2026-08-03', label: 'Synthetic B' }
      window.__vellaReportPeriods = { ...window.__vellaReportPeriods, digest: period }
      window.__vellaReportPeriodManual = { ...window.__vellaReportPeriodManual, digest: true }
      window.dispatchEvent(new CustomEvent('vella:report-period-updated', { detail: { reportKey: 'digest', period } }))
    })
    await expect.poll(() => starts).toBe(1)
    await page.locator('#sidebar [data-tab="repricer-stats"]').dispatchEvent('click')
    await page.getByText('DIGEST-NAV-STATS', { exact: true }).waitFor()
    expect(await page.locator('.wb-sales-funnel').count()).toBe(0)
    readyB = true
    await page.locator('#sidebar [data-tab="digest"]').dispatchEvent('click')
    await expect.poll(() => page.locator('.sf-state').innerText(), { timeout: 5000 }).toContain('загружены.')
    expect(await page.locator('.sf-filters').innerText()).toContain('01.08.2026')
    expect(cacheReadsB).toBe(2)
    expect(starts).toBe(1)
    expect(errors).toEqual([])
    expect(unexpected).toEqual([])
  } finally { await browser.close() }
}, 60_000)
