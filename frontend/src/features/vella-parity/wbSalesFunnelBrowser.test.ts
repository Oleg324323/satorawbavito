import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { expect, it } from 'vitest'
import { previousFunnelPeriod } from '../wb-reports/funnelDynamics'

it('renders only the funnel and table, keeps one working period, and does not resync on sorting or pagination', async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({
    configFile: false, envFile: false, root, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""' },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: { entry: fileURLToPath(new URL('./__fixtures__/reportLoadingBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'FunnelSmoke' } },
  })
  const output = (Array.isArray(result) ? result : [result]).flatMap(item => 'output' in item ? item.output : [])
  const bundle = output.find(item => item.type === 'chunk' && item.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing funnel smoke bundle')
  const css = output.filter(item => item.type === 'asset' && item.fileName.endsWith('.css')).map(item => item.type === 'asset' ? String(item.source) : '').join('\n')
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 950 }, serviceWorkers: 'block' })
    page.setDefaultTimeout(5000)
    await page.clock.setFixedTime(new Date('2026-10-10T12:00:00Z'))
    const reads: URLSearchParams[] = [], starts: string[] = [], unexpected: string[] = [], errors: string[] = []
    let settled = false
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', route => {
      const request = route.request(), url = new URL(request.url())
      if (request.resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (request.resourceType() === 'image') return route.fulfill({ contentType: 'image/gif', body: Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64') })
      if (url.origin === 'https://fonts.googleapis.com') return route.fulfill({ contentType: 'text/css', body: '' })
      if (url.pathname === '/api/v1/cabinet/team/users') return route.fulfill({ json: { data: [] } })
      if (url.pathname === '/api/v1/cabinet/wb-token') return route.fulfill({ json: { data: { hasToken: false } } })
      if (url.pathname === '/api/v1/cabinet/avito-credentials') return route.fulfill({ json: { data: { hasCredentials: false } } })
      if (url.pathname === '/api/wb/reports/work-demand/release') return route.fulfill({ json: {} })
      if (url.pathname === '/api/wb/reports/sales-funnel/totals-comparison') return route.fulfill({ json: {
        source: 'calculated_wb_sales_funnel_totals_v1',
        ...previousFunnelPeriod(url.searchParams.get('dateFrom')!, url.searchParams.get('dateTo')!),
        selectedDateFrom: url.searchParams.get('dateFrom'), selectedDateTo: url.searchParams.get('dateTo'),
        total: 51, complete: false, refreshRequired: false, totals: {}, selectedTotals: {}, previousTotals: {},
        ready: {}, unavailable: {}, coverage: {},
      } })
      if (url.pathname === '/api/wb/reports/sales-funnel/comparison') return route.fulfill({ json: {
        source: 'wb_sales_funnel_comparison_v1',
        ...previousFunnelPeriod(url.searchParams.get('dateFrom')!, url.searchParams.get('dateTo')!),
        selectedDateFrom: url.searchParams.get('dateFrom'), selectedDateTo: url.searchParams.get('dateTo'),
        items: [], totals: {}, total: 51, complete: false, refreshRequired: false,
        comparableProducts: {}, unavailable: {}, brands: [], coveredProducts: 0, page: 1, pageSize: 1,
      } })
      if (url.pathname === '/api/wb/reports/sales-funnel/refresh') { starts.push(url.search); return route.fulfill({ json: { status: 'partial', message: 'Synthetic partial coverage' } }) }
      if (url.pathname === '/api/wb/reports/sales-funnel') {
        reads.push(url.searchParams)
        const second = url.searchParams.get('page') === '2'
        const metrics = { visits: 100, carts: 20, orders: 8, orderAmount: 2343415, buyouts: 4, buyoutAmount: 1000000, buyoutPercent: 40, impressions: null, ctr: null, revenueShare: 2, cartConversion: 20, orderConversion: 40 }
        const items = Array.from({ length: second ? 1 : 50 }, (_, index) => ({ nmId: String(second ? 51 : index + 1), sellerArticle: second ? 'SECOND-PAGE' : index === 0 ? 'FIRST-PAGE' : `SKU-${index+1}`, title: 'Synthetic WB product', imageUrl: null, metrics }))
        return route.fulfill({ json: { items, totals: metrics, total: 51, page: second ? 2 : 1, pageSize: 50, complete: false, coveredProducts: 50, brands: ['A'], dateFrom: url.searchParams.get('dateFrom'), dateTo: url.searchParams.get('dateTo'), unavailable: { impressions: 'Показы недоступны в API WB', ctr: 'Нет показов' }, job: { status: 'partial' }, ...(settled ? { refreshRequired: false, unreportedProducts: 1, reportComplete: true, conversionCoverage: { cartConversion: { products: 50, total: 51, visits: 100, carts: 20 } } } : {}) } })
      }
      unexpected.push(`${request.method()} ${url.pathname}`)
      return route.abort()
    })
    await page.goto('http://localhost/wb/reports/digest')
    await page.addStyleTag({ content: css })
    await page.addScriptTag({ content: bundle.code })
    const funnel = page.locator('.wb-sales-funnel')
    await funnel.locator('.sf-table tbody tr').first().waitFor({ state: 'visible', timeout: 15000 })
    expect(await funnel.locator('.sf-stage').count()).toBe(4)
    expect(await funnel.locator('thead th').count()).toBe(11)
    expect(await funnel.getByRole('searchbox').count()).toBe(0)
    expect(await funnel.getByRole('combobox').count()).toBe(0)
    expect(await funnel.getByRole('columnheader', { name: /^Показы|^CTR/ }).count()).toBe(0)
    expect(await funnel.locator('.sf-note').count()).toBe(0)
    expect(await funnel.locator('.sf-stage').first().innerText()).toContain('Перешли в карточку')
    expect((await funnel.locator('.sf-stage').allTextContents()).join(' ')).not.toContain('Показы')
    expect(await page.locator('#globalPeriod').count()).toBe(1)
    expect(await page.locator('[data-vella-island="live-status-control"]').count()).toBe(0)
    expect(await page.locator('#tab-digest .balance-chart-card, #tab-digest .planfact-panel, #tab-digest .digest-period-context, #tab-digest .digest-action-queue').count()).toBe(0)
    await expect.poll(() => starts.length).toBe(1)
    const scroller = page.locator('#tab-digest:has(> .wb-sales-funnel)')
    for (const width of [1440, 768]) {
      await page.setViewportSize({ width, height: 700 })
      const conversionLines = await funnel.locator('th.sf-conversion button').evaluateAll(buttons => buttons.map(button => {
        const range = document.createRange()
        range.selectNodeContents(button)
        return new Set(Array.from(range.getClientRects()).filter(rect => rect.width > 0).map(rect => Math.round(rect.top))).size
      }))
      expect(conversionLines).toEqual([1, 1])
      await scroller.evaluate(el => { el.scrollTop = 0 })
      expect(await scroller.evaluate(el => el.scrollHeight > el.clientHeight)).toBe(true)
      // Hidden fixed calendars/drawer backdrops must not push the real header
      // below the viewport while computing the sticky toolbar offset.
      await expect.poll(() => funnel.locator('thead').evaluate(head => {
        const bounds = head.getBoundingClientRect()
        const wrap = head.closest('table')!.parentElement!.getBoundingClientRect()
        return Math.abs(bounds.top - wrap.top)
      })).toBeLessThan(4)
      await funnel.locator('.sf-stage').first().hover()
      await page.mouse.wheel(0, 10000)
      await expect.poll(() => scroller.evaluate(el => el.scrollTop + el.clientHeight >= el.scrollHeight - 2), { timeout: 5000 }).toBe(true)
      const pagination = await funnel.locator('.sf-pagination').boundingBox()
      expect(pagination && pagination.y >= 0 && pagination.y + pagination.height <= 700).toBe(true)
      // Vertical wheel events over the horizontally scrollable table must reach the tab too.
      await scroller.evaluate(el => { el.scrollTop = 800 })
      await expect.poll(() => funnel.locator('thead').evaluate(head => {
        const bounds = head.getBoundingClientRect(), tab = head.closest('.tab-content')!.getBoundingClientRect()
        const cell = head.querySelector('th')!.getBoundingClientRect()
        const hit = document.elementFromPoint(cell.left + 20, cell.top + cell.height / 2)
        return Math.abs(bounds.top - tab.top) < 2 && !!hit && head.contains(hit)
      })).toBe(true)
      await funnel.locator('tbody tr').nth(5).locator('.sf-product').hover()
      const previousTop = await scroller.evaluate(el => el.scrollTop)
      await page.mouse.wheel(0, 500)
      await expect.poll(() => scroller.evaluate(el => el.scrollTop), { timeout: 5000 }).toBeGreaterThan(previousTop)
    }
    await page.setViewportSize({ width: 1440, height: 950 })
    await funnel.getByRole('button', { name: 'Далее', exact: true }).click()
    await expect.poll(() => funnel.locator('tbody').innerText(), { timeout: 5000 }).toContain('SECOND-PAGE')
    await funnel.getByRole('button', { name: /^Заказали на сумму/ }).click()
    await expect.poll(() => reads.at(-1)?.get('sort')).toBe('orderAmount')
    await expect.poll(() => funnel.locator('tbody').innerText()).toContain('FIRST-PAGE')
    expect(starts.length).toBe(1)
    const wrap = funnel.locator('.sf-table-wrap')
    const before = await funnel.locator('tbody .sf-product').first().evaluate(el => el.getBoundingClientRect().left)
    await wrap.evaluate(el => { el.scrollLeft = 900 })
    const after = await funnel.locator('tbody .sf-product').first().evaluate(el => el.getBoundingClientRect().left)
    const stickyInfo = await funnel.locator('tbody .sf-product').first().evaluate(el => ({ position: getComputedStyle(el).position, left: getComputedStyle(el).left, table: el.closest('table')?.getBoundingClientRect().toJSON(), wrap: el.closest('.sf-table-wrap')?.getBoundingClientRect().toJSON() }))
    expect(Math.abs(after-before), JSON.stringify({ before, after, stickyInfo })).toBeLessThan(2)
    await page.getByRole('textbox', { name: 'Начало периода аналитики WB', exact: true }).fill('01.10.2026')
    await page.getByRole('textbox', { name: 'Конец периода аналитики WB', exact: true }).fill('08.10.2026')
    await page.locator('#globalPeriod').getByRole('button', { name: 'Применить', exact: true }).click()
    await expect.poll(() => reads.at(-1)?.get('dateTo')).toBe('2026-10-08')
    expect(reads.at(-1)?.get('dateFrom')).toBe('2026-10-01')
    await expect.poll(() => starts.length).toBe(2)
    if (process.env.WB_FUNNEL_SMOKE_SCREENSHOT) await page.screenshot({ path: process.env.WB_FUNNEL_SMOKE_SCREENSHOT })
    await page.setViewportSize({ width: 768, height: 950 })
    const stageWidths = await funnel.locator('.sf-stage').evaluateAll(elements => elements.map(el => el.getBoundingClientRect().width))
    expect(stageWidths.every((width, i) => i === 0 || width < stageWidths[i-1])).toBe(true)
    settled = true
    await page.goto('http://localhost/wb/reports/digest')
    await page.addStyleTag({ content: css })
    await page.addScriptTag({ content: bundle.code })
    await funnel.locator('.sf-table tbody tr').first().waitFor({ state: 'visible' })
    expect(await funnel.locator('.sf-state').innerText()).toBe('Данные за выбранный период загружены.')
    expect(await funnel.getByRole('button', { name: 'Повторить загрузку', exact: true }).count()).toBe(0)
    expect(await funnel.locator('.sf-stage-1').innerText()).toContain('CR 20%')
    expect(await funnel.locator('.sf-stage-0').innerText()).not.toContain('CTR')
    expect(starts.length).toBe(2) // Reopening fresh, exhausted report is read-only.
    expect(errors).toEqual([])
    expect(unexpected).toEqual([])
  } finally { await browser.close() }
}, 45000)
