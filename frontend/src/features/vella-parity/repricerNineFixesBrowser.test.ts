// Real UI components; every request intercepted, no backend/server/account used.
import fs from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { chromium, type Page } from 'playwright'
import { afterEach, beforeAll, expect, it } from 'vitest'

const article = 'synthetic-101', other = 'synthetic-102'
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
    build: { write: false, minify: false, lib: { entry: fileURLToPath(new URL('./__fixtures__/reportLoadingBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'RepricerNineFixes' } } })
  const output = (Array.isArray(result) ? result : [result]).flatMap(item => 'output' in item ? item.output : [])
  const entry = output.find(item => item.type === 'chunk' && item.isEntry)
  if (!entry || entry.type !== 'chunk') throw new Error('Missing browser bundle')
  code = entry.code
  css = output.filter(item => item.type === 'asset' && item.fileName.endsWith('.css')).map(item => item.type === 'asset' ? String(item.source) : '').join('\n')
}, 60000)

async function setup(options: { assignments?: Record<string, Assignment>; groups?: Group[]; events?: Config[]; at?: string; route?: string } = {}) {
  const browser = await chromium.launch({ headless: true })
  const page = await browser.newPage({ viewport: { width: 1800, height: 1000 }, timezoneId: 'America/Los_Angeles', serviceWorkers: 'block' })
  page.setDefaultTimeout(5000)
  const state = {
    assignments: options.assignments ?? { [article]: { id: 'stockout_guard', config: {} } },
    groups: options.groups ?? [], writes: [] as Array<{ articleIds: string[]; strategyName: string; config?: Config }>,
    previews: [] as Array<{ articleIds: string[]; strategyId: string; config?: Config }>, errors: [] as string[],
    delayNext: false, release: undefined as undefined | (() => Promise<void>),
  }
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
      for (const id of body.articleIds) state.assignments[id] = { id: strategy.id, config: body.config ?? {} }
      return route.fulfill({ json: { strategy, assignedCount: body.articleIds.length, items: [] } })
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
    if (url.pathname === '/api/v1/wb-repricer/sku') return route.fulfill({ json: {
      items: Object.entries(state.assignments).map(([id, assignment]) => ({
        meta: { articleId: id, nmId: id === article ? 101 : 102, name: 'Synthetic product', status: 'auto',
          currentPriceKopecks: 172000, basketsLast7d: 10, basketNorm: 10, assignmentSource: 'manual' },
        settings: { automationEnabled: true }, analytics: { basketsState: 'ok', periodStatsState: 'ok', financeState: 'no_data', stockState: 'ok' },
        strategy: { ...catalog.find(item => item.id === assignment.id), config: assignment.config, assignmentSource: 'manual' },
      })), total: Object.keys(state.assignments).length, summary: { skuCount: Object.keys(state.assignments).length },
      cache: { pagesCached: 1, totalCached: Object.keys(state.assignments).length, serverFiltersApplied: true,
        financeFetchedAt: 'now', periodStatsFetchedAt: 'now', basketsFetchedAt: 'now', adsFetchedAt: 'now' },
    } })
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
    else await page.locator(`#tbody tr[data-sku="${article}"]`).waitFor()
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

it('restores schedule after reload, preserves extra rules, and cancels without saving', async () => {
  const { browser, page, state, mount, choose } = await setup({ assignments: { [article]: { id: 'schedule', config: schedule } } })
  try {
    await mount(); await choose('Расписание')
    expect(await page.locator('#cfgScheduleDateFrom').inputValue()).toBe('2026-11-10')
    expect(await page.locator('#cfgScheduleDateTo').inputValue()).toBe('2026-11-12')
    expect(await page.locator('#cfgScheduleTimeFrom').inputValue()).toBe('08:30')
    expect(await page.locator('#cfgScheduleTimeTo').inputValue()).toBe('09:45')
    expect(await page.locator('#cfgScheduleMode').inputValue()).toBe('delta_pct')
    expect(await page.locator('#cfgScheduleDeltaPct').inputValue()).toBe('5.5')
    expect(await page.locator('[data-cfg-weekday]:checked').evaluateAll(inputs => inputs.map(input => input.getAttribute('data-cfg-weekday')))).toEqual(['1', '3'])
    await page.locator('#cfgScheduleMode').selectOption('hold')
    await page.locator('#cfgScheduleMode').selectOption('delta_pct')
    await cancel(page)
    expect(state.writes).toEqual([])
    await choose('Расписание'); await page.locator('#strategySaveBtn').click()
    await expect.poll(() => state.writes.length).toBe(1)
    expect(state.writes[0].config).toEqual(schedule)
    await mount(); await choose('Расписание')
    expect(await page.locator('#cfgScheduleDeltaPct').inputValue()).toBe('5.5')
    expect(state.errors).toEqual([])
  } finally { await browser.close() }
}, 45000)

it.each([{ mode: 'set_price', priceKopecks: 177160 }, { mode: 'delta_pct', deltaPct: 0 }])('retains saved action values, including zero: %j', async action => {
  const config = { scheduleRules: [{ id: 'one', dateFrom: null, dateTo: null, timeFrom: '00:00', timeTo: '23:59', daysOfWeek: [1,2,3,4,5,6,7], ...action }] }
  const { browser, page, state, choose } = await setup({ assignments: { [article]: { id: 'schedule', config } } })
  try {
    await choose('Расписание')
    expect(await page.locator('#cfgScheduleDateFrom').inputValue()).toBe('')
    expect(await page.locator(action.mode === 'set_price' ? '#cfgSchedulePriceRub' : '#cfgScheduleDeltaPct').inputValue()).toBe(action.mode === 'set_price' ? '1771.6' : '0')
    await page.locator('#strategySaveBtn').click()
    await expect.poll(() => state.writes.length).toBe(1)
    expect(state.writes[0].config).toEqual(config)
  } finally { await browser.close() }
}, 30000)

it('requires explicit replacement for mixed schedules and rejects an empty fixed price', async () => {
  const { browser, page, state, bulk, select } = await setup({ assignments: {
    [article]: { id: 'schedule', config: schedule }, [other]: { id: 'schedule', config: { scheduleRules: [{ mode: 'hold' }] } },
  } })
  try {
    await select(article); await select(other); await bulk('Расписание')
    expect(await page.locator('#strategySaveBtn').isDisabled()).toBe(true)
    expect(await page.locator('#cfgScheduleDateFrom').isDisabled()).toBe(true)
    expect(state.writes).toEqual([])
    await page.getByRole('button', { name: 'Задать новое расписание', exact: true }).click()
    await page.locator('#strategySaveBtn').click()
    expect(state.writes).toEqual([])
    await page.locator('#cfgScheduleMode').selectOption('hold')
    await page.locator('#strategySaveBtn').click()
    await expect.poll(() => state.writes.length).toBe(1)
    expect(state.writes[0].articleIds).toEqual([article, other])
  } finally { await browser.close() }
}, 30000)

it('restores existing group identity, plan and complete membership when opening one member', async () => {
  const group = { groupId: 'saved-group', groupName: 'Сохранённая группа', articleIds: [article, other], planOrders: 123 }
  const config = { groupId: group.groupId, groupName: group.groupName, groupArticleIds: group.articleIds, planOrders: 123 }
  const { browser, page, state, choose, mount } = await setup({ groups: [group], assignments: { [article]: { id: 'plan_fact_group', config } } })
  try {
    await mount(); await choose('План-факт на период групповой')
    expect(await page.locator('#cfgGroupExistingSelect').inputValue()).toBe('saved-group')
    expect(await page.locator('#cfgGroupName').inputValue()).toBe(group.groupName)
    expect(await page.locator('#cfgGroupPlanOrders').inputValue()).toBe('123')
    await cancel(page); expect(state.writes).toEqual([])
    await choose('План-факт на период групповой'); await page.locator('#strategySaveBtn').click()
    await expect.poll(() => state.writes.length).toBe(1)
    expect(state.writes[0]).toMatchObject({ articleIds: [article], config })
    expect(state.groups).toEqual([group])
    await mount(); await choose('План-факт на период групповой')
    expect(await page.locator('#cfgGroupPlanOrders').inputValue()).toBe('123')
    expect(state.errors).toEqual([])
  } finally { await browser.close() }
}, 30000)

it('blocks an unresolved saved group and requires an explicit choice for mixed groups', async () => {
  const groups = [{ groupId: 'a', groupName: 'A', articleIds: [article], planOrders: 123 }, { groupId: 'b', groupName: 'B', articleIds: [other], planOrders: 77 }]
  const { browser, page, state, choose, mount, select, bulk } = await setup({ groups, assignments: {
    [article]: { id: 'plan_fact_group', config: { groupId: 'missing', planOrders: 123 } },
    [other]: { id: 'plan_fact_group', config: { groupId: 'b', planOrders: 77 } },
  } })
  try {
    await choose('План-факт на период групповой')
    expect(await page.locator('#strategySaveBtn').isDisabled()).toBe(true)
    expect(await page.locator('#cfgGroupExistingSelect').inputValue()).toBe('__mixed')
    expect(await page.getByRole('alert').last().innerText()).toContain('Сохранённая группа не найдена')
    await cancel(page)
    state.assignments[article].config = { groupId: 'a', planOrders: 123 }
    await mount(); await select(article); await select(other); await bulk('План-факт на период групповой')
    expect(await page.locator('#strategySaveBtn').isDisabled()).toBe(true)
    await page.locator('#cfgGroupExistingSelect').selectOption('b')
    expect(await page.locator('#cfgGroupPlanOrders').inputValue()).toBe('77')
    expect(await page.locator('#strategySaveBtn').isDisabled()).toBe(false)
    expect(state.writes).toEqual([])
  } finally { await browser.close() }
}, 30000)

it.each(['schedule', 'plan_fact_group'])('prefills matching bulk configurations without saving: %s', async id => {
  const group = { groupId: 'shared', groupName: 'Общая группа', articleIds: [article, other], planOrders: 123 }
  const config = id === 'schedule' ? schedule : { groupId: group.groupId, groupArticleIds: group.articleIds, planOrders: group.planOrders }
  const { browser, page, state, select, bulk } = await setup({ groups: [group], assignments: {
    [article]: { id, config }, [other]: { id, config },
  } })
  try {
    await select(article); await select(other)
    await bulk(id === 'schedule' ? 'Расписание' : 'План-факт на период групповой')
    expect(await page.locator('#strategySaveBtn').isDisabled()).toBe(false)
    expect(await page.locator(id === 'schedule' ? '#cfgScheduleDeltaPct' : '#cfgGroupPlanOrders').inputValue()).toBe(id === 'schedule' ? '5.5' : '123')
    await cancel(page)
    expect(state.writes).toEqual([])
  } finally { await browser.close() }
}, 30000)

it.each([
  ['2026-11-02T20:59:00Z', '2026-11-02'], ['2026-11-02T21:00:00Z', '2026-11-03'],
  ['2026-10-31T21:30:00Z', '2026-11-01'], ['2026-12-31T21:30:00Z', '2027-01-01'],
])('initializes a new schedule in Moscow independently of browser timezone: %s', async (at, date) => {
  const { browser, page, choose } = await setup({ at })
  try { await choose('Расписание'); expect(await page.locator('#cfgScheduleDateFrom').inputValue()).toBe(date) }
  finally { await browser.close() }
}, 30000)

it('previews the selected strategy for multiple products and applies exactly that selection', async () => {
  const { browser, page, state, select, bulk } = await setup({ assignments: {
    [article]: { id: 'stockout_guard', config: {} }, [other]: { id: 'optimal_price', config: {} },
  } })
  try {
    await select(article); await select(other); await bulk('Контроль оборачиваемости')
    await page.locator('#bulkStrategyRows').filter({ visible: true }).last().waitFor()
    expect(state.previews[0]).toMatchObject({ articleIds: [article, other], strategyId: 'turnover_control' })
    expect(await page.locator('#bulkStrategyRows').innerText()).toContain('Selected strategy turnover_control')
    expect(state.writes).toEqual([])
    await page.locator('#m-bulkStrategy').getByRole('button', { name: 'Применить стратегию', exact: true }).click()
    await expect.poll(() => state.writes.length).toBe(1)
    expect(state.writes[0]).toMatchObject({ articleIds: [article, other], strategyName: 'Контроль оборачиваемости' })
    expect(state.errors).toEqual([])
  } finally { await browser.close() }
}, 30000)

it('ignores a delayed older preview after a newer row assignment', async () => {
  const { browser, page, state, select, bulk, choose } = await setup()
  try {
    await select(article); state.delayNext = true; await bulk('Контроль оборачиваемости')
    await expect.poll(() => !!state.release).toBe(true)
    await choose('Оптимальная цена')
    await expect.poll(() => state.assignments[article].id).toBe('optimal_price')
    await expect.poll(() => state.writes.length).toBe(1)
    await state.release!()
    await page.clock.runFor(100)
    expect(await page.locator('#m-bulkStrategy').isVisible()).toBe(false)
    expect(state.writes).toHaveLength(1)
  } finally { await browser.close() }
}, 30000)

function event(id: string, oldPriceKopecks: number, newPriceKopecks: number, trigger = 'night_median_up') {
  return { id, articleId: id, skuName: 'Synthetic product', timestamp: '2026-11-02T09:00:00Z', trigger, source: 'system',
    actor: { name: 'Система' }, oldPriceKopecks, newPriceKopecks,
    changePct: Math.round((newPriceKopecks - oldPriceKopecks) / Math.max(1, oldPriceKopecks) * 1000) / 10 }
}

it('renders confirmed History with exact kopecks and sign-correct Night Median styling', async () => {
  const events = [event('down', 172000, 161680), event('up', 172000, 177160), event('flat', 172000, 172000),
    event('tiny-down', 177102, 177101), event('restore-down', 172000, 161680, 'night_median_restore'),
    event('restore-up', 172000, 177160, 'night_median_restore'), event('restore-flat', 172000, 172000, 'night_median_restore'),
    event('zero', 1, 0), event('manual', 172000, 161680, 'manual'), event('liquidation', 172000, 161680, 'liquidation'),
    event('sync', 172000, 161680, 'wb_sync'), event('warmup', 172000, 161680, 'warmup_end')]
  const { browser, page, state } = await setup({ route: '/wb/repricer/changelog', events })
  try {
    for (const [id, tone] of [['down', 'dn'], ['up', 'up'], ['flat', 'sys'], ['tiny-down', 'dn'], ['restore-down', 'dn'],
      ['restore-up', 'up'], ['restore-flat', 'sys'], ['zero', 'dn'], ['manual', 'man'], ['liquidation', 'warn'], ['sync', 'sys'], ['warmup', 'warn']]) {
      const row = page.locator('[data-vella-island="history-entry"]').filter({
        has: page.locator('.hist-title > b').filter({ hasText: new RegExp(`^${id}$`) }),
      }).last()
      await row.waitFor()
      expect((await row.locator('.hist-icon').getAttribute('class'))?.split(' ')).toContain(tone)
    }
    const text = (await page.locator('[data-vella-island="history-list"]').last().innerText()).replace(/\s/g, '')
    for (const price of ['1616,80₽', '1771,60₽', '1771,01₽', '1720₽', '0₽']) expect(text).toContain(price)
    expect(state.errors).toEqual([])
  } finally { await browser.close() }
}, 30000)

it('renders the date-filtered History response from the isolated backend lifecycle', async () => {
  const source = process.env.REPRICER_TEST_HISTORY_ARTIFACT
  const events = source ? JSON.parse(await fs.readFile(source, 'utf8')).items : [event('confirmed-engine', 172000, 177160, 'algorithm')]
  const { browser, page } = await setup({ route: '/wb/repricer/changelog', events })
  try {
    const row = page.locator('[data-vella-island="history-entry"]').last()
    await row.waitFor()
    const text = (await row.locator('.hist-title').innerText()).replace(/\s/g, '')
    expect(text).toContain('1771,60₽')
    expect((await row.locator('.hist-icon').getAttribute('class'))?.split(' ')).toContain('up')
  } finally { await browser.close() }
}, 30000)
