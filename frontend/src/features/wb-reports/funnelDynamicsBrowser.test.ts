import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { beforeAll, expect, it } from 'vitest'
import { previousFunnelPeriod } from './funnelDynamics'

let code = '', css = ''
beforeAll(async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({ configFile: false, envFile: false, root, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""' },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: { entry: fileURLToPath(new URL('./__fixtures__/funnelDynamicsBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'FunnelDynamics' } } })
  const output = (Array.isArray(result) ? result : [result]).flatMap(item => 'output' in item ? item.output : [])
  const bundle = output.find(item => item.type === 'chunk' && item.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing fixture')
  code = bundle.code
  css = output.filter(item => item.type === 'asset' && item.fileName.endsWith('.css')).map(item => item.type === 'asset' ? String(item.source) : '').join('\n')
})

const metrics = (visits = 100) => ({ visits, carts: 20, orders: 8, buyouts: 4, revenueShare: 3,
  cartConversion: 20, orderConversion: 40, orderAmount: 100000, buyoutAmount: 50000, buyoutPercent: 40 })
const current = (params: URLSearchParams, complete = true) => ({
  dateFrom: params.get('dateFrom'), dateTo: params.get('dateTo'), total: 51, complete, refreshRequired: !complete,
  coveredProducts: 2, totals: metrics(), unavailable: {}, brands: [], page: 1, pageSize: 50,
  job: { status: complete ? 'complete' : 'running' },
  items: (params.get('page') === '2' ? ['3'] : ['1', '2']).map(nmId => ({ nmId, title: 'Synthetic WB', sellerArticle: `SKU-${nmId}`, imageUrl: null, metrics: metrics(nmId === '2' ? 0 : 100) })),
})
const comparison = (params: URLSearchParams, complete = true) => ({
  ...current(params, complete), ...previousFunnelPeriod(params.get('dateFrom')!, params.get('dateTo')!),
  source: 'wb_sales_funnel_comparison_v1',
  selectedDateFrom: params.get('dateFrom'), selectedDateTo: params.get('dateTo'),
  totals: {}, totalsUnavailable: 'API WB не передаёт готовую динамику для этих итогов.',
  // Deliberately reverse the source order and include an unrelated product.
  items: ['2', '999', '1', '3'].map(nmId => ({ nmId, metrics: metrics(nmId === '1' ? 50 : 0),
    selectedValues: metrics(nmId === '2' ? 0 : 100),
    dynamics: { ...Object.fromEntries(Object.keys(metrics()).map(key => [key, 5])), visits: nmId === '2' ? 0 : -7, carts: -3, orderConversion: null },
    ready: Object.fromEntries(Object.keys(metrics()).map(key => [key, key !== 'orderConversion'])) })),
})
const aggregateComparison = (params: URLSearchParams) => ({
  source: 'calculated_wb_sales_funnel_totals_v1',
  ...previousFunnelPeriod(params.get('dateFrom')!, params.get('dateTo')!),
  selectedDateFrom: params.get('dateFrom'), selectedDateTo: params.get('dateTo'),
  total: 51, complete: false, refreshRequired: false,
  totals: {}, selectedTotals: metrics(), previousTotals: {}, ready: {}, unavailable: {}, coverage: {},
})

