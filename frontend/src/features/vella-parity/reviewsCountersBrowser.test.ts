import path from 'node:path'
import { fileURLToPath } from 'node:url'
import react from '@vitejs/plugin-react'
import { chromium } from 'playwright'
import { build } from 'vite'
import { expect, it } from 'vitest'
import type { ReportRulesConfig, ReportRulesDraft, ReportRulesProfile } from '../wb-reports/reportRulesApi'
import type { WbReviewFeedback } from '../wb-reviews/api'

const at = '2026-09-14T12:00:00Z'
const feedbacks: WbReviewFeedback[] = ['draft', 'scheduled', 'new', 'sent'].map((kind, index) => ({
  feedbackId: `synthetic-${kind}`, nmId: 900001 + index,
  brandName: index % 2 ? 'Bless T' : 'Anomie studio', productName: `Тестовый товар ${index + 1}`,
  createdDate: at, syncedAt: at, text: `Синтетический отзыв ${index + 1}`,
  pros: '', cons: '', rating: 5, isAnswered: kind === 'sent', sourceStatus: 'fresh',
  latestDraft: kind === 'new' ? null : {
    draftId: `draft-${index + 1}`, rating: 5, brandVoiceId: 'wb-default',
    approvalState: 'required', sendState: kind === 'sent' ? 'sent' : kind === 'scheduled' ? 'ready_to_send' : 'draft_only',
    generatedText: `Подготовленный ответ ${index + 1}`, moderationState: 'clean', moderationReasons: [], updatedAt: at,
  },
}))

const config: ReportRulesConfig = {
  abc: { salesShare: { aPct: 20, bPct: 30, cPct: 50 }, netProfitShare: { aPct: 20, bPct: 30, cPct: 50 } },
  qualityBands: {
    ctrPct: { goodMin: 10, averageMin: 6 }, crPct: { goodMin: 4, averageMin: 2 },
    cartToOrderPct: { goodMin: 45, averageMin: 25 }, buyoutPct: { goodMin: 80, averageMin: 60 },
    marginPct: { goodMin: 25, thinMin: 10, lossBelow: 0 }, drrPct: { goodMax: 9, warnMin: 14 },
    roiPct: { goodMin: 250, warnBelow: 100 }, daysToOos: { warnBelow: 7 },
    stockUnits: { criticalBelow: 12 }, localizationPct: { badBelow: 60 },
  },
  automationMapping: { aaGood: 'raise_price', badCr: 'rnp', loss: 'liquidation', highDrr: 'stop_ads', oos: 'alert', cWeak: 'audit' },
}

