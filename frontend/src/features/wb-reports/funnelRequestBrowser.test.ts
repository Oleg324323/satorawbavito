import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { expect, it } from 'vitest'

it('leaves a stalled saved-data state and retries the same period safely', async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({ configFile: false, envFile: false, root, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""' },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: { entry: fileURLToPath(new URL('./__fixtures__/funnelRequestBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'FunnelDeadline' } } })
  const bundle = (Array.isArray(result) ? result : [result]).flatMap(item => 'output' in item ? item.output : [])
    .find(item => item.type === 'chunk' && item.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing fixture')
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block' })
    const requests: string[] = [], errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.clock.install()
    await page.route('**/*', route => {
      const request = route.request(), url = new URL(request.url())
      if (request.resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (url.pathname === '/api/wb/reports/sales-funnel/comparison') return route.fulfill({ status: 503, json: { detail: 'Synthetic comparison failure' } })
      if (url.pathname === '/api/wb/reports/sales-funnel/totals-comparison') return route.fulfill({ status: 503, json: { detail: 'Synthetic aggregate comparison failure' } })
      if (url.pathname === '/api/wb/reports/sales-funnel' && request.method() === 'GET') {
        requests.push(url.search)
        if (requests.length === 1) return // Deliberately unresolved, no WB access.
        return route.fulfill({ json: { dateFrom: '2026-10-11', dateTo: '2026-10-14', total: 1, complete: true,
          coveredProducts: 1, refreshRequired: false, totals: { visits: 0 }, unavailable: {}, brands: [], page: 1, pageSize: 50,
          items: [{ nmId: '101', title: 'Synthetic WB product', sellerArticle: 'LOADED', imageUrl: null, metrics: { visits: 0 } }] } })
      }
      errors.push(`${request.method()} ${url.pathname}`)
      return route.abort()
    })
    await page.goto('http://localhost/wb/reports')
    await page.addScriptTag({ content: bundle.code })
    await page.getByText('Читаем сохранённые данные…', { exact: true }).waitFor()
    await page.clock.fastForward(30_001)
    await page.getByRole('alert').waitFor()
    expect(await page.locator('.wb-sales-funnel').getAttribute('aria-busy')).toBe('false')
    await page.getByRole('button', { name: 'Повторить загрузку', exact: true }).click()
    await page.getByRole('link', { name: 'LOADED', exact: true }).waitFor()
    expect(requests).toHaveLength(2)
    expect(requests[0]).toBe(requests[1])
    expect(errors).toEqual([])
  } finally { await browser.close() }
}, 30_000)