it('recovers missing rows once while saved seller dynamics remain visible, then shows native card dynamics', async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block' })
    let restored = false
    const starts: string[] = []
    await page.route('**/*', route => {
      const url = new URL(route.request().url()), params = url.searchParams
      if (route.request().resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (url.pathname.endsWith('/refresh')) {
        starts.push(params.toString())
        return route.fulfill({ json: { status: 'queued' } })
      }
      if (url.pathname.endsWith('/totals-comparison')) return route.fulfill({ json: {
        ...aggregateComparison(params), complete: true, totals: { visits: 25 },
        selectedTotals: metrics(), previousTotals: metrics(80), ready: { visits: true },
      } })
      if (url.pathname.endsWith('/comparison')) return route.fulfill({ json: comparison(params) })
      return route.fulfill({ json: { ...current(params), total: restored ? 2 : 0,
        items: restored ? current(params).items : [], coveredProducts: restored ? 2 : 0,
        catalogState: restored ? 'ready' : 'missing', summaryComplete: true,
        complete: restored, refreshRequired: !restored, job: { status: restored ? 'complete' : 'queued' },
      } })
    })
    await page.goto('http://localhost/funnel'); await page.addScriptTag({ content: code })
    const summaryChange = page.locator('.sf-stage-metric .sf-change').first()
    await expect.poll(() => starts.length).toBe(1)
    await expect.poll(() => summaryChange.innerText()).toBe('↑ +25%')
    expect(await page.locator('.sf-state').innerText()).not.toContain('В подключённом аккаунте нет товаров')
    expect(await page.locator('.sf-state').innerText()).not.toContain('Полные данные: 0 из 0')
    expect(await page.locator('.sf-state').innerText()).not.toContain('не являются итогом всего периода')
    expect(await page.locator('tbody tr').count()).toBe(0)
    await page.getByRole('button', { name: /Перешли в карточку/ }).click()
    await expect.poll(() => summaryChange.innerText()).toBe('↑ +25%')
    expect(starts).toHaveLength(1)

    restored = true
    await page.getByRole('button', { name: /Перешли в карточку/ }).click()
    await expect.poll(() => page.locator('tbody tr').count()).toBe(2)
    await expect.poll(() => page.locator('tbody tr').first().innerText()).toContain('−7%')
    expect(await summaryChange.innerText()).toBe('↑ +25%')
    expect(starts).toHaveLength(1)

    await page.getByRole('button', { name: 'Другой период' }).click()
    await expect.poll(() => page.locator('.sf-period').innerText()).toContain('19.10.2026 — 25.10.2026')
    await expect.poll(() => page.locator('tbody tr').count()).toBe(2)
    await page.goto('http://localhost/away')
    await page.goto('http://localhost/funnel'); await page.addScriptTag({ content: code })
    await expect.poll(() => page.locator('tbody tr').count()).toBe(2)
    expect(starts).toHaveLength(1)
  } finally { await browser.close() }
})

it('distinguishes an uninitialized catalog, confirmed empty account, filtered empty result and failed recovery', async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block' })
    let state: 'missing' | 'empty' | 'ready' | 'failed' = 'missing'
    await page.route('**/*', route => {
      const url = new URL(route.request().url()), params = url.searchParams
      if (route.request().resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (url.pathname.endsWith('/refresh')) return route.fulfill({ json: { status: 'failed', message: 'Не удалось восстановить каталог WB. Повторите загрузку.' } })
      if (url.pathname.endsWith('/totals-comparison')) return route.fulfill({ json: aggregateComparison(params) })
      return route.fulfill({ json: { ...current(params), total: 0, items: [], coveredProducts: 0,
        catalogState: state === 'failed' ? 'missing' : state, complete: state !== 'missing', refreshRequired: state === 'failed',
        job: { status: state === 'failed' ? 'failed' : 'complete', message: state === 'failed' ? 'Не удалось восстановить каталог WB. Повторите загрузку.' : undefined },
      } })
    })
    await page.goto('http://localhost/funnel'); await page.addScriptTag({ content: code })
    const message = () => page.locator('.sf-state').innerText()
    await expect.poll(message).toContain('Каталог товаров WB ещё не загружен')
    expect(await message()).not.toContain('В подключённом аккаунте нет товаров')
    state = 'empty'; await page.getByRole('button', { name: /Перешли в карточку/ }).click()
    await expect.poll(message).toContain('В подключённом аккаунте нет товаров')
    state = 'ready'; await page.getByRole('button', { name: /Перешли в карточку/ }).click()
    await expect.poll(message).toContain('По выбранным фильтрам товары не найдены')
    state = 'failed'; await page.getByRole('button', { name: /Перешли в карточку/ }).click()
    await expect.poll(message).toContain('Не удалось восстановить каталог WB')
    expect(await page.getByRole('button', { name: 'Повторить загрузку', exact: true }).isVisible()).toBe(true)
  } finally { await browser.close() }
})

