import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { beforeAll, expect, it } from 'vitest'

let code = ''
beforeAll(async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({ configFile: false, envFile: false, root, logLevel: 'silent', plugins: [
    { name: 'sorting-auth-fixture', enforce: 'pre', transform(source, id) {
      if (id.endsWith('/__fixtures__/reportLoadingBrowser.tsx')) return source.replace('cabinetMe: null', "cabinetMe: ({ organization: { organizationId: 9 }, user: { userId: 1, permissions: [] } } as AuthContextValue['cabinetMe'])")
    } }, react()],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""' },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: { entry: fileURLToPath(new URL('./__fixtures__/reportLoadingBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'WbSortRegression' } } })
  const chunk = (Array.isArray(result) ? result : [result]).flatMap(item => 'output' in item ? item.output : []).find(item => item.type === 'chunk' && item.isEntry)
  if (!chunk || chunk.type !== 'chunk') throw new Error('Missing WB sorting fixture')
  code = chunk.code
}, 60_000)

it('P&L keeps finance during campaign loading and ignores an old response after real date changes', async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const page = await browser.newPage({ viewport: { width: 1600, height: 1000 }, serviceWorkers: 'block' })
    await page.addInitScript(() => {
      if (!crypto.randomUUID) Object.defineProperty(crypto, 'randomUUID', { value: () => '00000000-0000-4000-8000-000000000001' })
    })
    await page.clock.install({ time: new Date('2026-10-29T12:00:00Z') })
    let campaignsReady = false
    let releaseOld: (() => Promise<unknown>) | undefined
    const starts: string[] = [], errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    const report = (from: string, to: string, fresh: boolean) => ({
      dateRange: { from, to }, profitBasis: 'wb_financial_report', cache: { fresh },
      meta: { freshnessState: fresh ? 'fresh' : 'partial', sourceType: 'financial', dateRange: { from, to } },
      blockerIds: fresh ? [] : ['WB-02'], rows: [
        { rowId: 'nm-101', nmId: 101, sku: `PENDING-${from}`, label: 'Campaign product', revenueKopecks: 10000,
          cogsKopecks: 1000, commissionKopecks: 100, adSpendKopecks: fresh ? 400 : null,
          taxKopecks: 0, netProfitKopecks: fresh ? 8500 : null, marginPct: fresh ? 85 : null },
        { rowId: 'nm-102', nmId: 102, sku: 'KNOWN', label: 'Known product', revenueKopecks: 10000,
          cogsKopecks: 1000, commissionKopecks: 100, adSpendKopecks: 0, taxKopecks: 0,
          netProfitKopecks: 8900, marginPct: 89 },
      ],
    })
    await page.route('**/*', route => {
      const request = route.request(), url = new URL(request.url())
      if (request.resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (request.resourceType() === 'image') return route.abort()
      if (url.hostname === 'fonts.googleapis.com') return route.fulfill({ contentType: 'text/css', body: '' })
      const from = url.searchParams.get('from') ?? '', to = url.searchParams.get('to') ?? ''
      if (url.pathname === '/api/wb/reports/pnl/latest-cache') {
        if (from === '2026-10-18') {
          releaseOld = () => route.fulfill({ json: report(from, to, true) }).catch(() => undefined)
          return
        }
        return route.fulfill({ json: report(from, to, from === '2026-10-12' ? campaignsReady : true) })
      }
      if (url.pathname === '/api/wb/reports/pnl/jobs') {
        if (request.method() === 'POST') starts.push(from)
        return route.fulfill({ json: { state: campaignsReady ? 'completed' : 'running', stage: 'ads-fullstats' } })
      }
      return route.fulfill({ json: { data: [], items: [] } })
    })
    await page.goto('http://satorna.test/wb/reports/pnl')
    await page.evaluate(() => { window.__vellaReportPeriods = { pnl: { days: 7, mode: 'custom', fromIso: '2026-10-12', toIso: '2026-10-18', label: 'P&L test' } } })
    await page.addScriptTag({ content: code })
    const table = page.locator('#tab-pnl [data-vella-runtime-binding="backend-pnl"] table')
    const rows = table.locator('tr[data-report-row="pnl"]')
    try { await expect.poll(() => rows.count(), { timeout: 10000 }).toBe(2) } catch (error) {
      throw new Error(`${String(error)} ${JSON.stringify({ starts, errors, page: await page.locator('#tab-pnl').innerText() })}`)
    }
    const profitColumn = await table.getByRole('columnheader', { name: /^Прибыль/ }).evaluate((th: HTMLTableCellElement) => th.cellIndex)
    const pending = rows.filter({ hasText: 'PENDING-2026-10-12' })
    expect(await pending.locator('td').nth(profitColumn).innerText()).toBe('нет данных')
    expect(await rows.filter({ hasText: 'KNOWN' }).locator('td').nth(profitColumn).innerText()).toContain('89')
    campaignsReady = true
    await page.clock.runFor(20000)
    await expect.poll(() => pending.locator('td').nth(profitColumn).innerText()).toBe('85 ₽')
    expect(starts).toEqual(['2026-10-12'])
    async function select(from: string, to: string) {
      await page.locator('#globalPeriodRange').fill(from)
      await page.locator('#globalPeriodRangeTo').fill(to)
      await page.getByRole('button', { name: 'Применить', exact: true }).click()
    }
    await select('2026-10-18', '2026-10-18')
    await expect.poll(() => Boolean(releaseOld)).toBe(true)
    await select('2026-10-19', '2026-10-25')
    await rows.filter({ hasText: 'PENDING-2026-10-19' }).waitFor()
    await releaseOld!()
    await page.clock.runFor(2000)
    expect(await table.innerText()).toContain('PENDING-2026-10-19')
    expect(await table.innerText()).not.toContain('PENDING-2026-10-18')
    expect(errors).toEqual([])
  } finally { await browser.close() }
}, 60_000)

