// Real components/BrowserRouter; every request fulfilled locally, no live server.
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { chromium, type Page } from 'playwright'
import { beforeAll, expect, it } from 'vitest'

const definitions = [
  ['stockout_guard', 'Защита out of stock'], ['turnover_control', 'Контроль оборачиваемости'],
  ['plan_fact_daily', 'План-факт стандартный'], ['plan_fact_period', 'План-факт на период'],
  ['plan_fact_group', 'План-факт на период групповой'], ['plan_fact_interval', 'План-факт интервальный'],
  ['cross_marketplace', 'Кроссмаркетплейс'], ['illiquid', 'Неликвид'],
  ['optimal_price', 'Оптимальная цена'], ['baskets_orders', 'Динамика корзин и заказов'],
  ['metric_dynamics', 'Поддержание динамики показателя'], ['night_price_mode', 'Ночная медиана'],
  ['schedule', 'Расписание'], ['bundles', 'Комплекты'],
] as const
const catalog = definitions.map(([id, name]) => ({ id, name, type: 'stock', color: '#2563eb',
  description: `Authoritative description: ${id}`, applied: 0,
  rules: [{ label: 'Synthetic rule', value: id }],
  status: ['cross_marketplace', 'bundles'].includes(id) ? 'disabled' : id === 'illiquid' ? 'special' : 'active' }))
let code = '', css = ''
beforeAll(async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({ root, configFile: false, envFile: false, publicDir: false, logLevel: 'silent',
    plugins: [{ name: 'repricer-browser-navigation', enforce: 'pre', transform(source, id) {
      if (!id.endsWith('/__fixtures__/reportLoadingBrowser.tsx')) return null
      return source.replaceAll('MemoryRouter', 'BrowserRouter').replace(' initialEntries={[window.location.pathname]}', '')
        .replace('<AuthContext.Provider', '<button style={{ position: "fixed", zIndex: 999999, top: 30, right: 0 }} onClick={() => setAccessToken("synthetic-other-scope")}>Use another synthetic session</button><AuthContext.Provider')
    } }, react()],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""' },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: { entry: fileURLToPath(new URL('./__fixtures__/reportLoadingBrowser.tsx', import.meta.url)),
      formats: ['iife'], name: 'RepricerThreeFixes' } } })
  const output = (Array.isArray(result) ? result : [result]).flatMap(item => 'output' in item ? item.output : [])
  const entry = output.find(item => item.type === 'chunk' && item.isEntry)
  if (!entry || entry.type !== 'chunk') throw new Error('Missing browser bundle')
  code = entry.code
  css = output.filter(item => item.type === 'asset' && item.fileName.endsWith('.css')).map(item => item.type === 'asset' ? String(item.source) : '').join('\n')
}, 60000)

