import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium, type Page } from 'playwright'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { beforeAll, expect, it } from 'vitest'
import type { CanonicalAbcPnlPage } from '../wb-finance/canonicalAbcPnl'

// Synthetic contract values only; the page and its row renderers are real.
const item: CanonicalAbcPnlPage['items'][number] = {
  nmId: null, sellerArticle: 'SKU-1', catalogSkuId: null, operationCount: 1,
  revenueKopecks: 10000, salesRevenueKopecks: 10000, returnsRevenueKopecks: 0,
  mainRevenueKopecks: 10000, redemptionsRevenueKopecks: 0, lateCorrectionRevenueKopecks: 0,
  unknownRevenueKopecks: 0, salesUnits: 1, returnsUnits: 0, netUnits: 1,
  commissionKopecks: 0, logisticsKopecks: 0, storageKopecks: 0, acceptanceKopecks: 0,
  penaltyKopecks: 0, deductionKopecks: 0, additionalPaymentKopecks: 0, financeOtherExpensesKopecks: 0,
  compensationKopecks: 0, acquiringKopecks: 0, financeExpensesKopecks: 0,
  costValueState: 'missing', costEvidenceStatus: null, cogsKopecks: null, settlementProfitKopecks: null,
  economicsValueState: 'missing', economicsEvidenceStatus: null, taxKopecks: null, otherExpensesKopecks: null,
  profitBeforeAdsAndLoyaltyKopecks: null, advertisingSpendKopecks: null, profitBeforeLoyaltyKopecks: null,
  cashbackAmountKopecks: null, cashbackDiscountKopecks: null, cashbackCommissionChangeKopecks: null,
  loyaltyNetCostKopecks: null, profitBeforeInternalExpensesKopecks: null, profitAfterLoyaltyKopecks: null, salesClass: 'A', profitClass: null,
  abcCode: null, internalExpensesKopecks: null, netProfitKopecks: null, blockerIds: ['WB_PNL_COST_MISSING'],
}
const payload: CanonicalAbcPnlPage = {
  items: Array.from({ length: 60 }, (_, i) => ({ ...item, nmId: 500000000 + i, sellerArticle: `SKU-${i + 1}` })),
  total: 60, limit: 500, offset: 0,
  summary: {
    operationCount: 60, skuCount: 60, revenueKopecks: 600000, salesRevenueKopecks: 600000,
    returnsRevenueKopecks: 0, salesUnits: 60, returnsUnits: 0, netUnits: 60, commissionKopecks: 0,
    logisticsKopecks: 0, storageKopecks: 0, acceptanceKopecks: 0, penaltyKopecks: 0, deductionKopecks: 0,
    financeOtherExpensesKopecks: 0, compensationKopecks: 0, acquiringKopecks: 0, financeExpensesKopecks: 0,
    cogsKopecks: null, settlementProfitKopecks: null, taxKopecks: null, otherExpensesKopecks: null,
    profitBeforeAdsAndLoyaltyKopecks: null, advertisingSpendKopecks: null, unattributedAdvertisingSpendKopecks: null,
    profitBeforeLoyaltyKopecks: null, cashbackAmountKopecks: null, cashbackDiscountKopecks: null,
    cashbackCommissionChangeKopecks: null, loyaltyNetCostKopecks: null, profitBeforeInternalExpensesKopecks: null, profitAfterLoyaltyKopecks: null, internalExpensesKopecks: null, netProfitKopecks: null,
  },
  meta: {
    state: 'partial', marketplaceAccountId: 31, period: { dateFrom: '2026-09-01', dateTo: '2026-09-07',
      timezone: 'Europe/Moscow', startAt: '2026-08-31T21:00:00Z', endExclusiveAt: '2026-09-07T21:00:00Z', days: 7, temporalState: 'complete' },
    snapshot: null, formulaVersion: 'wb-abc-pnl-finance-rrdate-v4', costLedgerRevision: 0, economicsRevision: 0,
    advertisingSource: null, advertisingSnapshotChecksum: null, advertisingEvidenceStatus: null, blockerIds: ['WB_PNL_COST_MISSING'],
  }, timestamp: '2026-09-08T07:00:00Z',
}

