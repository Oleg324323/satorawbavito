import path from 'node:path'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { expect, it } from 'vitest'
import { encodeAvitoExtensionConnectionCode } from './extensionConnectionCode'

it('connects the website copy to the real popup with an exact local origin and only the original bearer token', async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({ configFile: false, envFile: false, root, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""' },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: { entry: path.join(root, 'src/features/vella-parity/__fixtures__/avitoPickingBrowser.tsx'), formats: ['iife'], name: 'AvitoConnectionSetup' } } })
  const bundle = (Array.isArray(result) ? result : [result]).flatMap(item => 'output' in item ? item.output : [])
    .find(item => item.type === 'chunk' && item.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing website fixture')
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const origin = 'http://127.0.0.1:5177', token = 'sat_avito_SYNTHETIC_ONLY_abc-123_xyz'
    const expectedCode = encodeAvitoExtensionConnectionCode(origin, token)
    const page = await browser.newPage(), errors: string[] = [], unexpected: string[] = []
    page.setDefaultTimeout(5000)
    let creates = 0
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', route => {
      const request = route.request(), url = new URL(request.url())
      if (request.resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<body class="vella-html-parity-root"><div id="root"></div></body>' })
      if (url.pathname === '/api/v1/avito/orders/extension-token/regenerate' && request.method() === 'POST') {
        creates += 1
        return route.fulfill({ json: { configured: true, token, tokenPrefix: 'sat_avito_SYNTHETIC' } })
      }
      if (url.pathname === '/api/v1/avito/orders/extension-token') return route.fulfill({ json: { configured: true, tokenPrefix: 'sat_avito_OLD' } })
      if (url.pathname === '/api/v1/avito/orders/returns-sync') return route.fulfill({ json: { enabled: false } })
      if (url.pathname === '/api/v1/avito/orders/queue') return route.fulfill({ json: {
        status: 'synced', filters: { mode: 'active', page: 1, limit: 50, accountId: null, search: '', historyFrom: null },
        rows: [], source: { complete: true }, summary: { total: 0, shown: 0, items: 0, totalKopecks: 0,
          confirmation: 0, readyToShip: 0, inTransit: 0, delivered: 0, closed: 0, canceled: 0, returns: 0, disputes: 0, requiredActions: 0, statusCounts: {} },
      } })
      if (url.pathname === '/favicon.ico') return route.fulfill({ status: 204 })
      unexpected.push(`${request.method()} ${url.pathname}`)
      return route.abort()
    })
    await page.goto(`${origin}/avito/orders`)
    await page.addScriptTag({ content: bundle.code })
    await page.getByRole('button', { name: 'Настройки расширения', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: 'Настройки расширения Avito Orders' })
    const copy = dialog.getByRole('button', { name: 'Скопировать код подключения', exact: true })
    expect(await copy.isDisabled()).toBe(true)
    await dialog.getByRole('button', { name: 'Создать новый токен', exact: true }).click()
    const field = dialog.getByLabel('Код подключения Satorna')
    await expect.poll(() => field.inputValue()).toBe(expectedCode)
    await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async () => { throw new Error('Synthetic clipboard denial') } } }))
    await copy.click()
    await expect.poll(() => dialog.getByRole('status').innerText()).toContain('Код выделен')
    expect(await field.evaluate((el: HTMLTextAreaElement) => el.value.slice(el.selectionStart, el.selectionEnd))).toBe(expectedCode)
    await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async (code: string) => { document.body.dataset.syntheticClipboard = code } } }))
    await copy.click()
    await expect.poll(() => dialog.getByRole('status').innerText()).toBe('Код подключения скопирован')
    const copied = await page.locator('body').getAttribute('data-synthetic-clipboard')
    expect(copied).toBe(expectedCode)

    const popup = await browser.newPage()
    popup.setDefaultTimeout(5000)
    popup.on('pageerror', error => errors.push(error.message))
    const manifest = JSON.parse(await readFile(path.join(root, '../avito-orders-extension/manifest.json'), 'utf8'))
    await popup.addInitScript(manifest => {
      const local: Record<string, unknown> = {}, sync: Record<string, unknown> = {}, permissions: unknown[] = []
      const store = (data: Record<string, unknown>) => ({
        get: async (keys: string | Record<string, unknown>) => typeof keys === 'string' ? { [keys]: data[keys] } : { ...keys, ...data },
        set: async (value: Record<string, unknown>) => { Object.assign(data, value) },
        remove: async (keys: string[]) => { keys.forEach(key => delete data[key]) },
      })
      Object.assign(window, { extensionTestStorage: { local, sync, permissions }, chrome: {
        storage: { local: store(local), sync: store(sync), onChanged: { addListener() {} } },
        runtime: { getManifest: () => manifest, sendMessage: async () => ({ ok: true, logs: [], running: false }) },
        permissions: { request: async (value: unknown) => { permissions.push(value); return true } },
      } })
    }, manifest)
    const popupSources = Object.fromEntries(await Promise.all(['popup.html', 'popup.css', 'connection.js', 'popup.js'].map(async name =>
      [name, await readFile(path.join(root, '../avito-orders-extension/src', name), 'utf8')],
    )))
    const authenticatedReads: string[] = []
    await popup.route('**/*', route => {
      const request = route.request(), url = new URL(request.url())
      if (url.pathname === '/api/v1/avito/orders/browser-collection-context' && request.method() === 'GET') {
        authenticatedReads.push(request.headers().authorization)
        return route.fulfill({ json: { orders: [] } })
      }
      const name = url.pathname.split('/').pop()!
      if (popupSources[name]) return route.fulfill({ contentType: name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html', body: popupSources[name] })
      if (url.pathname === '/favicon.ico') return route.fulfill({ status: 204 })
      unexpected.push(`${request.method()} ${url.pathname}`)
      return route.abort()
    })
    await popup.goto(`${origin}/popup.html`)
    await popup.getByRole('button', { name: 'Настройки', exact: true }).click()
    await popup.getByLabel('Код подключения Satorna').fill(copied!)
    await popup.getByRole('button', { name: 'Подключить', exact: true }).click()
    await popup.getByText('Подключение проверено', { exact: true }).waitFor()
    expect(await popup.getByText(`Адрес Satorna: ${origin}`, { exact: true }).isVisible()).toBe(true)
    expect(authenticatedReads).toEqual([`Bearer ${token}`])
    const stored = await popup.evaluate(() => (window as unknown as { extensionTestStorage: {
      local: { connection: unknown }; sync: Record<string, unknown>; permissions: unknown[];
    } }).extensionTestStorage)
    expect(stored.local.connection).toEqual({ backendUrl: origin, accessToken: token, originPinned: true })
    expect(stored.permissions).toEqual([{ origins: [`${origin}/*`] }])
    expect(JSON.stringify(stored).includes(expectedCode)).toBe(false)
    expect(await popup.getByLabel('Код подключения Satorna').inputValue()).toBe('')
    await popup.getByRole('button', { name: 'Сбор', exact: true }).click()
    expect(await popup.getByRole('button', { name: 'Собрать всё', exact: true }).isEnabled()).toBe(true)
    await popup.getByRole('button', { name: 'Настройки', exact: true }).click()
    await popup.getByLabel('Код подключения Satorna').fill('invalid-code')
    await popup.getByRole('button', { name: 'Подключить', exact: true }).click()
    await expect.poll(() => popup.locator('#connectionStatus').innerText()).toContain('Некорректный код')
    expect(authenticatedReads).toHaveLength(1)
    expect(await popup.evaluate(() => (window as unknown as { extensionTestStorage: { local: { connection: unknown } } }).extensionTestStorage.local.connection)).toEqual(stored.local.connection)
    await popup.getByLabel('Код подключения Satorna').fill(encodeAvitoExtensionConnectionCode('https://unknown.example.test:8443', token))
    await popup.getByRole('button', { name: 'Подключить', exact: true }).click()
    expect(await popup.locator('#confirmationDestination').innerText()).toBe('https://unknown.example.test:8443')
    expect(await popup.locator('#destinationConfirmation').isVisible()).toBe(true)
    expect(authenticatedReads).toHaveLength(1)
    await popup.getByRole('button', { name: 'Отмена', exact: true }).click()
    expect(await popup.locator('#destinationConfirmation').isVisible()).toBe(false)
    await popup.getByText('Дополнительные настройки', { exact: true }).click()
    expect(await popup.getByLabel('Адрес Satorna', { exact: true }).inputValue()).toBe(origin)
    expect(await popup.getByLabel('Токен Satorna', { exact: true }).inputValue()).toBe(token)
    await popup.close()
    const websiteStorage = await page.evaluate(() => [localStorage, sessionStorage].flatMap(store => Object.keys(store).map(key => store.getItem(key))).join(' '))
    expect(websiteStorage).not.toContain(expectedCode)
    expect(websiteStorage).not.toContain(token)
    await page.reload()
    await page.addScriptTag({ content: bundle.code })
    await page.getByRole('button', { name: 'Настройки расширения', exact: true }).click()
    expect(await copy.isDisabled()).toBe(true)
    expect(await field.count()).toBe(0)
    expect(creates).toBe(1)
    expect(unexpected).toEqual([])
    expect(errors).toEqual([])
  } finally { await browser.close() }
}, 45_000)