function event(id: string, timestamp: string) {
  return { id, articleId: id, skuName: 'Synthetic product', timestamp, trigger: 'night_median_up', source: 'system',
    oldPriceKopecks: 172000, newPriceKopecks: 166840, changePct: -3, actor: { name: 'Система' }, priceConfirmed: true }
}
async function setup(options: { route?: string; zone?: string; at?: string; events?: ReturnType<typeof event>[]; catalog?: 'ready' | 'delay' | 'error' | 'empty' } = {}) {
  const browser = await chromium.launch({ headless: true })
  const page = await browser.newPage({ viewport: { width: 1800, height: 1100 }, timezoneId: options.zone ?? 'Europe/Moscow', serviceWorkers: 'block' })
  page.setDefaultTimeout(4000)
  const state = { catalogMode: options.catalog ?? 'ready', catalogRequests: 0, paths: [] as string[],
    historyRequests: [] as Record<string, string>[], errors: [] as string[], release: undefined as undefined | (() => Promise<void>) }
  page.on('pageerror', error => state.errors.push(error.message))
  await page.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url())
    if (request.resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
    if (request.resourceType() === 'image') return route.fulfill({ status: 204 })
    if (url.hostname === 'fonts.googleapis.com') return route.fulfill({ body: '' })
    state.paths.push(url.pathname)
    if (url.pathname === '/api/v1/wb-repricer/strategies/catalog') {
      state.catalogRequests += 1
      if (state.catalogMode === 'delay') {
        state.catalogMode = 'ready'
        return new Promise<void>(resolve => { state.release = async () => {
          await route.fulfill({ json: { items: catalog.map(item => ({ ...item, description: 'Delayed catalog response' })), total: catalog.length } }).catch(() => undefined)
          resolve()
        } })
      }
      if (state.catalogMode === 'error') return route.fulfill({ status: 503, json: { detail: 'Synthetic catalog unavailable' } })
      const items = state.catalogMode === 'empty' ? [] : catalog
      return route.fulfill({ json: { items, total: items.length } })
    }
    if (url.pathname === '/api/v1/wb-repricer/changelog') {
      state.historyRequests.push(Object.fromEntries(url.searchParams))
      const from = url.searchParams.get('from'), trigger = url.searchParams.get('trigger')
      const items = (options.events ?? []).filter(item => (!from || Date.parse(item.timestamp) >= Date.parse(from)) && (!trigger || trigger === item.trigger))
      const number = Number(url.searchParams.get('page') ?? 1), limit = Number(url.searchParams.get('limit') ?? 50)
      return route.fulfill({ json: { items: items.slice((number - 1) * limit, number * limit), total: items.length } })
    }
    if (url.pathname === '/api/v1/wb-repricer/sku') return route.fulfill({ json: {
      items: [{ meta: { articleId: 'synthetic-101', nmId: 101, name: 'Synthetic product', status: 'auto',
        currentPriceKopecks: 172000, assignmentSource: 'manual' }, settings: { automationEnabled: true },
        analytics: { basketsState: 'ok' }, strategy: { ...catalog[0], config: {}, assignmentSource: 'manual' } }],
      total: 1, summary: { skuCount: 1 }, cache: { pagesCached: 1, totalCached: 1, serverFiltersApplied: true } } })
    const readJobs = ['/api/wb/reports/stats/jobs', '/api/wb/reports/work-demand/release', '/api/v1/wb-repricer/sync/run']
    if (request.method() !== 'GET' && !readJobs.includes(url.pathname)) state.errors.push(`Unexpected mutation: ${url.pathname}`)
    return route.fulfill({ json: { items: [], total: 0, state: 'completed', running: false, steps: [] } })
  })
  await page.clock.install({ time: new Date(options.at ?? '2026-11-02T21:30:00Z') })
  const mount = async () => {
    await page.goto('http://127.0.0.1:4199' + (options.route ?? '/wb/templates'))
    if (css) await page.addStyleTag({ content: css })
    await page.addScriptTag({ content: code })
    await page.locator('.vella-html-parity-root').waitFor()
    await page.clock.runFor(100)
  }
  const navigate = async (tab: string) => {
    await page.locator(`aside [data-tab="${tab}"]`).filter({ visible: true }).first().click()
    await page.clock.runFor(100)
  }
  const close = async () => { await state.release?.(); await browser.close(); expect(state.errors).toEqual([]) }
  try { await mount() } catch (error) { await close(); throw error }
  return { page, state, mount, navigate, close }
}
const cards = (page: Page) => page.locator('[data-strategy-card]').filter({ visible: true })
const period = (page: Page) => page.locator('#tab-history select').filter({ has: page.locator('option[value="today"]') }).filter({ visible: true }).last()