let bundleCode: string
beforeAll(async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({
    configFile: false, envFile: false, root, logLevel: 'silent',
    plugins: [{ name: 'canonical-auth-fixture', enforce: 'pre', transform(source, id) {
      if (!id.endsWith('/__fixtures__/reportLoadingBrowser.tsx')) return
      return source.replace('cabinetMe: null', `cabinetMe: ({ organization: { organizationId: window.location.hash === '#legacy' ? 9 : 7 }, user: { userId: 1, permissions: [] } } as AuthContextValue['cabinetMe'])`)
        .replace('const [accessToken, setAccessToken]', 'const [organizationId, setOrganizationId] = useState(auth.cabinetMe?.organization.organizationId ?? 7)\n  const [accessToken, setAccessToken]')
        .replace('<AuthContext.Provider value={{ ...auth, accessToken,', `<button style={{ position: 'fixed', zIndex: 999999, top: 22, right: 0 }} onClick={() => setOrganizationId(8)}>Change synthetic account</button><button style={{ position: 'fixed', zIndex: 999999, top: 44, right: 0 }} onClick={() => setAccessToken('synthetic-replaced')}>Replace synthetic token</button><AuthContext.Provider value={{ ...auth, cabinetMe: auth.cabinetMe ? { ...auth.cabinetMe, organization: { ...auth.cabinetMe.organization, organizationId } } : null, accessToken,`)
    } }, react()],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""',
      'import.meta.env.VITE_CANONICAL_WB_ABC_PNL_ROLLOUT': '"7:31,8:32"' },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: {
      entry: fileURLToPath(new URL('./__fixtures__/reportLoadingBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'AbcPnlReliability',
    } },
  })
  const chunk = (Array.isArray(result) ? result : [result]).flatMap(output => 'output' in output ? output.output : [])
    .find(output => output.type === 'chunk' && output.isEntry)
  if (!chunk || chunk.type !== 'chunk') throw new Error('Missing ABC/P&L browser bundle')
  bundleCode = chunk.code
}, 60_000)

