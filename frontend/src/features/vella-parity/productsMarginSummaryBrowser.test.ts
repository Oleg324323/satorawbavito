import path from 'node:path'
import { fileURLToPath } from 'node:url'
import react from '@vitejs/plugin-react'
import { chromium } from 'playwright'
import { build } from 'vite'
import { expect, it } from 'vitest'

it('renders whole-catalog margin and COGS when category costs are known and shared fees are included', async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({ root, configFile: false, envFile: false, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""', 'import.meta.env.VITE_WB_LIVE_ENABLED': '"false"' },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: { entry: fileURLToPath(new URL('./__fixtures__/productsCatalogCountBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'FactTaxTest' } },
  })
  const artifacts = (Array.isArray(result) ? result : [result]).flatMap(output => 'output' in output ? output.output : [])
  const bundle = artifacts.find(output => output.type === 'chunk' && output.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing factual tax fixture bundle')
  const styles = artifacts.flatMap(output => output.type === 'asset' && output.fileName.endsWith('.css') ? [String(output.source)] : []).join('\n')
  const scenarios = [
    { state: 'configured', summary: {
      settlementFormulaVersion: 'wb-final-payout-cogs-tax-v1', settlementProfitKopecks: null,
      settlementCogsKopecks: 86678000, settlementBlockers: ['settlement_account_expenses_unreconciled'],
      campaignAdvertisingVersion: 'wb-repricer-campaign-advertising-v1', campaignProfitKopecks: 84894739,
      revenueKopecks: 292025355,
    }, expected: '848947₽', ratio: '29.1%', net: 0 },

  ]
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL })
  try {
    for (const scenario of scenarios) {
      const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1440, height: 1000 } })
      const unexpected: string[] = [], errors: string[] = []
      const item = {
        meta: { articleId: 'FACT-TAX', nmId: 101, name: 'Synthetic tax contract', status: 'auto', currentPriceKopecks: 220000, basketsLast7d: 1, basketNorm: 1 },
        settings: { cogsKopecks: 50000, logisticsKopecks: 5000, minMarginPct: 15, wbCommissionPct: 15 },
        analytics: {
          abcCode: 'AA', abcPolicyVersion: 'wb-units-profit-cumulative-80-95-v1',
          settlementFormulaVersion: 'wb-final-payout-cogs-tax-v1', settlementProfitKopecks: null,
          settlementBlockers: ['settlement_account_expenses_unreconciled'],
          campaignAdvertisingVersion: 'wb-repricer-campaign-advertising-v1',
          repricerEconomicsVersion: 'wb-stock-campaign-no-deduction-v1',
          campaignProfitKopecks: 50000, campaignBlockers: [],
          financeState: 'ok', factTaxState: scenario.state, factTaxReason: scenario.state === 'missing' ? 'tax_policy_unconfirmed' : null,
          factNetProfitKopecks: scenario.state === 'configured' ? 0 : null, netProfitKopecks: scenario.state === 'configured' ? 0 : null,
          taxKopecks: scenario.state === 'configured' ? 0 : null, revenueKopecks: 1000000, plannedPeriodMarginKopecks: 120000,
          marginPct: 10, marginKopecks: 10000, commissionState: 'ok', commissionSource: 'tariffs.kgvpMarketplace', commissionDisplayPct: 15,
          baskets: 1, basketsState: 'ok', ordersUnits: 3, periodStatsState: 'ok', stockState: 'ok', wbStockUnits: 5,
        },
      }
      page.on('pageerror', error => errors.push(error.message))
      await page.route('**/*', route => {
        const request = route.request(), url = new URL(request.url())
        if (request.resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
        if (request.resourceType() === 'image') return route.fulfill({ contentType: 'image/gif', body: Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64') })
        if (url.origin === 'https://fonts.googleapis.com') return route.fulfill({ contentType: 'text/css', body: '' })
        if (url.origin === 'http://satorna.test' && request.method() === 'GET') {
          if (url.pathname === '/api/v1/wb-repricer/sku') return route.fulfill({ json: {
            items: [item], total: 1, itemsReturned: 1, page: 1, pageSize: 150, summary: scenario.summary,
            cache: { totalCached: 1, pagesCached: 1, financeFetchedAt: '2026-09-15T10:00:00Z',
              periodStatsFetchedAt: 'now', basketsFetchedAt: 'now', adsFetchedAt: 'now',
              adsDateFrom: url.searchParams.get('dateFrom'), adsDateTo: url.searchParams.get('dateTo'),
              financeFreshnessState: 'fresh',
              periodCoverage: { finance: { state: 'ok' }, orders: { state: 'ok' }, baskets: { state: 'ok' }, ads: { state: 'ok' } } },
          } })
          if (['/api/v1/wb-repricer/strategies/catalog', '/api/v1/wb-repricer/sku-groups', '/api/v1/wb-repricer/changelog'].includes(url.pathname)) return route.fulfill({ json: { items: [], total: 0 } })
          if (['/api/v1/wb-repricer/sync/status', '/api/v1/wb-repricer/worker/status'].includes(url.pathname)) return route.fulfill({ json: { state: 'completed', running: false, steps: [] } })
        }
        unexpected.push(`${request.method()} ${url.pathname}`)
        return route.abort()
      })
      await page.goto('http://satorna.test/wb/repricer')
      await page.addStyleTag({ content: styles })
      await page.addScriptTag({ content: bundle.code })
      const row = page.locator('#tbody tr[data-sku="FACT-TAX"]')
      await row.waitFor({ timeout: 15000 })
      const compact = (value: string) => value.replace(/\s+/g, '')
      expect.soft(compact(await page.locator('#kpiMarginRub').innerText()), JSON.stringify(scenario)).toBe(scenario.expected)
      expect.soft(compact(await page.locator('#kpiMargin').innerText()), JSON.stringify(scenario)).toBe(scenario.ratio)
      expect(compact(await page.locator('#kpiCogs').innerText())).toBe('866780₽')
      await row.getByText('AA', { exact: true }).waitFor()
      expect(unexpected).toEqual([])
      expect(errors).toEqual([])
      await page.close()
    }
  } finally { await browser.close() }
}, 60000)
