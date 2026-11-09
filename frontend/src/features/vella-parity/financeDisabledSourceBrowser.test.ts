import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium, type Page } from 'playwright'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { beforeAll, expect, it } from 'vitest'

const unavailableTitle = 'Операционные расходы недоступны'
const unavailableMessage = 'Интеграция с 1С отключена. Данные расходов за выбранный период не получены.'
let bundleCode: string

beforeAll(async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({
    configFile: false, envFile: false, root, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""' },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: {
      entry: fileURLToPath(new URL('./__fixtures__/reportLoadingBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'FinanceDisabledSourceTest',
    } },
  })
  const chunk = (Array.isArray(result) ? result : [result]).flatMap(output => 'output' in output ? output.output : [])
    .find(output => output.type === 'chunk' && output.isEntry)
  if (!chunk || chunk.type !== 'chunk') throw new Error('Missing finance disabled source bundle')
  bundleCode = chunk.code
}, 60_000)

async function mount(page: Page) {
  const requests: string[] = [], errors: string[] = [], consoleProblems: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('console', message => {
    if (!['error', 'warning'].includes(message.type())) return
    const text = message.text()
    if (text.startsWith('An empty string')) return // Existing legacy shell image warning.
    consoleProblems.push(text)
  })
  await page.route('**/*', route => {
    const request = route.request(), url = new URL(request.url())
    if (request.method() === 'GET' && url.origin === 'http://satorna.test' && url.pathname === '/wb/reports/expenses') {
      return route.fulfill({ contentType: 'text/html', body: '<title>Synthetic disabled 1C</title><div id="root"></div>' })
    }
    if (request.method() === 'GET' && request.resourceType() === 'image') {
      return route.fulfill({ contentType: 'image/gif', body: Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64') })
    }
    if (request.method() === 'GET' && url.origin === 'https://fonts.googleapis.com') return route.fulfill({ contentType: 'text/css', body: '' })
    if (request.method() === 'GET' && url.origin === 'http://satorna.test' && url.pathname === '/api/wb/reports/pnl') {
      requests.push(`${request.method()} ${url.pathname}${url.search}`)
      return route.fulfill({ json: { rows: [], kpis: [], cashFlow: { status: 'disabled' }, reportJob: { state: 'completed' } } })
    }
    if (request.method() === 'GET' && url.origin === 'http://satorna.test' && url.pathname.endsWith('/latest-cache')) {
      requests.push(`${request.method()} ${url.pathname}${url.search}`)
      return route.fulfill({ json: {
        meta: { state: 'partial' }, warning: unavailableMessage,
        kpis: [{ id: 'revenue', value: 123400 }, { id: 'net_profit', value: null }, { id: 'operational_expenses', value: null }],
        rows: [], cashFlow: { status: 'disabled' }, blockerIds: [],
        reportJob: { state: 'completed' },
      } })
    }
    requests.push(`${request.method()} ${url.pathname}${url.search}`)
    return route.abort()
  })
  await page.goto('http://satorna.test/wb/reports/expenses')
  await page.addScriptTag({ content: bundleCode })
  return { requests, errors, consoleProblems }
}

it('renders disabled 1C as unavailable on expenses without waiting or zero amounts', async () => {
  const browser = await chromium.launch({ headless: true })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1440, height: 1000 } })
    const evidence = await mount(page)
    const surface = page.locator('#tab-expenses')
    await surface.getByText(unavailableTitle, { exact: true }).waitFor({ timeout: 15_000 })
    expect(await surface.innerText()).toContain(unavailableMessage)
    expect(await surface.locator('.finance-1c-wait, [role="progressbar"], .stats, [data-report-row]').count()).toBe(0)
    expect(await surface.getByText('0 ₽', { exact: true }).count()).toBe(0)
    expect(evidence.requests.some(request => request.includes('/jobs'))).toBe(false)
    expect(evidence.errors).toEqual([])
    expect(evidence.consoleProblems).toEqual([])
    expect(page.url()).toBe('http://satorna.test/wb/reports/expenses')
    expect(await page.title()).toBe('Satorna — Отчёты WB')
    expect(await page.locator('vite-error-overlay, nextjs-portal').count()).toBe(0)
    if (process.env.SATORNA_REPORT_UI_SCREENSHOTS) {
      await page.screenshot({ path: '/tmp/finance-disabled-expenses-desktop.png' })
      await page.setViewportSize({ width: 390, height: 844 })
      await page.screenshot({ path: '/tmp/finance-disabled-expenses-mobile.png' })
    }
  } finally { await browser.close() }
}, 60_000)