it.each(['/wb/templates', '/wb/repricer/changelog'])('loads the catalog from %s without visiting Products', async route => {
  const test = await setup({ route })
  try {
    if (route.endsWith('/changelog')) await test.navigate('templates')
    await expect.poll(() => cards(test.page).count()).toBe(14)
    expect(test.state.catalogRequests).toBe(1)
    expect(await cards(test.page).evaluateAll(nodes => nodes.map(node => node.getAttribute('data-strategy-card')))).toEqual(catalog.map(item => item.id))
    expect(test.state.paths).not.toContain('/api/v1/wb-repricer/sku')
    expect(test.state.paths.some(item => /\/(stocks|promotions|sync\/run)$/.test(item))).toBe(false)
    if (process.env.REPRICER_THREE_FIXES_SCREENSHOTS && route === '/wb/templates') {
      await test.page.screenshot({ path: fileURLToPath(new URL('../../../../output/playwright/repricer-three-fixes-catalog.png', import.meta.url)) })
    }
    for (const id of ['cross_marketplace', 'bundles']) {
      await test.page.locator(`[data-strategy-card="${id}"]`).click()
      expect(await test.page.locator('#m-createTpl').isVisible()).toBe(false)
    }
    if (route.endsWith('/templates')) {
      await test.mount()
      await expect.poll(() => cards(test.page).count()).toBe(14)
    }
    await test.navigate('history'); await test.navigate('templates')
    await expect.poll(() => cards(test.page).count()).toBe(14)
  } finally { await test.close() }
}, 30000)

it.each(['delay', 'error', 'empty'] as const)('shows an honest %s catalog state and recovers', async mode => {
  const test = await setup({ catalog: mode })
  try {
    await expect.poll(() => test.state.catalogRequests).toBe(1)
    expect(await cards(test.page).count()).toBe(0)
    const tab = test.page.locator('#tab-templates')
    if (mode === 'delay') {
      expect(await tab.innerText()).toContain('Загружаем стратегии')
      await test.state.release?.()
    } else if (mode === 'error') {
      await tab.getByRole('button', { name: 'Повторить', exact: true }).waitFor()
      test.state.catalogMode = 'ready'
      await tab.getByRole('button', { name: 'Повторить', exact: true }).click()
    } else {
      await expect.poll(() => tab.innerText()).toContain('Нет доступных стратегий')
      test.state.catalogMode = 'ready'
      // The empty response remains authoritative; a fresh page can load new data.
      await test.mount()
    }
    await expect.poll(() => cards(test.page).count()).toBe(14)
  } finally { await test.close() }
}, 20000)

it('cancels a pending request on navigation and reloads the catalog on return', async () => {
  const test = await setup({ catalog: 'delay' })
  try {
    await expect.poll(() => Boolean(test.state.release)).toBe(true)
    await test.navigate('history')
    await test.navigate('templates')
    await expect.poll(() => cards(test.page).count()).toBe(14)
    expect(test.state.catalogRequests).toBe(2)
    await test.state.release?.()
    await test.page.clock.runFor(100)
    expect(await test.page.locator('#tab-templates').innerText()).not.toContain('Delayed catalog response')
  } finally { await test.close() }
}, 20000)

it('does not display a previous authentication scopes cached catalog', async () => {
  const test = await setup()
  try {
    await expect.poll(() => cards(test.page).count()).toBe(14)
    test.state.catalogMode = 'delay'
    await test.page.getByRole('button', { name: 'Use another synthetic session', exact: true }).click()
    await expect.poll(() => Boolean(test.state.release)).toBe(true)
    expect(await cards(test.page).count()).toBe(0)
    await test.page.getByRole('button', { name: 'Clear synthetic report session', exact: true }).click()
    await test.state.release?.()
    await test.page.clock.runFor(200)
    expect(await cards(test.page).count()).toBe(0)
  } finally { await test.close() }
}, 20000)

it('keeps the Products catalog and configured schedule/group editors working', async () => {
  const test = await setup({ route: '/wb/repricer' })
  try {
    await expect.poll(() => test.state.catalogRequests).toBe(1)
    await test.navigate('templates')
    await expect.poll(() => cards(test.page).count()).toBe(14)
    for (const [id, selector] of [['schedule', '#cfgScheduleMode'], ['plan_fact_group', '#cfgGroupExistingSelect']]) {
      await test.page.locator(`[data-strategy-card="${id}"]`).click()
      await test.page.locator(selector).waitFor({ state: 'visible' })
      await test.page.locator('#m-createTpl').getByRole('button', { name: 'Отмена', exact: true }).click()
    }
  } finally { await test.close() }
}, 20000)

