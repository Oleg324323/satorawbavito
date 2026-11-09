import path from 'node:path'
import { fileURLToPath } from 'node:url'
import react from '@vitejs/plugin-react'
import { chromium, type Browser, type Page } from 'playwright'
import { build } from 'vite'
import { afterAll, beforeAll, expect, it } from 'vitest'

let browser: Browser, code: string
beforeAll(async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const built = await build({ root, configFile: false, envFile: false, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""' },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: {
      entry: fileURLToPath(new URL('./__fixtures__/currentCostEditorBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'CostEditorTest',
    } },
  })
  const bundle = (Array.isArray(built) ? built : [built]).flatMap(item => 'output' in item ? item.output : [])
    .find(item => item.type === 'chunk' && item.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing cost editor fixture')
  code = bundle.code
  browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL })
}, 60_000)
afterAll(async () => { await browser?.close() })

async function mount(page: Page) { await page.goto('http://cost-editor.test'); await page.addScriptTag({ content: code }) }
function backend(page: Page, options: { delay?: number; fail?: boolean; conflict?: boolean; shared?: boolean } = {}) {
  let amount: number | null = null, version: number | null = null
  const writes: Array<{ amountKopecks: number; expectedCostVersionId: number | null; sourceReference: string }> = []
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  return { writes, errors, setup: page.route('**/*', async route => {
    if (route.request().resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
    if (route.request().url().endsWith('/cost-history')) return route.fulfill({ json: { data: [] } })
    if (!route.request().url().includes('/current-cost')) throw new Error('Unexpected API request')
    if (route.request().method() === 'POST') {
      const payload = route.request().postDataJSON(); writes.push(payload)
      if (options.fail) { options.fail = false; return route.fulfill({ status: 503, json: { detail: 'База недоступна' } }) }
      if (options.conflict) { options.conflict = false; amount = 70000; version = 8; return route.fulfill({ status: 409, json: { detail: 'Версия изменилась' } }) }
      amount = payload.amountKopecks; version = (version ?? 0) + 1
      return route.fulfill({ json: { data: { amountKopecks: amount, costVersionId: version, effectiveFrom: '2026-11-02T12:00:00Z' } } })
    }
    if (options.delay) await new Promise(resolve => setTimeout(resolve, options.delay))
    return route.fulfill({ json: { data: { catalogSkuId: version ? 1 : null, linkedProductCount: options.shared ? 2 : 1, canWrite: true,
      currentCost: { amountKopecks: amount, costVersionId: version, effectiveFrom: null } } } })
  }) }
}

it('saves on blur even while the first read is pending, then reloads the saved value', async () => {
  const page = await browser.newPage(); const api = backend(page, { delay: 350 }); await api.setup
  try {
    await mount(page)
    const field = page.getByRole('textbox')
    expect(await field.inputValue()).toBe('')
    await field.fill('450,25'); await page.getByRole('button', { name: 'Выйти из поля', exact: true }).click()
    await page.getByRole('status').filter({ hasText: 'Сохранено в БД' }).waitFor()
    expect(api.writes).toHaveLength(1); expect(api.writes[0].amountKopecks).toBe(45025)
    expect(await page.getByLabel('Обновления расчёта').textContent()).toBe('1')
    await mount(page); await page.getByRole('textbox').focus()
    await page.waitForFunction(() => document.querySelector('input')?.value === '450,25')
    expect(api.errors).toEqual([])
  } finally { await page.close() }
})

it('preserves failed input, retries the same command, and never reports a failed write as saved', async () => {
  const page = await browser.newPage(); const api = backend(page, { fail: true }); await api.setup
  try {
    await mount(page); const field = page.getByRole('textbox')
    await field.fill('0'); await field.press('Enter')
    await page.getByRole('status').filter({ hasText: 'Не сохранено: База недоступна' }).waitFor()
    expect(await field.inputValue()).toBe('0')
    expect(await page.getByLabel('Обновления расчёта').textContent()).toBe('0')
    await field.press('Enter'); await page.getByRole('status').filter({ hasText: 'Сохранено в БД' }).waitFor()
    expect(api.writes).toHaveLength(2); expect(api.writes[0]).toEqual(api.writes[1])
    expect(await field.inputValue()).toBe('0,00'); expect(api.errors).toEqual([])
  } finally { await page.close() }
})

it('keeps user input after a conflict and retries only after explicit confirmation', async () => {
  const page = await browser.newPage(); const api = backend(page, { conflict: true }); await api.setup
  try {
    await mount(page); const field = page.getByRole('textbox')
    await field.fill('450'); await field.press('Enter')
    await page.getByRole('status').filter({ hasText: 'Конфликт.' }).waitFor()
    expect(await field.inputValue()).toBe('450'); expect(api.writes).toHaveLength(1)
    await page.getByRole('button', { name: 'Выйти из поля', exact: true }).click()
    expect(api.writes).toHaveLength(1)
    await field.press('Enter'); await page.getByRole('status').filter({ hasText: 'Сохранено в БД' }).waitFor()
    expect(api.writes[1].expectedCostVersionId).toBe(8)
    expect(api.writes[1].sourceReference).not.toBe(api.writes[0].sourceReference)
    expect(api.errors).toEqual([])
  } finally { await page.close() }
})

it('requires confirmation before modifying a cost shared by multiple products', async () => {
  const page = await browser.newPage(); const api = backend(page, { shared: true }); await api.setup
  try {
    await mount(page); await page.getByRole('textbox').fill('500'); await page.getByRole('textbox').press('Enter')
    await page.getByRole('status').filter({ hasText: 'Подтвердите изменение' }).waitFor()
    expect(api.writes).toHaveLength(0)
    await page.getByRole('checkbox').check()
    await page.getByRole('button', { name: 'Сохранить себестоимость 123', exact: true }).click()
    await page.getByRole('status').filter({ hasText: 'Сохранено в БД' }).waitFor()
    expect(api.writes).toHaveLength(1); expect(api.errors).toEqual([])
  } finally { await page.close() }
})

it.each(['Другой товар', 'Другой кабинет'])('cancels a pending edit when switching context: %s', async button => {
  const page = await browser.newPage(); const api = backend(page, { delay: 500 }); await api.setup
  try {
    await mount(page); await page.getByRole('textbox').fill('450')
    await page.getByRole('button', { name: button, exact: true }).click()
    await page.waitForTimeout(700)
    expect(api.writes).toHaveLength(0); expect(await page.getByRole('textbox').inputValue()).toBe('')
    expect(api.errors).toEqual([])
  } finally { await page.close() }
})
