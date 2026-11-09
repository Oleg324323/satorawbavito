// Real UI components; every request intercepted, no backend/server/account used.
import fs from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { chromium, type Page } from 'playwright'
import { afterEach, beforeAll, expect, it } from 'vitest'

const article = 'synthetic-101', other = 'synthetic-102', third = 'synthetic-103'
const catalog = [
  ['stockout_guard', 'Защита out of stock'], ['turnover_control', 'Контроль оборачиваемости'],
  ['optimal_price', 'Оптимальная цена'], ['schedule', 'Расписание'],
  ['plan_fact_group', 'План-факт на период групповой'],
].map(([id, name]) => ({ id, name, type: 'stock', color: '#2563eb', status: 'active', rules: [] }))
type Config = Record<string, unknown>
type Assignment = { id: string; config: Config }
type Group = { groupId: string; groupName: string; articleIds: string[]; planOrders: number }
let code = '', css = ''
let currentErrors: string[] = []
afterEach(() => expect(currentErrors).toEqual([]))
beforeAll(async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({ root, configFile: false, envFile: false, publicDir: false, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""' },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: { entry: fileURLToPath(new URL('./__fixtures__/repricerFourFixesBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'RepricerFourFixes' } } })
  const output = (Array.isArray(result) ? result : [result]).flatMap(item => 'output' in item ? item.output : [])
  const entry = output.find(item => item.type === 'chunk' && item.isEntry)
  if (!entry || entry.type !== 'chunk') throw new Error('Missing browser bundle')
  code = entry.code
  css = output.filter(item => item.type === 'asset' && item.fileName.endsWith('.css')).map(item => item.type === 'asset' ? String(item.source) : '').join('\n')
}, 60000)