async function mount(page: Page, tab: 'abc' | 'pnl', legacy = false, canonicalPayload = payload) {
  const errors: string[] = [], unexpected: string[] = [], queries: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  // The unchanged legacy shell emits React's empty image src warning.
  page.on('console', message => { if (message.type() === 'error' && !message.text().startsWith('An empty string')) errors.push(message.text()) })
  await page.clock.setFixedTime(new Date('2026-09-11T12:00:00Z'))
  await page.route('**/*', route => {
    const request = route.request(), url = new URL(request.url())
    if (request.method() === 'GET' && url.origin === 'http://satorna.test') {
      if (url.pathname === `/wb/reports/${tab}`) return route.fulfill({ contentType: 'text/html', body: '<title>Synthetic report reliability</title><div id="root"></div>' })
      if (url.pathname === '/api/v1/cabinet/team/users') return route.fulfill({ json: { data: [] } })
      if (url.pathname === '/api/v1/cabinet/wb-token') return route.fulfill({ json: { data: { userId: '1', hasToken: false, tokenMasked: null, updatedAt: null } } })
      if (url.pathname === '/api/v1/cabinet/avito-credentials') return route.fulfill({ json: { data: { userId: '1', hasCredentials: false, clientIdMasked: null, clientSecretMasked: null, accessTokenExpiresAt: null, updatedAt: null } } })
      if (url.pathname === '/api/wb/reports/abc/latest-cache') return route.fulfill({ json: { rows: [] } })
      if (!legacy && url.pathname === '/api/v2/wb/reports/abc-pnl') {
        queries.push(url.search)
        const dateFrom = url.searchParams.get('dateFrom') ?? '2026-09-01', dateTo = url.searchParams.get('dateTo') ?? '2026-09-07'
        const start = new Date(`${dateFrom}T00:00:00+03:00`), end = new Date(`${dateTo}T00:00:00+03:00`)
        const days = (end.getTime() - start.getTime()) / 86400000 + 1
        return route.fulfill({ json: { ...canonicalPayload, meta: { ...canonicalPayload.meta, marketplaceAccountId: Number(url.searchParams.get('marketplaceAccountId')), period: { ...canonicalPayload.meta.period,
          dateFrom, dateTo, days, startAt: start.toISOString(), endExclusiveAt: new Date(end.getTime() + 86400000).toISOString(),
        } } } })
      }
      if (legacy && url.pathname === '/api/wb/reports/pnl/latest-cache') return route.fulfill({ json: {
        meta: { dateRange: { from: '2026-09-01', to: '2026-09-07' }, sourceType: 'financial' },
        kpis: [{ id: 'revenue', value: 6000 }, { id: 'net_profit', value: null }],
        rows: ['blocked', 'partial', 'ready'].map((sourceStatus, index) => ({
          articleId: `STATUS-${sourceStatus}`, productName: sourceStatus, sourceStatus, confidence: sourceStatus === 'ready' ? 'high' : 'blocked',
          revenueKopecks: (index + 1) * 1000, cogsKopecks: sourceStatus === 'ready' ? 1000 : null,
          commissionKopecks: 0, logisticsKopecks: 0, storageKopecks: 0, returnsPenaltyKopecks: 0,
          netProfitKopecks: sourceStatus === 'ready' ? 2000 : null,
        })),
      } })
      if (!legacy && url.pathname === '/api/wb/reports/pnl/latest-cache' && url.searchParams.get('source') === 'operational') return route.fulfill({ json: {
        meta: { dateRange: { from: '2026-09-01', to: '2026-09-07' }, sourceType: 'operational' },
        rows: [], cashFlow: null, reportJob: null,
      } })
      if (url.pathname === '/api/v1/wb-repricer/cache/coverage') {
        return route.fulfill({ json: { dateFrom: '2026-06-14', dateTo: '2026-09-11', sources: [], days: [], summary: { totalDays: 90, completeDays: 0, partialDays: 0, missingDays: 90 } } })
      }
    }
    if (request.resourceType() === 'image') return route.fulfill({ contentType: 'image/gif', body: Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64') })
    if (url.origin === 'https://fonts.googleapis.com') return route.fulfill({ contentType: 'text/css', body: '' })
    unexpected.push(`${request.method()} ${url.pathname}`)
    return route.abort()
  })
  await page.goto(`http://satorna.test/wb/reports/${tab}${legacy ? '#legacy' : ''}`)
  await page.evaluate(tab => { window.__vellaReportPeriods = { [tab]: { days: 7, mode: 'custom', fromIso: '2026-09-01', toIso: '2026-09-07', label: 'Synthetic period' } } }, tab)
  await page.addScriptTag({ content: bundleCode })
  await page.locator(`#tab-${tab} [data-report-row]`).first().waitFor({ timeout: 15_000 }).catch(async error => {
    throw new Error(`${error.message}\n${JSON.stringify({ errors, unexpected, queries, text: await page.locator('body').innerText() })}`)
  })
  expect(page.url()).toContain(`/wb/reports/${tab}`)
  expect(await page.title()).toBeTruthy()
  return { errors, unexpected, queries }
}

  it('shows confirmed profit without replacing unknown costs with zero', async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1440, height: 1000 } })
    // Hand checked, kopecks: 10000 - 2000 - 1500 = 6500;
    // 6500 - 300 - 200 = 6000; 6000 - 1000 = 5000; 5000 - 100 = 4900.
    // This is synthetic arithmetic, not an owner's tax rate or cost policy.
    const known: CanonicalAbcPnlPage['items'][number] = { ...item, nmId: 500000001, sellerArticle: 'KNOWN', cogsKopecks: 2000,
      costValueState: 'configured', costEvidenceStatus: 'dated', economicsValueState: 'configured', economicsEvidenceStatus: 'dated', blockerIds: [],
      commissionKopecks: 1000, logisticsKopecks: 200, storageKopecks: 100,
      acceptanceKopecks: 100, acquiringKopecks: 200, compensationKopecks: 100,
      financeExpensesKopecks: 1500, settlementProfitKopecks: 6500, taxKopecks: 300,
      otherExpensesKopecks: 200, profitBeforeAdsAndLoyaltyKopecks: 6000,
      advertisingSpendKopecks: 1000, profitBeforeLoyaltyKopecks: 5000,
      loyaltyNetCostKopecks: 100, profitBeforeInternalExpensesKopecks: 4900, profitAfterLoyaltyKopecks: 4900,
      internalExpensesKopecks: 0, netProfitKopecks: 4900 }
    const evidence = await mount(page, 'pnl', false, { ...payload,
      items: [known, { ...item, nmId: 500000002, sellerArticle: 'UNKNOWN' },
        { ...known, nmId: 500000003, sellerArticle: 'ZERO', advertisingSpendKopecks: 5900, profitBeforeLoyaltyKopecks: 100, profitBeforeInternalExpensesKopecks: 0, profitAfterLoyaltyKopecks: 0, netProfitKopecks: 0 },
        { ...known, nmId: 500000004, sellerArticle: 'LOSS', advertisingSpendKopecks: 6001, profitBeforeLoyaltyKopecks: -1, profitBeforeInternalExpensesKopecks: -101, profitAfterLoyaltyKopecks: -101, netProfitKopecks: -101 },
      ], total: 4, summary: { ...payload.summary, operationCount: 4, skuCount: 4,
        revenueKopecks: 40000, salesRevenueKopecks: 40000, salesUnits: 4, netUnits: 4,
        commissionKopecks: 3000, logisticsKopecks: 600, storageKopecks: 300,
        acceptanceKopecks: 300, acquiringKopecks: 600, compensationKopecks: 300,
        financeExpensesKopecks: 4500,
      } })
    const surface = page.locator('#tab-pnl')
    await surface.getByText('Полный состав расчёта', { exact: true }).click()
    const breakdown = surface.getByRole('table', { name: 'Состав прибыли по выбранным строкам' })
    const value = (label: string) => breakdown.getByRole('row').filter({ has: page.getByRole('rowheader', { name: label, exact: true }) }).getByRole('cell').first()
    expect(await value('Промежуточная прибыль WB').innerText()).toBe('нет данных')
    await surface.locator('.search input').fill('500000001')
    await expect.poll(() => surface.locator('[data-report-row]:visible').count()).toBe(1)
    for (const [label, expected] of [['Расходы WB, всего', '15,00 ₽'], ['Компенсации WB', '1,00 ₽'],
      ['Промежуточная прибыль WB', '65,00 ₽'], ['Налог', '3,00 ₽'],
      ['До рекламы и лояльности', '60,00 ₽'], ['До лояльности', '50,00 ₽'],
      ['Прибыль', '49,00 ₽']]) expect(await value(label).innerText()).toBe(expected)
    await surface.locator('.search input').fill('ZERO')
    await expect.poll(() => value('Прибыль').innerText()).toBe('0,00 ₽')
    await surface.locator('.search input').fill('LOSS')
    await expect.poll(() => value('Прибыль').innerText()).toBe('-1,01 ₽')
    if (process.env.SATORNA_REPORT_UI_SCREENSHOTS) {
      await breakdown.scrollIntoViewIfNeeded()
      await page.screenshot({ path: '/tmp/satorna-profit-breakdown-desktop.png' })
    }
    await page.setViewportSize({ width: 390, height: 844 })
    expect(await breakdown.isVisible()).toBe(false)
    if (process.env.SATORNA_REPORT_UI_SCREENSHOTS) await page.screenshot({ path: '/tmp/satorna-profit-breakdown-mobile.png' })
    await page.setViewportSize({ width: 1440, height: 1000 })
    await surface.locator('.search input').fill('absent')
    await expect.poll(() => value('Прибыль').innerText()).toBe('нет данных')
    expect(evidence.errors).toEqual([])
    expect(evidence.unexpected).toEqual([])
  } finally { await browser.close() }
}, 45_000)