it.each(['products', 'repricer-stats'] as const)('preserves %s sorting through real header clicks, filters and a source refresh', async tab => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const page = await browser.newPage({ viewport: { width: 1600, height: 1000 }, serviceWorkers: 'block' })
    await page.addInitScript(() => {
      if (!crypto.randomUUID) Object.defineProperty(crypto, 'randomUUID', { value: () => '00000000-0000-4000-8000-000000000001' })
    })
    await page.clock.install({ time: new Date('2026-10-21T12:00:00Z') })
    let reads = 0
    const writes: string[] = [], errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    const items = [12, 2, 10].map((value, index) => ({
      articleId: `SKU-${value}`, nmId: 100 + index, name: `Товар ${value}`, managerName: `Manager ${value}`,
      meta: { articleId: `SKU-${value}`, nmId: 100 + index, name: `Товар ${value}`, status: 'auto', currentPriceKopecks: value * 10000, basketsLast7d: value, basketNorm: 1 },
      settings: { cogsKopecks: 80000, minMarginPct: 15, wbCommissionPct: 15 },
      analytics: { financeState: 'ok', basketsState: 'ok', baskets: value, ordersUnits: value, periodStatsState: 'ok', stockState: 'ok', wbStockUnits: value,
        grossSalesUnits: value, grossSalesKopecks: (20 - value) * 10000, ordersKopecks: (20 - value) * 10000 },
      metrics: Object.fromEntries(['baskets', 'totalImpressions', 'impressions', 'totalClicks', 'clicks', 'adCartAdds', 'ctrPct', 'cartToOrderCrPct', 'orders', 'adSpendKopecks', 'drrPct', 'averagePriceKopecks'].map(key => [key, index === 2 ? null : value])),
      sources: {}, decision: { label: `Action ${value}` }, priceProtection: {},
    }))
    await page.route('**/*', route => {
      const request = route.request(), url = new URL(request.url())
      if (request.resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (request.resourceType() === 'image') return route.fulfill({ contentType: 'image/gif', body: Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64') })
      if (url.hostname === 'fonts.googleapis.com') return route.fulfill({ contentType: 'text/css', body: '' })
      if (request.method() !== 'GET') { writes.push(url.pathname); return route.abort() }
      if (url.pathname === '/api/v1/wb-repricer/sku' || url.pathname === '/api/v1/wb-repricer/stats') {
        reads++
        return route.fulfill({ json: { items, total: items.length, itemsReturned: items.length, page: 1, pageSize: 150,
          dateFrom: url.searchParams.get('dateFrom'), dateTo: url.searchParams.get('dateTo'), summary: { skuCount: items.length }, cache: { totalCached: items.length, pagesCached: 1 } } })
      }
      return route.fulfill({ json: { items: [], total: 0, state: 'completed', running: false, steps: [] } })
    })
    await page.goto(`http://satorna.test/wb/repricer${tab === 'products' ? '' : '/stats'}`)
    await page.addScriptTag({ content: code })
    const table = page.locator(tab === 'products' ? '#mainTable' : '#tab-repricer-stats table').first()
    const bodyRows = table.locator(tab === 'products' ? 'tbody tr[data-sku]' : 'tbody tr[data-article-id]')
    await expect.poll(() => bodyRows.count(), { timeout: 15000 }).toBe(3)
    await page.clock.runFor(1500)
    const headers = table.locator(tab === 'products' ? 'thead th[data-sort-key]' : 'thead th[data-stats-column]')
    const order = () => bodyRows.evaluateAll(nodes => nodes.map(node => (node as HTMLElement).dataset.sku ?? (node as HTMLElement).dataset.articleId))
    const originals = await bodyRows.evaluateAll(nodes => Object.fromEntries(nodes.map(node => [(node as HTMLElement).dataset.sku ?? (node as HTMLElement).dataset.articleId, node.textContent])))
    const readsBeforeSort = reads
    const writesBeforeSort = writes.length
    for (let column = 0; column < await headers.count(); column++) {
      const header = headers.nth(column), key = (await header.getAttribute(tab === 'products' ? 'data-sort-key' : 'data-stats-column'))!
      const firstDirection = await header.getAttribute('aria-sort') === 'descending' ? 'asc' : 'desc'
      for (const direction of [firstDirection, firstDirection === 'desc' ? 'asc' : 'desc'] as const) {
        const expected = tab === 'products' ? await page.evaluate(({ key, direction }) => window.eval(
          `PRODUCTS.slice().sort((a,b)=>compareReportValues(productSortValue(a,${JSON.stringify(key)}),productSortValue(b,${JSON.stringify(key)}),${JSON.stringify(direction)})).map(p=>p.sku)`,
        ), { key, direction }) : [...items].sort((a, b) => {
          const value = (item: typeof items[number]) => key === 'product' ? item.articleId : key === 'manager' ? item.managerName : key === 'action' ? item.decision.label : item.metrics[key]
          const x = value(a), y = value(b)
          if (x == null || y == null) return x == null && y == null ? 0 : x == null ? 1 : -1
          const delta = typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y), 'ru', { numeric: true })
          return direction === 'asc' ? delta : -delta
        }).map(item => item.articleId)
        await header.locator('.sort-arrow').click()
        await page.clock.runFor(32)
        await expect.poll(order, { message: `${tab} ${key} ${direction}` }).toEqual(expected)
        await expect.poll(() => header.getAttribute('aria-sort'), { message: `${tab} ${key} ${direction} indicator` }).toBe(direction === 'asc' ? 'ascending' : 'descending')
      }
    }
    expect(reads).toBe(readsBeforeSort)
    expect(writes).toHaveLength(writesBeforeSort)
    const cells = await bodyRows.evaluateAll(nodes => Object.fromEntries(nodes.map(node => [(node as HTMLElement).dataset.sku ?? (node as HTMLElement).dataset.articleId, node.textContent])))
    expect(cells).toEqual(originals)
    const sortedHeader = headers.last(), selectedOrder = await order(), selectedDirection = await sortedHeader.getAttribute('aria-sort')
    const help = sortedHeader.locator('.stat-tip, .tip-i').first()
    if (await help.count()) {
      await help.click(); expect(await order()).toEqual(selectedOrder)
      expect(await sortedHeader.getAttribute('aria-sort')).toBe(selectedDirection)
    }
    if (tab === 'repricer-stats') {
      await page.evaluate(() => window.__vellaLoadLiveRepricerStats!())
      expect(await order()).toEqual(selectedOrder)
      expect(await sortedHeader.getAttribute('aria-sort')).toBe(selectedDirection)
    } else {
      await page.evaluate(async () => { await window.__vellaLoadLiveRepricerProducts!() })
      await page.clock.runFor(32)
      expect(await order()).toEqual(selectedOrder)
      expect(await sortedHeader.getAttribute('aria-sort')).toBe(selectedDirection)
    }
    const search = page.locator(tab === 'products' ? '#searchTable' : '#tab-repricer-stats .search input')
    await search.fill('SKU-12'); await page.clock.runFor(500)
    await expect.poll(order).toEqual(['SKU-12'])
    await search.fill(''); await page.clock.runFor(500)
    await expect.poll(order).toEqual(selectedOrder)
    expect(errors).toEqual([])
  } finally { await browser.close() }
}, 60_000)