it('keeps review counts scoped, rules editable and product sync active after removing technical UI', async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({ root, configFile: false, envFile: false, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""', 'import.meta.env.VITE_WB_LIVE_ENABLED': '"false"', 'import.meta.env.VITE_CANONICAL_REVIEWS_ENABLED': '"false"' },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: { entry: fileURLToPath(new URL('./__fixtures__/reviewsCountersBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'ReviewsCountersTest' } },
  })
  const bundle = (Array.isArray(result) ? result : [result]).flatMap(output => 'output' in output ? output.output : []).find(output => output.type === 'chunk' && output.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing reviews counters bundle')
  const browser = await chromium.launch({ headless: true })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1440, height: 1000 } })
    const errors: string[] = [], unexpected: string[] = [], writes: { method: string; path: string; body: ReportRulesDraft & { previewToken?: string } }[] = []
    let syncFinished = false, productReads = 0
    let profile: ReportRulesProfile = { profileId: 1, organizationId: 7, version: 1, name: 'Стандартный профиль', preset: 'standard', config, createdByUserId: null, createdAt: null, isActive: true }
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', route => {
      const request = route.request(), url = new URL(request.url())
      if (request.resourceType() === 'document' && url.origin === 'http://satorna.test') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (request.resourceType() === 'image') return route.fulfill({ status: 204 })
      if (url.origin === 'https://fonts.googleapis.com') return route.fulfill({ contentType: 'text/css', body: '' })
      if (request.method() === 'GET') {
        if (url.pathname === '/api/v1/wb-reviews/feedbacks') return route.fulfill({ json: { items: feedbacks, total: 4, limit: 100, offset: 0 } })
        if (url.pathname === '/api/v1/wb-reviews/sync-settings') return route.fulfill({ json: { data: { organizationId: 7, enabled: true, intervalMinutes: 30, tokenType: 'personal', unansweredOnly: true, take: 500, lookbackDays: 30, aiPrompt: '', minIntervalMinutes: 1, createdAt: at, updatedAt: at } } })
        if (url.pathname === '/api/v1/wb-reviews/sync-status') return route.fulfill({ json: { data: { organizationId: 7, status: 'completed', enabled: true, intervalMinutes: 30, lastSyncedCount: 4, lastRunAt: at, lastCompletedAt: at, lastSourceStatus: 'fresh' } } })
        if (url.pathname === '/api/wb/reports/rules') return route.fulfill({ json: { profile, presets: { standard: config, conservative: config, aggressive: config }, automationActions: Object.values(config.automationMapping), canWrite: true } })
        if (url.pathname === '/api/wb/reports/rules/history') return route.fulfill({ json: { items: [] } })
        if (url.pathname === '/api/wb/reports/digest/plan') return route.fulfill({ json: { month: url.searchParams.get('month'), company: { revenuePlanKopecks: 0, marginPlanKopecks: 0 }, managers: [] } })
        if (url.pathname === '/api/v1/wb-repricer/sku') {
          productReads += 1
          return route.fulfill({ json: { items: [{ meta: { articleId: 'SYNTHETIC-SKU', nmId: 900001, name: 'Синтетический товар', status: 'auto', currentPriceKopecks: syncFinished ? 210000 : 200000 }, settings: {}, analytics: { baskets: 1, financeState: 'ok' } }], total: 1, page: 1, pageSize: 150, itemsReturned: 1, summary: { skuCount: 1 }, cache: { totalCached: 1, pagesCached: 1 } } })
        }
        if (url.pathname === '/api/v1/wb-repricer/sync/status') return route.fulfill({ json: { state: syncFinished ? 'completed' : 'running', running: !syncFinished, finishedAt: syncFinished ? at : null, steps: [] } })
        if (url.pathname === '/api/v1/wb-repricer/worker/status') return route.fulfill({ json: { pendingApprovals: [] } })
        if (['/api/v1/wb-repricer/strategies/catalog', '/api/v1/wb-repricer/sku-groups', '/api/v1/wb-repricer/changelog'].includes(url.pathname)) return route.fulfill({ json: { items: [], total: 0 } })
      }
      if (request.method() === 'POST' && url.pathname === '/api/wb/reports/rules/preview') {
        writes.push({ method: request.method(), path: url.pathname, body: request.postDataJSON() })
        return route.fulfill({ json: { affectedSkuCount: 1, statusChanges: 1, automationImpact: {}, sampleRows: [], warnings: [], availableReports: ['rnp'], previewToken: 'synthetic-preview' } })
      }
      if (request.method() === 'PUT' && url.pathname === '/api/wb/reports/rules') {
        const body = request.postDataJSON() as ReportRulesDraft & { previewToken: string }
        writes.push({ method: request.method(), path: url.pathname, body })
        profile = { ...profile, name: body.name, preset: body.preset, config: body.config, version: profile.version + 1 }
        return route.fulfill({ json: { profile } })
      }
      unexpected.push(`${request.method()} ${url.pathname}`)
      return route.abort()
    })
    await page.goto('http://satorna.test/wb/reviews')
    await page.clock.install()
    await page.addScriptTag({ content: bundle.code })
    const reviews = page.locator('#tab-reviews')
    await expect.poll(() => reviews.locator('#reviewsTableBody tr').count()).toBe(4)
    const counts = async (rows: number, queue: number, drafts: number, open: number) => {
      await expect.poll(() => reviews.locator('#reviewsTableBody tr').count()).toBe(rows)
      await expect.poll(() => reviews.locator('#reviewsQueueList .review-queue-item').count()).toBe(queue)
      expect(await reviews.locator('#reviewsKpiQueue').innerText()).toBe(String(queue))
      expect(await reviews.locator('#reviewsKpiAuto').innerText()).toBe(String(drafts))
      expect(await reviews.locator('#reviewsKpiOpen').innerText()).toBe(String(open))
      expect(await reviews.locator('#reviewsKpiBlocked').innerText()).toBe('0')
    }
    await counts(4, 2, 2, 3)
    expect(await reviews.locator('.stats').innerText()).toContain('Готовые черновики')
    expect(await reviews.locator('#reviewsKpiAvgTime').innerText()).toBe('—')
    expect(await reviews.locator('.stat-delta, .reviews-backend-state').count()).toBe(0)
    expect(await reviews.innerText()).not.toMatch(/ИСТОЧНИК: BACKEND|Автоответы сегодня|после загрузки backend|по правилам backend|draft-only режим|по ответу backend/)
    expect(await reviews.innerText()).toContain('отправка в WB отключена')
    await reviews.locator('#reviewsSearch').fill('900001')
    await counts(1, 1, 1, 1)
    await reviews.locator('#reviewsSearch').fill('')
    for (const [status, queue, drafts, open] of [['pending_review', 1, 1, 1], ['scheduled', 0, 1, 1]] as const) {
      await reviews.locator(`[data-review-filter="status"][data-value="${status}"]`).click()
      await counts(1, queue, drafts, open)
    }
    await reviews.locator('[data-review-filter="status"][data-value="all"]').click()
    for (const [sku, queue, open] of [['900003', 1, 1], ['900004', 0, 0]] as const) {
      await reviews.locator('#reviewsSearch').fill(sku)
      await counts(1, queue, 0, open)
    }
    await reviews.locator('#reviewsSearch').fill('')
    await counts(4, 2, 2, 3)
    expect(writes).toEqual([])

    await page.evaluate(() => { history.pushState({}, '', '/wb/reports/rules'); window.dispatchEvent(new PopStateEvent('popstate')) })
    const rules = page.locator('#tab-report-rules')
    await expect.poll(() => rules.locator('#thresholdProfileTitle').innerText()).toBe('Стандартный профиль')
    expect(await rules.innerText()).not.toMatch(/Глобальные пороги отчётов и автоматики|версия 1 · standard|Пороги меняют классификацию/)
    expect.soft(await page.getByRole('button', { name: 'Экспорт', exact: true }).evaluateAll(buttons => buttons.map(button => button.outerHTML))).toEqual([])
    expect(await rules.locator('.threshold-preset').count()).toBe(3)
    expect(await rules.locator('.settings-nav').isVisible()).toBe(true)
    expect(await rules.locator('#thresholdSaveBtn').isDisabled()).toBe(true)
    await rules.locator('[data-threshold-field="qualityBands.marginPct.lossBelow"]').fill('-5')
    await rules.getByRole('button', { name: 'Проверить влияние', exact: true }).click()
    await expect.poll(() => rules.locator('#thresholdSaveBtn').isDisabled()).toBe(false)
    await rules.locator('#thresholdSaveBtn').click()
    await expect.poll(() => writes.length).toBe(2)
    expect(writes.map(write => [write.method, write.path])).toEqual([['POST', '/api/wb/reports/rules/preview'], ['PUT', '/api/wb/reports/rules']])
    expect(writes[1].body).toEqual({ ...writes[0].body, previewToken: 'synthetic-preview' })
    expect(writes[1].body.config.abc).toEqual(config.abc)
    expect(writes[1].body.config.qualityBands.marginPct.lossBelow).toBe(-5)
    expect(profile.version).toBe(2)

    await page.evaluate(() => { history.pushState({}, '', '/wb/repricer'); window.dispatchEvent(new PopStateEvent('popstate')) })
    await expect.poll(async () => (await page.locator('#tbody').innerText()).replace(/\s+/g, ' ')).toContain('2 000 ₽')
    await expect.poll(() => page.evaluate(() => window.__vellaProductsFullSyncRunning)).toBe(true)
    const before = productReads
    syncFinished = true
    await page.clock.fastForward(15001)
    await expect.poll(() => productReads).toBeGreaterThan(before)
    await expect.poll(async () => (await page.locator('#tbody').innerText()).replace(/\s+/g, ' ')).toContain('2 100 ₽')
    expect(await page.locator('.worker-layout, .worker-overlay, [data-vella-island="products-backend-cache-controls"]').count()).toBe(0)
    expect(await page.locator('.vella-html-parity-root').evaluate(element => element.getBoundingClientRect().top)).toBe(0)
    expect(unexpected).toEqual([])
    expect(errors).toEqual([])
  } finally { await browser.close() }
}, 60_000)
