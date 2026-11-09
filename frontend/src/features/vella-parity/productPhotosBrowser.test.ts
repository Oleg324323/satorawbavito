import path from 'node:path'
import { fileURLToPath } from 'node:url'
import react from '@vitejs/plugin-react'
import { chromium } from 'playwright'
import { build } from 'vite'
import { expect, it } from 'vitest'

const image = Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64')
const sourceUrl = (id: number) => `https://basket-77.wbbasket.ru/vol0/part0/${id}/images/big/1.webp`
const fallbackUrl = (id: number) => `https://basket-01.wbbasket.ru/vol0/part0/${id}/images/c516x688/1.webp`
const products = [
  { sku: 'PHOTO-SOURCE', nmId: 101, photoUrl: sourceUrl(101) },
  { sku: 'PHOTO-NO-URL', nmId: 102, photoUrl: null },
  { sku: 'PHOTO-RETRY', nmId: 103, photoUrl: sourceUrl(103) },
  { sku: 'PHOTO-UNAVAILABLE', nmId: 104, photoUrl: sourceUrl(104) },
]

it.each([
  ['/wb/repricer', '#tab-products'],
  ['/wb/repricer/stats', '#tab-repricer-stats'],
  ['/wb/reports/week-over-week', '#tab-week'],
])('uses source photos with bounded failure handling on %s', async (routePath, tabSelector) => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({ root, configFile: false, envFile: false, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""', 'import.meta.env.VITE_WB_LIVE_ENABLED': '"false"' },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: { entry: fileURLToPath(new URL('./__fixtures__/productsCatalogCountBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'ProductPhotosTest' } },
  })
  const artifacts = (Array.isArray(result) ? result : [result]).flatMap(output => 'output' in output ? output.output : [])
  const bundle = artifacts.find(output => output.type === 'chunk' && output.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing product photos fixture')
  const styles = artifacts.flatMap(output => output.type === 'asset' && output.fileName.endsWith('.css') ? [String(output.source)] : []).join('\n')
  const browser = await chromium.launch({ headless: true })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1440, height: 1000 } })
    const photos: string[] = [], errors: string[] = [], unexpected: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', route => {
      const request = route.request(), url = new URL(request.url())
      if (request.resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (request.resourceType() === 'image') {
        if (/\/10[1-4]\/images\//.test(url.pathname)) {
          photos.push(url.href)
          if (url.href !== sourceUrl(101) && url.href !== fallbackUrl(102) && url.href !== fallbackUrl(103)) return route.fulfill({ status: 404, body: '' })
        }
        return route.fulfill({ contentType: 'image/gif', body: image })
      }
      if (url.origin === 'https://fonts.googleapis.com') return route.fulfill({ contentType: 'text/css', body: '' })
      if (url.origin === 'http://satorna.test' && request.method() === 'GET') {
        if (url.pathname === '/api/v1/wb-repricer/sku') {
          const pageNumber = Number(url.searchParams.get('page') || 1)
          const pageProducts = pageNumber === 1
            ? products.concat(Array.from({ length: 146 }, (_, index) => ({ sku: `ZZZ-FILLER-${index}`, nmId: 2000 + index, photoUrl: `http://satorna.test/filler-${index}.gif` })))
            : [{ ...products[0], sku: 'PHOTO-PAGE-TWO' }]
          return route.fulfill({ json: {
          items: pageProducts.map(product => ({
            meta: { articleId: product.sku, nmId: product.nmId, imageUrl: product.photoUrl, name: product.sku, status: 'auto', currentPriceKopecks: 100000, basketsLast7d: 1, basketNorm: 1 },
            settings: { cogsKopecks: 10000 }, analytics: { baskets: 1, basketsState: 'ok', periodStatsState: 'ok', stockState: 'ok' },
          })), total: 151, itemsReturned: pageProducts.length, page: pageNumber, pageSize: 150, summary: { skuCount: 151 }, cache: { totalCached: 151, pagesCached: 2 },
        } })
        }
        if (url.pathname === '/api/v1/wb-repricer/stats') return route.fulfill({ json: {
          items: products.map(product => ({ articleId: product.sku, nmId: product.nmId, imageUrl: product.photoUrl, name: product.sku, metrics: {}, decision: {}, sources: {}, priceProtection: {}, flags: [] })),
          total: 4, itemsReturned: 4, page: 1, pageSize: 500, summary: { skuCount: 4 },
        } })
        if (url.pathname === '/api/wb/reports/week-over-week/latest-cache') return route.fulfill({ json: {
          meta: { freshnessState: 'fresh', sourceType: 'operational' }, filters: { dateRange: { from: url.searchParams.get('from'), to: url.searchParams.get('to') } },
          rows: products.map(product => ({ ...product, productName: product.sku })), reportJob: null,
        } })
        if (['/api/v1/wb-repricer/strategies/catalog', '/api/v1/wb-repricer/sku-groups', '/api/v1/wb-repricer/changelog'].includes(url.pathname)) return route.fulfill({ json: { items: [], total: 0 } })
        if (['/api/v1/wb-repricer/sync/status', '/api/v1/wb-repricer/worker/status'].includes(url.pathname)) return route.fulfill({ json: { state: 'completed', running: false, steps: [] } })
      }
      unexpected.push(`${request.method()} ${url.pathname}`)
      return route.abort()
    })
    await page.goto(`http://satorna.test${routePath}`)
    await page.addStyleTag({ content: styles })
    await page.addScriptTag({ content: bundle.code })
    const tab = page.locator(tabSelector)
    const row = (sku: string) => tab.locator('tbody tr').filter({ has: page.getByText(sku, { exact: true }) })
    await row('PHOTO-SOURCE').waitFor({ timeout: 15000 })
    const photo = (sku: string) => row(sku).locator('img').first()
    await page.waitForFunction(selector => Array.from(document.querySelectorAll<HTMLImageElement>(`${selector} tbody img`)).filter(img => img.closest('tr')?.textContent?.includes('PHOTO-')).every(img => img.complete), tabSelector, { polling: 50, timeout: 10000 })
    expect.soft(await photo('PHOTO-SOURCE').getAttribute('src')).toBe(sourceUrl(101))
    expect.soft(await photo('PHOTO-NO-URL').count()).toBe(1)
    expect.soft(await photo('PHOTO-SOURCE').evaluate(img => (img as HTMLImageElement).naturalWidth)).toBeGreaterThan(0)
    if (await photo('PHOTO-NO-URL').count()) expect.soft(await photo('PHOTO-NO-URL').getAttribute('src')).toBe(fallbackUrl(102))
    expect.soft(await photo('PHOTO-RETRY').getAttribute('src')).toBe(fallbackUrl(103))
    expect.soft(await photo('PHOTO-UNAVAILABLE').getAttribute('alt')).toBe('Фото недоступно')
    expect.soft(photos.filter(url => url.includes('/101/'))).toEqual([sourceUrl(101)])
    expect.soft(photos.filter(url => url.includes('/104/')).length).toBeLessThanOrEqual(2)
    await photo('PHOTO-UNAVAILABLE').evaluate(img => { img.setAttribute('src', 'http://satorna.test/recovered-photo.gif') })
    await expect.poll(() => photo('PHOTO-UNAVAILABLE').evaluate(img => (img as HTMLImageElement).naturalWidth)).toBe(1)
    expect.soft(await photo('PHOTO-UNAVAILABLE').getAttribute('alt')).toBe('')
    if (routePath.endsWith('/stats')) {
      expect.soft(await page.locator('aside .nav-item.nav-sub-active').getAttribute('data-tab')).toBe('repricer-stats')
      expect.soft(await page.locator('aside .nav-item[data-tab="products"]').getAttribute('class')).not.toContain('nav-sub-active')
    }
    await page.screenshot({ path: `/tmp/satorna-photos-${tabSelector.slice(5)}.png` })
    if (routePath === '/wb/repricer') {
      await page.locator('.vella-products-sticky-pagination').getByRole('button', { name: '›', exact: true }).click()
      await row('PHOTO-PAGE-TWO').waitFor()
      expect(await photo('PHOTO-PAGE-TWO').getAttribute('src')).toBe(sourceUrl(101))
      await page.locator('aside .nav-item[data-tab="repricer-stats"]').click()
      const statsPhoto = page.locator('#repricerStatsBody [data-sku="PHOTO-SOURCE"] img')
      await statsPhoto.waitFor()
      expect(await statsPhoto.getAttribute('src')).toBe(sourceUrl(101))
      expect(await page.locator('aside .nav-item.nav-sub-active').getAttribute('data-tab')).toBe('repricer-stats')
      await page.locator('aside .nav-item[data-tab="products"]').click()
      await expect.poll(() => page.locator('#tbody tr[data-sku^="PHOTO-"] img').count()).toBeGreaterThan(0)
      expect(await page.locator('aside .nav-item.nav-sub-active').getAttribute('data-tab')).toBe('products')
    }
    expect(unexpected).toEqual([])
    expect(errors).toEqual([])
  } finally { await browser.close() }
}, 60000)
