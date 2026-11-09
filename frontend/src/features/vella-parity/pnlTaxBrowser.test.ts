import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { expect, it } from 'vitest'

// Minimal populated subset of private PnlBackendReport/PnlBackendRow DTOs.
// Their remaining fields are optional; no tax amount is computed by this fixture.
type PnlTaxFixture = {
  meta: { title: string; generatedAt: string; freshnessState: string; sourceType: string; dateRange: { from: string; to: string } }
  rows: Array<{ articleId: string; productName: string; category: string; nmId: null; photoUrl: null; taxKopecks: number | null }>
  kpis: []; cashFlow: null; reportJob: null
}

it('renders backend P&L tax as unknown, zero or supplied amount under the Tax column', async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({
    configFile: false, envFile: false, root, logLevel: 'silent', plugins: [{
      name: 'synthetic-cabinet', enforce: 'pre', transform(source, id) {
        if (!id.endsWith('/__fixtures__/reportLoadingBrowser.tsx')) return
        return source.replace('cabinetMe: null', `cabinetMe: ({ organization: { organizationId: 7 }, user: { userId: 1, permissions: [] } } as AuthContextValue['cabinetMe'])`)
      },
    }, react()],
    define: { 'process.env.NODE_ENV': JSON.stringify('test'), 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('') },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: {
      entry: fileURLToPath(new URL('./__fixtures__/reportLoadingBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'PnlTaxTest',
    } },
  })
  const outputs = Array.isArray(result) ? result : [result]
  const chunk = outputs.flatMap(output => 'output' in output ? output.output : []).find(output => output.type === 'chunk' && output.isEntry)
  if (!chunk || chunk.type !== 'chunk') throw new Error('Missing P&L tax browser fixture bundle')
  const browser = await chromium.launch({ headless: true })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1512, height: 982 } })
    await page.clock.setFixedTime(new Date('2026-09-09T12:00:00Z'))
    const queries: string[] = [], unexpected: string[] = [], errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', route => {
      const request = route.request(), url = new URL(request.url())
      if (request.method() === 'GET' && url.origin === 'http://satorna.test' && url.pathname === '/wb/reports/pnl') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (request.method() === 'GET' && request.resourceType() === 'image') return route.fulfill({ contentType: 'image/gif', body: Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64') })
      if (request.method() === 'GET' && url.origin === 'https://fonts.googleapis.com' && url.pathname === '/css2') return route.fulfill({ contentType: 'text/css', body: '' })
      if (url.pathname === '/api/v1/cabinet/team/users') return route.fulfill({ json: { data: [] } })
      if (url.pathname === '/api/v1/cabinet/wb-token') return route.fulfill({ json: { data: { hasToken: false } } })
      if (url.pathname === '/api/v1/cabinet/avito-credentials') return route.fulfill({ json: { data: { hasCredentials: false } } })
      if (request.method() !== 'GET' || url.origin !== 'http://satorna.test' || url.pathname !== '/api/wb/reports/pnl/latest-cache') {
        unexpected.push(`${request.method()} ${url.pathname}`); return route.abort()
      }
      queries.push(url.search)
      const payload: PnlTaxFixture = {
        meta: { title: 'Synthetic P&L', generatedAt: '2026-09-09T12:00:00Z', freshnessState: 'fresh', sourceType: 'financial',
          dateRange: { from: url.searchParams.get('from') ?? '', to: url.searchParams.get('to') ?? '' } },
        rows: [
          { articleId: 'SYNTHETIC-TAX-UNKNOWN', productName: 'Synthetic unknown tax', category: 'Synthetic', nmId: null, photoUrl: null, taxKopecks: null },
          { articleId: 'SYNTHETIC-TAX-ZERO', productName: 'Synthetic zero tax', category: 'Synthetic', nmId: null, photoUrl: null, taxKopecks: 0 },
          { articleId: 'SYNTHETIC-TAX-AMOUNT', productName: 'Synthetic supplied tax', category: 'Synthetic', nmId: null, photoUrl: null, taxKopecks: 123400 },
        ], kpis: [], cashFlow: null, reportJob: null,
      }
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify(payload) })
    })
    await page.goto('http://satorna.test/wb/reports/pnl')
    await page.evaluate(() => {
      window.__vellaReportPeriods = { pnl: { days: 7, mode: 'custom', fromIso: '2026-08-01', toIso: '2026-08-07', label: 'Synthetic P&L period' } }
    })
    await page.addScriptTag({ content: chunk.code })
    const table = page.locator('#tab-pnl [data-vella-runtime-binding="backend-pnl"] table')
    await table.getByText('Артикул: SYNTHETIC-TAX-UNKNOWN', { exact: true }).waitFor({ timeout: 15_000 })
    expect(await table.isVisible()).toBe(true)
    expect(await table.locator('tr[data-report-row="pnl"]').count()).toBe(3)
    const taxHeader = table.getByRole('columnheader', { name: /^Налог\s/ })
    expect(await taxHeader.count()).toBe(1)
    expect(await taxHeader.evaluate(header => header.firstChild?.textContent?.trim())).toBe('Налог')
    const taxColumn = await taxHeader.evaluate((header: HTMLTableCellElement) => header.cellIndex)
    for (const [article, expected] of [
      ['SYNTHETIC-TAX-UNKNOWN', 'нет данных'],
      ['SYNTHETIC-TAX-ZERO', '0 ₽'],
      ['SYNTHETIC-TAX-AMOUNT', '1\u00a0234 ₽'],
    ]) {
      const row = table.locator('tr[data-report-row="pnl"]').filter({ has: page.getByText(`Артикул: ${article}`, { exact: true }) })
      expect(await row.count()).toBe(1)
      expect(await row.locator('td').nth(taxColumn).innerText()).toBe(expected)
    }
    expect(queries).toHaveLength(1)
    const query = new URLSearchParams(queries[0])
    expect([query.get('from'), query.get('to'), query.get('groupBy'), query.get('source'), query.get('preset')])
      .toEqual(['2026-08-01', '2026-08-07', 'sku', 'financial', 'custom'])
    expect(unexpected).toEqual([])
    expect(errors).toEqual([])
  } finally { await browser.close() }
}, 60_000)
