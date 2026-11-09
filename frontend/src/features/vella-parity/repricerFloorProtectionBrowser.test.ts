import path from 'node:path'
import { fileURLToPath } from 'node:url'
import react from '@vitejs/plugin-react'
import { chromium } from 'playwright'
import { build } from 'vite'
import { expect, it } from 'vitest'

const floors = [
  { sku: 'PMIN-CALCULATED', raw: 0, effective: 90000, display: '900₽', editor: '', after: '' },
  { sku: 'PMIN-LIQUIDATION', raw: 50000, effective: 77700, display: '777₽', editor: '500', after: '400' },
  { sku: 'PMIN-OLD-MANUAL', raw: 125000, effective: undefined, display: '1250₽', editor: '1250', after: '1000' },
  { sku: 'PMIN-OLD-CALCULATED', raw: 0, effective: undefined, display: '786₽', editor: '', after: '' },
]
const items = floors.map((floor, index) => ({
  meta: { articleId: floor.sku, nmId: 100 + index, name: floor.sku, status: 'auto', currentPriceKopecks: 220000, basketsLast7d: 1, basketNorm: 1 },
  settings: { cogsKopecks: 50000, logisticsKopecks: 5000, wbCommissionPct: 15, minMarginPct: 15, pMinKopecks: floor.raw,
    ...floor.effective == null ? {} : { effectivePMinKopecks: floor.effective, effectivePMinSource: 'calculated' } },
  analytics: { sppPct: 20, baskets: 1, basketsState: 'ok', periodStatsState: 'ok', stockState: 'ok' },
}))
const skipped = [
  { sku: 'NO-STRATEGY', status: 'not_configured', reason: 'no_strategy', label: 'Не назначена стратегия', detail: 'не назначена стратегия' },
  { sku: 'NO-WB-ID', status: 'not_configured', reason: 'no_nm_id', label: 'Не указан артикул WB', detail: 'нет артикула WB' },
  { sku: 'MANUAL', status: 'paused', reason: 'manual_mode', label: 'Ручной режим', detail: 'ручной режим' },
  { sku: 'DISABLED', status: 'paused', reason: 'automation_disabled', label: 'Автоматизация выключена', detail: 'автоматизация выключена' },
  { sku: 'WARMUP', status: 'paused', reason: 'warmup', label: 'Прогрев', detail: 'идёт прогрев' },
]
const statsItems = [
  ...skipped.map(row => ({ articleId: row.sku,
    decision: { id: row.status === 'paused' ? 'hold' : 'not_configured', label: row.label, tone: row.status === 'paused' ? 'neutral' : 'warn', reasons: [row.reason] },
    priceProtection: { status: row.status, skipReason: row.reason, blockerIds: ['spp'], effectivePMinKopecks: 90000, effectivePMinSource: 'calculated' },
  })),
  { articleId: 'READY', decision: { id: 'can_recalculate', label: 'Готово к пересчёту', tone: 'ok', reasons: [] }, priceProtection: { status: 'can_recalculate', blockerIds: [] } },
  { articleId: 'BLOCKED', decision: { id: 'price_blocked', label: 'Цена заблокирована', tone: 'bad', reasons: ['spp'] }, priceProtection: { status: 'blocked', blockerIds: ['spp'] } },
].map(row => ({ ...row, nmId: row.articleId === 'NO-WB-ID' ? null : 101, name: row.articleId,
  sources: row.articleId === 'READY' ? { status: 'ready', missing: [] } : { status: 'blocked', missing: ['spp'] },
  metrics: { baskets: 1 }, flags: [] }))

