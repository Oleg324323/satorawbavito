import path from 'node:path'
import { fileURLToPath } from 'node:url'
import react from '@vitejs/plugin-react'
import { chromium } from 'playwright'
import { build } from 'vite'
import { expect, it } from 'vitest'

it('keeps manager assignment above the page, traps focus and saves selected SKU once', async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({ root, configFile: false, envFile: false, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""', 'import.meta.env.VITE_WB_LIVE_ENABLED': '"false"' },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: { entry: fileURLToPath(new URL('./__fixtures__/productsCatalogCountBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'ManagerModalTest' } },
  })
  const bundle = (Array.isArray(result) ? result : [result]).flatMap(output => 'output' in output ? output.output : []).find(output => output.type === 'chunk' && output.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing manager modal bundle')
  const browser = await chromium.launch({ headless: true })
  let release = () => {}
  let gate = new Promise<void>(resolve => { release = resolve })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1440, height: 1000 } })
    const writes: { sku: string; body: unknown }[] = [], errors: string[] = [], unexpected: string[] = []
    let fail = true, reads = 0
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      if (request.resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (request.resourceType() === 'image') return route.fulfill({ status: 204 })
      if (url.origin === 'https://fonts.googleapis.com') return route.fulfill({ contentType: 'text/css', body: '' })
      const manager = url.pathname.match(/^\/api\/v1\/wb-repricer\/sku\/(SKU-\d+)\/manager-simple$/)
      if (manager && request.method() === 'POST') {
        writes.push({ sku: manager[1], body: request.postDataJSON() })
        await gate
        return route.fulfill({ status: fail ? 503 : 200, json: fail ? { error: { code: 'UNAVAILABLE', message: 'Тестовая ошибка назначения' } } : {} })
      }
      if (url.pathname === '/api/v1/wb-repricer/sku' && request.method() === 'GET') {
        reads += 1
        return route.fulfill({ json: { items: [1, 2, 3].map(index => ({ meta: { articleId: `SKU-${index}`, nmId: 100 + index, currentPriceKopecks: 200000, name: 'Тестовый товар', status: 'auto', managerId: reads > 1 && index < 3 ? 'manager-maria-dudina' : null, managerName: reads > 1 && index < 3 ? 'Мария Дудина' : null }, settings: {}, analytics: { financeState: 'ok', basketsState: 'ok', baskets: 1, ordersUnits: 1 } })), total: 3, itemsReturned: 3, page: 1, pageSize: 150, summary: { skuCount: 3 }, cache: { totalCached: 3, pagesCached: 1 } } })
      }
      if (request.method() === 'GET' && ['/api/v1/wb-repricer/strategies/catalog', '/api/v1/wb-repricer/sku-groups', '/api/v1/wb-repricer/changelog'].includes(url.pathname)) return route.fulfill({ json: { items: [], total: 0 } })
      if (request.method() === 'GET' && ['/api/v1/wb-repricer/sync/status', '/api/v1/wb-repricer/worker/status'].includes(url.pathname)) return route.fulfill({ json: { state: 'completed', running: false, steps: [] } })
      unexpected.push(`${request.method()} ${url.pathname}`)
      return route.abort()
    })
    await page.goto('http://satorna.test/wb/repricer')
    await page.addScriptTag({ content: bundle.code })
    await page.locator('#tbody tr[data-sku]').first().waitFor()
    for (const index of [0, 1]) {
      const checkbox = page.locator('#tbody tr[data-sku]').nth(index).locator('td').first().locator('.cb')
      if (!(await checkbox.getAttribute('class'))?.includes('on')) await checkbox.click()
    }
    const trigger = page.locator('#bulkBar').getByRole('button', { name: 'Назначить менеджера', exact: true })
    await trigger.click()
    const modal = page.locator('#m-bulkAssignManager')
    await modal.locator('#bulkAssignReason').fill('Проверка выбранных SKU')
    expect(await modal.locator('#bulkAssignRows tr').count()).toBe(2)
    expect.soft(await modal.getByText(/XLSX/).count()).toBe(0)
    for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }, { width: 720, height: 450 }]) {
      await page.setViewportSize(viewport)
      const box = (await modal.locator('.modal').boundingBox())!
      expect(box.x).toBeGreaterThanOrEqual(0)
      expect(box.y).toBeGreaterThanOrEqual(0)
      expect(box.x + box.width).toBeLessThanOrEqual(viewport.width)
      expect(box.y + box.height).toBeLessThanOrEqual(viewport.height)
      expect(await modal.evaluate(element => { const button = element.querySelector('.btn-primary')!; const r = button.getBoundingClientRect(); return button.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)) })).toBe(true)
      await modal.locator('.btn-primary').focus()
      await page.keyboard.press('Tab')
      expect(await modal.evaluate(element => element.contains(document.activeElement))).toBe(true)
      await page.screenshot({ path: `/tmp/satorna-manager-modal-${viewport.width}.png` })
    }
    await page.setViewportSize({ width: 1440, height: 1000 })
    await page.keyboard.press('Escape')
    expect(await trigger.evaluate(element => element === document.activeElement)).toBe(true)
    await trigger.click()
    await modal.locator('#bulkAssignReason').fill('Проверка выбранных SKU')
    const save = modal.getByRole('button', { name: 'Назначить', exact: true })
    expect(await page.evaluate(() => window.PRODUCTS?.filter(product => product.sel).map(product => product.sku))).toEqual(['SKU-1', 'SKU-2'])
    await save.click()
    await expect.poll(() => writes.length).toBe(2)
    expect(await save.isDisabled()).toBe(true)
    release()
    await expect.poll(() => save.isDisabled()).toBe(false)
    await page.setViewportSize({ width: 720, height: 450 })
    const errorToast = modal.locator('.toast.warn').last()
    await expect.poll(() => errorToast.evaluate(element => { const r = element.getBoundingClientRect(); return element.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)) })).toBe(true)
    expect(await errorToast.innerText()).toContain('Тестовая ошибка назначения')
    await page.screenshot({ path: '/tmp/satorna-manager-modal-error-fixed-720.png' })
    expect(await modal.locator('#bulkAssignReason').inputValue()).toBe('Проверка выбранных SKU')
    expect(await modal.isVisible()).toBe(true)
    fail = false
    await page.setViewportSize({ width: 1440, height: 1000 })
    gate = Promise.resolve()
    await save.click()
    await expect.poll(() => reads).toBe(2)
    await expect.poll(() => modal.isVisible()).toBe(false)
    expect(await page.locator('#tbody').getByText('Мария Д.', { exact: true }).count()).toBe(2)
    expect(writes.map(item => item.sku)).toEqual(['SKU-1', 'SKU-2', 'SKU-1', 'SKU-2'])
    expect(writes.every(item => (item.body as { reason: string }).reason === 'Проверка выбранных SKU')).toBe(true)
    expect(writes.every(item => (item.body as { managerUserId: string }).managerUserId === 'manager-maria-dudina')).toBe(true)
    for (const index of [0, 1]) await page.locator('#tbody tr[data-sku]').nth(index).locator('td').first().locator('.cb').click()
    const groupTrigger = page.locator('#bulkBar').getByRole('button', { name: 'Добавить в группу', exact: true })
    for (const viewport of [{ width: 390, height: 844 }, { width: 720, height: 450 }]) {
      await groupTrigger.click()
      const group = page.locator('[data-vella-island="products-sku-group-modal"]')
      await group.waitFor()
      await page.setViewportSize(viewport)
      const box = (await group.locator('.modal').boundingBox())!
      expect(box.x).toBeGreaterThanOrEqual(0)
      expect(box.y).toBeGreaterThanOrEqual(0)
      expect(box.x + box.width).toBeLessThanOrEqual(viewport.width)
      expect(box.y + box.height).toBeLessThanOrEqual(viewport.height)
      await group.getByRole('button', { name: 'Сохранить группу', exact: true }).focus()
      await page.keyboard.press('Tab')
      expect(await group.evaluate(element => element.contains(document.activeElement))).toBe(true)
      await group.locator('select').focus()
      await page.keyboard.press('Shift+Tab')
      expect(await group.evaluate(element => element.contains(document.activeElement))).toBe(true)
      await page.screenshot({ path: `/tmp/satorna-group-modal-${viewport.width}.png` })
      await page.setViewportSize({ width: 1440, height: 1000 })
      await page.keyboard.press('Escape')
      await expect.poll(() => group.count()).toBe(0)
      expect(await groupTrigger.evaluate(element => element === document.activeElement)).toBe(true)
    }
    expect(writes).toHaveLength(4)
    expect(unexpected).toEqual([])
    expect(errors).toEqual([])
  } finally { release(); await browser.close() }
}, 60_000)
