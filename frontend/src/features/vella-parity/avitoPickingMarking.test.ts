import path from 'node:path'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { describe, expect, it } from 'vitest'
import { encodeAvitoExtensionConnectionCode } from '@/features/avito/extensionConnectionCode'

function ordersResponse(search = '', refreshing = false) {
  return {
    status: refreshing ? 'blocked' : 'synced', filters: { mode: 'active', accountId: null, page: 1, limit: 50, search, historyFrom: null },
    summary: { total: search ? 0 : 1, shown: search ? 0 : 1, confirmation: 0, readyToShip: 1, inTransit: 0, delivered: 0,
      closed: 0, canceled: 0, returns: 0, disputes: 0, requiredActions: 0, items: 2,
      totalKopecks: 10000, statusCounts: { ready_to_ship: 1 } },
    rows: search ? [] : [{
      orderId: 'TEST-ORDER-1', jobNumber: 'TEST-JOB-1', shipmentNumber: '001 286 40390', stickerNumber: 'TEST-STICKER-1', stickerNumberState: 'confirmed', stickerLabelId: 1, stickerDocumentId: 1, accountId: 'test-account', accountName: 'Test account',
      status: 'ready_to_ship', deliveryType: null, deliveryService: null, createdAt: null, updatedAt: null,
      buyerName: null, buyerId: null, buyerPhone: null, recipientName: null, recipientPhone: null, address: null,
      trackNumber: 'TEST-TRACK-1', returnStatus: null, totalKopecks: 10000, availableActions: [], schedules: [], sourceStatus: 'ready_to_ship',
      items: [{ itemId: 'TEST-ITEM-1', title: 'Test shirt', quantity: 2, priceKopecks: 5000, barcode: 'TEST-BARCODE-1',
        sellerArticle: 'TEST-SKU-1', size: 'M', color: 'white', imageUrl: 'http://satorna.test/test-product.png', returnMatches: [], reuseSuggestion: null }],
    }],
    source: { complete: !refreshing, error: refreshing ? { code: 'refresh_in_progress' } : null },
  }
}