it('sorts every server-paginated funnel column with real clicks and matching aria without synchronization', async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block' })
    const records = [
      { nmId: '1', sellerArticle: 'Товар 10', title: 'Item', imageUrl: null, metrics: metrics(650) },
      { nmId: '2', sellerArticle: 'Товар 2', title: 'Item', imageUrl: null, metrics: metrics(700) },
      { nmId: '3', sellerArticle: 'Товар 1', title: 'Item', imageUrl: null, metrics: metrics(0) },
    ]
    const starts: string[] = [], reads: URLSearchParams[] = []
    const collator = new Intl.Collator('ru', { numeric: true, sensitivity: 'base' })
    await page.route('**/*', route => {
      const request = route.request(), url = new URL(request.url()), params = url.searchParams
      if (request.resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (url.pathname.endsWith('/refresh')) { starts.push(url.pathname); return route.abort() }
      if (url.pathname.endsWith('/totals-comparison')) return route.fulfill({ json: aggregateComparison(params) })
      if (url.pathname.endsWith('/comparison')) return route.fulfill({ json: { ...comparison(params), items: [] } })
      reads.push(params)
      const key = params.get('sort')!, direction = params.get('direction')!
      const items = [...records].sort((a, b) => {
        const result = key === 'sku' ? collator.compare(a.sellerArticle, b.sellerArticle)
          : (a.metrics[key as keyof typeof a.metrics] ?? 0) - (b.metrics[key as keyof typeof b.metrics] ?? 0)
        return direction === 'asc' ? result : -result
      })
      return route.fulfill({ json: { ...current(params), total: 3, items } })
    })
    await page.goto('http://localhost/funnel'); await page.addScriptTag({ content: code })
    await page.locator('tbody tr').first().waitFor()
    const headers = page.locator('thead th')
    for (const header of await headers.all()) {
      const arrow = header.locator('.sf-sort-arrow')
      expect(await arrow.isVisible()).toBe(true)
      expect(await arrow.innerText()).toBe(await header.getAttribute('aria-sort') === 'none' ? '↕' : '↓')
    }
    for (let index = 0; index < await headers.count(); index++) {
      for (const direction of ['desc', 'asc'] as const) {
        await headers.nth(index).getByRole('button').click()
        await expect.poll(() => reads.at(-1)?.get('direction')).toBe(direction)
        expect(await headers.nth(index).getAttribute('aria-sort')).toBe(direction === 'asc' ? 'ascending' : 'descending')
        expect(await headers.nth(index).locator('.sf-sort-arrow').innerText()).toBe(direction === 'asc' ? '↑' : '↓')
        expect(await headers.filter({ has: page.locator('[aria-hidden="true"]') }).count()).toBe(await headers.count())
        for (const inactive of await page.locator('thead th[aria-sort="none"] .sf-sort-arrow').all()) expect(await inactive.innerText()).toBe('↕')
        if (index === 0) {
          expect(reads.at(-1)?.get('sort')).toBe('sku')
          await expect.poll(() => page.locator('tbody tr').first().getByRole('link').first().innerText()).toBe(direction === 'asc' ? 'Товар 1' : 'Товар 10')
        }
      }
    }
    await headers.first().click({ position: { x: 3, y: 3 } })
    await expect.poll(() => reads.at(-1)?.get('sort')).toBe('sku')
    expect(await headers.first().getAttribute('aria-sort')).toBe('descending')
    await headers.first().getByRole('button').press('Enter')
    await expect.poll(() => reads.at(-1)?.get('direction')).toBe('asc')
    expect(starts).toEqual([])
    expect(reads).toHaveLength(3 + await headers.count() * 2)
  } finally { await browser.close() }
}, 30_000)

