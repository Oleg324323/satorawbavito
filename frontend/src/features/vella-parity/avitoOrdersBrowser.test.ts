import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { beforeAll, expect, it } from 'vitest'

let code: string
beforeAll(async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({
    root, configFile: false, envFile: false, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': JSON.stringify('test'), 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('') },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: {
      entry: fileURLToPath(new URL('./__fixtures__/reportLoadingBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'AvitoOrdersTest',
    } },
  })
  const outputs = Array.isArray(result) ? result : [result]
  const chunk = outputs.flatMap(output => 'output' in output ? output.output : []).find(output => output.type === 'chunk' && output.isEntry)
  if (!chunk || chunk.type !== 'chunk') throw new Error('Missing Avito Orders fixture bundle')
  code = chunk.code
}, 60_000)

it.each(['populated', 'populated-no-extension', 'populated-chat-confirmed', 'populated-chat-review', 'empty', 'error'])('renders read-only Avito Orders %s with one request owner', async outcome => {
  const browser = await chromium.launch({ headless: true })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1440, height: 1000 } })
    await page.clock.setFixedTime(new Date('2026-09-09T12:00:00Z'))
    const queries: string[] = [], auxiliary: string[] = [], unexpected: string[] = [], errors: string[] = []
    let phase = 'initial'
    const populated = outcome.startsWith('populated')
    const queryPhases: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', route => {
      const request = route.request(), url = new URL(request.url())
      if (request.method() === 'GET' && url.origin === 'http://satorna.test' && url.pathname === '/avito/orders') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (request.method() === 'GET' && request.resourceType() === 'image') return route.fulfill({ contentType: 'image/gif', body: Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64') })
      if (request.method() === 'GET' && url.origin === 'https://fonts.googleapis.com' && url.pathname === '/css2') return route.fulfill({ contentType: 'text/css', body: '' })
      if (request.method() === 'GET' && url.origin === 'http://satorna.test' && ['/api/v1/avito/orders/extension-token', '/api/v1/avito/orders/returns-sync'].includes(url.pathname)) {
        auxiliary.push(url.pathname)
        return route.fulfill({ contentType: 'application/json', body: JSON.stringify(url.pathname.endsWith('extension-token')
          ? { configured: false } : { enabled: false, intervalMinutes: 60, periodDays: 30 }) })
      }
      if (request.method() !== 'GET' || url.origin !== 'http://satorna.test' || url.pathname !== '/api/v1/avito/orders/queue') {
        unexpected.push(`${request.method()} ${url.pathname}`); return route.abort()
      }
      queries.push(url.search)
      queryPhases.push(phase)
      if (outcome === 'error') return route.fulfill({ status: 503, contentType: 'application/json', body: '{"error":{"code":"SYNTHETIC_ORDERS_UNAVAILABLE","message":"Synthetic Orders unavailable"}}' })
      // Full required AvitoOrdersBackendResponse/AvitoOrderRow/AvitoOrderItem fields from the consumer.
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({
        status: 'synced',
        filters: { mode: 'active', page: 1, limit: 50, accountId: null, search: '', historyFrom: null },
        summary: { total: populated ? 1 : 0, shown: populated ? 1 : 0, confirmation: 0, readyToShip: populated ? 1 : 0,
          inTransit: 0, delivered: 0, closed: 0, canceled: 0, returns: 0, disputes: 0, requiredActions: 0,
          items: populated ? 1 : 0, totalKopecks: populated ? 450000 : 0, statusCounts: populated ? { ready_to_ship: 1 } : {} },
        rows: populated ? [{ orderId: 'synthetic-order-1', marketplaceId: 'synthetic-market-1', accountId: 'synthetic-account', accountName: 'Synthetic account',
          status: 'ready_to_ship', deliveryType: null, deliveryService: null, dropoffProvider: 'Яндекс Доставка', createdAt: '2026-09-09T10:00:00Z', updatedAt: '2026-09-09T12:00:00Z',
          buyerName: null, buyerId: null, buyerPhone: null, recipientName: null, recipientPhone: null, address: null, trackNumber: null,
          returnStatus: null, totalKopecks: 450000, availableActions: [], schedules: [], sourceStatus: 'synced',
          items: [{ itemId: 'synthetic-item-1', title: 'Synthetic read-only order product', quantity: 1, priceKopecks: 450000,
            sellerArticle: 'SYNTHETIC-ORDER-SKU', size: outcome === 'populated-chat-review' ? null : 'M',
            ...(outcome.includes('-chat-') ? { sizeMode: 'chat_ai', sizeState: outcome.endsWith('confirmed') ? 'confirmed' : 'needs_review',
              sizeReason: outcome.endsWith('confirmed') ? null : 'ambiguous_reply', sizeEvidence: outcome.endsWith('confirmed') ? { reply: 'Мне нужен М' } : {} } : {}),
            color: 'Synthetic blue', imageUrl: null, returnMatches: [], reuseSuggestion: null }],
        }] : [],
        source: { browserSnapshot: outcome === 'populated' ? { capturedAt: '2026-09-09T12:00:00Z', orders: 1, items: 1, collector: {} } : null },
      }) })
    })
    await page.goto('http://satorna.test/avito/orders')
    const response = page.waitForResponse(response => new URL(response.url()).pathname === '/api/v1/avito/orders/queue')
    await page.addScriptTag({ content: code })
    await response
    await expect.poll(() => auxiliary.length, { timeout: 15_000 }).toBe(2)
    expect(queries).toHaveLength(1)
    const query = new URLSearchParams(queries[0])
    expect([...query.entries()]).toEqual([['mode', 'active'], ['limit', '50'], ['page', '1']])
    const surface = page.locator('#tab-orders-print')
    if (populated) {
      await surface.getByText('Synthetic read-only order product', { exact: true }).first().waitFor({ timeout: 15_000 })
      expect(await surface.getByRole('columnheader', { name: 'Пункт приема', exact: true }).count()).toBe(1)
      expect(await surface.locator('.orders-picking-table tbody').getByText('Яндекс Доставка', { exact: true }).count()).toBe(1)
      if (outcome === 'populated-chat-confirmed') {
        expect(await surface.getByText('Из ответа покупателя', { exact: true }).count()).toBe(1)
        expect(await surface.locator('td[title="Мне нужен М"]').innerText()).toContain('M')
      }
      if (outcome === 'populated-chat-review') {
        expect(await surface.getByText('Размер неоднозначен', { exact: true }).count()).toBe(1)
        expect(await surface.getByText('Нужна проверка', { exact: true }).count()).toBe(1)
      }
      expect(await surface.locator('.avito-orders-extension-empty').isVisible()).toBe(false)
      await surface.getByText('Synthetic read-only order product', { exact: true }).first().click()
      await surface.locator('.avito-order-detail-window').waitFor({ state: 'visible' })
      expect(await surface.locator('.avito-order-detail-window').innerText()).toContain('SYNTHETIC-ORDER-SKU')
      await surface.locator('.avito-order-detail-window .modal-close').click()
      const beforeLogout = [...queries]
      phase = 'logout'
      await page.getByRole('button', { name: 'Clear synthetic report session', exact: true }).click()
      await expect.poll(() => surface.getByText('Synthetic read-only order product', { exact: true }).count()).toBe(0)
      await surface.getByText('Не удалось загрузить заказы', { exact: true }).waitFor()
      expect(await surface.locator('.avito-orders-extension-empty').isVisible()).toBe(false)
      expect({ beforeLogout, queries, queryPhases, auxiliary, unexpected, errors }).toEqual({
        beforeLogout: [queries[0]], queries: [queries[0]], queryPhases: ['initial'],
        auxiliary: ['/api/v1/avito/orders/extension-token', '/api/v1/avito/orders/returns-sync'], unexpected: [], errors: [],
      })
      expect(auxiliary).toHaveLength(2)
    } else if (outcome === 'empty') {
      await surface.getByText('Нет заказов в этом режиме', { exact: true }).waitFor({ state: 'visible' })
      expect(await surface.getByRole('button', { name: /Обновить данные/ }).isVisible()).toBe(true)
      expect(await surface.getByText('Synthetic read-only order product', { exact: true }).count()).toBe(0)
    } else {
      const expectedTitle = 'Не удалось загрузить заказы'
      await expect.poll(async () => ({
        expectedStateVisible: await surface.getByText(expectedTitle, { exact: true }).isVisible(),
        onboardingVisible: await surface.locator('.avito-orders-extension-empty').isVisible(),
      }), { timeout: 3000 }).toEqual({ expectedStateVisible: true, onboardingVisible: false })
    }
    expect(unexpected).toEqual([])
    expect(errors).toEqual([])
  } finally { await browser.close() }
}, 45_000)