describe('Avito picking without the empty marking slot', () => {
  it('aligns loaded row and search-empty colspan while preserving order and sticker identifiers', async () => {
    const root = fileURLToPath(new URL('../../../', import.meta.url))
    const result = await build({
      configFile: false, envFile: false, root, logLevel: 'silent', plugins: [react()],
      define: { 'process.env.NODE_ENV': JSON.stringify('test'), 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('') },
      resolve: { alias: { '@': path.join(root, 'src') } },
      build: { write: false, minify: false,
        lib: { entry: fileURLToPath(new URL('./__fixtures__/avitoPickingBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'AvitoPickingTest' } },
    })
    const outputs = Array.isArray(result) ? result : [result]
    const bundle = outputs.flatMap((output) => 'output' in output ? output.output : [])
      .find((output) => output.type === 'chunk' && output.isEntry)
    if (!bundle || bundle.type !== 'chunk') throw new Error('Missing local test bundle')
    const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
    try {
      const page = await browser.newPage({ serviceWorkers: 'block' })
      const unexpected: string[] = []
      const errors: string[] = []
      let queueRequests = 0
      let tokenCreates = 0
      let exports = 0
      const syntheticToken = 'sat_avito_SYNTHETIC_ONLY_NOT_A_REAL_CREDENTIAL_1234567890'
      const connectionCode = encodeAvitoExtensionConnectionCode('http://satorna.test', syntheticToken)
      page.on('pageerror', (error) => errors.push(error.message))
      await page.route('**/*', async (route) => {
        const request = route.request()
        const url = new URL(request.url())
        if (request.method() === 'POST' && url.origin === 'http://satorna.test' && url.pathname === '/api/v1/avito/orders/extension-token/regenerate') {
          tokenCreates++
          return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ configured: true, token: syntheticToken, tokenPrefix: 'sat_avito_SYNTHETIC' }) })
        }
        if (request.method() !== 'GET' || url.origin !== 'http://satorna.test') {
          unexpected.push(`${request.method()} ${url.pathname}`)
          return route.abort()
        }
        if (url.pathname === '/avito/orders') return route.fulfill({ contentType: 'text/html', body: '<body class="vella-html-parity-root"><div id="root"></div></body>' })
        if (url.pathname === '/favicon.ico') return route.fulfill({ status: 204 })
        if (url.pathname === '/api/v1/avito/orders/picking-list.xlsx') {
          exports++
          return exports === 1
            ? route.fulfill({ contentType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', headers: { 'content-disposition': 'attachment; filename="avito-picking-test.xlsx"' }, body: Buffer.from('synthetic-xlsx') })
            : route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"AVITO_ORDERS_QUEUE_NOT_FRESH"}' })
        }
        if (url.pathname === '/api/v1/avito/orders/labels/1/barcode.png' || url.pathname === '/test-product.png') return route.fulfill({ contentType: 'image/png', body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1cAAAAASUVORK5CYII=', 'base64') })
        let data: unknown
        if (url.pathname === '/api/v1/avito/orders/queue') {
          queueRequests++
          const search = url.searchParams.get('search') ?? ''
          data = ordersResponse(search, !search)
        }
        else if (url.pathname === '/api/v1/avito/orders/extension-token') data = { configured: true, tokenPrefix: 'sat_avito_OLD' }
        else if (url.pathname === '/api/v1/avito/orders/returns-sync') data = { enabled: false, intervalMinutes: 30, periodDays: 30, status: { state: 'ready' }, inventory: { status: 'ready', candidates: 54 } }
        else { unexpected.push(`${request.method()} ${url.pathname}`); return route.abort() }
        return route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) })
      })
      await page.goto('http://satorna.test/avito/orders')
      await page.addScriptTag({ content: bundle.code })
      const table = page.locator('table.orders-picking-table')
      await table.waitFor({ state: 'visible', timeout: 10_000 })
      await table.getByText('TEST-ITEM-1', { exact: true }).waitFor({ timeout: 10_000 })
      expect(queueRequests).toBe(1)
      expect(await page.getByText(/Обновляем в фоне/).isVisible()).toBe(true)
      expect(await page.getByRole('button', { name: 'Сформировать лист подбора', exact: true }).isEnabled()).toBe(true)
      expect(await page.getByRole('button', { name: 'Сформировать лист возвратов', exact: true }).isEnabled()).toBe(true)
      const queueFilters = page.locator('.avito-orders-toolbar .chips')
      expect(await queueFilters.getByRole('button', { name: /^Возвраты/ }).count()).toBe(1)
      expect(await queueFilters.getByRole('button', { name: /^Едут обратно/ }).count()).toBe(0)
      expect(await queueFilters.getByRole('button', { name: /^Требует проверки/ }).count()).toBe(0)
      expect(await queueFilters.getByRole('button').count()).toBe(6)
      expect(await table.innerText()).toContain('Test shirt')
      expect(await table.innerText()).not.toContain('TEST-SKU-1')
      expect(await table.innerText()).not.toContain('TEST-BARCODE-1')
      expect(await table.innerText()).not.toContain('КИЗ')
      expect(await table.locator('thead th').count()).toBe(11)
      expect(await table.getByRole('button', { name: 'Открыть', exact: true }).count()).toBe(0)
      expect(await table.locator('.orders-inline-actions').count()).toBe(0)
      const productPhoto = table.locator('img.orders-picking-photo').first()
      const photoBox = await productPhoto.boundingBox()
      expect(photoBox!.width).toBe(66)
      expect(photoBox!.height).toBe(66)
      expect(await productPhoto.evaluate(el => getComputedStyle(el).objectFit)).toBe('contain')
      for (const name of ['Бренд', 'Артикул продавца', 'Возврат', 'Баркод']) {
        expect(await table.getByRole('columnheader', { name, exact: true }).count()).toBe(0)
      }
      const cells = table.locator('tbody tr').first().locator('td')
      expect(await cells.count()).toBe(11)
      expect(await cells.nth(1).innerText()).toContain('TEST-JOB-1')
      expect(await cells.nth(2).innerText()).toContain('001 286 40390')
      expect(await cells.nth(5).innerText()).toBe('2')
      expect(await cells.nth(8).innerText()).toContain('TEST-STICKER-1')
      await expect.poll(() => cells.nth(8).getByRole('img', { name: 'Штрихкод TEST-STICKER-1' }).count()).toBe(1)
      const barcodeImage = cells.nth(8).getByRole('img', { name: 'Штрихкод TEST-STICKER-1' })
      await expect.poll(() => barcodeImage.evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth > 0)).toBe(true)
      const barcodeLayout = await barcodeImage.evaluate((el: HTMLImageElement) => {
        const rect = el.getBoundingClientRect()
        return { width: rect.width, height: rect.height, ratio: el.naturalWidth / el.naturalHeight, fit: getComputedStyle(el).objectFit }
      })
      expect(barcodeLayout.width).toBe(220)
      expect(barcodeLayout.width / barcodeLayout.height).toBeCloseTo(barcodeLayout.ratio, 2)
      expect(barcodeLayout.fit).toBe('contain')
      expect(await cells.nth(8).getByRole('button', { name: 'Оригинал PDF' }).isVisible()).toBe(true)
      expect(await cells.nth(9).innerText()).toBe('TEST-ITEM-1')
      expect(await page.getByText('54 поз. · синхронизация: готово').isVisible()).toBe(true)
      await page.getByRole('button', { name: 'Настройки расширения', exact: true }).click()
      const dialog = page.getByRole('dialog', { name: 'Настройки расширения Avito Orders' })
      expect(await dialog.getByRole('button', { name: 'Скопировать код подключения' }).isDisabled()).toBe(true)
      expect(await dialog.innerText()).toContain('Это только начало токена')
      await dialog.getByRole('button', { name: 'Создать новый токен' }).click()
      const tokenField = dialog.getByLabel('Код подключения Satorna')
      await expect.poll(() => tokenField.inputValue()).toBe(connectionCode)
      expect(tokenCreates).toBe(1)
      await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
        writeText: async () => { throw new Error('Synthetic clipboard denial') },
      } }))
      await dialog.getByRole('button', { name: 'Скопировать код подключения' }).click()
      await expect.poll(() => dialog.getByRole('status').innerText()).toContain('Код выделен')
      expect(await tokenField.evaluate((el: HTMLTextAreaElement) => el.value.slice(el.selectionStart, el.selectionEnd))).toBe(connectionCode)
      await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
        writeText: async (value: string) => { (window as unknown as { copiedTestToken: string }).copiedTestToken = value },
      } }))
      await dialog.getByRole('button', { name: 'Скопировать код подключения' }).click()
      await expect.poll(() => dialog.getByRole('status').innerText()).toBe('Код подключения скопирован')
      expect(await page.evaluate(() => (window as unknown as { copiedTestToken: string }).copiedTestToken)).toBe(connectionCode)
      await dialog.getByRole('button', { name: 'Закрыть', exact: true }).last().click()
      await page.getByRole('button', { name: 'Настройки расширения', exact: true }).click()
      expect(await tokenField.inputValue()).toBe(connectionCode)
      await page.setViewportSize({ width: 390, height: 844 })
      const tokenBox = await tokenField.boundingBox()
      expect(tokenBox!.x).toBeGreaterThanOrEqual(0)
      expect(tokenBox!.x + tokenBox!.width).toBeLessThanOrEqual(390)
      expect(await tokenField.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true)
      await dialog.getByRole('button', { name: 'Закрыть', exact: true }).last().click()
      await page.setViewportSize({ width: 1280, height: 720 })
      const downloadEvent = page.waitForEvent('download')
      await page.getByRole('button', { name: 'Сформировать лист подбора', exact: true }).click()
      const download = await downloadEvent
      expect(download.suggestedFilename()).toBe('avito-picking-test.xlsx')
      expect(await download.failure()).toBeNull()
      expect(await page.getByRole('region', { name: 'Проверка листа подбора' }).count()).toBe(0)
      expect(exports).toBe(1)
      await page.getByRole('button', { name: 'Сформировать лист подбора', exact: true }).click()
      await expect.poll(() => page.getByRole('alert').innerText(), { timeout: 5000 }).toContain('Сервер временно недоступен')
      expect(await page.getByRole('button', { name: 'Сформировать лист подбора', exact: true }).isEnabled()).toBe(true)
      await page.getByPlaceholder('Поиск по номеру, заданию, артикулу, названию', { exact: true }).fill('not-present-in-fixture')
      const empty = table.locator('tbody tr.avito-orders-empty-row td')
      await empty.waitFor({ state: 'visible' })
      expect(await empty.getAttribute('colspan')).toBe('11')
      expect(await empty.innerText()).toContain('Ничего не нашлось')
      // Include the actual legacy width constraint: the JSX screen-only override
      // must win even when this rule is loaded later, without changing printing.
      const legacyHtml = await readFile(path.join(root, 'public/vella-production.html'), 'utf8')
      const shellRule = legacyHtml.match(/\.orders-print-shell\s*\{[^}]+\}/)?.[0]
      expect(shellRule).toContain('max-width: 1220px')
      await page.addStyleTag({ content: `* { box-sizing: border-box; } body { margin: 0; } ${shellRule}` })
      const shell = page.getByRole('region', { name: 'Лист подбора Авито', exact: true })
      for (const width of [1440, 1920, 2560, 390]) {
        await page.setViewportSize({ width, height: 900 })
        const box = await shell.boundingBox()
        expect(Math.abs(box!.width - (width - 40))).toBeLessThan(2)
        expect(Math.abs(box!.x - 20)).toBeLessThan(2)
        const wrap = await page.locator('.orders-picking-table-wrap').boundingBox()
        expect(wrap!.width).toBeLessThanOrEqual(box!.width + 1)
      }
      await page.setViewportSize({ width: 1920, height: 900 })
      await page.emulateMedia({ media: 'print' })
      expect(Math.round((await shell.boundingBox())!.width)).toBe(1220)
      await page.emulateMedia({ media: 'screen' })
      expect(unexpected).toEqual([])
      expect(errors).toEqual([])
    } finally { await browser.close() }
  }, 60_000)
})
