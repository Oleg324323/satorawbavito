import { lazy, Suspense, useLayoutEffect, type ComponentType } from 'react'
import { setWbDemandRoute } from '@/lib/wbDemand'
import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { RouteErrorBoundary } from '@/components/RouteErrorBoundary'
import { ThemeProvider } from '@/components/ThemeProvider'
import { AppShell } from '@/layouts/AppShell'
import { ComingSoon } from '@/pages/stub/ComingSoon'
import { AuthProvider } from './features/auth/AuthProvider'
import { LoginPage } from './features/auth/LoginPage'
import { RegisterPage } from './features/auth/RegisterPage'
import { PublicOnlyAuth, RequireAuth } from './features/auth/AuthRoutes'
import { WbPrivacyPage } from './features/privacy/WbPrivacyPage'

const TemplatesPage = lazyRoute(() => import('./features/wb-repricer/TemplatesPage').then(({ TemplatesPage }) => ({ default: TemplatesPage })))
const VellaFoundationWorkbench = lazyRoute(() => import('./components/vella/VellaFoundationWorkbench').then(({ VellaFoundationWorkbench }) => ({ default: VellaFoundationWorkbench })))
const VellaSystemCatalog = lazyRoute(() => import('./components/vella-system/VellaSystemCatalog').then(({ VellaSystemCatalog }) => ({ default: VellaSystemCatalog })))
const VellaStaticPage = lazyRoute(() => import('./features/vella-static/VellaStaticPage').then(({ VellaStaticPage }) => ({ default: VellaStaticPage })))
const VellaReactApp = lazyRoute(() => import('./features/vella-react/VellaReactApp').then(({ VellaReactApp }) => ({ default: VellaReactApp })))
const VellaHtmlRepricer = lazyRoute(() => import('./components/vella-system/html/VellaHtmlRepricer').then(({ VellaHtmlRepricer }) => ({ default: VellaHtmlRepricer })))
const WbRepricerPage = lazyRoute(() => import('./features/wb-repricer/WbRepricerPage').then(({ WbRepricerPage }) => ({ default: WbRepricerPage })))
const WbRepricerChangelogPage = lazyRoute(() => import('./features/wb-repricer/WbRepricerChangelogPage').then(({ WbRepricerChangelogPage }) => ({ default: WbRepricerChangelogPage })))
const WbRepricerStatsPage = lazyRoute(() => import('./features/wb-repricer/WbRepricerStatsPage').then(({ WbRepricerStatsPage }) => ({ default: WbRepricerStatsPage })))
const WbReportsPage = lazyRoute(() => import('./features/wb-reports/WbReportsPage').then(({ WbReportsPage }) => ({ default: WbReportsPage })))
const WbSourcesPage = lazyRoute(() => import('./features/wb-sources/WbSourcesPage').then(({ WbSourcesPage }) => ({ default: WbSourcesPage })))
const WbRepricerWiki = lazyRoute(() => import('./wiki/pages/WbRepricerWiki').then(({ WbRepricerWiki }) => ({ default: WbRepricerWiki })))
const LiquidationWiki = lazyRoute(() => import('./wiki/pages/LiquidationWiki').then(({ LiquidationWiki }) => ({ default: LiquidationWiki })))
const PromotionsWiki = lazyRoute(() => import('./wiki/pages/PromotionsWiki').then(({ PromotionsWiki }) => ({ default: PromotionsWiki })))
const AlgorithmWiki = lazyRoute(() => import('./wiki/pages/AlgorithmWiki').then(({ AlgorithmWiki }) => ({ default: AlgorithmWiki })))
const TemplatesWiki = lazyRoute(() => import('./wiki/pages/TemplatesWiki').then(({ TemplatesWiki }) => ({ default: TemplatesWiki })))
const OrdersPrintListPage = lazyRoute(() => import('./features/orders/OrdersPrintListPage').then(({ OrdersPrintListPage }) => ({ default: OrdersPrintListPage })))
const LazyVellaHtmlParityPage = lazyRoute(() => import('./features/vella-parity/VellaHtmlParityPage')
  .then(module => ({ default: module.VellaHtmlParityPage })))

