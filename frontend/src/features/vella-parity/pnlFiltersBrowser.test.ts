import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { expect, it } from 'vitest'

it('applies every P&L control through real clicks without reloading financial data or changing values', async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({ configFile: false, envFile: false, root, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""' },
    resolve: { alias: { '@': path.join(root, 'src') } }, build: { write: false, minify: false, lib: {
      entry: fileURLToPath(new URL('./__fixtures__/pnlFiltersBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'PnlFiltersBrowser',
    } } })
  const output = (Array.isArray(result) ? result : [result]).flatMap(r => 'output' in r ? r.output : [])
  const bundle = output.find(r => r.type === 'chunk' && r.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing P&L filters fixture')
  const browser = await chromium.launch({ headless: true })
  try {
    const page = await browser.newPage({ viewport: { width: 1512, height: 982 }, serviceWorkers: 'block' })
    await page.clock.setFixedTime(new Date('2026-10-30T12:00:00Z'))
    const groups = [0, 1, 2, 3, 4, 5]
    const states = ['manual', 'locomotive', 'new', 'illiquid', 'liquidation', 'auto']
    const types = ['shirt', 'hoodie', 'longsleeve', 'shorts', 'balaclava', null]
    const stock = [0, 6, 21, 101, 5, null], margin = [-5, 10, 0, 15, -1, null]
    const rows = Array.from({ length: 120 }, (_, i) => ({ nmId: i + 1, sku: `SKU-${i + 1}`, productName: `Product ${i + 1}`,
      revenueKopecks: 10000 + i, cogsKopecks: 100, netProfitKopecks: i - 100, marginPct: margin[i % 6] }))
    const products = Object.fromEntries(rows.map((row, i) => {
      const g = i % 6
      return [row.nmId, { managerId: g % 3 === 0 ? 'me' : g % 3 === 1 ? 'other' : null,
        managerName: g % 3 === 0 ? 'One' : 'Two', brand: g % 2 ? 'Beta' : 'Alpha', status: states[g],
        promotion: g === 5 ? null : g % 2 === 0, garment: types[g], attrs: g === 5 ? [] : [g % 2 ? 'male' : 'female', g % 2 ? 'black' : 'white'],
        strategyId: g % 2 ? 's2' : 's1', strategyName: g % 2 ? 'Second strategy' : 'First strategy', stockUnits: stock[g],
        pminKopecks: g === 0 ? 0 : 100, changed24h: g === 0, currentPriceKopecks: g === 5 ? null : (g + 1) * 10000,
        orders: g === 5 ? null : g, baskets: g === 5 ? null : g * 5 }]
    }))
    const financialRequests: string[] = [], metadataRequests: string[] = [], unexpected: string[] = [], errors: string[] = [], allRequests: string[] = []
    let holdMetadata: (() => void) | undefined
    page.on('pageerror', error => errors.push(error.message))
    page.on('request', request => { if (request.url().includes('/api/')) allRequests.push(request.url()) })
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      if (url.pathname === '/wb/reports/pnl') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (request.resourceType() === 'image') return route.fulfill({ status: 204 })
      if (url.hostname === 'fonts.googleapis.com') return route.fulfill({ body: '' })
      if (url.pathname === '/api/v1/cabinet/team/users') return route.fulfill({ json: { data: [] } })
      if (url.pathname === '/api/v1/cabinet/wb-token') return route.fulfill({ json: { data: { hasToken: false } } })
      if (url.pathname === '/api/v1/cabinet/avito-credentials') return route.fulfill({ json: { data: { hasCredentials: false } } })
      if (url.pathname === '/api/wb/reports/pnl/latest-cache') {
        financialRequests.push(url.search)
        return route.fulfill({ json: { rows, kpis: [], meta: { freshnessState: 'fresh', sourceType: 'financial', generatedAt: '2026-10-30T12:00:00Z',
          dateRange: { from: url.searchParams.get('from'), to: url.searchParams.get('to') } } } })
      }
      if (url.pathname === '/api/wb/reports/pnl/filter-metadata' && request.method() === 'POST') {
        const body = request.postDataJSON()
        metadataRequests.push(body.dateFrom)
        if (body.dateFrom === '2026-10-12') await new Promise<void>(resolve => { holdMetadata = resolve })
        const scoped = body.dateFrom === '2026-10-12' ? Object.fromEntries(Object.entries(products).map(([nm, p]) => [nm, { ...p, brand: 'Late wrong period' }])) : products
        return route.fulfill({ json: { products: scoped, dateFrom: body.dateFrom, dateTo: body.dateTo, marketplaceAccountId: 12 } }).catch(() => undefined)
      }
      unexpected.push(`${request.method()} ${url.pathname}`)
      return route.abort()
    })
    await page.goto('http://127.0.0.1:4199/wb/reports/pnl')
    await page.evaluate(() => { window.__vellaReportPeriods = { pnl: { days: 7, mode: 'custom', fromIso: '2026-10-05', toIso: '2026-10-11', label: '7 дней' } } })
    for (const css of output.filter(r => r.type === 'asset' && r.fileName.endsWith('.css'))) {
      if (css.type === 'asset') await page.addStyleTag({ content: String(css.source) })
    }
    await page.addScriptTag({ content: bundle.code })
    const body = page.locator('#tab-pnl [data-vella-island="pnl-live-table-body"]')
    await body.waitFor({ timeout: 8000 }).catch(async error => { throw new Error(`${error}\n${JSON.stringify({ errors, unexpected, financialRequests, allRequests, text: (await page.locator('body').innerText()).slice(-600) })}`) })
    const count = async () => Number(await body.getAttribute('data-vella-row-count'))
    const trigger = page.locator('#pnl-filters-trigger'), panel = page.locator('#pnl-product-filters')
    const open = async () => { await trigger.click(); await panel.waitFor(); await panel.getByRole('button', { name: 'Применить', exact: true }).isEnabled() }
    const clear = async () => { if (!await panel.isVisible()) await open(); await panel.getByRole('button', { name: 'Очистить', exact: true }).click(); expect(await count()).toBe(120) }
    const apply = async () => { await panel.getByRole('button', { name: 'Применить', exact: true }).click(); await panel.waitFor({ state: 'hidden' }) }
    const verify = async (allowed: number[]) => {
      const expected = rows.filter((_, i) => allowed.includes(i % 6))
      expect(await count()).toBe(expected.length)
      const actual = await body.locator('tr[data-report-row="pnl"]').allTextContents()
      expect(actual.map(text => Number(text.match(/WB (\d+)/)?.[1]))).toEqual(expected.slice(0, 50).map(row => row.nmId))
    }
    expect(await count()).toBe(120)
    expect(await body.locator('tr[data-report-row="pnl"]').count()).toBe(50)
    await open()
    await panel.getByRole('button', { name: /^Alpha/ }).click()
    expect(await count()).toBe(120) // Draft selection has not been applied.
    await panel.press('Escape')
    expect(await trigger.getAttribute('aria-expanded')).toBe('false')
    await open(); expect(await panel.getByRole('button', { name: /^Все бренды/ }).getAttribute('aria-pressed')).toBe('true')
    await page.screenshot({ path: path.join(root, '../output/playwright/pnl-filters-desktop.png') })
    await page.setViewportSize({ width: 640, height: 900 })
    expect(await page.getByText('P&L доступен в desktop-версии', { exact: true }).isVisible()).toBe(true) // Preserve the existing mobile gate.
    await page.screenshot({ path: path.join(root, '../output/playwright/pnl-filters-mobile.png') })
    await page.setViewportSize({ width: 1512, height: 982 })
    await panel.getByRole('button', { name: /^Alpha/ }).click(); await apply(); await verify([0, 2, 4])
    const cases: Array<[string, string, number[]]> = [
      ['Сегменты', 'Все', groups], ['Сегменты', 'SKU ниже порогов', [0, 1, 3, 4]], ['Сегменты', 'Без P_min', [0]],
      ['Сегменты', 'Маржа ниже 0', [0, 4]], ['Сегменты', 'Изменения 24ч', [0]], ['Сегменты', 'Неликвид', [3]],
      ['Ответственный', 'Все менеджеры', groups], ['Ответственный', 'Мои товары', [0, 3]], ['Ответственный', 'Без ответственного', [2, 5]],
      ['Ответственный', 'One', [0, 3]], ['Ответственный', 'Two', [1, 4]],
      ['Состояние товара', 'Все товары', groups], ['Состояние товара', 'Локомотивы', [1]], ['Состояние товара', 'Новинки', [2]],
      ['Состояние товара', 'Неликвид', [3]], ['Состояние товара', 'Ликвидация', [4]], ['Состояние товара', 'Ручной режим', [0]], ['Состояние товара', 'Без минимальной цены', [0]],
      ['Акции', 'Все акции', groups], ['Акции', 'В акции', [0, 2, 4]], ['Акции', 'Без акции', [1, 3]],
      ['Бренд', 'Все бренды', groups], ['Бренд', 'Alpha', [0, 2, 4]], ['Бренд', 'Beta', [1, 3, 5]],
      ['Стратегия', 'Все стратегии', groups], ['Стратегия', 'First strategy', [0, 2, 4]], ['Стратегия', 'Second strategy', [1, 3, 5]],
      ['Остаток', 'Любой', groups], ['Остаток', 'Заканчивается ≤5', [0, 4]], ['Остаток', 'Низкий 6–20', [1]], ['Остаток', 'Норма 21–100', [2]], ['Остаток', 'Избыток >100', [3]],
    ]
    for (const [group, name, allowed] of cases) {
      await clear(); await open()
      await panel.getByRole('group', { name: group, exact: true }).getByRole('button', { name: new RegExp(`^${name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(?:\\d|\\s|$)`) }).click()
      await apply(); await verify(allowed)
    }
    for (const [group, labels] of [['Тип одежды', ['Футболка', 'Худи', 'Лонгслив', 'Шорты', 'Балаклава']], ['Пол / цвет', ['Жен.', 'Муж.', 'Белый', 'Чёрный']]] as const) {
      for (let selected = 0; selected < labels.length; selected++) {
        await clear(); await open()
        for (const label of labels.filter((_, i) => i !== selected)) await panel.getByRole('group', { name: group, exact: true }).getByRole('button', { name: label, exact: true }).click()
        await apply(); await verify(group === 'Тип одежды' ? [selected] : selected % 2 ? [1, 3] : [0, 2, 4])
      }
    }
    for (const [label, from, to, allowed] of [
      ['Маржа, %', '-5', '0', [0, 2, 4]], ['Корзины за период', '0', '0', [0]],
      ['Заказы за период', '1', '', [1, 2, 3, 4]], ['Цена, ₽', '', '200,00', [0, 1]],
    ] as const) {
      await clear(); await open()
      await panel.getByLabel(`${label} от`, { exact: true }).fill(from)
      await panel.getByLabel(`${label} до`, { exact: true }).fill(to)
      await apply(); await verify([...allowed])
    }
    await clear(); await open(); await panel.getByLabel('Цена, ₽ от', { exact: true }).fill('abc')
    expect(await panel.getByRole('button', { name: 'Применить', exact: true }).isDisabled()).toBe(true)
    await panel.getByLabel('Цена, ₽ от', { exact: true }).fill('20'); await panel.getByLabel('Цена, ₽ до', { exact: true }).fill('10')
    expect(await panel.getByRole('alert').innerText()).toContain('больше')
    await clear()
    await page.getByPlaceholder('SKU для финансового разбора...').fill('SKU-120')
    expect(await count()).toBe(1) // Matching product was outside the initial render window.
    expect(await body.innerText()).toContain('Product 120')
    await clear()
    await page.getByRole('columnheader', { name: /^Выручка/ }).click()
    expect(await page.getByRole('columnheader', { name: /^Выручка/ }).getAttribute('aria-sort')).toBe('descending')
    await open(); await panel.getByRole('group', { name: 'Бренд', exact: true }).getByRole('button', { name: /^Alpha/ }).click(); await apply()
    const first = body.locator('tr[data-report-row="pnl"]').first()
    expect(await first.innerText()).toContain('Product 119')
    expect(await first.innerText()).toContain('101,18 ₽') // Original supplied revenue remains unchanged.
    await page.getByRole('columnheader', { name: /^Выручка/ }).click()
    expect(await page.getByRole('columnheader', { name: /^Выручка/ }).getAttribute('aria-sort')).toBe('ascending')
    expect(await body.locator('tr[data-report-row="pnl"]').first().innerText()).toContain('Product 1')
    expect(metadataRequests).toEqual(['2026-10-05'])
    expect(financialRequests).toHaveLength(1)
    await clear(); await page.getByRole('combobox', { name: 'Ответственный P&L' }).selectOption('mine')
    expect(await count()).toBe(40)
    await open(); expect(await panel.getByRole('group', { name: 'Ответственный', exact: true }).getByRole('button', { name: /^Мои товары/ }).getAttribute('aria-pressed')).toBe('true')
    await clear()
    // A late metadata response for an older period cannot replace the newer one.
    await open()
    const changePeriod = async (from: string, to: string) => {
      await page.evaluate(({ from, to }) => {
        window.__vellaReportPeriods!.pnl = { days: 7, mode: 'custom', fromIso: from, toIso: to, label: '7 дней' }
        window.dispatchEvent(new CustomEvent('vella:report-period-updated', { detail: { reportKey: 'pnl' } }))
      }, { from, to })
    }
    await changePeriod('2026-10-12', '2026-10-18')
    await page.waitForFunction(() => document.querySelector('#pnl-product-filters [role="status"]') !== null)
    await changePeriod('2026-10-19', '2026-10-25')
    await panel.getByRole('button', { name: 'Применить', exact: true }).waitFor()
    await page.waitForFunction(() => !document.querySelector('#pnl-product-filters [role="status"]'))
    holdMetadata?.()
    await panel.getByRole('group', { name: 'Бренд', exact: true }).getByRole('button', { name: /^Alpha/ }).click(); await apply()
    expect(await count()).toBe(60)
    await page.getByRole('button', { name: 'Switch test organization' }).click()
    await page.waitForFunction(() => document.querySelector('#pnl-filters-trigger')?.textContent === 'Фильтры')
    expect(await count()).toBe(120)
    expect(unexpected).toEqual([])
    expect(errors).toEqual([])
  } finally { await browser.close() }
}, 120_000)

