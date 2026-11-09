import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { expect, it } from 'vitest'

it.each(['empty', 'failure', 'rate-limit'])('keeps actual Avito overview period, loading and %s data aligned', async outcome => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({
    configFile: false, envFile: false, root, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': JSON.stringify('test'), 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('') },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: {
      entry: fileURLToPath(new URL('./__fixtures__/reportLoadingBrowser.tsx', import.meta.url)),
      formats: ['iife'], name: 'AvitoOverviewTest',
    } },
  })
  const outputs = Array.isArray(result) ? result : [result]
  const bundle = outputs.flatMap(output => 'output' in output ? output.output : [])
    .find(output => output.type === 'chunk' && output.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing synthetic overview bundle')
  const browser = await chromium.launch({ headless: true })
  let release = () => {}
  const gate = new Promise<void>(resolve => { release = resolve })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block', timezoneId: 'Europe/Moscow', viewport: { width: 1440, height: 1000 } })
    await page.clock.setFixedTime('2026-09-09T12:00:00Z')
    const queries: string[] = [], chatQueries: string[] = [], unexpected: string[] = [], errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      if (request.method() === 'GET' && url.origin === 'http://satorna.test' && url.pathname === '/avito/overview') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (request.method() === 'GET' && request.resourceType() === 'image') return route.fulfill({ contentType: 'image/gif', body: Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64') })
      if (request.method() === 'GET' && url.origin === 'https://fonts.googleapis.com' && url.pathname === '/css2') return route.fulfill({ contentType: 'text/css', body: '' })
      if (request.method() === 'GET' && url.origin === 'http://satorna.test' && url.pathname === '/api/v1/avito/chats') {
        chatQueries.push(url.search)
        return route.fulfill({ contentType: 'application/json', body: JSON.stringify({
          status: 'synced', summary: { total: 0, unread: 0, withItems: 0, messages: 0 },
          account: { accountId: null, accountName: null }, chats: [], messages: {}, source: {},
        }) })
      }
      if (request.method() !== 'GET' || url.origin !== 'http://satorna.test' || url.pathname !== '/api/v1/avito/overview') {
        unexpected.push(`${request.method()} ${url.pathname}`); return route.abort()
      }
      queries.push(url.search)
      const first = queries.length === 1
      if (!first && outcome !== 'rate-limit') await gate
      if (!first && outcome === 'failure') return route.fulfill({ status: 503, contentType: 'application/json', body: '{"error":{"code":"SYNTHETIC_UNAVAILABLE","message":"Источник временно недоступен"}}' })
      const dateFrom = url.searchParams.get('dateFrom')!, dateTo = url.searchParams.get('dateTo')!
      const rateLimited = outcome === 'rate-limit' && first
      const hasItem = outcome === 'rate-limit' ? !first : first
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({
        status: rateLimited ? 'partial' : 'synced', period: { dateFrom, dateTo, days: (Date.parse(dateTo) - Date.parse(dateFrom)) / 86400000 + 1 },
        summary: { impressions: 1234, views: hasItem ? 321 : 0, contacts: 12, favorites: 2,
          spendKopecks: 12300, orders: 0, buyouts: 0, conversionPct: null,
          orderConversionPct: null, buyoutPct: null, totalListings: rateLimited ? null : hasItem ? 1 : 0,
          activeListings: rateLimited ? null : hasItem ? 1 : 0, inactiveListings: rateLimited ? null : 0, removedListings: 0,
          oldListings: 0, blockedListings: 0, chats: 0, unreadChats: 0, reviews: 0,
          unansweredReviews: 0, answeredReviews: 0, lowReviews: 0, ratingScore: null, blockers: rateLimited ? 1 : 0 },
        accounts: [], events: [], source: rateLimited ? { errors: { stats: { code: 'rate_limited', retryAfterUntil: '2026-09-09T12:00:00.200Z' } } } : {}, topItems: hasItem ? [{
          itemId: 'synthetic-item', title: 'SYNTHETIC-OVERVIEW-ITEM', accountId: 'synthetic-account',
          accountName: 'Synthetic account', category: null, url: null, views: 321,
          contacts: 12, favorites: 2, spendKopecks: 12300, orders: 0, buyouts: 0,
          conversionPct: null, sourceStatus: 'fresh',
        }] : [],
      }) })
    })
    await page.goto('http://satorna.test/avito/overview')
    await page.addScriptTag({ content: bundle.code })
    const surface = page.locator('#tab-avito-overview')
    if (outcome === 'rate-limit') {
      await surface.getByText('Топ объявлений обновится позже', { exact: true }).waitFor({ state: 'visible' })
      expect(await surface.locator('.stat-val').first().innerText()).toBe('—')
      await expect.poll(() => queries.length, { timeout: 15_000 }).toBe(2)
      expect(new URLSearchParams(queries[1]).get('forceRefresh')).toBe('true')
      await surface.getByText('SYNTHETIC-OVERVIEW-ITEM', { exact: true }).waitFor({ state: 'visible' })
      expect(unexpected, JSON.stringify(unexpected)).toEqual([])
      expect(errors).toEqual([])
      return
    }
    await surface.getByText('SYNTHETIC-OVERVIEW-ITEM', { exact: true }).waitFor({ state: 'visible', timeout: 15_000 })
    expect(await surface.locator('[data-vella-island="avito-overview-kpi-strip"]').evaluate(element =>
      getComputedStyle(element).gridTemplateColumns.split(/\s+/).length)).toBe(4)
    expect(queries).toHaveLength(1)
    await surface.getByText('SYNTHETIC-OVERVIEW-ITEM', { exact: true }).click()
    const modal = surface.locator('.avito-overview-item-modal')
    await modal.waitFor({ state: 'visible' })
    expect(await modal.locator('#avitoOverviewItemViews').innerText()).toBe('321')
    expect(await modal.getByRole('link', { name: 'Открыть объявления' }).getAttribute('href')).toBe('/avito/listings')
    await modal.getByRole('button', { name: 'Закрыть', exact: true }).click()
    await modal.waitFor({ state: 'detached' })
    await surface.getByRole('tab', { name: '7 дней', exact: true }).click()
    await expect.poll(() => queries.length, { timeout: 15_000 }).toBe(2)
    expect(await surface.getByRole('tab', { name: '7 дней', exact: true }).getAttribute('aria-selected')).toBe('true')
    await surface.getByText('Загружаем обзор Авито', { exact: true }).waitFor({ state: 'visible' })
    expect(await surface.getByText('SYNTHETIC-OVERVIEW-ITEM', { exact: true }).count()).toBe(0)
    expect(await surface.locator('.stat-val').allTextContents()).toEqual(Array(7).fill('…'))
    const period = new URLSearchParams(queries[1])
    expect([period.get('dateFrom'), period.get('dateTo')]).toEqual(['2026-09-03', '2026-09-09'])
    expect((Date.parse(period.get('dateTo')!) - Date.parse(period.get('dateFrom')!)) / 86400000).toBe(6)
    expect(period.get('forceRefresh')).toBeNull()
    release()
    await surface.getByText(outcome === 'empty' ? 'Нет объявлений за период' : 'Обзор временно недоступен', { exact: true }).waitFor({ state: 'visible' })
    expect(await surface.getByText('SYNTHETIC-OVERVIEW-ITEM', { exact: true }).count()).toBe(0)
    await page.getByRole('button', { name: 'Clear synthetic report session', exact: true }).click()
    await surface.getByText('Обзор временно недоступен', { exact: true }).waitFor({ state: 'visible' })
    // In the failure case this heading already existed before logout. Wait for
    // the actual seven-value session transition, not that pre-existing heading.
    await expect.poll(() => surface.locator('.stat-val').allTextContents()).toEqual(Array(7).fill('—'))
    expect(queries).toHaveLength(2)
    expect(chatQueries).toHaveLength(2)
    expect(chatQueries.every(query => query === '?limit=50&offset=0')).toBe(true)
    expect(unexpected, JSON.stringify(unexpected)).toEqual([])
    expect(errors).toEqual([])
  } finally { release(); await browser.close() }
}, 60_000)