function lazyRoute<T extends ComponentType>(load: () => Promise<{ default: T }>) {
  return lazy(() => load().catch(() => ({ default: ChunkLoadError as unknown as T })))
}

function RouteLoading() {
  return (
    <div role="status" className="flex min-h-screen items-center justify-center bg-slate-950 px-6 text-slate-100">
      <div className="rounded-2xl border border-white/10 bg-white/5 px-5 py-4 text-sm">Загружаем раздел...</div>
    </div>
  )
}

function ChunkLoadError() {
  return (
    <div role="alert" className="flex min-h-screen items-center justify-center bg-slate-950 px-6 text-slate-100">
      <div className="rounded-2xl border border-white/10 bg-white/5 px-5 py-4 text-sm">
        <h1 className="mb-2 text-lg">Раздел не загрузился</h1>
        <p className="mb-4">Не удалось загрузить код раздела. Обновите страницу, чтобы повторить загрузку.</p>
        <button type="button" className="rounded-lg border border-white/20 px-4 py-2" onClick={() => window.location.reload()}>
          Обновить страницу
        </button>
      </div>
    </div>
  )
}

function VellaHtmlParityPage() {
  // Import failures have explicit document reload recovery; render errors still reach RouteErrorBoundary.
  return (
    <Suspense fallback={<RouteLoading />}>
      <LazyVellaHtmlParityPage />
    </Suspense>
  )
}

