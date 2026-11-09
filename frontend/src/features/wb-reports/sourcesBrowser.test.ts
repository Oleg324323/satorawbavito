import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { chromium } from 'playwright'
import { expect, it } from 'vitest'

it('renders verified sources, filters/sorts/details, failed retry and stale-scope protection with real clicks', async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const built = await build({ root, configFile: false, envFile: false, publicDir: false, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""' },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: { entry: fileURLToPath(new URL('./__fixtures__/sourcesBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'SourcesRegression' } } })
  const output = (Array.isArray(built) ? built : [built]).flatMap(item => 'output' in item ? item.output : [])
  const entry = output.find(item => item.type === 'chunk' && item.isEntry)
  if (!entry || entry.type !== 'chunk') throw new Error('Missing Sources bundle')
  const browser = await chromium.launch({ headless: true })
  const page = await browser.newPage({ serviceWorkers: 'block' })
  const requests: string[] = [], errors: string[] = []
  let release!: () => Promise<void>, calls = 0
  const receipt = { marketplaceAccountId: 1, items: [
    { id: 'stocks', status: 'ready', observedAt: '2026-11-05T10:11:12Z', dateFrom: '2026-11-05', dateTo: '2026-11-05', count: 0, detail: 'Verified stocks receipt' },
    { id: 'ads', status: 'partial', observedAt: '2026-11-04T10:11:12Z', dateFrom: '2026-10-05', dateTo: '2026-10-11', count: 2, detail: 'Incomplete campaign coverage' },
  ] }
  page.on('pageerror', error => errors.push(error.message))
  await page.route('**/*', async route => {
    const request = route.request()
    if (request.resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
    requests.push(`${request.method()} ${new URL(request.url()).pathname}`)
    if (!request.url().endsWith('/sources/status')) throw new Error('Unexpected network operation')
    expect(request.method()).toBe('GET')
    calls++
    if (calls === 1 || calls === 3) return new Promise<void>(resolve => {
      release = async () => { await route.fulfill({ json: receipt }).catch(() => undefined); resolve() }
    })
    if (calls === 2) return route.fulfill({ status: 503, json: { detail: 'unavailable' } })
    return route.fulfill({ json: { marketplaceAccountId: 2, items: [{ id: 'stocks', status: 'disconnected', detail: 'Disconnected account' }] } })
  })
  try {
    await page.goto('http://127.0.0.1:4199/wb/sources')
    await page.addScriptTag({ content: entry.code })
    await page.getByText('Читаем состояние источников…', { exact: true }).waitFor()
    expect(await page.locator('#sourcesBody tr').count()).toBe(9)
    expect(await page.locator('#sourcesBody').textContent()).not.toMatch(/сегодня 08:|Мария|Максим|май 2026|готово для репрайсера/)
    await release()
    await expect.poll(() => page.locator('[data-source-id="stocks"] td').nth(4).textContent()).toContain('Свежее')
    expect(await page.locator('[data-source-id="stocks"]').textContent()).toContain('Записей: 0')
    expect(await page.locator('[data-source-id="one-c"] td').nth(4).textContent()).toBe('Неизвестно')
    const chips = page.getByRole('group', { name: 'Фильтры источников' })
    await chips.getByRole('button', { name: 'Требует действия', exact: true }).click()
    expect(await page.locator('#sourcesBody [data-source-id]').count()).toBe(1)
    await page.getByLabel('Поиск источников').fill('WB')
    expect(await page.locator('#sourcesBody [data-source-id]').count()).toBe(1)
    await page.getByRole('button', { name: 'Сбросить', exact: true }).click()
    const header = page.getByRole('columnheader', { name: /Источник/ })
    await header.click()
    expect(await header.getAttribute('aria-sort')).toBe('descending')
    const first = await page.locator('#sourcesBody tr').first().getAttribute('data-source-id')
    await header.click()
    expect(await header.getAttribute('aria-sort')).toBe('ascending')
    expect(await page.locator('#sourcesBody tr').first().getAttribute('data-source-id')).not.toBe(first)
    await page.getByRole('button', { name: 'Подключение 1С', exact: true }).click()
    const detail = page.getByRole('dialog')
    expect(await detail.textContent()).toContain('Статус: Неизвестно')
    expect(await detail.textContent()).not.toMatch(/требует правил|2 строки|май 2026|Максим/)
    await detail.getByRole('button', { name: 'Закрыть', exact: true }).click()
    expect(requests).toHaveLength(1)
    await page.getByRole('button', { name: 'Повторить чтение', exact: true }).click()
    await page.getByText('Не удалось прочитать состояние источников. Повторите загрузку.', { exact: true }).waitFor()
    expect(await page.locator('[data-source-id="stocks"] td').nth(4).textContent()).toBe('Неизвестно')
    await page.getByRole('button', { name: 'Повторить чтение', exact: true }).click()
    await expect.poll(() => calls).toBe(3)
    await page.getByRole('button', { name: 'Switch organization' }).click()
    await expect.poll(() => page.locator('[data-source-id="stocks"] td').nth(4).textContent()).toBe('Не подключено')
    await release()
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
    expect(await page.locator('[data-source-id="stocks"] td').nth(4).textContent()).toBe('Не подключено')
    expect(requests).toHaveLength(4)
    expect(errors).toEqual([])
  } finally { await release?.().catch(() => undefined); await browser.close() }
}, 60000)