it.each(['abc', 'pnl'] as const)('searches all loaded %s rows before pagination and preserves filters', async tab => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1512, height: 982 } })
    const evidence = await mount(page, tab)
    const surface = page.locator(`#tab-${tab}`), rows = surface.locator('[data-report-row]:visible')
    const search = surface.locator('.search input'), more = surface.getByRole('button', { name: /^Показать ещё/ })
    expect(await rows.count()).toBe(50)
    await search.fill('SKU-60')
    await expect.poll(() => rows.count()).toBe(1)
    expect(await rows.innerText()).toContain('SKU-60')
    expect(await more.count()).toBe(0)
    await search.fill('SKU-')
    await expect.poll(() => rows.count()).toBe(50)
    await more.click()
    await expect.poll(() => rows.count()).toBe(60)
    await search.fill('SKU-60')
    await expect.poll(() => rows.count()).toBe(1)
    expect(await rows.innerText()).toContain('SKU-60')
    await search.fill('absent')
    await expect.poll(() => rows.count()).toBe(0)
    await search.fill('')
    await expect.poll(() => rows.count()).toBe(50)
    await more.click()
    await expect.poll(() => rows.count()).toBe(60)
    if (tab === 'pnl') {
      await surface.locator('select.adv-select').selectOption('mine')
      await expect.poll(() => rows.count()).toBe(0)
      await search.fill('SKU-60')
      expect(await rows.count()).toBe(0)
      await surface.locator('select.adv-select').selectOption('unassigned')
      await expect.poll(() => rows.count()).toBe(1)
    } else {
      await surface.locator('.chips .chip').filter({ hasText: /^Новинки$/ }).click()
      await expect.poll(() => rows.count()).toBe(0)
      await search.fill('SKU-60')
      expect(await rows.count()).toBe(0)
      await surface.locator('.chips .chip').filter({ hasText: /^Все товары$/ }).click()
      await expect.poll(() => rows.count()).toBe(1)
    }
    if (process.env.SATORNA_REPORT_UI_SCREENSHOTS) {
      await surface.locator('.report-table-wrap').evaluate(element => { element.scrollLeft = 0 })
      await page.screenshot({ path: `/tmp/report-ui-${tab}-search.png` })
    }
    expect(evidence.errors).toEqual([])
    expect(evidence.unexpected).toEqual([])
  } finally { await browser.close() }
}, 45_000)