it('preserves the Repricer reference filter controls and reset behavior', async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({ configFile: false, envFile: false, root, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""' },
    resolve: { alias: { '@': path.join(root, 'src') } }, build: { write: false, minify: false, lib: {
      entry: fileURLToPath(new URL('./__fixtures__/repricerStatsRaceBrowser.ts', import.meta.url)), formats: ['iife'], name: 'RepricerFilterReference',
    } } })
  const bundle = (Array.isArray(result) ? result : [result]).flatMap(r => 'output' in r ? r.output : []).find(r => r.type === 'chunk' && r.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing Repricer fixture')
  const browser = await chromium.launch({ headless: true })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block' })
    await page.route('**/*', route => {
      const url = new URL(route.request().url())
      if (url.pathname === '/wb/repricer/stats') return route.fulfill({ contentType: 'text/html', body: `<button id="reload">Reload</button><button id="logout">Logout</button>
        <section id="tab-repricer-stats"><div class="search"><input></div><button id="repricerStatsFiltersTrigger">Фильтры</button>
        <div id="repricerStatsFilters" hidden></div><button id="repricerStatsReset">Сбросить</button>
        <span id="repricerStatsFilterLabel"></span><div class="stats"></div><div data-filter-summary><span></span></div>
        <table><thead><tr><th data-stats-column="impressions">Показы</th></tr></thead><tbody id="repricerStatsBody"></tbody></table></section>` })
      if (url.pathname !== '/api/v1/wb-repricer/stats') return route.abort()
      return route.fulfill({ json: { total: 2, dateFrom: url.searchParams.get('dateFrom'), dateTo: url.searchParams.get('dateTo'),
        items: ['Alpha', 'Beta'].map((brand, i) => ({ articleId: brand, brand, metrics: { impressions: i + 10 }, sources: {}, priceProtection: {}, decision: {} })), summary: { baskets: 0, orders: 0 } } })
    })
    await page.goto('http://127.0.0.1:4199/wb/repricer/stats')
    await page.evaluate(() => { window.__vellaReportPeriods = { 'repricer-stats': { days: 7, mode: 'custom', fromIso: '2026-08-01', toIso: '2026-08-07', label: '7 дней' } } })
    await page.addScriptTag({ content: bundle.code })
    await page.waitForFunction(() => document.body.dataset.firstSettled === 'true')
    await page.locator('#repricerStatsFiltersTrigger').click()
    await page.locator('[data-stats-key="brand"][data-stats-value="Beta"]').first().click()
    expect(await page.locator('#repricerStatsBody [data-report-row]:visible').count()).toBe(1)
    expect(await page.locator('#repricerStatsBody').innerText()).toContain('Beta')
    await page.locator('#repricerStatsReset').click()
    expect(await page.locator('#repricerStatsBody [data-report-row]:visible').count()).toBe(2)
  } finally { await browser.close() }
}, 30_000)
