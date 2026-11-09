import { test, expect } from 'vitest'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { chromium } from 'playwright'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

test('WB live products mount without installing legacy table writers or crashing the route', async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({
    configFile: false, root, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': JSON.stringify('test'),
      'import.meta.env.VITE_API_BASE_URL': JSON.stringify(''),
      'import.meta.env.VITE_WB_LIVE_ENABLED': JSON.stringify('true') },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false,
      lib: { entry: fileURLToPath(new URL('./__fixtures__/wbLivePageBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'WbLivePageTest' } },
  })
  const bundle = (Array.isArray(result) ? result : [result]).flatMap(item => 'output' in item ? item.output : [])
    .find(item => item.type === 'chunk' && item.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('No browser bundle')
  const browser = await chromium.launch({ headless: true })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block' })
    const errors: string[] = [], requests: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', route => {
      const url = new URL(route.request().url())
      if (url.origin === 'http://satorna.test' && url.pathname === '/wb/repricer') {
        return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      }
      if (url.pathname.startsWith('/api/')) requests.push(url.pathname)
      return route.abort()
    })
    await page.goto('http://satorna.test/wb/repricer')
    await page.addScriptTag({ content: bundle.code })
    for (const output of (Array.isArray(result) ? result : [result])) {
      if ('output' in output) for (const asset of output.output) {
        if (asset.type === 'asset' && asset.fileName.endsWith('.css')) await page.addStyleTag({ content: String(asset.source) })
      }
    }
    await expect.poll(async () => ({
      errors, mounted: await page.getByLabel('Аккаунт Wildberries', { exact: true }).count(),
    }), { timeout: 15_000 }).toEqual({ errors: [], mounted: 1 })
    expect(requests.filter(url => url.startsWith('/api/v1/wb-repricer/'))).toEqual([])
    expect(await page.locator('[data-vella-island="products-sticky-pagination"]').count()).toBe(0)
    expect(await page.locator('#subtabsContext').isVisible()).toBe(false)
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: 900 })
      const layout = await page.locator('[data-wb-live="products"]').evaluate(element => {
        const control = element.querySelector('select')!
        return { padding: parseFloat(getComputedStyle(element).paddingLeft),
          controlHeight: control.getBoundingClientRect().height }
      })
      expect(layout.padding).toBeGreaterThanOrEqual(16)
      expect(layout.controlHeight).toBeGreaterThanOrEqual(36)
    }
  } finally { await browser.close() }
}, 60000)