function RoutedApp() {
  const location = useLocation()
  useLayoutEffect(() => { setWbDemandRoute(`${location.key}:${location.pathname}${location.search}`) }, [location.key, location.pathname, location.search])
  return (
    <RouteErrorBoundary resetKey={location.pathname}>
      <Suspense fallback={<RouteLoading />}>
        <Routes>
            <Route element={<PublicOnlyAuth />}>
              <Route path="/auth/login" element={<LoginPage />} />
              <Route path="/auth/register" element={<RegisterPage />} />
            </Route>

            <Route element={<RequireAuth />}>
              <Route path="/internal/vella-system" element={<VellaSystemCatalog />} />
              <Route path="/internal/vella-components" element={<VellaFoundationWorkbench />} />
              <Route path="/internal/vella-parity/reports" element={<VellaHtmlParityPage />} />
              <Route path="/internal/vella-parity/*" element={<VellaHtmlParityPage />} />

              <Route path="/internal/vella-react/repricer" element={<VellaHtmlRepricer />} />
              <Route path="/internal/vella-react/repricer/sku/:articleId" element={<VellaReactApp />} />
              <Route path="/internal/vella-react/repricer/stats" element={<WbRepricerStatsPage />} />
              <Route path="/internal/vella-react/repricer/changelog" element={<VellaReactApp />} />
              <Route path="/internal/vella-react/repricer/liquidation" element={<VellaReactApp />} />
              <Route path="/internal/vella-react/repricer/promos" element={<VellaReactApp />} />
              <Route path="/internal/vella-react/promotions" element={<VellaReactApp />} />
              <Route path="/internal/vella-react/liquidation" element={<VellaReactApp />} />
              <Route path="/internal/vella-react/reports" element={<VellaReactApp />} />
              <Route path="/internal/vella-react/reports/monitor" element={<VellaReactApp />} />
              <Route path="/internal/vella-react/reports/rules" element={<VellaReactApp />} />
              <Route path="/internal/vella-react/reports/abc" element={<VellaReactApp />} />
              <Route path="/internal/vella-react/reports/rnp" element={<VellaReactApp />} />
              <Route path="/internal/vella-react/reports/pnl" element={<VellaReactApp />} />
              <Route path="/internal/vella-react/reports/expenses" element={<WbReportsPage />} />
              <Route path="/internal/vella-react/reports/ads" element={<VellaReactApp />} />
              <Route path="/internal/vella-react/reports/sales" element={<VellaReactApp />} />
              <Route path="/internal/vella-react/reports/stock" element={<VellaReactApp />} />
              <Route path="/internal/vella-react/reports/week-over-week" element={<VellaReactApp />} />
              <Route path="/internal/vella-react/reports/*" element={<VellaReactApp />} />
              <Route path="/internal/vella-react/sources" element={<WbSourcesPage />} />
              <Route path="/internal/vella-react/notifications" element={<VellaReactApp />} />
              <Route path="/internal/vella-react/*" element={<VellaReactApp />} />

              <Route path="/internal/vella-preview/repricer" element={<WbRepricerPage />} />
              <Route path="/internal/vella-preview/repricer/sku/:articleId" element={<WbRepricerPage initialDrawerOpen />} />
              <Route path="/internal/vella-preview/templates" element={<TemplatesPage />} />
              <Route path="/internal/vella-preview/repricer/changelog" element={<WbRepricerChangelogPage />} />
              <Route path="/internal/vella-preview/repricer/stats" element={<WbRepricerStatsPage />} />
              <Route path="/internal/vella-preview/repricer/liquidation" element={<VellaStaticPage />} />
              <Route path="/internal/vella-preview/repricer/promos" element={<VellaStaticPage />} />
              <Route path="/internal/vella-preview/repricer/work-status" element={<VellaStaticPage />} />
              <Route path="/internal/vella-preview/promotions" element={<VellaStaticPage />} />
              <Route path="/internal/vella-preview/liquidation" element={<VellaStaticPage />} />
              <Route path="/internal/vella-preview/reports" element={<VellaStaticPage />} />
              <Route path="/internal/vella-preview/reports/monitor" element={<VellaStaticPage />} />
              <Route path="/internal/vella-preview/reports/rules" element={<VellaStaticPage />} />
              <Route path="/internal/vella-preview/reports/abc" element={<VellaStaticPage />} />
              <Route path="/internal/vella-preview/reports/rnp" element={<VellaStaticPage />} />
              <Route path="/internal/vella-preview/reports/pnl" element={<VellaStaticPage />} />
              <Route path="/internal/vella-preview/reports/expenses" element={<WbReportsPage />} />
              <Route path="/internal/vella-preview/reports/ads" element={<VellaStaticPage />} />
              <Route path="/internal/vella-preview/reports/sales" element={<VellaStaticPage />} />
              <Route path="/internal/vella-preview/reports/stock" element={<VellaStaticPage />} />
              <Route path="/internal/vella-preview/reports/week-over-week" element={<VellaStaticPage />} />
              <Route path="/internal/vella-preview/reports/*" element={<VellaStaticPage />} />
              <Route path="/internal/vella-preview/sources" element={<WbSourcesPage />} />

              <Route path="/wb/repricer" element={<VellaHtmlParityPage />} />
              <Route path="/wb/repricer/stats" element={<VellaHtmlParityPage />} />
              <Route path="/wb/repricer/sku/:articleId" element={<VellaHtmlParityPage />} />
              <Route path="/wb/repricer/changelog" element={<VellaHtmlParityPage />} />
              <Route path="/wb/repricer/simulator" element={<Navigate to="/wb/repricer" replace />} />
              <Route path="/wb/repricer/liquidation" element={<VellaHtmlParityPage />} />
              <Route path="/wb/repricer/promos" element={<VellaHtmlParityPage />} />
              <Route path="/wb/repricer/work-status" element={<VellaHtmlParityPage />} />
              <Route path="/wb/algorithm" element={<VellaHtmlParityPage />} />
              <Route path="/wb/templates" element={<VellaHtmlParityPage />} />
              <Route path="/wb/promotions" element={<VellaHtmlParityPage />} />
              <Route path="/wb/liquidation" element={<VellaHtmlParityPage />} />
              <Route path="/wb/reports" element={<VellaHtmlParityPage />} />
              <Route path="/wb/reports/monitor" element={<VellaHtmlParityPage />} />
              <Route path="/wb/reports/rules" element={<VellaHtmlParityPage />} />
              <Route path="/wb/reports/abc" element={<VellaHtmlParityPage />} />
              <Route path="/wb/reports/rnp" element={<VellaHtmlParityPage />} />
              <Route path="/wb/reports/pnl" element={<VellaHtmlParityPage />} />
              <Route path="/wb/reports/expenses" element={<Navigate to="/wb/reports/pnl" replace />} />
              <Route path="/wb/reports/ads" element={<VellaHtmlParityPage />} />
              <Route path="/wb/reports/sales" element={<VellaHtmlParityPage />} />
              <Route path="/wb/reports/stock" element={<VellaHtmlParityPage />} />
              <Route path="/wb/reports/week-over-week" element={<VellaHtmlParityPage />} />
              <Route path="/wb/reports/*" element={<VellaHtmlParityPage />} />
              <Route path="/wb/sources" element={<VellaHtmlParityPage />} />
              <Route path="/wb/reviews" element={<VellaHtmlParityPage />} />
              <Route path="/notifications" element={<VellaHtmlParityPage />} />
              <Route path="/avito" element={<Navigate to="/avito/repricer" replace />} />
              <Route path="/avito/overview" element={<Navigate to="/avito/repricer" replace />} />
              <Route path="/avito/chats" element={<VellaHtmlParityPage />} />
              <Route path="/avito/inbox" element={<VellaHtmlParityPage />} />
              <Route path="/avito/messages" element={<VellaHtmlParityPage />} />
              <Route path="/avito/orders" element={<VellaHtmlParityPage />} />
              <Route path="/avito/orders/archive" element={<VellaHtmlParityPage />} />
              <Route path="/avito/listings/*" element={<Navigate to="/avito/repricer" replace />} />
              <Route path="/avito/repricer" element={<VellaHtmlParityPage />} />
              <Route path="/avito/reviews" element={<VellaHtmlParityPage />} />
              <Route path="/avito/stats" element={<VellaHtmlParityPage />} />
              <Route path="/avito/statistics" element={<VellaHtmlParityPage />} />
              <Route path="/avito/notifications" element={<VellaHtmlParityPage />} />
              <Route path="/avito/privacy" element={<VellaHtmlParityPage />} />
              <Route path="/orders/archive" element={<VellaHtmlParityPage />} />
              <Route path="/settings/profile" element={<VellaHtmlParityPage />} />
              <Route path="/settings/access" element={<VellaHtmlParityPage />} />
              <Route path="/settings/imports" element={<VellaHtmlParityPage />} />

              <Route path="/" element={<Navigate to="/wb/repricer" replace />} />
              <Route path="/dashboard" element={<Navigate to="/wb/repricer" replace />} />
              <Route path="/wb/repricer/dashboard" element={<Navigate to="/wb/repricer" replace />} />
              <Route path="/wb/repricer/algorithm" element={<Navigate to="/wb/algorithm" replace />} />
              <Route path="/settings" element={<Navigate to="/settings/profile" replace />} />
              <Route path="/settings/marketplaces" element={<Navigate to="/settings/profile" replace />} />
              <Route path="/settings/notifications" element={<Navigate to="/notifications" replace />} />
              <Route path="/settings/sessions" element={<Navigate to="/settings/profile" replace />} />
              <Route path="/settings/audit" element={<Navigate to="/settings/access" replace />} />

              <Route element={<AppShell />}>
                <Route path="/avito/accounts" element={<ComingSoon />} />
                <Route path="/avito/chat-bot" element={<ComingSoon />} />
                <Route path="/avito/wallets" element={<ComingSoon />} />

                <Route path="/orders" element={<OrdersPrintListPage />} />
                <Route path="/orders/returns" element={<ComingSoon />} />

                <Route path="/help" element={<Navigate to="/wiki/wb-repricer" replace />} />

                <Route path="/wiki" element={<Navigate to="/wiki/wb-repricer" replace />} />
                <Route path="/wiki/wb-repricer" element={<WbRepricerWiki />} />
                <Route path="/wiki/liquidation" element={<LiquidationWiki />} />
                <Route path="/wiki/promotions" element={<PromotionsWiki />} />
                <Route path="/wiki/algorithm" element={<AlgorithmWiki />} />
                <Route path="/wiki/templates" element={<TemplatesWiki />} />

                <Route path="*" element={<ComingSoon />} />
              </Route>
            </Route>
        </Routes>
      </Suspense>
    </RouteErrorBoundary>
  )
}

export function AppEntryRoutes() {
  return (
    <Routes>
      <Route path="/wb/privacy" element={<WbPrivacyPage />} />
      <Route path="*" element={<AuthProvider><RoutedApp /></AuthProvider>} />
    </Routes>
  )
}

export default function App() {
  return (
    <ThemeProvider>
      <BrowserRouter>
        <AppEntryRoutes />
      </BrowserRouter>
    </ThemeProvider>
  )
}
