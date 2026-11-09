import { expect, it } from 'vitest'
import { chromium, type Page } from 'playwright'
import { createServer } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'
import { withSyntheticVite } from './test-support/syntheticVite'

const root = fileURLToPath(new URL('../', import.meta.url))
const parityPath = '/src/features/vella-parity/VellaHtmlParityPage.tsx'
const ordersPath = '/src/features/orders/OrdersPrintListPage.tsx'
const deferredRoutePaths = [
  '/src/components/vella-system/VellaSystemCatalog.tsx',
  '/src/components/vella/VellaFoundationWorkbench.tsx',
  '/src/components/vella-system/html/VellaHtmlRepricer.tsx',
  '/src/features/vella-react/VellaReactApp.tsx',
  '/src/features/vella-static/VellaStaticPage.tsx',
  ordersPath,
  '/src/features/wb-repricer/TemplatesPage.tsx',
  '/src/features/wb-repricer/WbRepricerPage.tsx',
  '/src/features/wb-repricer/WbRepricerChangelogPage.tsx',
  '/src/features/wb-repricer/WbRepricerSimulatorPage.tsx',
  '/src/features/wb-repricer/WbRepricerStatsPage.tsx',
  '/src/features/wb-reports/WbReportsPage.tsx',
  '/src/features/wb-sources/WbSourcesPage.tsx',
  '/src/wiki/pages/WbRepricerWiki.tsx',
  '/src/wiki/pages/LiquidationWiki.tsx',
  '/src/wiki/pages/PromotionsWiki.tsx',
  '/src/wiki/pages/AlgorithmWiki.tsx',
  '/src/wiki/pages/TemplatesWiki.tsx',
]
// Only the expensive page is synthetic. App, routing, auth gates and error boundary are real.
const parityFixture = `import React from 'react';
import {useLocation} from 'react-router-dom';
export function VellaHtmlParityPage(){const location=useLocation();
const [ready,setReady]=React.useState(false);
React.useEffect(()=>{import('/src/features/vella-parity/vellaProductionSnapshot.generated.ts').then(module=>setReady(module.vellaProductionHtml.length>0))},[]);
return ready?React.createElement('h1',null,'Synthetic parity '+location.pathname):null;}`

async function navigate(page: Page, path: string) {
  await page.evaluate(path => { history.pushState({}, '', path); dispatchEvent(new PopStateEvent('popstate')) }, path)
}