it('preserves expanded ABC rows across unchanged renderer replays and in-place cell updates', async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1512, height: 982 } })
    const evidence = await mount(page, 'abc')
    await page.clock.runFor(1000) // Run the existing 250/750 ms manager retries before the controlled replay.
    const rows = page.locator('#tab-abc [data-report-row]:visible')
    await page.locator('#tab-abc').getByRole('button', { name: /^Показать ещё/ }).click()
    await expect.poll(() => rows.count()).toBe(60)
    await page.evaluate(() => window.eval('renderAbcDemoRows()'))
    await page.clock.runFor(1000)
    await expect.poll(() => rows.count()).toBe(60)
    // Publications must still rerender cells even when source rows are mutated in place.
    await page.evaluate(() => {
      window.__vellaAbcLiveRows![0].price = '12345'
      window.__vellaPublishAbcRowsSnapshot?.()
    })
    await expect.poll(() => rows.first().innerText()).toContain('12345')
    expect(await rows.count()).toBe(60)
    expect(await page.evaluate(() => window.__vellaAbcRowsSnapshot?.().rows.length)).toBe(60)
    if (process.env.SATORNA_REPORT_UI_SCREENSHOTS) {
      await page.locator('#tab-abc .report-table-wrap').evaluate(element => { element.scrollLeft = 0 })
      await page.locator('#tab-abc').evaluate(element => element.scrollIntoView({ block: 'start' }))
      await page.screenshot({ path: '/tmp/abc-pagination-desktop.png' })
      await page.setViewportSize({ width: 390, height: 844 })
      await page.screenshot({ path: '/tmp/abc-pagination-mobile.png' })
      expect(await rows.count()).toBe(60)
    }
    expect(evidence.queries).toHaveLength(2)
    expect(evidence.errors).toEqual([])
    expect(evidence.unexpected).toEqual([])
  } finally { await browser.close() }
}, 45_000)