it('renders full seller totals and aggregate changes without replacing catalog row values or native dynamics', async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block' })
    let starts = 0
    await page.route('**/*', route => {
      const url = new URL(route.request().url()), params = url.searchParams
      if (route.request().resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (url.pathname.endsWith('/refresh')) { starts++; return route.fulfill({ json: { status: 'complete' } }) }
      if (url.pathname.endsWith('/comparison')) return route.fulfill({ json: comparison(params) })
      if (url.pathname.endsWith('/totals-comparison')) return route.fulfill({ json: {
        ...aggregateComparison(params), complete: true, total: 3063, totals: { visits: 900, carts: 0, cartConversion: -90 },
        selectedTotals: { ...metrics(1000), cartConversion: 2 }, previousTotals: { ...metrics(), cartConversion: 20 },
        ready: { visits: true, carts: true, cartConversion: true },
      } })
      return route.fulfill({ json: { ...current(params), totals: { ...metrics(1000), cartConversion: 2 },
        summaryComplete: true, summaryProducts: 3063 } })
    })
    await page.goto('http://localhost/funnel'); await page.addScriptTag({ content: code })
    await expect.poll(() => page.locator('.sf-stage-0 .sf-change').innerText()).toBe('↑ +900%')
    expect(await page.locator('.sf-stage-0 strong').innerText()).toBe('1\u00a0000')
    expect(await page.locator('.sf-stage-rate').innerText()).toContain('CR 2%')
    expect(await page.locator('tbody tr').first().locator('td').nth(2).innerText()).toContain('100')
    expect(await page.locator('tbody tr').first().locator('td').nth(2).innerText()).toContain('↓ −7%')
    expect(starts).toBe(0)
  } finally { await browser.close() }
}, 30_000)

it('finishes selected loading first, reuses native/cached comparisons by nmId and never resyncs on sort/page', async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block' })
    await page.clock.install()
    const starts: URLSearchParams[] = [], reads: string[] = [], errors: string[] = []
    let selectedComplete = false
    let selectedTerminal = false
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', route => {
      const url = new URL(route.request().url()), params = url.searchParams
      if (route.request().resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (url.pathname.endsWith('/refresh')) { starts.push(params); return route.fulfill({ json: { status: 'running' } }) }
      reads.push(url.pathname)
      if (url.pathname.endsWith('/totals-comparison')) return route.fulfill({ json: aggregateComparison(params) })
      return route.fulfill({ json: url.pathname.endsWith('/comparison') ? comparison(params) : {
        ...current(params, selectedComplete), job: { status: selectedTerminal ? 'complete' : 'running' },
      } })
    })
    await page.goto('http://localhost/funnel')
    await page.addStyleTag({ content: css }); await page.addScriptTag({ content: code })
    const table = page.locator('.sf-table')
    await page.getByRole('link', { name: 'SKU-1', exact: true }).waitFor()
    await expect.poll(() => starts.length).toBe(1)
    expect(reads.some(path => path.endsWith('/comparison'))).toBe(false)
    expect(reads.some(path => path.endsWith('/totals-comparison'))).toBe(false)
    expect(await table.locator('tbody tr').first().locator('td').nth(2).innerText()).toContain('100')
    selectedComplete = true
    await page.clock.fastForward(2500)
    await expect.poll(() => reads.length).toBeGreaterThan(1)
    expect(reads.some(path => path.endsWith('/comparison'))).toBe(false) // Full rows do not mean provider pagination has finished.
    expect(reads.some(path => path.endsWith('/totals-comparison'))).toBe(false)
    selectedTerminal = true
    await page.clock.fastForward(2500)
    await expect.poll(() => table.locator('tbody tr').first().locator('td').nth(2).innerText()).toContain('↓ −7%')
    expect(await table.locator('tbody tr').nth(1).locator('td').nth(2).innerText()).toContain('0%')
    expect(await table.locator('tbody tr').first().locator('td').nth(2).locator('.sf-change').getAttribute('title')).toContain('04.10.2026 — 09.10.2026')
    expect(await table.locator('tbody tr').first().locator('td').nth(3).innerText()).toContain('↓ −3%')
    expect(await table.locator('tbody tr').first().locator('td').nth(6).locator('.sf-change').innerText()).toBe('—') // No local fallback despite known base values.
    expect(await page.locator('.sf-stage-rate').innerText()).toContain('CR 20%')
    expect(await page.locator('.sf-stage-rate .sf-change').innerText()).toBe('—')
    await page.getByRole('button', { name: 'Далее', exact: true }).click()
    await page.getByRole('link', { name: 'SKU-3', exact: true }).waitFor()
    await expect.poll(() => table.locator('tbody .sf-change-up').count()).toBeGreaterThan(0)
    await page.getByRole('button', { name: /^Заказали на сумму/ }).click()
    await page.getByRole('link', { name: 'SKU-1', exact: true }).waitFor()
    expect(starts).toHaveLength(1)
    expect(errors).toEqual([])
  } finally { await browser.close() }
}, 30_000)