it('exports picking and returns from saved data without invoking the extension or opening tabs', async () => {
  const browser = await chromium.launch({ headless: true })
  try {
    const page = await browser.newPage({ acceptDownloads: true })
    await page.clock.setFixedTime(new Date('2026-09-09T12:00:00Z'))
    const exports: string[] = [], unexpected: string[] = [], popups: string[] = []
    page.on('popup', popup => popups.push(popup.url()))
    await page.addInitScript(() => {
      Object.assign(window, { syntheticExtensionRequests: [] })
      window.addEventListener('message', event => {
        if (event.data?.source === 'satorna-web') (window as unknown as { syntheticExtensionRequests: string[] }).syntheticExtensionRequests.push(event.data.type)
      })
    })
    await page.route('**/*', route => {
      const url = new URL(route.request().url())
      if (url.pathname === '/avito/orders') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (url.hostname === 'fonts.googleapis.com') return route.fulfill({ contentType: 'text/css', body: '' })
      if (url.pathname === '/api/v1/avito/orders/extension-token') return route.fulfill({ json: { configured: true } })
      if (url.pathname === '/api/v1/avito/orders/returns-sync') return route.fulfill({ json: { enabled: false } })
      if (url.pathname === '/api/v1/avito/orders/picking-list/freshness') return route.fulfill({ json: { fresh: true } })
      if (url.pathname.endsWith('-list.xlsx')) {
        exports.push(url.pathname)
        return route.fulfill({ contentType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', headers: { 'content-disposition': 'attachment; filename="synthetic.xlsx"' }, body: Buffer.from('synthetic-export') })
      }
      if (url.pathname === '/api/v1/avito/orders/queue') return route.fulfill({ json: {
        status: 'synced', filters: { mode: 'active', page: 1, limit: 50, accountId: null, search: '', historyFrom: null }, accounts: [],
        summary: { total: 1, shown: 1, items: 1, confirmation: 0, readyToShip: 1, inTransit: 0, delivered: 0,
          closed: 0, canceled: 0, returns: 0, disputes: 0, requiredActions: 0, totalKopecks: 450000, statusCounts: { ready_to_ship: 1 } }, source: { complete: true },
        rows: [{ orderId: 'synthetic', marketplaceId: '000123', accountId: 'account', accountName: 'Synthetic', status: 'ready_to_ship',
          createdAt: '2026-09-09T10:00:00Z', updatedAt: '2026-09-09T12:00:00Z',
          totalKopecks: 450000, availableActions: [], schedules: [], sourceStatus: 'synced',
          items: [{ itemId: '123', title: 'Synthetic export item', quantity: 1, priceKopecks: 450000,
            sellerArticle: 'SYNTHETIC', imageUrl: null, size: 'M', color: 'black', returnMatches: [], reuseSuggestion: null }] }],
      } })
      if (route.request().resourceType() === 'image') return route.fulfill({ contentType: 'image/gif', body: Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64') })
      unexpected.push(url.pathname); return route.abort()
    })
    await page.goto('http://satorna.test/avito/orders')
    await page.addScriptTag({ content: code })
    const surface = page.locator('#tab-orders-print')
    await surface.getByText('Synthetic export item', { exact: true }).first().waitFor({ timeout: 5000 }).catch(async error => {
      throw new Error(`${error.message}\n${(await surface.innerText()).slice(0, 3500)}\nUnexpected: ${unexpected.join(',')}`)
    })
    for (const name of ['Сформировать лист подбора', 'Сформировать лист возвратов']) {
      const download = page.waitForEvent('download')
      await surface.getByRole('button', { name, exact: true }).click()
      expect((await download).suggestedFilename()).toBe('synthetic.xlsx')
    }
    expect(exports).toEqual(['/api/v1/avito/orders/picking-list.xlsx', '/api/v1/avito/orders/returns-list.xlsx'])
    expect(await page.evaluate(() => (window as unknown as { syntheticExtensionRequests: string[] }).syntheticExtensionRequests)).toEqual([])
    expect(popups).toEqual([])
    expect(unexpected).toEqual([])
  } finally { await browser.close() }
}, 45_000)