it('resets expanded ABC rows for same-size filters, sorting and source changes', async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1512, height: 982 } })
    const evidence = await mount(page, 'abc')
    await page.clock.runFor(1000)
    await page.evaluate(() => { window.__vellaAbcLiveRows!.forEach(row => { row.filters = ['Все товары', 'Новинки'] }) })
    const surface = page.locator('#tab-abc'), rows = surface.locator('[data-report-row]:visible')
    const changes = [
      () => surface.locator('.search input').fill('SKU-'),
      () => surface.locator('.chips .chip').filter({ hasText: /^Новинки$/ }).click(),
      () => page.evaluate(() => { window.eval("reportFilterState.abc.manager = 'unassigned'"); window.__vellaPublishAbcRowsSnapshot?.() }),
      // Equal prices keep exactly the same ordered rows; changing the sort still resets the view.
      () => page.evaluate(() => { window.eval("setReportSortStack('abc', [{ key: 'price', dir: 'asc' }])"); window.__vellaPublishAbcRowsSnapshot?.() }),
      () => page.evaluate(() => { window.__vellaAbcLiveRows!.reverse(); window.__vellaPublishAbcRowsSnapshot?.() }),
      () => page.evaluate(() => { window.__vellaAbcLiveRows = [...window.__vellaAbcLiveRows!]; window.__vellaPublishAbcRowsSnapshot?.() }),
      () => page.evaluate(() => { window.__vellaAbcLiveRows![0] = { ...window.__vellaAbcLiveRows![0] }; window.__vellaPublishAbcRowsSnapshot?.() }),
    ]
    for (const change of changes) {
      await surface.getByRole('button', { name: /^Показать ещё/ }).click()
      await expect.poll(() => rows.count()).toBe(60)
      await change()
      await expect.poll(() => rows.count()).toBe(50)
      expect(await page.evaluate(() => window.__vellaAbcRowsSnapshot?.().rows.length)).toBe(60)
    }
    expect(evidence.errors).toEqual([])
    expect(evidence.unexpected).toEqual([])
  } finally { await browser.close() }
}, 45_000)

it('resets expanded ABC rows on real period, token and organization/account changes', async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1512, height: 982 } })
    const evidence = await mount(page, 'abc')
    await page.clock.runFor(1000)
    const surface = page.locator('#tab-abc'), rows = surface.locator('[data-report-row]:visible')
    const changes = [
      async () => {
        await page.getByRole('button', { name: 'Начало периода аналитики WB', exact: true }).click()
        await page.locator('.products-cache-calendar-day[aria-label^="2026-09-02:"]').click()
        await page.locator('.products-cache-calendar-day[aria-label^="2026-09-06:"]').click()
        await page.getByRole('button', { name: 'Применить', exact: true }).click()
      },
      () => page.getByRole('button', { name: 'Replace synthetic token', exact: true }).click(),
      () => page.getByRole('button', { name: 'Change synthetic account', exact: true }).click(),
    ]
    for (const change of changes) {
      await surface.getByRole('button', { name: /^Показать ещё/ }).click()
      await expect.poll(() => rows.count()).toBe(60)
      const previousQueries = evidence.queries.length
      await change()
      await expect.poll(() => evidence.queries.length).toBeGreaterThan(previousQueries)
      await expect.poll(() => rows.count()).toBe(50)
      expect(await page.evaluate(() => window.__vellaAbcRowsSnapshot?.().rows.length)).toBe(60)
    }
    expect(evidence.queries.some(query => query.includes('dateFrom=2026-09-02&dateTo=2026-09-06'))).toBe(true)
    expect(evidence.queries.some(query => query.includes('marketplaceAccountId=32'))).toBe(true)
    await page.getByRole('button', { name: 'Clear synthetic report session', exact: true }).click()
    await expect.poll(() => rows.count()).toBe(0)
    expect(evidence.errors).toEqual([])
    expect(evidence.unexpected).toEqual([])
  } finally { await browser.close() }
}, 45_000)

