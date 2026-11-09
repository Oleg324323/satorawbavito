// Audit: actual React/legacy UI, entirely intercepted synthetic API. No live writes.
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { chromium } from 'playwright'
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
const catalog = definitions.map(([id, name]) => ({ id, name, type: 'stock', color: '#2563eb', rules: [], applied: 0,
  status: ['cross_marketplace', 'bundles'].includes(id) ? 'disabled' : id === 'illiquid' ? 'special' : 'active' }))
let code = '', css = ''
beforeAll(async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({ root, configFile: false, envFile: false, publicDir: false, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""' },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: { entry: fileURLToPath(new URL('./__fixtures__/reportLoadingBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'RepricerStrategyAudit' } } })
  const output = (Array.isArray(result) ? result : [result]).flatMap(item => 'output' in item ? item.output : [])
  const entry = output.find(item => item.type === 'chunk' && item.isEntry)
  if (!entry || entry.type !== 'chunk') throw new Error('Missing audit bundle')
  code = entry.code
  css = output.filter(item => item.type === 'asset' && item.fileName.endsWith('.css')).map(item => item.type === 'asset' ? String(item.source) : '').join('\n')
}, 60000)

async function setup() {
  const browser = await chromium.launch({ headless: true })
  const page = await browser.newPage({ viewport: { width: 1800, height: 1000 }, serviceWorkers: 'block' })
  const state = { assigned: 'stockout_guard', failNext: false, delayNext: false,
    release: undefined as undefined | (() => Promise<void>), writes: [] as Array<Record<string, unknown>>, errors: [] as string[] }
  page.on('pageerror', error => state.errors.push(error.message))
  await page.route('**/*', async route => {
    const req = route.request(), url = new URL(req.url())
    if (req.resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
    if (req.resourceType() === 'image') return route.fulfill({ status: 204 })
    if (url.hostname === 'fonts.googleapis.com') return route.fulfill({ body: '' })
    if (url.pathname === '/api/v1/wb-repricer/strategy-assignments/bulk') {
      const body = req.postDataJSON(); state.writes.push(body)
      expect(body.articleIds).toEqual(['AUDIT-443894764'])
      const strategy = catalog.find(item => item.id === body.strategyName || item.name === body.strategyName)
      const next = strategy?.id ?? 'none'
      expect(body.executeAfterAssign).toBe(next !== 'none')
      const complete = async () => {
        if (state.failNext) { state.failNext = false; await route.fulfill({ status: 500, json: { detail: 'Synthetic save failure' } }); return }
        state.assigned = next
        await route.fulfill({ json: { strategy: strategy ?? { id: 'none', name: 'не задана' },
          assignedCount: Number(next !== 'none'), unassignedCount: Number(next === 'none'), items: [] } })
      }
      if (state.delayNext) {
        state.delayNext = false
        return new Promise<void>(resolve => { state.release = async () => { await complete(); resolve() } })
      }
      return complete()
    }
    if (url.pathname === '/api/v1/wb-repricer/strategies/catalog') return route.fulfill({ json: { items: catalog, total: catalog.length } })
    if (url.pathname === '/api/v1/wb-repricer/sku') {
      const strategy = catalog.find(item => item.id === state.assigned)
      return route.fulfill({ json: {
        items: [{ meta: { articleId: 'AUDIT-443894764', nmId: 443894764, name: 'Synthetic audit only', status: strategy ? 'auto' : 'manual',
          currentPriceKopecks: 100000, basketsLast7d: 10, basketNorm: 10, assignmentSource: strategy ? 'manual' : 'none' },
          settings: { automationEnabled: !!strategy }, analytics: { basketsState: 'ok', periodStatsState: 'ok', financeState: 'no_data', stockState: 'ok' },
          strategy: { ...(strategy ?? catalog[0]), id: state.assigned, assignmentSource: strategy ? 'manual' : 'none' } }],
        total: 1, summary: { skuCount: 1 }, cache: { pagesCached: 1, totalCached: 1, serverFiltersApplied: true,
          financeFetchedAt: 'now', periodStatsFetchedAt: 'now', basketsFetchedAt: 'now', adsFetchedAt: 'now' },
      } })
    }
    if (req.method() !== 'GET') expect(url.pathname).not.toMatch(/\/(?:prices?|apply|upload)(?:\/|$)/)
    return route.fulfill({ json: { items: [], total: 0, state: 'completed', running: false, steps: [] } })
  })
  const mount = async () => {
    await page.goto('http://127.0.0.1:4199/wb/repricer')
    if (css) await page.addStyleTag({ content: css })
    await page.addScriptTag({ content: code })
    await page.locator('#tbody tr[data-sku="AUDIT-443894764"]').waitFor()
  }
  const row = page.locator('#tbody tr[data-sku="AUDIT-443894764"]')
  const choose = async (name: string) => {
    await row.locator('.tpl-chip,.tpl-none').click()
    await page.locator('.cell-drop-item').filter({ hasText: name }).first().click()
  }
  await mount()
  return { browser, page, state, row, mount, choose }
}

it('uses actual row menus for every selectable strategy, configuration, clear, and reload', async () => {
  const { browser, page, state, row, mount, choose } = await setup()
  try {
    for (const strategy of catalog.filter(item => item.status !== 'disabled')) {
      const before = state.writes.length
      await choose(strategy.name)
      if (strategy.id === 'schedule') {
        await page.locator('#cfgScheduleMode').selectOption('hold')
        await page.locator('#strategySaveBtn').click()
      } else if (strategy.id === 'plan_fact_group') {
        await page.locator('#cfgGroupPlanOrders').fill('5')
        await page.locator('#strategySaveBtn').click()
      }
      await expect.poll(() => state.writes.length).toBe(before + 1)
      await expect.poll(() => state.assigned).toBe(strategy.id)
      await expect.poll(() => row.locator('.tpl-chip').innerText()).toContain(strategy.name)
      const write = state.writes.at(-1)!
      if (strategy.id === 'schedule') expect(write.config).toMatchObject({ scheduleRules: [{ mode: 'hold' }] })
      if (strategy.id === 'plan_fact_group') expect(write.config).toMatchObject({ planOrders: 5, groupArticleIds: ['AUDIT-443894764'] })
      await mount()
      expect(await row.locator('.tpl-chip').innerText()).toContain(strategy.name)
    }
    await choose('— нет —')
    await expect.poll(() => state.assigned).toBe('none')
    await mount()
    expect(await row.locator('.tpl-none').count()).toBe(1)
    await choose('Защита out of stock')
    await expect.poll(() => state.assigned).toBe('stockout_guard')
    await mount()
    expect(await row.locator('.tpl-chip').innerText()).toContain('Защита out of stock')
    expect(state.errors).toEqual([])
  } finally { await browser.close() }
}, 60000)

it('rolls a rejected save back to the persisted strategy', async () => {
  const { browser, state, row, choose } = await setup()
  try {
    state.failNext = true
    await choose('Контроль оборачиваемости')
    await expect.poll(() => state.writes.length).toBe(1)
    await expect.poll(() => row.locator('.tpl-chip').innerText()).toContain('Защита out of stock')
    expect(state.assigned).toBe('stockout_guard')
    expect(state.errors).toEqual([])
  } finally { await browser.close() }
}, 30000)

it('disabled strategies do not submit an assignment through the actual row menu', async () => {
  const { browser, page, state, choose } = await setup()
  try {
    for (const strategy of catalog.filter(item => item.status === 'disabled')) {
      await choose(strategy.name)
      await expect.poll(() => page.getByText(`Стратегия «${strategy.name}» временно выключена`, { exact: true }).count()).toBeGreaterThan(0)
    }
    expect(state.writes).toEqual([])
    expect(state.assigned).toBe('stockout_guard')
  } finally { await browser.close() }
}, 30000)

it('a delayed older strategy save cannot overwrite a newer clear selection', async () => {
  const { browser, state, row, choose } = await setup()
  try {
    state.delayNext = true
    await choose('Контроль оборачиваемости')
    await expect.poll(() => Boolean(state.release)).toBe(true)
    await choose('— нет —')
    await expect.poll(() => row.locator('.tpl-chip,.tpl-none').innerText()).toMatch(/нет|Не назначена/)
    expect(state.writes).toHaveLength(1)
    await state.release?.()
    await expect.poll(() => state.assigned).toBe('none')
    await expect.poll(() => row.locator('.tpl-chip,.tpl-none').innerText()).toMatch(/нет|Не назначена/)
  } finally { await state.release?.().catch(() => undefined); await browser.close() }
}, 30000)
