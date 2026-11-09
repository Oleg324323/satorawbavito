import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { expect, it } from 'vitest'

it.each(['success', 'error', 'logout', 'pagination', 'pagination-error', 'latest-error'] as const)('isolates repricer statistics and aggregates: %s', async scenario => {
  const paginated = scenario.startsWith('pagination')
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({
    configFile: false, envFile: false, root, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': JSON.stringify('test'), 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('') },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false,
      lib: { entry: fileURLToPath(new URL('./__fixtures__/repricerStatsRaceBrowser.ts', import.meta.url)), formats: ['iife'], name: 'StatsRaceTest' } },
  })
  const outputs = Array.isArray(result) ? result : [result]
  const bundle = outputs.flatMap(output => 'output' in output ? output.output : [])
    .find(output => output.type === 'chunk' && output.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing synthetic stats bundle')
  const browser = await chromium.launch({ headless: true })
  let releaseFirst = () => {}
  const firstGate = new Promise<void>(resolve => { releaseFirst = resolve })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1280, height: 720 } })
    const unexpected: string[] = [], errors: string[] = [], requests: [string, number][] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      if (request.method() === 'GET' && request.resourceType() === 'image' && url.href === 'https://basket-01.wbbasket.ru/vol0/part0/101/images/c516x688/1.webp') {
        return route.fulfill({ contentType: 'image/gif', body: Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64') })
      }
      if (url.origin !== 'http://satorna.test' || request.method() !== 'GET') {
        unexpected.push(`${request.method()} ${url.pathname}`); return route.abort()
      }
      if (url.pathname === '/wb/repricer/stats') return route.fulfill({ contentType: 'text/html', body:
        '<title>Synthetic repricer statistics</title><button id="reload">Reload filtered statistics</button><button id="logout">Clear test session</button><section id="tab-repricer-stats"><div class="search"><input></div><div class="stats">' + Array.from({ length: 4 }, () => '<div class="stat"><span class="stat-val">STALE-KPI</span><span class="stat-delta up down neutral">STALE-DELTA</span></div>').join('') + '</div><table><tbody id="repricerStatsBody"></tbody></table><div data-filter-summary><span>STALE-SUMMARY</span></div></section>' })
      if (url.pathname === '/favicon.ico') return route.fulfill({ status: 204 })
      if (url.pathname !== '/api/v1/wb-repricer/stats') { unexpected.push(url.pathname); return route.abort() }
      const query = url.searchParams.get('q') ?? ''
      const pageNumber = Number(url.searchParams.get('page'))
      requests.push([query, pageNumber])
      if (query && scenario === 'latest-error') return route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":{"code":"SYNTHETIC_ERROR"}}' })
      if (!query && (!paginated || pageNumber === 2)) await firstGate
      if (!query && (scenario === 'error' || scenario === 'pagination-error' && pageNumber === 2)) return route.fulfill({ status: 500, contentType: 'application/json', body: '{"error":{"code":"SYNTHETIC_ERROR"}}' })
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({
        items: [{ articleId: query ? 'NEWER-SKU' : 'OLDER-SKU', nmId: 101, name: 'Synthetic product',
          brand: 'Synthetic', managerName: '', metrics: {}, decision: {}, sources: {}, priceProtection: {}, flags: [] }],
        total: !query && paginated ? 501 : 1, itemsReturned: 1, page: pageNumber, pageSize: 500,
        summary: { baskets: query ? 902 : 401, orders: 10 },
      }) })
    })
    await page.goto('http://satorna.test/wb/repricer/stats')
    await page.addScriptTag({ content: bundle.code })
    await expect.poll(() => requests.length).toBe(paginated ? 2 : 1)
    if (!paginated) {
      expect(await page.locator('.stat-val').allTextContents()).toEqual(['—', '—', '—', '—'])
      expect(await page.locator('[data-filter-summary] span').innerText()).toBe('')
    }
    await page.getByRole('button', { name: 'Reload filtered statistics', exact: true }).click()
    if (scenario === 'latest-error') await page.getByText('Статистика репрайсера недоступна', { exact: false }).waitFor({ state: 'visible' })
    else {
      await page.getByText('NEWER-SKU', { exact: true }).waitFor({ state: 'visible' })
      expect(await page.locator('.stat-val').first().innerText()).toBe('902')
    }
    if (scenario === 'logout') await page.getByRole('button', { name: 'Clear test session', exact: true }).click()
    releaseFirst()
    await page.waitForFunction(() => document.body.dataset.firstSettled === 'true')
    expect(await page.getByText('NEWER-SKU', { exact: true }).count()).toBe(scenario === 'logout' || scenario === 'latest-error' ? 0 : 1)
    expect(await page.getByText('OLDER-SKU', { exact: true }).count()).toBe(0)
    if (scenario === 'latest-error' || scenario === 'logout') {
      expect(await page.locator('.stat-val').allTextContents()).toEqual(['—', '—', '—', '—'])
      expect(await page.locator('[data-filter-summary] span').innerText()).toBe('')
    }
    expect(await page.title()).toBe('Synthetic repricer statistics')
    if (scenario === 'success') await page.screenshot({ path: '/tmp/satorna-t4-repricer-stats-race.png' })
    await page.getByRole('button', { name: 'Clear test session', exact: true }).click()
    expect(await page.getByText('NEWER-SKU', { exact: true }).count()).toBe(0)
    expect(await page.locator('#repricerStatsBody').innerText()).toContain('Статистика репрайсера недоступна')
    expect(await page.locator('.stat-val').allTextContents()).toEqual(['—', '—', '—', '—'])
    expect(await page.locator('.stat-delta').allTextContents()).toEqual(['', '', '', ''])
    expect(await page.locator('.stat-delta.up, .stat-delta.down, .stat-delta.neutral').count()).toBe(0)
    expect(await page.locator('[data-filter-summary] span').innerText()).toBe('')
    expect(requests).toEqual(paginated ? [['', 1], ['', 2], ['second', 1]] : [['', 1], ['second', 1]])
    expect(unexpected).toEqual([])
    expect(errors).toEqual([])
  } finally { releaseFirst(); await browser.close() }
}, 60_000)

