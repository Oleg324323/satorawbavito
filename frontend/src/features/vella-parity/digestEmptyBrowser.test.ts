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
    configFile: false, envFile: false, root, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': JSON.stringify('test'), 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('') },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: {
      entry: fileURLToPath(new URL('./__fixtures__/reportLoadingBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'DigestEmptyTest',
    } },
  })
  const outputs = Array.isArray(result) ? result : [result]
  const chunk = outputs.flatMap(output => 'output' in output ? output.output : []).find(output => output.type === 'chunk' && output.isEntry)
  if (!chunk || chunk.type !== 'chunk') throw new Error('Missing Digest browser fixture bundle')
  code = chunk.code
}, 60_000)

it.each(['empty', 'zero-activity'])('renders WB funnel %s without invented values or old digest requests', async scenario => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1440, height: 950 } })
    const unexpected: string[] = [], errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', route => {
      const request = route.request(), url = new URL(request.url())
      if (request.resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (request.resourceType() === 'image') return route.fulfill({ contentType: 'image/gif', body: Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64') })
      if (url.origin === 'https://fonts.googleapis.com') return route.fulfill({ contentType: 'text/css', body: '' })
      if (url.pathname === '/api/wb/reports/work-demand/release') return route.fulfill({ json: {} })
      if (url.pathname === '/api/wb/reports/sales-funnel') {
        const empty = scenario === 'empty'
        return route.fulfill({ json: { items: empty ? [] : [{ nmId: '1', sellerArticle: 'ZERO-SKU', title: 'Synthetic', imageUrl: null, metrics: { orders: 0, visits: 0 } }], totals: { orders: empty ? null : 0, visits: empty ? null : 0, impressions: null }, total: empty ? 0 : 1, page: 1, pageSize: 50, complete: true, coveredProducts: empty ? 0 : 1, brands: [], dateFrom: url.searchParams.get('dateFrom'), dateTo: url.searchParams.get('dateTo'), unavailable: {} } })
      }
      unexpected.push(request.method() + ' ' + url.pathname); return route.abort()
    })
    await page.goto('http://localhost/wb/reports/digest')
    await page.addScriptTag({ content: code })
    const funnel = page.locator('.wb-sales-funnel')
    await expect.poll(() => funnel.locator('.sf-state').innerText(), { timeout: 15000 }).toContain(scenario === 'empty' ? 'нет товаров' : 'загружены.')
    expect(await funnel.locator('.sf-stage').count()).toBe(4)
    expect(await funnel.locator('.sf-stage').first().innerText()).toContain(scenario === 'empty' ? '—' : '0')
    expect(await funnel.locator('.sf-stage').nth(2).innerText()).toContain(scenario === 'empty' ? '—' : '0')
    expect(await funnel.locator('tbody tr').count()).toBe(scenario === 'empty' ? 0 : 1)
    expect(errors).toEqual([])
    expect(unexpected).toEqual([])
  } finally { await browser.close() }
}, 45000)
