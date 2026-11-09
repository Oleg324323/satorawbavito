import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { chromium } from 'playwright'
import { expect, it } from 'vitest'

it.each(['success', 'execution-error', 'lost-ack', 'rejected-none'] as const)('keeps confirmed assignment state through real clicks after %s and rejects stale reads', async outcome => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({ root, configFile: false, envFile: false, publicDir: false, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""' },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: { entry: fileURLToPath(new URL('./__fixtures__/reportLoadingBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'StrategyClearRegression' } } })
  const output = (Array.isArray(result) ? result : [result]).flatMap(item => 'output' in item ? item.output : [])
  const entry = output.find(item => item.type === 'chunk' && item.isEntry)
  if (!entry || entry.type !== 'chunk') throw new Error('Missing test bundle')
  const css = output.filter(item => item.type === 'asset' && item.fileName.endsWith('.css')).map(item => item.type === 'asset' ? String(item.source) : '').join('\n')
  const browser = await chromium.launch({ headless: true })
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 }, serviceWorkers: 'block' })
  const errors: string[] = [], writes: Array<Record<string, unknown>> = []
  const catalog = [{ id: 'stockout_guard', name: 'Защита out of stock', type: 'stock', status: 'active', color: '#2563eb', rules: [], applied: 1 }]
  let assigned = true
  let revision = 'before'
  let revisionReads = 0
  let delayNextRead = false
  let releaseOld: (() => Promise<unknown>) | undefined
  page.on('pageerror', error => errors.push(error.message))
  await page.route('**/*', async route => {
    const req = route.request(), url = new URL(req.url())
    if (req.resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
    if (req.resourceType() === 'image') return route.fulfill({ status: 204 })
    if (url.hostname === 'fonts.googleapis.com') return route.fulfill({ body: '' })
    if (url.pathname === '/api/v1/wb-repricer/strategy-assignments/bulk') {
      const body = req.postDataJSON(); writes.push(body)
      expect(body.articleIds).toEqual(['CLEAR-443894764'])
      expect(body.expectedRevisions).toEqual({ 'CLEAR-443894764': revision })
      if (body.strategyName === 'none' && outcome === 'rejected-none') {
        return route.fulfill({ status: 409, json: { detail: { code: 'ASSIGNMENT_CONFLICT', message: 'Назначение изменено в другом окне' } } })
      }
      assigned = body.strategyName !== 'none'
      revision = assigned ? 'assigned' : 'cleared'
      expect(body.executeAfterAssign).toBe(assigned)
      if (assigned) delayNextRead = true
      if (assigned && outcome === 'lost-ack') return route.fulfill({ status: 500, json: { detail: 'synthetic post-commit failure' } })
      return route.fulfill({ json: { strategy: assigned ? catalog[0] : { id: 'none', name: 'не задана' }, assignedCount: Number(assigned), unassignedCount: Number(!assigned), items: [], assignmentRevisions: { 'CLEAR-443894764': revision },
        ...(assigned && outcome === 'execution-error' ? { executionError: { code: 'STRATEGY_EXECUTION_FAILED', message: 'Назначение сохранено; исполнение не подтверждено' } } : {}) } })
    }
    if (url.pathname.endsWith('/configuration-state')) {
      revisionReads++
      return route.fulfill({ json: { articleId: 'CLEAR-443894764', assignmentRevision: revision } })
    }
    if (url.pathname === '/api/v1/wb-repricer/strategies/catalog') return route.fulfill({ json: { items: catalog, total: 1 } })
    if (url.pathname === '/api/v1/wb-repricer/sku') {
      const payload = {
      items: [{ meta: { articleId: 'CLEAR-443894764', nmId: 443894764, assignmentRevision: revision, name: 'Synthetic single-product test', status: assigned ? 'auto' : 'manual', currentPriceKopecks: 100000, basketsLast7d: 10, basketNorm: 10, assignmentSource: assigned ? 'manual' : 'none' },
        settings: { automationEnabled: assigned }, analytics: { basketsState: 'ok', periodStatsState: 'ok', financeState: 'no_data', stockState: 'ok' },
        strategy: { ...catalog[0], id: assigned ? 'stockout_guard' : 'metric_dynamics', name: assigned ? catalog[0].name : 'Динамика показателей', assignmentSource: assigned ? 'manual' : 'derived' } }],
      total: 1, summary: { skuCount: 1 }, cache: { pagesCached: 1, totalCached: 1, serverFiltersApplied: true, financeFetchedAt: 'now', periodStatsFetchedAt: 'now', basketsFetchedAt: 'now', adsFetchedAt: 'now' },
      }
      if (delayNextRead) {
        delayNextRead = false
        return new Promise<void>(resolve => { releaseOld = async () => { await route.fulfill({ json: payload, headers: { 'x-repricer-old-read': 'true' } }); resolve() } })
      }
      return route.fulfill({ json: payload })
    }
    if (req.method() !== 'GET') expect(url.pathname).not.toMatch(/\/(?:prices?|apply|upload)(?:\/|$)/)
    return route.fulfill({ json: { items: [], total: 0, state: 'completed', running: false, steps: [] } })
  })
  const mount = async () => {
    await page.goto('http://127.0.0.1:4199/wb/repricer')
    if (css) await page.addStyleTag({ content: css })
    await page.addScriptTag({ content: entry.code })
    await page.locator('#tbody tr[data-sku="CLEAR-443894764"]').waitFor()
  }
  try {
    await mount()
    const row = page.locator('#tbody tr[data-sku="CLEAR-443894764"]')
    await row.locator('.tpl-chip').click()
    await page.locator('.cell-drop-item').filter({ hasText: 'Защита out of stock' }).click()
    await expect.poll(() => Boolean(releaseOld)).toBe(true)
    await row.locator('.tpl-chip').click()
    await page.locator('.cell-drop-item').filter({ hasText: '— нет —' }).click()
    await expect.poll(() => writes.length).toBe(2)
    const rejected = outcome === 'rejected-none'
    if (rejected) {
      await page.getByText('Назначение изменено в другом окне', { exact: true }).waitFor()
      expect(await row.locator('.tpl-none').count()).toBe(0)
    } else await expect.poll(() => row.locator('.tpl-none').count()).toBe(1)
    expect(assigned).toBe(rejected)
    expect(revisionReads).toBe(outcome === 'lost-ack' || rejected ? 1 : 0)
    const oldRead = page.waitForResponse(response => response.headers()['x-repricer-old-read'] === 'true')
    await releaseOld?.()
    await (await oldRead).finished()
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))))
    expect(await row.locator('.tpl-none').count()).toBe(rejected ? 0 : 1)
    expect(await page.getByText('Стратегия «Защита out of stock» сохранена для CLEAR-443894764', { exact: true }).count()).toBe(0)
    await mount()
    expect(await row.locator('.tpl-none').count()).toBe(rejected ? 0 : 1)
    expect(await row.locator('.tpl-chip').count()).toBe(rejected ? 1 : 0)
    expect(errors).toEqual([])
  } finally { await releaseOld?.().catch(() => undefined); await browser.close() }
}, 60000)
