import { expect, it } from 'vitest'
import { chromium } from 'playwright'
import { createServer } from 'vite'
import react from '@vitejs/plugin-react'
import { withSyntheticVite } from '../../test-support/syntheticVite'
import { fileURLToPath } from 'node:url'
import { mkdir } from 'node:fs/promises'

const root = fileURLToPath(new URL('../../../', import.meta.url))
const fixture = `
import React from 'react'; import {createRoot} from 'react-dom/client'; import {MemoryRouter} from 'react-router-dom';
import {AuthContext} from '/src/features/auth/authContext.ts';
import {AvitoStatsIsland} from '/src/features/vella-parity/VellaHtmlParityPage.tsx';
const h=React.createElement;
function Fixture(){const [session,setSession]=React.useState(1);
const auth={accessToken:'synthetic-only',cabinetMe:{activeSession:{sessionId:'synthetic-'+session},organization:{organizationId:1},user:{userId:1,permissions:['cabinet:read']}}};
return h(MemoryRouter,{initialEntries:['/avito/stats']},h(AuthContext.Provider,{value:auth},h('h1',null,'Synthetic Avito statistics'),h('button',{onClick:()=>setSession(v=>v+1)},'Change synthetic session'),h(AvitoStatsIsland,{replacementKey:'synthetic'})));}
createRoot(document.getElementById('root')).render(h(Fixture));`
const metrics = { impressions: '0', views: '900719925474099312345', contactsMessenger: null, contacts: '1', contactsShowPhone: null,
  contactsShowPhoneAndMessenger: '0', favorites: '3', spendKopecks: '900719925474099399999', orders: null, buyouts: '0' }