it('App defers unrelated route pages and recovers rejected lazy imports with reload', async () => {
  const browser = await chromium.launch({ headless: true })
  try {
    for (const authenticated of [false, true]) {
      const server = await createServer({ root, configFile: false, envFile: false,
        resolve: { alias: { '@': `${root}src` } }, cacheDir: '/tmp/satorna-app-lazy-proof/vite-cache',
        define: { 'import.meta.env.VITE_AUTH_BYPASS': JSON.stringify(String(authenticated)),
          'import.meta.env.VITE_API_BASE_URL': JSON.stringify(''), 'import.meta.env.VITE_CANONICAL_ORDERS_ENABLED': '"false"' },
        plugins: [react(), { name: 'synthetic-parity-chunk',
          resolveId(id) { if (id === '/__lazy-fixture.jsx') return '\0lazy-fixture.jsx' },
          load(id) { if (id === '\0lazy-fixture.jsx') return parityFixture },
        }], server: { host: '127.0.0.1', port: 0, strictPort: true },
      })
      await withSyntheticVite(server, `app-lazy-${authenticated}`, async origin => {
        const page = await browser.newPage({ serviceWorkers: 'block' })
        page.setDefaultTimeout(15_000)
        let parityRequests = 0, snapshotRequests = 0
        const routeRequests = Object.fromEntries(deferredRoutePaths.map(path => [path, 0]))
        let release: (() => Promise<void>) | undefined
        const external: string[] = []
        const errors: string[] = []
        const pending = new Set<string>()
        page.on('request', request => pending.add(request.url()))
        page.on('requestfinished', request => pending.delete(request.url()))
        page.on('requestfailed', request => pending.delete(request.url()))
        page.on('pageerror', error => errors.push(error.message))
        page.on('console', message => {
          if (message.type() !== 'error') return
          // The only allowed resource error is our deliberately unavailable synthetic API.
          if (message.location().url.startsWith(`${origin}/api/`) && message.text().includes('status of 503')) return
          errors.push(message.text())
        })
        try {
          await page.route('**/*', async route => {
            const url = new URL(route.request().url())
            // Existing global CSS imports this font; keep the proof completely offline.
            if (url.origin === 'https://fonts.googleapis.com') return route.fulfill({ contentType: 'text/css', body: '' })
            if (url.origin !== origin) { external.push(url.origin); return route.abort() }
            if (url.pathname.includes('vellaProductionSnapshot.generated')) snapshotRequests++
            if (url.pathname in routeRequests) routeRequests[url.pathname]++
            if (url.pathname === parityPath) {
              parityRequests++
              release = async () => {
                const fixture = await server.transformRequest('/__lazy-fixture.jsx')
                if (!fixture) throw new Error('Synthetic parity fixture failed to transform')
                await route.fulfill({ contentType: 'application/javascript', body: fixture.code })
              }
              return
            }
            if (url.pathname.startsWith('/api/')) return route.fulfill({ status: 503, contentType: 'application/json', body: '{"error":{"code":"SYNTHETIC_UNAVAILABLE"}}' })
            return route.continue()
          })
          try {
            await page.goto(`${origin}${authenticated ? '/orders' : '/auth/login'}`)
          } catch (error) {
            throw new Error(`Synthetic navigation failed: ${JSON.stringify({
              pending: [...pending].map(url => new URL(url).pathname), parityRequests, snapshotRequests, errors,
            })}`, { cause: error })
          }
          if (!authenticated) {
            await page.getByRole('button', { name: 'Войти', exact: true }).waitFor()
          } else {
            await page.getByRole('heading', { name: 'Лист печати на сегодня', exact: true }).waitFor()
          }
          const expectedRouteRequests = Object.fromEntries(deferredRoutePaths.map(path => [path, 0]))
          if (authenticated) expectedRouteRequests[ordersPath] = 1
          expect(routeRequests).toEqual(expectedRouteRequests)
          expect(parityRequests).toBe(0); expect(snapshotRequests).toBe(0)
          if (authenticated) {
            await navigate(page, '/wiki/algorithm')
            await page.getByRole('heading', { name: 'Настройки алгоритма', exact: true }).waitFor()
            expect(routeRequests['/src/wiki/pages/AlgorithmWiki.tsx']).toBe(1)
            await navigate(page, '/internal/vella-parity/reports')
            await page.getByRole('status').filter({ hasText: 'Загружаем раздел' }).waitFor()
            expect(parityRequests).toBe(1)
            await release!()
            await page.getByRole('heading', { name: 'Synthetic parity /internal/vella-parity/reports' }).waitFor()
            expect(snapshotRequests).toBe(1)
            await navigate(page, '/avito/reviews')
            await page.getByRole('heading', { name: 'Synthetic parity /avito/reviews' }).waitFor()
            expect(parityRequests).toBe(1)
            await navigate(page, '/orders')
            await page.getByRole('heading', { name: 'Лист печати на сегодня', exact: true }).waitFor()
          }
          expect(await page.locator('vite-error-overlay').count()).toBe(0)
          expect(external).toEqual([])
          expect(errors).toEqual([])
        } finally { await page.close() }

        if (authenticated) {
          const failed = await browser.newPage({ serviceWorkers: 'block' })
          let attempts = 0
          try {
            await failed.route('**/*', async route => {
              const url = new URL(route.request().url())
              if (url.origin === 'https://fonts.googleapis.com') return route.fulfill({ contentType: 'text/css', body: '' })
              if (url.origin !== origin || url.pathname.startsWith('/api/')) return route.abort()
              if (url.pathname === parityPath) {
                attempts++
                if (attempts === 1) return route.abort('failed')
                const fixture = await server.transformRequest('/__lazy-fixture.jsx')
                if (!fixture) throw new Error('Synthetic parity fixture failed to transform')
                return route.fulfill({ contentType: 'application/javascript', body: fixture.code })
              }
              return route.continue()
            })
            await failed.goto(`${origin}/internal/vella-parity/reports`)
            await failed.getByRole('alert').getByRole('heading', { name: 'Раздел не загрузился' }).waitFor()
            expect(attempts).toBe(1)
            await navigate(failed, '/orders')
            await failed.getByRole('heading', { name: 'Лист печати на сегодня', exact: true }).waitFor()
            await navigate(failed, '/internal/vella-parity/reports')
            await failed.getByRole('alert').waitFor()
            expect(attempts).toBe(1)
            await failed.getByRole('button', { name: 'Обновить страницу', exact: true }).click()
            await failed.getByRole('heading', { name: 'Synthetic parity /internal/vella-parity/reports' }).waitFor()
            expect(attempts).toBe(2)
            await navigate(failed, '/orders')
            await failed.getByRole('heading', { name: 'Лист печати на сегодня', exact: true }).waitFor()
            expect(attempts).toBe(2)
          } finally { await failed.close() }

          const failedRoute = await browser.newPage({ serviceWorkers: 'block' })
          failedRoute.setDefaultTimeout(5_000)
          let routeAttempts = 0
          try {
            await failedRoute.route('**/*', async route => {
              const url = new URL(route.request().url())
              if (url.origin === 'https://fonts.googleapis.com') return route.fulfill({ contentType: 'text/css', body: '' })
              if (url.origin !== origin || url.pathname.startsWith('/api/')) return route.abort()
              if (url.pathname === ordersPath && ++routeAttempts === 1) return route.abort('failed')
              return route.continue()
            })
            await failedRoute.goto(`${origin}/orders`)
            await failedRoute.getByRole('alert').getByRole('heading', { name: 'Раздел не загрузился' }).waitFor()
            expect(routeAttempts).toBe(1)
            await failedRoute.getByRole('button', { name: 'Обновить страницу', exact: true }).click()
            await failedRoute.getByRole('heading', { name: 'Лист печати на сегодня', exact: true }).waitFor()
            expect(routeAttempts).toBe(2)
          } finally { await failedRoute.close() }
        }
      })
    }
  } finally { await browser.close() }
}, 60_000)
