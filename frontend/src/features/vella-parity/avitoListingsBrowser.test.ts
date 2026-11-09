import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { expect, it } from 'vitest'

// Complete required AvitoListingsBackendResponse/Row fields from the actual island contract.
function listingResponse(dateFrom: string, dateTo: string, populated: boolean) {
  return {
    status: 'synced' as const, period: { dateFrom, dateTo, days: 7 },
    summary: {
      total: populated ? 1 : 0, active: populated ? 1 : 0, inactive: 0, removed: 0, old: 0,
      blocked: 0, partial: 0, views: populated ? 137 : 0, contactsMessenger: populated ? 9 : 0,
      contacts: populated ? 11 : 0, contactsShowPhone: populated ? 2 : 0, contactsShowPhoneAndMessenger: 0,
      favorites: populated ? 3 : 0, spendKopecks: populated ? 123400 : 0, orders: populated ? 4 : 0, buyouts: populated ? 2 : 0,
    },
    accounts: [{ accountId: 'synthetic-account', accountName: 'Synthetic listing account', itemCount: populated ? 1 : 0,
      activeItemCount: populated ? 1 : 0, inactiveItemCount: 0 }],
    rows: populated ? [{
      itemId: 'synthetic-listing-001', title: 'Synthetic previous-period listing',
      accountId: 'synthetic-account', accountName: 'Synthetic listing account', category: 'Synthetic category',
      url: null, imageUrl: null, photoUrl: null, photos: [], images: [], status: 'active' as const,
      priceKopecks: 450000, impressions: 1000, views: 137, contactsMessenger: 9, contacts: 11,
      contactsShowPhone: 2, contactsShowPhoneAndMessenger: 0, favorites: 3, spendKopecks: 123400,
      orders: 4, buyouts: 2, sourceStatus: 'synced', updatedAt: '2026-09-09T12:00:00Z', contactConversionPct: 8.03,
    }] : [], source: {},
  }
}

it.each(['empty', 'error'])('hides previous Avito listing rows, open detail and KPI values while the applied period loads → %s', async outcome => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({
    configFile: false, envFile: false, root, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': JSON.stringify('test'), 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('') },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false,
      lib: { entry: fileURLToPath(new URL('./__fixtures__/reportLoadingBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'AvitoListingsTest' } },
  })
  const outputs = Array.isArray(result) ? result : [result]
  const bundle = outputs.flatMap(output => 'output' in output ? output.output : [])
    .find(output => output.type === 'chunk' && output.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing Avito Listings test bundle')
  const browser = await chromium.launch({ headless: true })
  let release = () => {}
  const gate = new Promise<void>(resolve => { release = resolve })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1512, height: 982 } })
    await page.clock.setFixedTime(new Date('2026-09-09T12:00:00Z'))
    const requests: string[] = [], unexpected: string[] = [], errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      if (request.method() === 'GET' && url.origin === 'http://satorna.test' && url.pathname === '/avito/listings') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (request.method() === 'GET' && request.resourceType() === 'image') return route.fulfill({ contentType: 'image/gif', body: Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64') })
      if (request.method() === 'GET' && url.origin === 'https://fonts.googleapis.com' && url.pathname === '/css2') return route.fulfill({ contentType: 'text/css', body: '' })
      if (request.method() !== 'GET' || url.origin !== 'http://satorna.test' || url.pathname !== '/api/v1/avito/listings') {
        unexpected.push(`${request.method()} ${url.pathname}`); return route.abort()
      }
      requests.push(url.search)
      const first = requests.length === 1
      if (!first) await gate
      if (!first && outcome === 'error') return route.fulfill({ status: 503, contentType: 'application/json', body: '{"error":{"code":"SYNTHETIC_LISTINGS_UNAVAILABLE","message":"Synthetic listings unavailable"}}' })
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify(listingResponse(url.searchParams.get('dateFrom')!, url.searchParams.get('dateTo')!, first)) })
    })
    await page.goto('http://satorna.test/avito/listings')
    await page.addScriptTag({ content: bundle.code })
    const surface = page.locator('#tab-avito-listings')
    const row = surface.locator('[data-vella-island="avito-listings-table-row"]')
    await row.getByText('Synthetic previous-period listing', { exact: true }).waitFor({ timeout: 15_000 })
    expect(requests).toHaveLength(1)
    const initialQuery = new URLSearchParams(requests[0])
    expect(initialQuery.get('forceRefresh')).toBeNull()
    expect(initialQuery.get('dateFrom')).toBe(await surface.getByLabel('Дата начала объявлений Авито').inputValue())
    expect(initialQuery.get('dateTo')).toBe(await surface.getByLabel('Дата окончания объявлений Авито').inputValue())
    expect(initialQuery.get('pageSize')).toBe('100')
    const detail = page.locator('#avitoListingDetailPanel')
    await row.click()
    await detail.locator('#avitoDetailTitle').waitFor()
    expect(await detail.locator('#avitoDetailTitle').innerText()).toBe('Synthetic previous-period listing')
    await detail.locator('button[data-tip="Закрыть карточку"]').click()
    await detail.waitFor({ state: 'hidden' })
    await row.click()
    await detail.waitFor()
    await page.keyboard.press('Escape')
    await detail.waitFor({ state: 'hidden' })
    await surface.getByLabel('Дата начала объявлений Авито').fill('2026-08-01')
    await surface.getByLabel('Дата окончания объявлений Авито').fill('2026-08-07')
    expect(requests).toHaveLength(1)
    await row.click()
    await detail.waitFor()
    // The open overlay blocks toolbar pointer access. Activate the real button/React handler
    // programmatically to exercise period replacement while a detail is already selected.
    await surface.getByRole('button', { name: 'Применить', exact: true }).evaluate((button: HTMLButtonElement) => button.click())
    await expect.poll(() => requests.length, { timeout: 15_000 }).toBe(2)
    const secondQuery = new URLSearchParams(requests[1])
    expect([secondQuery.get('dateFrom'), secondQuery.get('dateTo'), secondQuery.get('pageSize'), secondQuery.get('forceRefresh')])
      .toEqual(['2026-08-01', '2026-08-07', '100', null])
    const heldState = {
      rows: await row.count(), detailTitle: await detail.locator('#avitoDetailTitle').allTextContents(),
      loadingVisible: await surface.getByText('Загружаем объявления', { exact: true }).isVisible(),
      kpis: await surface.locator('.avito-listings-kpis .stat-val').allTextContents(),
      unexpected, errors,
    }
    expect(heldState).toEqual({ rows: 0, detailTitle: [], loadingVisible: true, kpis: ['—', '—', '—', '—', '—', '—', '—'], unexpected: [], errors: [] })
    release()
    await surface.getByText(outcome === 'empty' ? 'Нет результатов по выбранному фильтру' : 'Не удалось загрузить объявления', { exact: true }).waitFor()
    expect(await row.count()).toBe(0)
    await page.getByRole('button', { name: 'Clear synthetic report session', exact: true }).click()
    await surface.getByText('Не удалось загрузить объявления', { exact: true }).waitFor()
    expect(await surface.locator('.avito-listings-kpis .stat-val').allTextContents()).toEqual(['—', '—', '—', '—', '—', '—', '—'])
    expect(requests).toHaveLength(2)
    expect(unexpected).toEqual([])
    expect(errors).toEqual([])
  } finally { release(); await browser.close() }
}, 60_000)