const rows = Array.from({ length: 61 }, (_, index) => {
  const value = index === 60 ? 700 : index === 0 ? 650 : index % 9 - 2
  const money = value * 100
  return { sku: `SORT-${index}`, articleId: `SORT-${index}`, nmId: 500000000 + index,
    productName: `Product ${index}`, category: `Group ${index}`, color: `Color ${index}`, strategyName: `Strategy ${index}`,
    manager: `Manager ${index}`, managerName: `Manager ${index}`, managerId: 'unassigned', photoUrl: null,
    productStatus: 'normal', abcCode: 'AA', promotionStatus: 'no', sourceStatus: 'ready', confidence: value,
    priceBeforeSppKopecks: money, priceKopecks: money, sppPct: value, cogsPerUnitKopecks: money,
    marginKopecks: money, marginPct: 1000 - value, netTotalKopecks: money, netProfitKopecks: money,
    cogsKopecks: money, revenueKopecks: money, commissionKopecks: money, logisticsKopecks: money,
    storageKopecks: money, penaltyKopecks: money, acceptanceKopecks: money, taxKopecks: money,
    logisticsCostPct: 1000 - value, commissionCostPct: 1000 - value, storageCostPct: 1000 - value,
    ordersComposite: { units: value, kopecks: (1000 - value) * 100 }, salesComposite: { units: value, kopecks: (1000 - value) * 100 },
    baskets: { units: value, deltaPct: 999 - value }, clicks: value, cartCount: value, openCount: value,
    cartCrPct: value, atcrPct: value, cartToOrderPct: value, wishlistCount: value, buyoutPct: value,
    adCartAdds: value, adOrders: value, adImpressions: value, adClicks: value, adCtrPct: 1000 - value,
    adSpendKopecks: money, drrPct: value, drrSalesPct: 1000 - value, organicOrderCountEstimated: value,
    campaignId: 1000 + index, campaignName: `Campaign ${index}`, campaignStatus: '9', attributionLevel: 'exact_sku',
    impressions: value, ctrPct: value, budgetTotalKopecks: money, budgetFresh: true, budgetFetchedAt: '2026-10-21T11:59:00Z',
    warehouseName: `Warehouse ${index}`, totalStockUnits: value, wbStockUnits: value, stockUnits: value,
    fromClientUnits: value, toClientUnits: value, availableUnits: value, ordersPerDay: value, daysToOos: value,
    ktrIndex: value, localizationPct: value, logisticsPerUnitKopecks: money, decision: `Decision ${index}`, comment: `Comment ${index}`,
    orders: { units: value, kopecks: (1000 - value) * 100, deltaPct: 1000 - value },
    sales: { units: value, kopecks: (1000 - value) * 100, deltaPct: 1000 - value },
    marginPctMetric: { percent: value }, profit: { kopecks: money, deltaPct: 1000 - value },
    stockSnapshotCoveragePct: value, stockAvailability7d: [true, true, false], stockOutDays: value, conclusion: `Conclusion ${index}`,
  }
})