it.each(['abc', 'pnl'] as const)('allows past canonical %s dates without legacy coverage and rejects future dates', async tab => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1512, height: 982 } })
    const evidence = await mount(page, tab)
    await page.getByRole('button', { name: 'Начало периода аналитики WB', exact: true }).click()
    const past = page.locator('.products-cache-calendar-day[aria-label^="2026-09-02:"]')
    await expect.poll(() => past.getAttribute('aria-disabled')).toBe('false')
    expect(await page.locator('.products-cache-calendar-day[aria-label^="2026-09-12:"]').getAttribute('aria-disabled')).toBe('true')
    if (process.env.SATORNA_REPORT_UI_SCREENSHOTS) await page.screenshot({ path: `/tmp/report-ui-${tab}-calendar.png` })
    await past.click()
    await page.locator('.products-cache-calendar-day[aria-label^="2026-09-06:"]').click()
    await page.getByRole('button', { name: 'Применить', exact: true }).click()
    await expect.poll(() => evidence.queries.some(query => query.includes('dateFrom=2026-09-02') && query.includes('dateTo=2026-09-06'))).toBe(true)
    await expect.poll(() => page.locator(`#tab-${tab} [data-report-row]:visible`).count()).toBe(50)
    expect(evidence.errors).toEqual([])
    expect(evidence.unexpected).toEqual([])
  } finally { await browser.close() }
}, 45_000)

it('starts a reopened WB data calendar from the beginning of a new range', async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1512, height: 982 } })
    const evidence = await mount(page, 'abc')
    await page.getByRole('button', { name: 'Начало периода аналитики WB', exact: true }).click()
    await page.locator('.products-cache-calendar-day[aria-label^="2026-09-02:"]').click()
    await page.locator('.products-cache-calendar-day[aria-label^="2026-09-06:"]').click()
    await page.getByRole('button', { name: 'Применить', exact: true }).click()
    await expect.poll(() => evidence.queries.some(query => query.includes('dateFrom=2026-09-02') && query.includes('dateTo=2026-09-06'))).toBe(true)

    await page.getByRole('button', { name: 'Календарь данных', exact: true }).click()
    const fromTab = page.getByRole('button', { name: 'С 2026-09-02', exact: true })
    expect(await fromTab.getAttribute('class')).toContain('active')
    await page.locator('.products-cache-calendar-day[aria-label^="2026-09-07:"]').click()
    await page.locator('.products-cache-calendar-day[aria-label^="2026-09-09:"]').click()
    await page.getByRole('button', { name: 'Применить', exact: true }).click()
    await expect.poll(() => evidence.queries.some(query => query.includes('dateFrom=2026-09-07') && query.includes('dateTo=2026-09-09'))).toBe(true)
    await expect.poll(() => page.locator('#tab-abc [data-report-row]:visible').count()).toBe(50)
    expect(evidence.errors).toEqual([])
    expect(evidence.unexpected).toEqual([])
  } finally { await browser.close() }
}, 45_000)

it('labels blocked and partial legacy P&L rows without claiming readiness', async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block' })
    const evidence = await mount(page, 'pnl', true)
    for (const status of ['blocked', 'partial', 'ready']) {
      const row = page.locator('#tab-pnl [data-report-row]').filter({ hasText: `STATUS-${status}` })
      if (status === 'ready') expect(await row.innerText()).toContain('готово')
      else expect(await row.innerText()).not.toContain('готово')
    }
    const search = page.locator('#tab-pnl .search input'), totals = page.locator('#tab-pnl .pnl-flow-item b')
    await search.fill('STATUS-ready')
    await expect.poll(() => totals.allTextContents()).toEqual(['30 ₽', '10 ₽', '0 ₽', '0 ₽', '0 ₽', '20 ₽ · 66,7%'])
    await search.fill('STATUS-partial')
    await expect.poll(() => totals.allTextContents()).toEqual(['20 ₽', 'нет данных', '0 ₽', '0 ₽', '0 ₽', 'нет данных · нет данных'])
    await search.fill('absent')
    await expect.poll(() => totals.allTextContents()).toEqual(['0 ₽', '0 ₽', '0 ₽', '0 ₽', '0 ₽', '0 ₽ · нет данных'])
    await search.fill('')
    await expect.poll(() => totals.allTextContents()).toEqual(['60 ₽', 'нет данных', '0 ₽', '0 ₽', '0 ₽', 'нет данных · нет данных'])
    await page.getByRole('button', { name: 'Начало периода аналитики WB', exact: true }).click()
    // The rollout-disabled legacy report still uses its existing coverage gate.
    expect(await page.locator('.products-cache-calendar-day[aria-label^="2026-09-02:"]').getAttribute('aria-disabled')).toBe('true')
    expect(evidence.errors).toEqual([])
    expect(evidence.unexpected).toEqual([])
  } finally { await browser.close() }
}, 45_000)