it.each(['/wb/repricer', '/wb/repricer/stats'])('uses the backend floor and decision contract on %s', async routePath => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({ root, configFile: false, envFile: false, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""', 'import.meta.env.VITE_WB_LIVE_ENABLED': '"false"' },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: { entry: fileURLToPath(new URL('./__fixtures__/productsCatalogCountBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'RepricerFloorProtectionTest' } },
  })
  const artifacts = (Array.isArray(result) ? result : [result]).flatMap(output => 'output' in output ? output.output : [])
  const bundle = artifacts.find(output => output.type === 'chunk' && output.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing floor/protection fixture')
  const styles = artifacts.flatMap(output => output.type === 'asset' && output.fileName.endsWith('.css') ? [String(output.source)] : []).join('\n')
  const browser = await chromium.launch({ headless: true })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1440, height: 1000 } })
    const errors: string[] = [], unexpected: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', route => {
      const request = route.request(), url = new URL(request.url())
      if (request.resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (request.resourceType() === 'image') return route.fulfill({ contentType: 'image/gif', body: Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64') })
      if (url.origin === 'https://fonts.googleapis.com') return route.fulfill({ contentType: 'text/css', body: '' })
      if (url.origin === 'http://satorna.test' && request.method() === 'GET') {
        if (url.pathname === '/api/v1/wb-repricer/sku') return route.fulfill({ json: { items, total: 4, itemsReturned: 4, page: 1, pageSize: 150, summary: { skuCount: 4 }, cache: { totalCached: 4, pagesCached: 1 } } })
        if (url.pathname === '/api/v1/wb-repricer/stats') return route.fulfill({ json: {
          items: statsItems, total: 7, itemsReturned: 7, page: 1, pageSize: 500,
          summary: { skuCount: 7, canRecalculate: 1, priceBlocked: 1, sourceReady: 1, sourcePartial: 0, sourceBlocked: 6, baskets: 7 },
        } })
        const skuMatch = url.pathname.match(/^\/api\/v1\/wb-repricer\/sku\/([^/]+)\/(settings|changelog|pricing-status|timeseries)$/)
        if (skuMatch && floors.some(row => row.sku === skuMatch[1])) {
          if (skuMatch[2] === 'settings') return route.fulfill({ json: items.find(row => row.meta.articleId === skuMatch[1]) })
          if (skuMatch[2] === 'changelog') return route.fulfill({ json: { items: [] } })
          return route.fulfill({ status: 404, json: { error: { code: 'SYNTHETIC_NO_HISTORY', message: 'Нет истории в тесте' } } })
        }
        if (['/api/v1/wb-repricer/strategies/catalog', '/api/v1/wb-repricer/sku-groups', '/api/v1/wb-repricer/changelog'].includes(url.pathname)) return route.fulfill({ json: { items: [], total: 0 } })
        if (['/api/v1/wb-repricer/sync/status', '/api/v1/wb-repricer/worker/status'].includes(url.pathname)) return route.fulfill({ json: { state: 'completed', running: false, steps: [] } })
      }
      unexpected.push(`${request.method()} ${url.pathname}`)
      return route.abort()
    })
    await page.goto(`http://satorna.test${routePath}`)
    await page.addStyleTag({ content: styles })
    await page.addScriptTag({ content: bundle.code })
    if (routePath === '/wb/repricer') {
      await page.locator('#tbody tr[data-sku="PMIN-CALCULATED"]').waitFor({ timeout: 15000 })
      for (const floor of floors) {
        await page.locator(`#tbody tr[data-sku="${floor.sku}"] .sku`).click()
        await page.locator('#dPanel.open').waitFor()
        expect.soft((await page.locator('#dPmin').innerText()).replace(/\s/g, '')).toBe(floor.display)
        await page.locator('#dPanel [data-dtab="algo"]').click()
        await page.locator('#drawerPminBeforeInput').waitFor()
        expect.soft(await page.locator('#drawerPminBeforeInput').inputValue()).toBe(floor.editor)
        await page.locator('#priceBasisAfterBtn').click()
        expect.soft(await page.locator('#drawerPminAfterInput').inputValue()).toBe(floor.after)
        if (floor.sku === 'PMIN-CALCULATED') await page.screenshot({ path: '/tmp/satorna-effective-pmin-local.png' })
        await page.locator('#dPanel .drawer-close').click()
      }
    } else {
      const tab = page.locator('#tab-repricer-stats')
      await tab.getByText('NO-STRATEGY', { exact: true }).first().waitFor({ timeout: 15000 })
      for (const row of skipped) {
        const rendered = tab.locator('tbody tr').filter({ has: page.locator(`[data-sku="${row.sku}"]`) })
        expect.soft(await rendered.locator('[data-stats-column="protection"]').innerText()).toContain(row.status === 'paused' ? 'приостановлено' : 'не настроено')
        expect.soft(await rendered.locator('[data-stats-column="action"] .report-tag').innerText()).toBe(row.label)
        expect.soft(await rendered.locator('.report-decision-reason').innerText()).toBe(row.detail)
        expect.soft(await rendered.getAttribute('data-report-tags')).not.toMatch(/цена заблокирована|готово к пересчёту/)
      }
      expect(await tab.locator('[data-stats-card="canRecalculate"] .stat-val').innerText()).toBe('1 товаров')
      expect(await tab.locator('[data-stats-card="priceBlocked"] .stat-val').innerText()).toBe('1 товаров')
      await tab.locator('.report-table-wrap').evaluate(element => { element.scrollLeft = element.scrollWidth })
      await page.screenshot({ path: '/tmp/satorna-stats-skipped-local.png' })
    }
    expect(unexpected).toEqual([])
    expect(errors).toEqual([])
  } finally { await browser.close() }
}, 60000)