it.each(['abc', 'rnp', 'pnl', 'ads', 'stock', 'week'] as const)('sorts every %s header in both directions through real clicks without changing cells or reloading reports', async tab => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const page = await browser.newPage({ viewport: { width: 1600, height: 1000 }, serviceWorkers: 'block' })
    await page.addInitScript(() => {
      if (!crypto.randomUUID) Object.defineProperty(crypto, 'randomUUID', { value: () => '00000000-0000-4000-8000-000000000001' })
    })
    await page.clock.install({ time: new Date('2026-10-21T12:00:00Z') })
    const routeName = tab === 'week' ? 'week-over-week' : tab
    const reads: string[] = [], mutations: string[] = [], errors: string[] = [], apiGets: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', route => {
      const request = route.request(), url = new URL(request.url())
      if (request.resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (request.resourceType() === 'image') return route.fulfill({ contentType: 'image/gif', body: Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64') })
      if (url.hostname === 'fonts.googleapis.com') return route.fulfill({ contentType: 'text/css', body: '' })
      if (request.method() === 'POST' && url.pathname === '/api/wb/reports/ads/budgets') return route.fulfill({ json: { state: 'completed', items: [] } })
      if (request.method() !== 'GET') { mutations.push(url.pathname); return route.abort() }
      if (url.pathname.startsWith('/api/')) apiGets.push(url.pathname)
      if (url.pathname === `/api/wb/reports/${routeName}/latest-cache`) {
        reads.push(url.search)
        const source = (tab === 'abc' ? [rows[0], rows[1], rows[60]] : rows).map(row => ({ ...row, baskets: tab === 'week' ? row.baskets : row.cartCount,
          marginPct: tab === 'week' ? row.marginPctMetric : row.marginPct }))
        return route.fulfill({ json: { meta: { freshnessState: 'fresh', sourceType: 'operational' }, cache: { fresh: true },
          filters: { dateRange: { from: url.searchParams.get('from'), to: url.searchParams.get('to') } }, rows: source, reportJob: null } })
      }
      return route.fulfill({ json: { data: [], days: [] } })
    })
    await page.goto(`http://satorna.test/wb/reports/${routeName}`)
    await page.evaluate(tabId => { window.__vellaReportPeriods = { [tabId]: { days: 7, mode: 'custom', fromIso: '2026-10-12', toIso: '2026-10-18', label: 'Synthetic sort period' } } }, tab)
    await page.addScriptTag({ content: code })
    const surface = page.locator(`#tab-${tab}`), table = surface.locator('.report-table-wrap table').first()
    const data = table.locator('tbody tr[data-report-row]')
    try { await expect.poll(() => data.count(), { timeout: 5000, message: `Loaded ${tab} rows` }).toBe(tab === 'abc' ? 3 : 50) } catch (error) {
      throw new Error(`${String(error)}\n${JSON.stringify({ apiGets, reads, errors, state: await surface.innerText() })}`, { cause: error })
    }
    if (tab !== 'abc') {
      await surface.getByRole('button', { name: /^Показать ещё/ }).click()
      await expect.poll(() => data.count()).toBe(61)
    }
    if (tab === 'abc') await page.waitForFunction(() => !window.__vellaAbcLiveLoading)
    // Finish the shell's existing deferred initialization (including its
    // 720 ms enhancer replay) before measuring requests caused by clicks.
    await page.clock.runFor(1500)
    if (tab === 'abc') await page.waitForFunction(() => !window.__vellaAbcLiveLoading)
    const readsBeforeSorting = reads.length
    await surface.evaluate(tab => { tab.scrollTop = 0; tab.querySelectorAll('.report-table-wrap').forEach(wrap => { wrap.scrollTop = 0; wrap.scrollLeft = 0 }) })
    const baseline = await data.evaluateAll(nodes => nodes.map(row => ({
      id: (row as HTMLTableRowElement).cells[0].textContent!.match(/SORT-\d+/)![0],
      cells: [...(row as HTMLTableRowElement).cells].map(cell => {
        const clone = cell.cloneNode(true) as HTMLElement
        clone.querySelectorAll('.sub, .subline, small, .stat-tip, .rnp-help-tip').forEach(node => node.remove())
        return { text: clone.innerText || clone.textContent || '', numeric: cell.classList.contains('num'), content: cell.textContent }
      }),
    })))
    const originals = new Map(baseline.map(row => [row.id, row.cells.map(cell => cell.content)]))
    const collator = new Intl.Collator('ru', { numeric: true, sensitivity: 'base' })
    const headers = table.locator('thead th'), count = await headers.count()
    const idOrder = () => data.evaluateAll(nodes => nodes.map(row => (row as HTMLTableRowElement).cells[0].textContent!.match(/SORT-\d+/)![0]))
    for (let column = 0; column < count; column++) {
      const key = (row: typeof baseline[number]) => {
        const cell = row.cells[column], text = cell.text.trim()
        if (!text || /^(—|нет данных)$/.test(text)) return null
        if (!cell.numeric) return text
        const match = text.match(/^[↑↓]?\s*([+−–-]?\s*\d[\d\s\u00a0\u202f]*(?:[.,]\d+)?)/)
        return match ? Number(match[1].replace(/\s/g, '').replace(/[−–]/g, '-').replace(',', '.')) : null
      }
      for (const direction of ['desc', 'asc'] as const) {
        await headers.nth(column).locator('.sort-arrow').click()
        if (tab === 'abc' && await surface.getByRole('button', { name: /^Показать ещё/ }).count()) {
          await surface.getByRole('button', { name: /^Показать ещё/ }).click()
        }
        const expected = [...baseline].sort((a, b) => {
          const x = key(a), y = key(b)
          if (x == null || y == null) return x == null && y == null ? 0 : x == null ? 1 : -1
          const diff = typeof x === 'number' && typeof y === 'number' ? x - y : collator.compare(String(x), String(y))
          return direction === 'asc' ? diff : -diff
        }).map(row => row.id)
        await expect.poll(idOrder, { message: `${tab} column ${column} ${direction}` }).toEqual(expected)
        expect(await headers.nth(column).getAttribute('aria-sort')).toBe(direction === 'asc' ? 'ascending' : 'descending')
        expect(await headers.nth(column).locator('.sort-arrow').innerText()).toBe(direction === 'asc' ? '↑' : '↓')
      }
    }
    const after = await data.evaluateAll(nodes => nodes.map(row => ({ id: (row as HTMLTableRowElement).cells[0].textContent!.match(/SORT-\d+/)![0], cells: [...(row as HTMLTableRowElement).cells].map(cell => cell.textContent) })))
    after.forEach(row => expect(row.cells).toEqual(originals.get(row.id)))
    const first = headers.first(), help = first.locator('.stat-tip, .rnp-help-tip').first()
    if (await help.count()) { const before = await idOrder(); await help.click(); expect(await idOrder()).toEqual(before) }
    await first.focus(); await first.press('Enter')
    expect(await first.getAttribute('aria-sort')).toBe('descending')
    await first.press('Space')
    expect(await first.getAttribute('aria-sort')).toBe('ascending')
    const search = surface.locator('.search input').first()
    if (await search.count()) {
      await search.fill('SORT-60')
      await expect.poll(() => data.filter({ visible: true }).count()).toBe(1)
      expect(await data.filter({ visible: true }).first().innerText()).toContain('SORT-60')
      await search.fill('')
      await expect.poll(() => data.filter({ visible: true }).count()).toBeGreaterThan(1)
      expect(await first.getAttribute('aria-sort')).toBe('ascending')
    }
    expect(reads).toHaveLength(readsBeforeSorting)
    expect(mutations).toEqual([]); expect(errors).toEqual([])
    if (tab === 'abc') {
      const ordered = await idOrder()
      await page.evaluate(async () => { await window.__vellaLoadLiveAbcReport!() })
      expect(await idOrder()).toEqual(ordered)
      expect(await first.getAttribute('aria-sort')).toBe('ascending')
    }
  } finally { await browser.close() }
}, 60_000)