it.each([
  ['Europe/Moscow', '2026-11-02T21:30:00Z', '2026-11-02T21:00:00.000Z'],
  ['Europe/Moscow', '2026-11-03T09:00:00Z', '2026-11-02T21:00:00.000Z'],
  ['Europe/Moscow', '2026-12-31T21:01:00Z', '2026-12-31T21:00:00.000Z'],
  ['America/New_York', '2026-03-08T16:00:00Z', '2026-03-08T05:00:00.000Z'],
  ['America/New_York', '2026-11-01T17:00:00Z', '2026-11-01T04:00:00.000Z'],
])('Today starts at local midnight in %s at %s', async (zone, at, start) => {
  const events = [event('synthetic-yesterday', new Date(Date.parse(start) - 1).toISOString()), event('synthetic-midnight', start)]
  const test = await setup({ route: '/wb/repricer/changelog', zone, at, events })
  try {
    await period(test.page).selectOption('today')
    await expect.poll(() => test.state.historyRequests.at(-1)?.from).toBe(start)
    await expect.poll(() => test.page.locator('[data-vella-island="history-entry"]').filter({ visible: true }).count()).toBe(1)
    const tab = test.page.locator('#tab-history')
    expect(await tab.innerText()).toContain('synthetic-midnight')
    expect(await tab.innerText()).not.toContain('synthetic-yesterday')
    expect(await tab.locator('.row-count').innerText()).toBe('1 из 1 событий')
    const title = await tab.locator('.hist-title').innerText()
    expect(title.replace(/\s/g, '')).toContain('1668,40₽')
    expect(await tab.locator('.hist-icon').getAttribute('class')).toContain('dn')
    if (process.env.REPRICER_THREE_FIXES_SCREENSHOTS && zone === 'Europe/Moscow' && at === '2026-11-02T21:30:00Z') {
      await test.page.screenshot({ path: fileURLToPath(new URL('../../../../output/playwright/repricer-three-fixes-today.png', import.meta.url)) })
    }
  } finally { await test.close() }
}, 20000)

it('keeps server totals, pagination, filter resets and other periods working', async () => {
  const start = '2026-11-02T21:00:00.000Z'
  const events = [event('synthetic-yesterday', '2026-11-02T20:59:59Z'),
    ...Array.from({ length: 51 }, (_, index) => event(`synthetic-${index}`, new Date(Date.parse(start) + index * 1000).toISOString()))]
  const test = await setup({ route: '/wb/repricer/changelog', events })
  try {
    await period(test.page).selectOption('today')
    const tab = test.page.locator('#tab-history')
    await expect.poll(() => tab.locator('.row-count').innerText()).toBe('50 из 51 событий')
    await tab.getByRole('button', { name: 'Загрузить ещё 50 событий', exact: true }).click()
    await expect.poll(() => tab.locator('.row-count').innerText()).toBe('51 из 51 событий')
    expect(test.state.historyRequests.at(-1)).toMatchObject({ page: '2', from: start })
    for (const [preset, days] of [['week', 7], ['month', 30]] as const) {
      await period(test.page).selectOption(preset)
      await expect.poll(() => test.state.historyRequests.at(-1)?.page).toBe('1')
      const delta = Date.parse(await test.page.evaluate(() => new Date().toISOString())) - Date.parse(test.state.historyRequests.at(-1)!.from)
      expect(Math.abs(delta - days * 86400000)).toBeLessThan(2000)
      await expect.poll(() => tab.locator('.row-count').innerText()).toBe('50 из 52 событий')
      if (preset === 'week') {
        await tab.getByRole('button', { name: 'Загрузить ещё 50 событий', exact: true }).click()
        await expect.poll(() => tab.locator('.row-count').innerText()).toBe('52 из 52 событий')
      }
    }
    await period(test.page).selectOption('custom')
    await tab.locator('input[type="date"]').fill('2026-11-03')
    await expect.poll(() => test.state.historyRequests.at(-1)?.from).toBe('2026-11-03T00:00:00.000Z')
    await expect.poll(() => tab.locator('.row-count').innerText()).toBe('0 из 0 событий')
  } finally { await test.close() }
}, 30000)