it('keeps the real parity shell from replacing Sources rows or installing duplicate demo handlers', async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const built = await build({ root, configFile: false, envFile: false, publicDir: false, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""' },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: { entry: fileURLToPath(new URL('./__fixtures__/sourcesShellBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'SourcesShellRegression' } } })
  const entry = (Array.isArray(built) ? built : [built]).flatMap(item => 'output' in item ? item.output : []).find(item => item.type === 'chunk' && item.isEntry)
  if (!entry || entry.type !== 'chunk') throw new Error('Missing shell bundle')
  const browser = await chromium.launch({ headless: true })
  const page = await browser.newPage({ serviceWorkers: 'block' })
  const errors: string[] = [], writes: string[] = []
  let reads = 0
  page.on('pageerror', error => errors.push(error.message))
  await page.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url())
    if (request.resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
    if (request.resourceType() === 'image') return route.fulfill({ status: 204 })
    if (url.hostname === 'fonts.googleapis.com') return route.fulfill({ body: '' })
    if (request.method() !== 'GET') writes.push(url.pathname)
    if (url.pathname.endsWith('/sources/status')) {
      reads++
      return route.fulfill({ json: { marketplaceAccountId: 1, items: [{ id: 'stocks', status: 'ready', count: 0, detail: 'Saved stock receipt' }] } })
    }
    return route.fulfill({ json: { items: [], total: 0, state: 'completed', running: false, steps: [] } })
  })
  try {
    await page.goto('http://127.0.0.1:4199/wb/sources')
    await page.addScriptTag({ content: entry.code })
    const surface = page.locator('#tab-sources')
    await expect.poll(() => surface.locator('[data-source-id="stocks"] td').nth(4).textContent()).toContain('Свежее')
    expect(await surface.locator('#sourcesBody tr').count()).toBe(9)
    expect(await surface.locator('[data-report-empty]').count()).toBe(0)
    await surface.getByRole('button', { name: 'Свежее', exact: true }).click()
    expect(await surface.locator('#sourcesBody tr').count()).toBe(1)
    await surface.getByRole('button', { name: 'Сбросить', exact: true }).click()
    expect(await surface.locator('#sourcesBody tr').count()).toBe(9)
    expect(await surface.locator('.is-demo-active').count()).toBe(0)
    expect(await page.locator('.toast').filter({ hasText: /Действие:|Фильтр:/ }).count()).toBe(0)
    expect(reads).toBe(1)
    expect(writes).toEqual([])
    expect(errors).toEqual([])
  } finally { await browser.close() }
}, 60000)