it('synthetic Avito statistics: default-off legacy, explicit reads, exact values and changed-scope cancellation', async () => {
  const screenshots = '/tmp/satorna-avito-statistics-ui-proof'
  await mkdir(screenshots, { recursive: true })
  const browser = await chromium.launch({ headless: true })
  try {
    for (const enabled of [false, true]) {
      const server = await createServer({ root, configFile: false, envFile: false, resolve: { alias: { '@': `${root}src` } }, cacheDir: `${screenshots}/vite-cache`,
        define: { 'import.meta.env.VITE_CANONICAL_AVITO_STATS_ENABLED': JSON.stringify(String(enabled)), 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('') },
        server: { host: '127.0.0.1', port: 0, strictPort: true },
        plugins: [react(), { name: 'synthetic-avito-statistics', resolveId(id) { if (id === '/__synthetic-avito.jsx') return '\0synthetic-avito.jsx' },
          load(id) { if (id === '\0synthetic-avito.jsx') return fixture },
          configureServer(vite) { vite.middlewares.use(async (req, res, next) => {
            if (req.url !== '/__synthetic-avito') return next()
            res.setHeader('Content-Type', 'text/html'); res.end(await vite.transformIndexHtml('/__synthetic-avito', '<html><head><title>Synthetic Avito statistics</title></head><body><div id="root"></div><script type="module" src="/__synthetic-avito.jsx"></script></body></html>'))
          }) },
        }],
      })
      await withSyntheticVite(server, `avito-statistics-${enabled}`, async origin => {
      const page = await browser.newPage({ viewport: { width: 1366, height: 900 }, serviceWorkers: 'block' })
      page.setDefaultTimeout(15_000)
      const errors: string[] = [], unexpected: string[] = [], stats: string[] = []
      let legacy = 0, discovery = 0, held: (() => Promise<void>) | undefined
      page.on('pageerror', error => errors.push(error.message))
      page.on('console', message => { if (message.type() === 'error') errors.push(message.text()) })
      try {
        await page.route('**/*', async route => {
          const request = route.request(), url = new URL(request.url())
          const json = (value: unknown) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(value) })
          if (url.origin !== origin) { unexpected.push(url.origin + url.pathname); return route.abort() }
          if (!url.pathname.startsWith('/api/')) return route.continue()
          if (request.method() !== 'GET') { unexpected.push(request.method() + url.pathname); return route.abort() }
          if (url.pathname === '/api/v2/cabinet/marketplace-accounts') {
            discovery++
            return json({ data: [11, 12, 13].map(id => ({ marketplaceAccountId: id, provider: id === 13 ? 'wb' : 'avito', externalAccountId: String(1000 + id), displayName: `Synthetic ${id}`, status: 'connected' })) })
          }
          if (url.pathname === '/api/v1/avito/stats') {
            legacy++
            return json({ status: 'synced', period: { dateFrom: url.searchParams.get('dateFrom'), dateTo: url.searchParams.get('dateTo'), days: 7 },
              summary: { ...Object.fromEntries(Object.keys(metrics).map(key => [key, null])), conversionPct: null, orderConversionPct: null, buyoutPct: null, problemRows: 0 },
              timeline: [], accounts: [], rows: [], source: {} })
          }
          const match = /^\/api\/v2\/avito\/accounts\/(11|12)\/statistics$/.exec(url.pathname)
          if (match) {
            stats.push(url.pathname + url.search)
            const accountId = Number(match[1]), external = String(accountId + 1000)
            const result = { data: { marketplaceAccountId: accountId, provider: 'avito', externalAccountId: external,
              dateFrom: url.searchParams.get('dateFrom'), dateTo: url.searchParams.get('dateTo'), status: 'partial',
              rows: [{ itemId: `account:${external}:totals`, sourceStatus: 'partial', metrics: { ...metrics, views: accountId === 12 ? '12' : metrics.views } }],
              daily: [{ date: url.searchParams.get('dateFrom'), metrics }] } }
            if (stats.length === 2) { held = () => json(result).catch(() => {}); return }
            return json(result)
          }
          unexpected.push(request.method() + url.pathname); return route.abort()
        })
        await page.goto(`${origin}/__synthetic-avito`)
        await page.getByRole('heading', { name: 'Synthetic Avito statistics' }).waitFor()
        expect(await page.title()).toBe('Synthetic Avito statistics'); expect(page.url()).toBe(`${origin}/__synthetic-avito`)
        if (!enabled) {
          await page.getByText('Дневная статистика не получена. Обновите данные, чтобы построить график.', { exact: true }).waitFor()
          expect(legacy).toBe(1); expect(discovery).toBe(0); expect(stats).toEqual([])
          expect(await page.locator('[data-canonical-avito-statistics]').count()).toBe(0)
        } else {
          const select = page.getByLabel('Аккаунт статистики Авито', { exact: true })
          await select.selectOption('11')
          expect(await select.locator('option[value="13"]').count()).toBe(0)
          const from = page.getByLabel('Дата начала статистики Авито', { exact: true }), to = page.getByLabel('Дата окончания статистики Авито', { exact: true })
          expect(await from.inputValue()).toBe(''); expect(await to.inputValue()).toBe('')
          await from.fill('2026-09-01'); await to.fill('2026-09-10'); await to.fill('2026-09-09')
          expect(stats).toEqual([]); expect(legacy).toBe(0)
          const load = page.getByRole('button', { name: 'Загрузить статистику', exact: true })
          await load.click()
          const table = page.getByRole('table', { name: 'Статистика выбранного аккаунта Авито', exact: true })
          await table.getByText(metrics.views, { exact: true }).waitFor()
          expect(stats).toEqual(['/api/v2/avito/accounts/11/statistics?dateFrom=2026-09-01&dateTo=2026-09-09'])
          expect(await table.getByRole('row', { name: 'Заказы Нет данных', exact: true }).count()).toBe(1)
          expect(await table.getByRole('row', { name: 'Показы 0', exact: true }).count()).toBe(1)
          expect(await table.getByText(metrics.spendKopecks, { exact: true }).count()).toBe(1)
          await page.getByText('Дневные наблюдения: 1', { exact: true }).click()
          await page.getByRole('table', { name: 'Дневная статистика Авито', exact: true }).waitFor()
          await page.screenshot({ path: `${screenshots}/statistics.png`, fullPage: false })
          await to.fill('2026-09-10')
          expect(await table.count()).toBe(0); expect(stats).toHaveLength(1)
          await load.click(); await expect.poll(() => stats.length).toBe(2)
          await select.selectOption('12')
          expect(await from.inputValue()).toBe(''); expect(await table.count()).toBe(0)
          await from.fill('2026-09-02'); await to.fill('2026-09-10')
          expect(stats).toHaveLength(2)
          await load.click(); await table.getByRole('row', { name: 'Просмотры 12', exact: true }).waitFor()
          await held?.()
          expect(await table.getByText(metrics.views, { exact: true }).count()).toBe(0)
          expect(await page.getByRole('status').filter({ hasText: 'Аккаунт Авито: 1012.' }).count()).toBe(1)
          await page.setViewportSize({ width: 390, height: 844 }); await page.screenshot({ path: `${screenshots}/statistics-mobile.png`, fullPage: false })
          await page.getByRole('button', { name: 'Change synthetic session' }).click()
          await page.getByText('Выберите аккаунт и период.', { exact: false }).waitFor()
          expect(await table.count()).toBe(0); expect(stats).toHaveLength(3); expect(legacy).toBe(0)
        }
        expect(await page.locator('vite-error-overlay').count()).toBe(0)
        expect(errors).toEqual([]); expect(unexpected).toEqual([])
      } finally { await page.close() }
      })
    }
  } finally { await browser.close() }
}, 60_000)

it('ephemeral fixture closes its listener if browser/page setup fails after binding', async () => {
  const server = await createServer({ root, configFile: false, envFile: false, server: { host: '127.0.0.1', hmr: false },
    cacheDir: '/tmp/satorna-avito-statistics-ui-proof/failed-setup-cache', optimizeDeps: { noDiscovery: true, include: [] } })
  await expect(withSyntheticVite(server, 'intentional-setup-failure', async () => { throw new Error('Synthetic setup failure') })).rejects.toThrow('Synthetic setup failure')
  expect(server.httpServer?.listening).toBe(false)
  expect(server.httpServer?.address()).toBeNull()
})