it('loads missing native dynamics for the exact pair in the same demand view and preserves current values on failure', async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block' })
    await page.clock.install()
    const starts: URLSearchParams[] = []
    let failed = false
    await page.route('**/*', route => {
      const url = new URL(route.request().url()), params = url.searchParams
      if (route.request().resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (url.pathname.endsWith('/refresh')) { starts.push(params); return route.fulfill({ json: { status: 'running' } }) }
      if (url.pathname.endsWith('/comparison')) return failed
        ? route.fulfill({ status: 503, json: { detail: 'Synthetic WB comparison failure' } })
        : route.fulfill({ json: comparison(params, false) })
      if (url.pathname.endsWith('/totals-comparison')) return route.fulfill({ json: aggregateComparison(params) })
      return route.fulfill({ json: current(params) })
    })
    await page.goto('http://localhost/funnel'); await page.addScriptTag({ content: code })
    await page.getByRole('link', { name: 'SKU-1', exact: true }).waitFor()
    await expect.poll(() => starts.length).toBe(1)
    expect(Object.fromEntries(starts[0])).toEqual({ dateFrom: '2026-10-10', dateTo: '2026-10-15', nativeComparison: 'true', from: '2026-10-10', to: '2026-10-15' })
    failed = true
    await page.clock.fastForward(2500)
    await page.getByRole('button', { name: 'Повторить сравнение', exact: true }).waitFor()
    expect(await page.locator('tbody tr').first().locator('td').nth(2).innerText()).toContain('100')
    expect(await page.locator('.sf-state').innerText()).toContain('Данные за выбранный период загружены.')
    expect(starts).toHaveLength(1)
  } finally { await browser.close() }
}, 30_000)

it('ignores an old comparison after dates change, including responses ignoring abort', async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block' })
    // Deliberately ignore fetch's AbortSignal to exercise publication protection.
    await page.addInitScript(() => {
      const original = window.fetch
      window.fetch = (input, init) => original(input, { ...init, signal: undefined })
    })
    let release: (() => void) | undefined
    let oldRequested = false
    const starts: string[] = []
    await page.route('**/*', async route => {
      const url = new URL(route.request().url()), params = url.searchParams
      if (route.request().resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (url.pathname.endsWith('/refresh')) { starts.push(url.search); return route.fulfill({ json: { status: 'complete' } }) }
      if (url.pathname.endsWith('/comparison') && params.get('dateFrom') === '2026-10-10') {
        oldRequested = true
        await new Promise<void>(resolve => { release = resolve })
        return route.fulfill({ json: comparison(params) })
      }
      if (url.pathname.endsWith('/totals-comparison')) return route.fulfill({ json: aggregateComparison(params) })
      return route.fulfill({ json: url.pathname.endsWith('/comparison') ? { ...comparison(params), items: [] } : current(params) })
    })
    await page.goto('http://localhost/funnel'); await page.addScriptTag({ content: code })
    await expect.poll(() => oldRequested).toBe(true)
    await page.getByRole('button', { name: 'Другой период', exact: true }).click()
    await expect.poll(() => page.locator('.sf-comparison-period').innerText()).toContain('12.10.2026 — 18.10.2026')
    await expect.poll(() => page.locator('tbody .sf-change-up').count()).toBe(0)
    release?.()
    await expect.poll(() => page.locator('.sf-period').innerText()).toContain('19.10.2026 — 25.10.2026')
    expect(await page.locator('tbody .sf-change-up').count()).toBe(0)
    expect(starts).toEqual([])
  } finally { await browser.close() }
}, 30_000)