async function setup(options: { assignments?: Record<string, Assignment>; groups?: Group[]; events?: Config[]; at?: string; route?: string; serverSearch?: boolean } = {}) {
  const browser = await chromium.launch({ headless: true })
  const page = await browser.newPage({ viewport: { width: 1800, height: 1000 }, timezoneId: 'America/Los_Angeles', serviceWorkers: 'block' })
  page.setDefaultTimeout(5000)
  const state = {
    assignments: options.assignments ?? { [article]: { id: 'stockout_guard', config: {} } },
    groups: options.groups ?? [], writes: [] as Array<{ articleIds: string[]; strategyName: string; config?: Config }>,
    previews: [] as Array<{ articleIds: string[]; strategyId: string; config?: Config }>, errors: [] as string[],
    automation: { [article]: true } as Record<string, boolean>, automationWrites: [] as Config[],
    skuQueries: [] as string[],
    skuSessions: [] as string[],
    skuRequests: [] as string[], automationFailures: {} as Record<string, number>,
    delayAutomation: false, releaseAutomation: undefined as undefined | (() => Promise<void>),
    delayNextSku: false, releaseSku: undefined as undefined | (() => Promise<void>),
    delayNextAssignment: false, releaseAssignment: undefined as undefined | (() => Promise<void>),
    delayNext: false, release: undefined as undefined | (() => Promise<void>),
  }
  for (const id of Object.keys(state.assignments)) state.automation[id] = true
  currentErrors = state.errors
  page.on('pageerror', error => state.errors.push(error.message))
  await page.route('**/*', async route => {
    const req = route.request(), url = new URL(req.url())
    if (req.resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
    if (req.resourceType() === 'image') return route.fulfill({ status: 204 })
    if (url.hostname === 'fonts.googleapis.com') return route.fulfill({ body: '' })
    if (url.pathname === '/api/v1/wb-repricer/strategies/catalog') return route.fulfill({ json: { items: catalog, total: catalog.length } })
    if (url.pathname === '/api/v1/wb-repricer/sku-groups') {
      expect(req.method()).toBe('GET')
      return route.fulfill({ json: { items: state.groups, total: state.groups.length } })
    }
    if (url.pathname === '/api/v1/wb-repricer/strategy-assignments/bulk') {
      const body = req.postDataJSON(); state.writes.push(body)
      const strategy = catalog.find(item => item.id === body.strategyName || item.name === body.strategyName)!
      const complete = async () => {
        for (const id of body.articleIds) { state.assignments[id] = { id: strategy.id, config: body.config ?? {} }; state.automation[id] = true }
        await route.fulfill({ json: { strategy, assignedCount: body.articleIds.length, items: [] } })
      }
      if (state.delayNextAssignment) {
        state.delayNextAssignment = false
        return new Promise<void>(resolve => { state.releaseAssignment = async () => { await complete(); resolve() } })
      }
      return complete()
    }
    const automationMatch = url.pathname.match(/^\/api\/v1\/wb-repricer\/sku\/([^/]+)\/automation$/)
    if (automationMatch && req.method() === 'PATCH') {
      const id = decodeURIComponent(automationMatch[1]), body = req.postDataJSON()
      state.automationWrites.push({ articleId: id, ...body })
      expect(Object.keys(state.assignments)).toContain(id)
      const complete = async () => {
        const status = state.automationFailures[id]
        if (status) return route.fulfill({ status, json: { detail: 'Synthetic save denied' } })
        state.automation[id] = body.automationEnabled
        return route.fulfill({ json: { automationEnabled: body.automationEnabled, status: body.automationEnabled ? 'auto' : 'manual' } })
      }
      if (state.delayAutomation) {
        state.delayAutomation = false
        return new Promise<void>(resolve => { state.releaseAutomation = async () => { await complete(); resolve() } })
      }
      return complete()
    }
    if (url.pathname === '/api/v1/wb-repricer/strategies/preview') {
      const body = req.postDataJSON(); state.previews.push(body)
      const complete = async () => route.fulfill({ json: { runId: 'synthetic-preview', executedCount: body.articleIds.length, skippedCount: 0, blockedCount: 0,
        items: body.articleIds.map((id: string) => ({ articleId: id, frontendStrategyId: body.strategyId, status: 'executed',
          oldPriceKopecks: 172000, recommendedPriceKopecks: body.strategyId === 'turnover_control' ? 161680 : 177160,
          explanation: `Selected strategy ${body.strategyId}` })) } })
      if (state.delayNext) {
        state.delayNext = false
        return new Promise<void>(resolve => { state.release = async () => { await complete(); resolve() } })
      }
      return complete()
    }
    if (url.pathname === '/api/v1/wb-repricer/changelog') {
      const from = url.searchParams.get('from')
      const items = (options.events ?? []).filter(item => !from || Date.parse(String(item.timestamp)) >= Date.parse(from))
      return route.fulfill({ json: { items, total: items.length, page: 1, limit: 50 } })
    }
    if (url.pathname === '/api/v1/wb-repricer/sku') {
      state.skuSessions.push(req.headers().authorization ?? '')
      const query = url.searchParams.get('q') ?? ''; state.skuQueries.push(query); state.skuRequests.push(url.search)
      const rows = Object.entries(state.assignments).filter(([id]) => !options.serverSearch || id === (query === '101' ? article : other))
      const payload = {
      items: rows.map(([id, assignment]) => ({
        meta: { articleId: id, nmId: Number(id.split('-').at(-1)), name: 'Synthetic product', status: state.automation[id] === false ? 'manual' : 'auto',
          currentPriceKopecks: 172000, basketsLast7d: 10, basketNorm: 10, assignmentSource: 'manual' },
        settings: { automationEnabled: state.automation[id] !== false }, analytics: { basketsState: 'ok', periodStatsState: 'ok', financeState: 'no_data', stockState: 'ok' },
        strategy: { ...catalog.find(item => item.id === assignment.id), config: assignment.config, assignmentSource: 'manual' },
      })), total: Object.keys(state.assignments).length, summary: { skuCount: Object.keys(state.assignments).length },
      cache: { pagesCached: 1, totalCached: Object.keys(state.assignments).length, serverFiltersApplied: true,
        financeFetchedAt: 'now', periodStatsFetchedAt: 'now', basketsFetchedAt: 'now', adsFetchedAt: 'now' },
      }
      if (state.delayNextSku) {
        state.delayNextSku = false
        return new Promise<void>(resolve => { state.releaseSku = async () => { await route.fulfill({ json: payload }); resolve() } })
      }
      return route.fulfill({ json: payload })
    }
    // These are existing read-only report preparation/demand endpoints. They
    // are fulfilled locally; none can start a real job or reach a backend.
    const readJobs = ['/api/wb/reports/stats/jobs', '/api/v1/wb-repricer/sync/run', '/api/wb/reports/work-demand/release']
    if (req.method() !== 'GET' && !readJobs.includes(url.pathname)) {
      state.errors.push(`Unexpected mutation: ${url.pathname}`)
      return route.fulfill({ status: 403, json: { detail: 'Forbidden test mutation' } })
    }
    return route.fulfill({ json: { items: [], total: 0, state: 'completed', running: false, steps: [] } })
  })
  await page.clock.install({ time: new Date(options.at ?? '2026-11-02T21:30:00Z') })
  const mount = async () => {
    await page.goto('http://127.0.0.1:4199' + (options.route ?? '/wb/repricer'))
    if (css) await page.addStyleTag({ content: css })
    await page.addScriptTag({ content: code })
    if (options.route?.endsWith('/changelog')) await page.locator('[data-vella-island="history-list"]').filter({ visible: true }).last().waitFor()
    else {
      await page.locator(`#tbody tr[data-sku="${options.serverSearch ? other : article}"]`).waitFor()
      await page.waitForFunction(() => !window.__vellaProductsLoading && typeof window.__vellaSetRepricerManual === 'function')
    }
  }
  const choose = async (name: string, id = article) => {
    await page.locator(`#tbody tr[data-sku="${id}"] .tpl-chip`).click()
    await page.locator('.cell-drop-item').filter({ hasText: name }).first().click()
    if (name === 'Расписание') await page.locator('#cfgScheduleMode').waitFor({ state: 'visible' })
    if (name === 'План-факт на период групповой') await page.locator('#cfgGroupExistingSelect').waitFor({ state: 'visible' })
  }
  const bulk = async (name: string) => {
    await page.getByRole('button', { name: /^Применить стратегию/ }).filter({ visible: true }).first().click()
    await page.locator(`[data-bulk-strategy="${name}"]`).filter({ visible: true }).last().click()
    if (name === 'Расписание') await page.locator('#cfgScheduleMode').waitFor({ state: 'visible' })
    if (name === 'План-факт на период групповой') await page.locator('#cfgGroupExistingSelect').waitFor({ state: 'visible' })
  }
  const select = async (id: string) => {
    const row = page.locator(`#tbody tr[data-sku="${id}"]`)
    if (await row.locator('input[type="checkbox"]').count()) await row.locator('input[type="checkbox"]').first().check()
    else await row.locator('.cb').first().click()
  }
  await mount()
  return { browser, page, state, mount, choose, bulk, select }
}

const schedule = { scheduleRules: [
  { id: 'saved-1', dateFrom: '2026-11-10', dateTo: '2026-11-12', timeFrom: '08:30', timeTo: '09:45', daysOfWeek: [1, 3], mode: 'delta_pct', deltaPct: 5.5 },
  { id: 'saved-2', dateFrom: null, dateTo: null, timeFrom: '12:00', timeTo: '13:00', daysOfWeek: [5], mode: 'hold' },
] }
async function cancel(page: Page) {
  await page.locator('#m-createTpl').getByRole('button', { name: 'Отмена', exact: true }).click()
}

it('reopens the complete inline group returned by normal assignment without a registry entry', async () => {
  // Actual backend normalizer generates groupId but does not publish a registry group.
  const config = { groupId: 'group-generated-by-normal-assignment', groupName: 'Synthetic target group', groupArticleIds: [article], planOrders: 9 }
  const { browser, page, choose } = await setup({ groups: [], assignments: { [article]: { id: 'plan_fact_group', config } } })
  try {
    await choose('План-факт на период групповой')
    const evidence = { scope: 'ISOLATED', generatedConfig: config, saveDisabled: await page.locator('#strategySaveBtn').isDisabled(), plan: await page.locator('#cfgGroupPlanOrders').inputValue(), warnings: await page.locator('.strategy-form-warning[role=alert]').allTextContents() }
    await fs.writeFile(new URL('../../../../output/repricer-four-fixes-2026-11-03/inline-group-isolated.json', import.meta.url), JSON.stringify(evidence, null, 2))
    await page.screenshot({ path: new URL('../../../../output/repricer-four-fixes-2026-11-03/inline-group-isolated.png', import.meta.url).pathname, fullPage: true })
    expect(evidence.saveDisabled).toBe(false)
    expect(evidence.plan).toBe('9')
  } finally { await browser.close() }
}, 30000)

it('persists disabling automation when the selected product is moved to manual with the bulk button', async () => {
  const { browser, page, state, select, mount } = await setup()
  try {
    const assignment = JSON.stringify(state.assignments[article])
    await select(article)
    await page.getByRole('button', { name: 'Перевести в ручной', exact: true }).click()
    await page.waitForTimeout(200)
    await fs.writeFile(new URL('../../../../output/repricer-four-fixes-2026-11-03/manual-bulk-isolated.json', import.meta.url), JSON.stringify({
      scope: 'ISOLATED', automationWrites: state.automationWrites, automation: state.automation[article],
      label: await page.locator(`tr[data-sku="${article}"] [data-column-id="status"]`).innerText(),
    }, null, 2))
    await expect.poll(() => state.automationWrites.length, { timeout: 1500 }).toBe(1)
    expect(state.automation[article]).toBe(false)
    await expect.poll(async () => (await page.locator('body').innerText()).includes('сохранено 1, ошибок 0'), { timeout: 5000 }).toBe(true)
    await mount()
    await expect.poll(() => page.locator(`tr[data-sku="${article}"] [data-column-id="status"]`).innerText()).toMatch(/ручной/)
    expect(JSON.stringify(state.assignments[article])).toBe(assignment)
  } finally { await browser.close() }
}, 30000)

it('persists disabling automation when the product status selector is changed to manual', async () => {
  const { browser, page, state, mount } = await setup()
  try {
    const assignment = JSON.stringify(state.assignments[article])
    await page.locator(`tr[data-sku="${article}"] [data-column-id="status"] .cell-drop-wrap`).click()
    await page.locator('.cell-drop-item').filter({ hasText: /^ручной$/i }).click()
    await page.waitForTimeout(200)
    await fs.writeFile(new URL('../../../../output/repricer-four-fixes-2026-11-03/manual-row-isolated.json', import.meta.url), JSON.stringify({
      scope: 'ISOLATED', automationWrites: state.automationWrites, automation: state.automation[article],
      label: await page.locator(`tr[data-sku="${article}"] [data-column-id="status"]`).innerText(),
    }, null, 2))
    await expect.poll(() => state.automationWrites.length, { timeout: 1500 }).toBe(1)
    expect(state.automation[article]).toBe(false)
    await expect.poll(async () => (await page.locator('body').innerText()).includes('сохранено 1, ошибок 0'), { timeout: 5000 }).toBe(true)
    await mount()
    await expect.poll(() => page.locator(`tr[data-sku="${article}"] [data-column-id="status"]`).innerText()).toMatch(/ручной/)
    expect(JSON.stringify(state.assignments[article])).toBe(assignment)
  } finally { await browser.close() }
}, 30000)

it('preserves the active server search after a real bulk strategy application refresh', async () => {
  const { browser, page, state, select, bulk } = await setup({ serverSearch: true,
    assignments: { [article]: { id: 'stockout_guard', config: {} }, [other]: { id: 'stockout_guard', config: {} } } })
  try {
    await page.locator('#searchTable').fill('101')
    // The clock is fake only in this isolated browser, never in the live session.
    await page.clock.runFor(500)
    await page.locator(`tr[data-sku="${article}"]`).waitFor()
    await select(article)
    await bulk('Контроль оборачиваемости')
    await page.locator('#m-bulkStrategy').filter({ visible: true }).waitFor()
    await page.locator('#m-bulkStrategy .btn-primary').click()
    await page.locator('#m-bulkStrategy').waitFor({ state: 'hidden' })
    await page.waitForTimeout(200)
    await fs.writeFile(new URL('../../../../output/repricer-four-fixes-2026-11-03/bulk-search-isolated.json', import.meta.url), JSON.stringify({
      scope: 'ISOLATED', queryStillVisible: await page.locator('#searchTable').inputValue(), skuQueries: state.skuQueries,
      assignment: state.assignments[article], rowArticles: await page.locator('tr[data-sku]').evaluateAll(rows => rows.map(row => (row as HTMLElement).dataset.sku)),
    }, null, 2))
    expect(state.assignments[article].id).toBe('turnover_control')
    expect(await page.locator('#searchTable').inputValue()).toBe('101')
    await expect.poll(() => page.locator(`tr[data-sku="${article}"]`).count(), { timeout: 1500 }).toBe(1)
  } finally { await browser.close() }
}, 30000)

it('keeps the complete inline group when saving from a subset, cancelling and reloading', async () => {
  const config = { groupId: 'generated-inline', groupName: 'Saved inline', groupArticleIds: [article, other, third], planOrders: 9 }
  const { browser, page, state, choose, mount } = await setup({ assignments: { [article]: { id: 'plan_fact_group', config } } })
  try {
    await choose('План-факт на период групповой')
    expect(await page.locator('#cfgGroupPlanOrders').inputValue()).toBe('9')
    await cancel(page)
    expect(state.writes).toHaveLength(0)
    await choose('План-факт на период групповой')
    await page.locator('#strategySaveBtn').click()
    await page.locator('#m-createTpl').waitFor({ state: 'hidden' })
    expect(state.writes[0].articleIds).toEqual([article])
    expect(state.writes[0].config).toEqual(config)
    expect(state.groups).toEqual([])
    await mount()
    await choose('План-факт на период групповой')
    expect(await page.locator('#cfgGroupPlanOrders').inputValue()).toBe('9')
    expect(await page.locator('#strategySaveBtn').isDisabled()).toBe(false)
  } finally { await browser.close() }
}, 30000)

it.each(['legacy', 'registry'])('keeps the valid %s group editable', async mode => {
  const config = { ...(mode === 'registry' ? { groupId: 'registered' } : {}), groupName: 'Saved group', groupArticleIds: [article, other], planOrders: 9 }
  const { browser, page, choose } = await setup({ groups: mode === 'registry' ? [{ groupId: 'registered', groupName: 'Saved group', articleIds: [article, other], planOrders: 9 }] : [],
    assignments: { [article]: { id: 'plan_fact_group', config } } })
  try {
    await choose('План-факт на период групповой')
    expect(await page.locator('#cfgGroupPlanOrders').inputValue()).toBe('9')
    expect(await page.locator('#strategySaveBtn').isDisabled()).toBe(false)
  } finally { await browser.close() }
}, 30000)

it('keeps a dangling reference and mixed inline plans blocked', async () => {
  const { browser, page, choose, state, select, bulk } = await setup({ assignments: {
    [article]: { id: 'plan_fact_group', config: { groupId: 'missing-registry-reference' } },
    [other]: { id: 'plan_fact_group', config: { groupId: 'inline', groupArticleIds: [article, other], planOrders: 11 } },
  } })
  try {
    await choose('План-факт на период групповой')
    expect(await page.locator('#strategySaveBtn').isDisabled()).toBe(true)
    await cancel(page)
    state.assignments[article].config = { groupId: 'inline', groupArticleIds: [article, other], planOrders: 9 }
    await page.reload()
    // Remount through the same isolated fixture bundle after document reload.
    if (css) await page.addStyleTag({ content: css })
    await page.addScriptTag({ content: code })
    await page.locator(`tr[data-sku="${article}"]`).waitFor()
    await select(article); await select(other); await bulk('План-факт на период групповой')
    expect(await page.locator('#strategySaveBtn').isDisabled()).toBe(true)
    expect(state.writes).toHaveLength(0)
  } finally { await browser.close() }
}, 30000)

async function rowManual(page: Page, id = article) {
  await page.locator(`tr[data-sku="${id}"] [data-column-id="status"] .cell-drop-wrap`).click()
  await page.locator('.cell-drop-item').filter({ hasText: /^ручной$/i }).click()
}

it('handles partial bulk failure without changing the unselected SKU or clearing failed selection', async () => {
  const assignments = Object.fromEntries([article, other, third].map(id => [id, { id: 'stockout_guard', config: { keep: id } }]))
  const { browser, page, state, select } = await setup({ assignments })
  try {
    const before = JSON.stringify(state.assignments)
    state.automationFailures[other] = 403
    await select(article); await select(other)
    await page.getByRole('button', { name: 'Перевести в ручной', exact: true }).click()
    await expect.poll(() => state.automation[article]).toBe(false)
    try {
      await expect.poll(async () => (await page.locator('body').innerText()).includes('сохранено 1, ошибок 1'), { timeout: 5000 }).toBe(true)
      await expect.poll(() => page.getByRole('button', { name: 'Перевести в ручной', exact: true }).isEnabled(), { timeout: 5000 }).toBe(true)
    } finally {
      await fs.writeFile(new URL('../../../../output/repricer-four-fixes-2026-11-03/partial-bulk-observed.json', import.meta.url), JSON.stringify({
        automation: state.automation, writes: state.automationWrites, queries: state.skuRequests,
        controls: await page.evaluate(() => ({
          bar: document.querySelector('#bulkBar')?.outerHTML,
          selected: [...document.querySelectorAll('#tbody tr.sel')].map(row => row.getAttribute('data-sku')),
          loading: (window as unknown as { __vellaProductsLoading: boolean }).__vellaProductsLoading,
        })),
        body: await page.locator('body').innerText(),
      }, null, 2))
    }
    expect(state.automation[other]).toBe(true); expect(state.automation[third]).toBe(true)
    expect(state.automationWrites.map(w => w.articleId).sort()).toEqual([article, other].sort())
    expect(JSON.stringify(state.assignments)).toBe(before)
    await expect.poll(() => page.locator(`tr[data-sku="${other}"] .cb`).getAttribute('class'), { timeout: 5000 }).toContain('on')
    await expect.poll(() => page.locator(`tr[data-sku="${article}"] .cb`).getAttribute('class'), { timeout: 5000 }).not.toContain('on')
    expect((await page.locator('body').innerText())).toContain('сохранено 1, ошибок 1')
    delete state.automationFailures[other]
    await page.getByRole('button', { name: 'Перевести в ручной', exact: true }).click()
    await expect.poll(async () => (await page.locator('body').innerText()).includes('сохранено 1, ошибок 0'), { timeout: 5000 }).toBe(true)
    expect(state.automation[other]).toBe(false)
    expect(state.automation[third]).toBe(true)
    expect(state.automationWrites.map(w => w.articleId)).toEqual([article, other, other])
  } finally { await browser.close() }
}, 30000)

it.each([401, 409, 500])('does not show a local-only manual state after a %s save failure', async status => {
  const { browser, page, state } = await setup()
  try {
    state.automationFailures[article] = status
    await rowManual(page)
    await expect.poll(() => state.automationWrites.length).toBe(1)
    await expect.poll(async () => (await page.locator('body').innerText()).includes('сохранено 0, ошибок 1')).toBe(true)
    expect(state.automation[article]).toBe(true)
    expect(await page.locator(`tr[data-sku="${article}"] [data-column-id="status"]`).innerText()).not.toMatch(/ручной/)
  } finally { await browser.close() }
}, 30000)

it('ignores duplicate pending row pauses and preserves a newer bulk selection', async () => {
  const { browser, page, state, select } = await setup({ assignments: {
    [article]: { id: 'stockout_guard', config: {} }, [third]: { id: 'stockout_guard', config: {} },
  } })
  try {
    state.delayAutomation = true
    await select(article)
    await page.getByRole('button', { name: 'Перевести в ручной', exact: true }).click()
    await expect.poll(() => !!state.releaseAutomation).toBe(true)
    await rowManual(page)
    await select(third)
    await select(article) // A newer deselection must survive as well.
    expect(state.automationWrites).toHaveLength(1)
    expect(await page.getByRole('button', { name: 'Перевести в ручной', exact: true }).isDisabled()).toBe(true)
    await state.releaseAutomation!()
    await expect.poll(async () => (await page.locator('body').innerText()).includes('сохранено 1, ошибок 0'), { timeout: 5000 }).toBe(true)
    await expect.poll(() => page.getByRole('button', { name: 'Перевести в ручной', exact: true }).isEnabled()).toBe(true)
    await expect.poll(() => page.locator(`tr[data-sku="${third}"] .cb`).getAttribute('class'), { timeout: 5000 }).toContain('on')
    await expect.poll(() => page.locator(`tr[data-sku="${article}"] .cb`).getAttribute('class'), { timeout: 5000 }).not.toContain('on')
    expect(state.automation[third]).toBe(true)
  } finally { await browser.close() }
}, 30000)

it('rejects a stale pre-pause read and orders a pause after a pending strategy save', async () => {
  const { browser, page, state, choose } = await setup()
  try {
    state.delayNextAssignment = true
    await choose('Контроль оборачиваемости')
    await expect.poll(() => !!state.releaseAssignment).toBe(true)
    state.delayNextSku = true
    await page.locator('#searchTable').fill('101'); await page.clock.runFor(500)
    await expect.poll(() => !!state.releaseSku).toBe(true)
    // The table shows its loading state during this deliberately held GET.
    // Invoke the same installed controller already covered by both UI controls.
    await page.evaluate(id => { void window.__vellaSetRepricerManual?.([id]) }, article)
    expect(state.automationWrites).toHaveLength(0)
    await state.releaseAssignment!()
    await expect.poll(() => state.automation[article]).toBe(false)
    await expect.poll(async () => (await page.locator(`tr[data-sku="${article}"] [data-column-id="status"]`).innerText()).trim()).toBe('ручной')
    await state.releaseSku!(); await page.clock.runFor(500)
    expect(state.assignments[article].id).toBe('turnover_control')
    expect(state.automationWrites).toHaveLength(1)
    expect(await page.locator(`tr[data-sku="${article}"] [data-column-id="status"]`).innerText()).toMatch(/ручной/)
    expect(await page.locator(`tr[data-sku="${article}"] [data-column-id="tpl"]`).innerText()).toMatch(/Контроль оборачиваемости/)
  } finally { await browser.close() }
}, 30000)

it('sends no mutation for an empty manual selection', async () => {
  const { browser, page, state } = await setup()
  try {
    await page.waitForFunction(() => typeof window.__vellaSetRepricerManual === 'function')
    await page.evaluate(async () => { await window.__vellaSetRepricerManual?.([]) })
    expect(state.automationWrites).toEqual([])
    expect(state.automation[article]).toBe(true)
  } finally { await browser.close() }
}, 30000)

it.each(['schedule', 'manual'])('keeps the searched SKU and list parameters after %s saving', async action => {
  const { browser, page, state, choose } = await setup({ serverSearch: true, assignments: {
    [article]: { id: action === 'schedule' ? 'schedule' : 'stockout_guard', config: action === 'schedule' ? schedule : {} },
    [other]: { id: 'stockout_guard', config: {} },
  } })
  try {
    await page.locator('#searchTable').fill('101'); await page.clock.runFor(500)
    await page.locator(`tr[data-sku="${article}"]`).waitFor()
    // Establish a non-default server-list state, then use the real save control.
    await page.evaluate(() => {
      window.__vellaProductsListState = { ...window.__vellaProductsListState, page: 3, pageSize: 25, topMode: false }
      window.__vellaProductsFilters = { status: 'all', brand: 'Synthetic brand', manager: 'unassigned', promo: 'all' }
      window.__vellaProductsAdvancedSnapshot = () => ({ stock: 'normal' })
      window.__vellaProductsPeriodMode = 'custom'
      window.__vellaProductsPeriodDays = 14
      window.__vellaProductsPeriodFromIso = '2026-10-20'
      window.__vellaProductsPeriodToIso = '2026-11-02'
    })
    await page.locator('#mainTable th[data-column-id="price"]').click()
    const sort = await page.evaluate(() => window.eval('JSON.stringify(productSortStack)'))
    const before = state.skuRequests.length
    if (action === 'schedule') {
      await choose('Расписание')
      await page.locator('#strategySaveBtn').click()
      await page.locator('#m-createTpl').waitFor({ state: 'hidden' })
      expect(state.writes.at(-1)?.config).toEqual(schedule)
    } else {
      await rowManual(page)
      await expect.poll(async () => (await page.locator('body').innerText()).includes('сохранено 1, ошибок 0')).toBe(true)
      expect(state.automation[article]).toBe(false)
    }
    const refreshes = state.skuRequests.slice(before).map(q => new URLSearchParams(q))
    expect(refreshes.length).toBeGreaterThan(0)
    expect(refreshes.every(q => q.get('q') === '101' && q.get('page') === '3' && q.get('pageSize') === '25' && q.get('topMode') === 'false')).toBe(true)
    expect(refreshes.every(q => q.get('brand') === 'Synthetic brand' && q.get('manager') === 'unassigned'
      && q.get('dateFrom') === '2026-10-20' && q.get('dateTo') === '2026-11-02'
      && JSON.parse(q.get('filters') ?? '{}').stock === 'normal')).toBe(true)
    expect(await page.evaluate(() => window.eval('JSON.stringify(productSortStack)'))).toBe(sort)
    expect(await page.locator('#searchTable').inputValue()).toBe('101')
    expect(await page.locator(`tr[data-sku="${article}"]`).count()).toBe(1)
  } finally { await browser.close() }
}, 30000)

it('retains a newer search when a post-pause list refresh completes late', async () => {
  const { browser, page, state } = await setup({ serverSearch: true, assignments: {
    [article]: { id: 'stockout_guard', config: {} }, [other]: { id: 'stockout_guard', config: {} },
  } })
  try {
    await page.locator('#searchTable').fill('101'); await page.clock.runFor(500)
    await page.locator(`tr[data-sku="${article}"]`).waitFor()
    state.delayNextSku = true
    await rowManual(page)
    await expect.poll(() => !!state.releaseSku).toBe(true)
    await page.locator('#searchTable').fill('102'); await page.clock.runFor(500)
    await page.locator(`tr[data-sku="${other}"]`).waitFor()
    await state.releaseSku!()
    await expect.poll(async () => (await page.locator('body').innerText()).includes('сохранено 1, ошибок 0'), { timeout: 5000 }).toBe(true)
    await page.clock.runFor(500)
    expect(await page.locator('#searchTable').inputValue()).toBe('102')
    expect(await page.locator('#tbody tr[data-sku]').evaluateAll(rows => rows.map(row => row.getAttribute('data-sku')))).toEqual([other])
    expect(state.automation[article]).toBe(false)
  } finally { await browser.close() }
}, 30000)

it('does not carry a previous user selection into the same SKU in a new session', async () => {
  const { browser, page, state, select } = await setup()
  try {
    await select(article)
    await expect.poll(() => page.locator(`tr[data-sku="${article}"] .cb`).getAttribute('class'), { timeout: 5000 }).toContain('on')
    await page.getByRole('button', { name: 'Switch synthetic user', exact: true }).click()
    await expect.poll(() => state.skuSessions.at(-1)).toBe('Bearer synthetic-repricer-session-2')
    await expect.poll(() => page.locator(`tr[data-sku="${article}"] .cb`).getAttribute('class'), { timeout: 5000 }).not.toContain('on')
    expect(await page.locator('#bulkBar').getAttribute('class')).not.toContain('show')
    expect(state.automationWrites).toHaveLength(0)
  } finally { await browser.close() }
}, 30000)