it.each([false, true])('keeps every received statistics page and retries a failed page (failure=%s)', async failure => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({
    configFile: false, envFile: false, root, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""' },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: {
      entry: fileURLToPath(new URL('./__fixtures__/repricerStatsRaceBrowser.ts', import.meta.url)), formats: ['iife'], name: 'StatsBatchTest',
    } },
  })
  const bundle = (Array.isArray(result) ? result : [result]).flatMap(output => 'output' in output ? output.output : [])
    .find(output => output.type === 'chunk' && output.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing statistics batching bundle')
  const browser = await chromium.launch({ headless: true })
  let release = () => {}
  const lastPage = new Promise<void>(resolve => { release = resolve })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block' })
    const queries: number[] = [], errors: string[] = [], unexpected: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', async route => {
      const url = new URL(route.request().url())
      if (url.origin !== 'http://satorna.test' || route.request().method() !== 'GET') { unexpected.push(url.href); return route.abort() }
      if (url.pathname === '/wb/repricer/stats') return route.fulfill({ contentType: 'text/html', body:
        '<title>Synthetic statistics batching</title><button id="reload">Reload</button><button id="logout">Logout</button><section id="tab-repricer-stats"><div class="search"><input></div><div class="stats"><div class="stat"><span class="stat-val"></span><span class="stat-delta"></span></div></div><table><tbody id="repricerStatsBody"></tbody></table><nav id="repricerStatsPager"></nav><div data-filter-summary><span></span></div></section>' })
      if (url.pathname === '/favicon.ico') return route.fulfill({ status: 204 })
      if (url.pathname !== '/api/v1/wb-repricer/stats') { unexpected.push(url.pathname); return route.abort() }
      const pageNumber = Number(url.searchParams.get('page'))
      queries.push(pageNumber)
      if (pageNumber === 3) {
        await lastPage
        if (failure && queries.filter(number => number === 3).length === 1) return route.fulfill({ status: 503, json: { error: { code: 'SYNTHETIC_UNAVAILABLE', message: 'Источник временно недоступен' } } })
      }
      return route.fulfill({ json: { items: Array.from({ length: pageNumber === 3 ? 1 : 500 }, (_, i) => ({
        articleId: `BATCH-SKU-${(pageNumber - 1) * 500 + i + 1}`, name: 'Synthetic product',
        metrics: {}, decision: {}, sources: {}, priceProtection: {}, flags: [],
      })), total: 1001, itemsReturned: pageNumber === 3 ? 1 : 500, page: pageNumber, pageSize: 500, summary: { baskets: 1001, orders: 1001 } } })
    })
    await page.goto('http://satorna.test/wb/repricer/stats')
    await page.addScriptTag({ content: bundle.code })
    await expect.poll(() => queries).toEqual([1, 2, 3])
    const rows = page.locator('#repricerStatsBody [data-sku]')
    expect(await rows.count()).toBe(100)
    expect(await page.locator('#repricerStatsPager').innerText()).toContain('страница 1 из 10')
    await page.locator('#repricerStatsPager [data-stats-page="next"]').click()
    expect(await rows.first().innerText()).toContain('BATCH-SKU-101')
    await page.locator('#repricerStatsPager [data-stats-page="prev"]').click()
    expect(await page.locator('[data-filter-summary]').innerText()).toContain('1 001')
    expect(await page.locator('.stat-val').innerText()).toBe('1 001')
    release()
    await page.waitForFunction(() => document.body.dataset.firstSettled === 'true')
    expect(await rows.count()).toBe(100)
    if (failure) {
      await page.getByRole('alert').filter({ hasText: 'Источник временно недоступен' }).waitFor()
      expect(await page.locator('#repricerStatsBody [data-report-row]').count()).toBe(100)
      await page.getByRole('button', { name: 'Повторить загрузку', exact: true }).click()
      await expect.poll(() => queries).toEqual([1, 2, 3, 3])
      expect(await page.getByRole('alert').count()).toBe(0)
    }
    await page.locator('.search input').fill('BATCH-SKU-1001')
    await page.getByText('BATCH-SKU-1001', { exact: true }).waitFor()
    expect(await rows.count()).toBe(1)
    expect(queries).toEqual(failure ? [1, 2, 3, 3] : [1, 2, 3])
    expect(unexpected).toEqual([])
    expect(errors).toEqual([])
  } finally { release(); await browser.close() }
}, 60_000)