it('reads revised selected values before rendering their matching native dynamics', async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block' })
    let selectedReads = 0
    const starts: string[] = []
    await page.route('**/*', route => {
      const url = new URL(route.request().url()), params = url.searchParams
      if (route.request().resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (url.pathname.endsWith('/refresh')) { starts.push(url.search); return route.fulfill({ json: { status: 'complete' } }) }
      if (url.pathname.endsWith('/comparison')) {
        const data = comparison(params)
        data.items = data.items.map(item => item.nmId === '1' ? { ...item, selectedValues: metrics(101) } : item)
        return route.fulfill({ json: data })
      }
      if (url.pathname.endsWith('/totals-comparison')) {
        const data = aggregateComparison(params)
        if (selectedReads > 1) data.selectedTotals = metrics(101)
        return route.fulfill({ json: data })
      }
      selectedReads += 1
      const data = current(params)
      if (selectedReads > 1) data.items[0].metrics = metrics(101)
      return route.fulfill({ json: data })
    })
    await page.goto('http://localhost/funnel'); await page.addScriptTag({ content: code })
    const cell = page.locator('tbody tr').first().locator('td').nth(2)
    await expect.poll(() => cell.innerText()).toContain('101')
    await expect.poll(() => cell.innerText()).toContain('↓ −7%')
    expect(selectedReads).toBe(2)
    expect(starts).toEqual([])
  } finally { await browser.close() }
}, 30_000)

it('does not treat a failed selected job as completion even when saved rows are full', async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block' })
    await page.clock.install()
    let finished = false, comparisons = 0
    const starts: URLSearchParams[] = []
    await page.route('**/*', route => {
      const url = new URL(route.request().url()), params = url.searchParams
      if (route.request().resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (url.pathname.endsWith('/refresh')) { starts.push(params); return route.fulfill({ json: { status: 'running' } }) }
      if (url.pathname.endsWith('/comparison')) { comparisons += 1; return route.fulfill({ json: comparison(params) }) }
      if (url.pathname.endsWith('/totals-comparison')) return route.fulfill({ json: aggregateComparison(params) })
      return route.fulfill({ json: { ...current(params), job: { status: finished ? 'complete' : 'failed' } } })
    })
    await page.goto('http://localhost/funnel'); await page.addScriptTag({ content: code })
    await expect.poll(() => starts.length).toBe(1)
    expect(starts[0].has('nativeComparison')).toBe(false)
    expect(comparisons).toBe(0)
    expect(await page.locator('tbody tr').first().locator('td').nth(2).innerText()).toContain('100')
    finished = true
    await page.clock.fastForward(2500)
    await expect.poll(() => comparisons).toBe(1)
    expect(starts).toHaveLength(1)
  } finally { await browser.close() }
}, 30_000)

it('loads missing aggregate history in the selected demand view without delaying native product dynamics', async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block' })
    await page.clock.install()
    let available = false
    const starts: URLSearchParams[] = [], views: string[] = []
    await page.route('**/*', route => {
      const url = new URL(route.request().url()), params = url.searchParams
      if (route.request().resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (url.pathname.endsWith('/refresh')) {
        starts.push(params); views.push(route.request().headers()['x-wb-view'])
        return route.fulfill({ json: { status: 'running' } })
      }
      if (url.pathname.endsWith('/comparison')) return route.fulfill({ json: comparison(params) })
      if (url.pathname.endsWith('/totals-comparison')) {
        views.push(route.request().headers()['x-wb-view'])
        return route.fulfill({ json: { ...aggregateComparison(params), complete: available, refreshRequired: !available,
          job: { status: available ? 'complete' : 'running' },
          totals: available ? { visits: 100 / 3, carts: -20, orders: 0, buyouts: -100, cartConversion: 20 } : {},
          previousTotals: { visits: 75, carts: 25, orders: 8, buyouts: 10, cartConversion: 20 / 1.2 },
          ready: Object.fromEntries(['visits', 'carts', 'orders', 'buyouts', 'cartConversion'].map(key => [key, available])),
        } })
      }
      return route.fulfill({ json: current(params) })
    })
    await page.goto('http://localhost/funnel'); await page.addScriptTag({ content: code })
    await expect.poll(() => starts.length).toBe(1)
    expect(Object.fromEntries(starts[0])).toEqual({ dateFrom: '2026-10-10', dateTo: '2026-10-15', aggregateComparison: 'true', from: '2026-10-10', to: '2026-10-15' })
    expect(new Set(views).size).toBe(1)
    expect(views[0]).toBeTruthy()
    await expect.poll(() => page.locator('tbody tr').first().locator('td').nth(2).innerText()).toContain('↓ −7%')
    expect(await page.locator('.sf-stage-0').innerText()).toContain('100')
    expect(await page.locator('.sf-stage-0 .sf-change').innerText()).toBe('…')
    available = true
    await page.clock.fastForward(2500)
    await expect.poll(() => page.locator('.sf-stage-0 .sf-change').innerText()).toBe('↑ +33,33%')
    expect(await page.locator('.sf-stage-rate .sf-change').innerText()).toBe('↑ +20%')
    expect(await page.locator('.sf-stage-0 .sf-change').getAttribute('title')).toContain('Динамика рассчитана из полных итогов WB обоих периодов, без ограничения локальным каталогом')
    expect(await page.locator('tbody tr').first().locator('td').nth(2).locator('.sf-change').getAttribute('title')).toContain('Динамика передана API WB')
    await page.getByRole('button', { name: 'Далее', exact: true }).click()
    await page.getByRole('link', { name: 'SKU-3', exact: true }).waitFor()
    expect(starts).toHaveLength(1)
  } finally { await browser.close() }
}, 30_000)

