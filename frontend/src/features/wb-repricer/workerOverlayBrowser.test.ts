import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { beforeAll, expect, it } from 'vitest'

let code: string
beforeAll(async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({ configFile: false, envFile: false, root, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""' },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: { entry: fileURLToPath(new URL('./__fixtures__/workerOverlayBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'WorkerPopupFixture' } } })
  const bundle = (Array.isArray(result) ? result : [result]).flatMap(item => 'output' in item ? item.output : [])
    .find(item => item.type === 'chunk' && item.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing worker fixture')
  code = bundle.code
}, 30_000)

const recommendation = (id: string) => ({ approvalId: id, articleId: `SYNTHETIC-${id}`, nmId: 101,
  status: 'pending', oldPriceKopecks: 100_000, recommendedPriceKopecks: 106_000 })

it.each(['status', 'approve', 'bulk'] as const)('never opens an in-flight receipt popup after %s, polling or remount', async scenario => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block' })
    await page.clock.install()
    const inflight = ['sent', 'applying', 'needs_attention', 'pending'].map((status, index) => ({
      ...recommendation(`receipt-${index}`), status, applyState: 'applying', wbMutationSent: true,
      priceConfirmed: false, jobId: `synthetic-job-${index}`,
    }))
    let approvals = scenario === 'status' ? inflight : [recommendation('one'), ...(scenario === 'bulk' ? [recommendation('two')] : []), ...inflight]
    let reads = 0
    const writes: string[] = [], errors: string[] = []
    const bulkTargets: string[][] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', route => {
      const request = route.request(), url = new URL(request.url())
      if (request.resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (url.pathname === '/api/v1/wb-repricer/worker/status' && request.method() === 'GET') {
        reads += 1
        return route.fulfill({ json: { pendingApprovals: approvals } })
      }
      if (request.method() === 'POST' && ['/api/v1/wb-repricer/price-approvals/one/approve', '/api/v1/wb-repricer/price-approvals/bulk'].includes(url.pathname)) {
        writes.push(url.pathname)
        const targets = scenario === 'bulk' ? request.postDataJSON().approvalIds as string[] : ['one']
        if (scenario === 'bulk') bulkTargets.push(targets)
        const results = approvals.filter(item => targets.includes(item.approvalId)).map(item => ({ approvalId: item.approvalId, ok: false,
          approval: { ...item, status: 'sent', applyState: 'applying', wbMutationSent: true, priceConfirmed: false },
          job: { state: 'applying', lastApplyResult: { wbMutationSent: true, priceConfirmed: false } },
        }))
        approvals = inflight
        return route.fulfill({ json: scenario === 'bulk' ? { results } : results[0] })
      }
      errors.push(`${request.method()} ${url.pathname}`)
      return route.abort()
    })
    await page.goto('http://localhost/wb/repricer')
    await page.addScriptTag({ content: code })
    await expect.poll(() => reads).toBe(1)
    await page.evaluate(() => {
      document.body.dataset.pricePublications = '0'
      window.addEventListener('vella:products-rows-updated', () => {
        document.body.dataset.pricePublications = String(Number(document.body.dataset.pricePublications) + 1)
      })
    })
    if (scenario !== 'status') {
      await page.getByRole('dialog', { name: 'Нужно подтвердить изменение цены' }).waitFor()
      await page.getByRole('button', { name: scenario === 'bulk' ? 'Принять все цены 2' : 'Отправить цену в WB' }).click()
      await expect.poll(() => writes.length).toBe(1)
    }
    await expect.poll(() => page.getByRole('dialog').count()).toBe(0)
    const previousReads = reads
    await page.clock.fastForward(15_001)
    await expect.poll(() => reads).toBeGreaterThan(previousReads)
    expect(await page.getByRole('dialog').count()).toBe(0)
    const beforeRemount = reads
    await page.getByRole('button', { name: 'Remount worker' }).click()
    await expect.poll(() => reads).toBeGreaterThan(beforeRemount)
    expect(await page.getByRole('dialog').count()).toBe(0)
    expect(writes).toHaveLength(scenario === 'status' ? 0 : 1)
    expect(bulkTargets).toEqual(scenario === 'bulk' ? [['one', 'two']] : [])
    expect(await page.locator('body').getAttribute('data-price-publications')).toBe('0')
    expect(errors).toEqual([])
  } finally { await browser.close() }
}, 30_000)