it('keeps current totals and native row dynamics visible when aggregate comparison fails', async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block' })
    const starts: string[] = []
    await page.route('**/*', route => {
      const url = new URL(route.request().url()), params = url.searchParams
      if (route.request().resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (url.pathname.endsWith('/refresh')) { starts.push(url.search); return route.fulfill({ json: { status: 'complete' } }) }
      if (url.pathname.endsWith('/totals-comparison')) return route.fulfill({ status: 503, json: { detail: 'Synthetic aggregate failure' } })
      return route.fulfill({ json: url.pathname.endsWith('/comparison') ? comparison(params) : current(params) })
    })
    await page.goto('http://localhost/funnel'); await page.addScriptTag({ content: code })
    await page.getByRole('button', { name: 'Повторить сравнение итогов', exact: true }).waitFor()
    expect(await page.locator('.sf-stage-0').innerText()).toContain('100')
    expect(await page.locator('tbody tr').first().locator('td').nth(2).innerText()).toContain('↓ −7%')
    expect(starts).toEqual([])
  } finally { await browser.close() }
}, 30_000)

it('rejects delayed aggregate results after dates change even if fetch ignores abort', async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block' })
    await page.addInitScript(() => {
      const original = window.fetch
      window.fetch = (input, init) => original(input, { ...init, signal: undefined })
    })
    let release: (() => void) | undefined
    let oldRequested = false
    await page.route('**/*', async route => {
      const url = new URL(route.request().url()), params = url.searchParams
      if (route.request().resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (url.pathname.endsWith('/totals-comparison')) {
        const old = params.get('dateFrom') === '2026-10-10'
        if (old) {
          oldRequested = true
          await new Promise<void>(resolve => { release = resolve })
        }
        return route.fulfill({ json: { ...aggregateComparison(params), complete: true,
          totals: { visits: old ? 999 : 12.5 }, ready: { visits: true },
        } })
      }
      return route.fulfill({ json: url.pathname.endsWith('/comparison') ? comparison(params) : current(params) })
    })
    await page.goto('http://localhost/funnel'); await page.addScriptTag({ content: code })
    await expect.poll(() => oldRequested).toBe(true)
    await page.getByRole('button', { name: 'Другой период', exact: true }).click()
    await expect.poll(() => page.locator('.sf-stage-0 .sf-change').innerText()).toBe('↑ +12,5%')
    release?.()
    await expect.poll(() => page.locator('.sf-period').innerText()).toContain('19.10.2026 — 25.10.2026')
    expect(await page.locator('.sf-stage-0 .sf-change').innerText()).toBe('↑ +12,5%')
  } finally { await browser.close() }
}, 30_000)
