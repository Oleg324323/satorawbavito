import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { chromium } from 'playwright'
import { describe, expect, it, vi } from 'vitest'
import { installAbcLiveDataBridge } from '../vella-parity/VellaHtmlParityPage'

const root = process.cwd()

function read(path: string) {
  return readFileSync(join(root, path), 'utf8')
}

function htmlSection(html: string, id: string) {
  const start = html.indexOf(`id="${id}"`)
  if (start === -1) return ''
  const next = html.indexOf('<div class="tab-content"', start + 1)
  return next === -1 ? html.slice(start) : html.slice(start, next)
}

function readSourceFiles(dir: string): Array<{ path: string; text: string }> {
  return readdirSync(join(root, dir)).flatMap((name) => {
    const path = join(dir, name)
    const normalizedPath = path.replaceAll('\\', '/')
    const abs = join(root, path)
    if (statSync(abs).isDirectory()) return readSourceFiles(path)
    if (!/\.(ts|tsx)$/.test(name)) return []
    // Assertions may name historical components without shipping them to users.
    if (/\.(test|spec)\.tsx?$/.test(name)) return []
    if (normalizedPath.endsWith('features/vella-parity/VellaHtmlParityPage.tsx')) return []
    return [{ path, text: read(path) }]
  })
}

describe('vella source of truth', () => {
  it('keeps public Vella routes on the intended implementation surface', () => {
    const app = read('src/App.tsx')

    expect(app).toContain('<Route path="/internal/vella-preview/repricer" element={<WbRepricerPage />} />')
    expect(app).toContain('<Route path="/internal/vella-preview/repricer/sku/:articleId" element={<WbRepricerPage initialDrawerOpen />} />')
    expect(app).toContain('<Route path="/internal/vella-preview/templates" element={<TemplatesPage />} />')
    expect(app).toContain('<Route path="/internal/vella-preview/repricer/changelog" element={<WbRepricerChangelogPage />} />')
    expect(app).toContain('<Route path="/internal/vella-preview/repricer/liquidation" element={<VellaStaticPage />} />')
    expect(app).toContain('<Route path="/internal/vella-preview/repricer/promos" element={<VellaStaticPage />} />')
    expect(app).toContain('<Route path="/internal/vella-preview/promotions" element={<VellaStaticPage />} />')
    expect(app).toContain('<Route path="/internal/vella-preview/liquidation" element={<VellaStaticPage />} />')
    expect(app).toContain('<Route path="/internal/vella-preview/reports" element={<VellaStaticPage />} />')
    expect(app).toContain('<Route path="/internal/vella-preview/reports/monitor" element={<VellaStaticPage />} />')
    expect(app).toContain('<Route path="/internal/vella-preview/reports/rules" element={<VellaStaticPage />} />')
    expect(app).toContain('<Route path="/wb/repricer" element={<VellaHtmlParityPage />} />')
    expect(app).toContain('<Route path="/wb/repricer/stats" element={<VellaHtmlParityPage />} />')
    expect(app).toContain('<Route path="/wb/repricer/changelog" element={<VellaHtmlParityPage />} />')
    expect(app).toContain('<Route path="/wb/templates" element={<VellaHtmlParityPage />} />')
    expect(app).toContain('<Route path="/wb/promotions" element={<VellaHtmlParityPage />} />')
    expect(app).toContain('<Route path="/wb/liquidation" element={<VellaHtmlParityPage />} />')
    expect(app).toContain('<Route path="/wb/reports" element={<VellaHtmlParityPage />} />')
    expect(app).toContain('<Route path="/wb/reports/rules" element={<VellaHtmlParityPage />} />')
    expect(app).toContain('<Route path="/wb/reports/expenses" element={<Navigate to="/wb/reports/pnl" replace />} />')
    expect(app).toContain('<Route path="/wb/sources" element={<VellaHtmlParityPage />} />')
    expect(app).toContain('<Route path="/wb/reviews" element={<VellaHtmlParityPage />} />')
    expect(app).toContain('<Route path="/avito" element={<Navigate to="/avito/repricer" replace />} />')
    expect(app).toContain('<Route path="/avito/reviews" element={<VellaHtmlParityPage />} />')
    expect(app).toContain('<Route path="/avito/orders" element={<VellaHtmlParityPage />} />')
    expect(app).toContain('<Route path="/avito/listings/*" element={<Navigate to="/avito/repricer" replace />} />')
    expect(app).toContain('<Route path="/avito/stats" element={<VellaHtmlParityPage />} />')
    expect(app).toContain('<Route path="/avito/notifications" element={<VellaHtmlParityPage />} />')
    expect(app).toContain('<Route path="/settings/profile" element={<VellaHtmlParityPage />} />')
    expect(app).toContain('<Route path="/settings/access" element={<VellaHtmlParityPage />} />')
    expect(app.indexOf('<Route path="/wb/repricer" element={<VellaHtmlParityPage />} />')).toBeLessThan(
      app.indexOf('<Route element={<AppShell />}>'),
    )

    const staticPage = read('src/features/vella-static/VellaStaticPage.tsx')
    expect(staticPage).toContain("pathname.endsWith('/reports/monitor')")
    expect(staticPage).toContain("return { tab: 'digest', mode: 'now' }")
    expect(staticPage).toContain("pathname.endsWith('/reports/rules')")
    expect(staticPage).toContain("return 'report-rules'")
    expect(staticPage).toContain("pathname.endsWith('/reports/expenses')")
    expect(staticPage).toContain("return 'expenses'")
    expect(staticPage).toContain("pathname.endsWith('/wb/sources')")
    expect(staticPage).toContain("return 'sources'")
    expect(staticPage).toContain("pathname.endsWith('/repricer/stats')")
    expect(staticPage).toContain("return 'repricer-stats'")
    expect(staticPage).toContain("pathname.endsWith('/avito/reviews')")
    expect(staticPage).toContain("return 'avito-reviews'")
    expect(staticPage).toContain("pathname.endsWith('/repricer/liquidation')")
    expect(staticPage).toContain("pathname.endsWith('/repricer/promos')")
    expect(staticPage).toContain("pathname.startsWith('/wb/reviews')")
    expect(staticPage).toContain("pathname.startsWith('/wb/reports/')")
    expect(staticPage).toContain("pathname.endsWith('/settings/profile')")
    expect(staticPage).toContain("return 'settings-profile'")
    expect(staticPage).toContain("pathname.endsWith('/settings/access')")
    expect(staticPage).toContain("return 'settings-access'")

    const html = read('public/vella-production.html')
    expect(html).toContain('href="/brand/satorna-icon.svg"')
    expect(html).toContain('src="brand/satorna-logo-white.svg"')
    expect(html).toContain('data-srcs="brand/satorna-logo-white.svg|/brand/satorna-logo-white.svg"')
    expect(html).toContain('src="brand/satorna-icon.svg"')
    expect(html).toContain('data-srcs="brand/satorna-icon.svg|/brand/satorna-icon.svg"')
    expect(html).toContain('logo-fallback')
    expect(html).toContain('initSidebarLogo')
    expect(html).not.toContain('href="brand/satorna-icon.svg"')
    expect(html).toContain("'/wb/liquidation': 'liq'")
    expect(html).toContain("'/wb/templates': 'templates'")
    expect(html).toContain("'/wb/promotions': 'promos'")
    expect(html).toContain("'/wb/reviews': 'reviews'")
    expect(html).toContain("'/avito/reviews': 'avito-reviews'")
    expect(html).toContain('id="tab-avito-reviews"')
    expect(html).toContain('id="avitoReviewsNavCount"')
    expect(html).toContain('<button class="chip active" type="button" data-avito-review-filter="status" data-value="all" aria-pressed="true"')
    expect(html).toContain("const totals = { open: 14, queue: 7, auto: 31, blocked: 4 }")
    expect(html).toContain("set('avitoReviewsNavCount', totals.open)")
    expect(html).toContain("visibleQueue + ' из ' + totals.queue + ' отзывов'")
    expect(html).toContain("onclick=\"openAvitoReviewsSettingsDrawer()\"")
    expect(html).toContain('id="avitoReviewsSettingsDrawer"')
    expect(html).toContain('Джейсон Стейтем-мем · только Авито')
    expect(html).toContain('WB остаётся в нормальном маркетплейс-тоне без мемных цитат')
    expect(html).toContain("'avito-notifications': ['Уведомления Авито доступны в desktop-версии'")
    expect(html).toContain("'/settings/profile': 'settings-profile'")
    expect(html).toContain("'/settings/access': 'settings-access'")
    expect(html).toContain("liq: '/wb/liquidation'")
    expect(html).toContain("promos: '/wb/promotions'")
    expect(html).toContain("reviews: '/wb/reviews'")
    expect(html).toContain("'avito-reviews': '/avito/reviews'")
    expect(html).toContain("'settings-profile': '/settings/profile'")
    expect(html).toContain("'settings-access': '/settings/access'")
    expect(html).toContain('1С · Движение денежных средств')
    expect(html).toContain('Правило статьи ДДС')
    expect(html).toContain('Оплата от покупателей_РВБ')
    expect(html).toContain('сверка cashflow')
    expect(html).toContain('SKU-драйвер')
    expect(html).toContain('expense-row')
    expect(html).toContain('expense-open-btn')
    expect(html).not.toContain('class="expense-click-row"')
    expect(html).not.toContain("article:'Перемещение между кассами, счетами'")
    expect(html).not.toContain("article:'Дивиденды'")
    expect(html).not.toContain("article:'Оплата поставщикам_Гурлен'")
    expect(html).not.toContain('<th>Брать</th>')
  })

  it('requires live backend rows for the ABC report table without static row fallback', async () => {
    // Keep the independent UI guard: bridge state alone cannot prevent a renderer fallback.
    const parityPage = read('src/features/vella-parity/VellaHtmlParityPage.tsx')
    for (const demo of [': REPORT_ABC_DATA', '324 840 ₽', '68 940 ₽', 'ABC source',
      'Все SKU · реклама частичная', '+41 200 ₽']) {
      expect(parityPage).not.toContain(demo)
    }
    const runtime = Object.assign(new EventTarget(), {
      location: { pathname: '/wb/reports/abc', search: '', origin: 'http://localhost' },
      localStorage: { getItem: () => null, setItem: () => undefined },
      setTimeout: (callback: () => void) => { callback(); return 0 },
      __vellaReportPeriods: {
        abc: { days: 7, fromIso: '2026-09-01', toIso: '2026-09-07', mode: 'custom' },
      },
    })
    vi.stubGlobal('window', runtime)
    const requests: Array<{ url: string; method: string }> = []
    let outcome: 'rows' | 'empty' | 'error' = 'rows'
    vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      requests.push({ url, method: init?.method ?? 'GET' })
      if (!url.includes('/api/wb/reports/abc/latest-cache?') || (init?.method ?? 'GET') !== 'GET') {
        throw new Error('Unexpected request in synthetic ABC fixture')
      }
      return new Response(JSON.stringify(outcome === 'error'
        ? { detail: 'Synthetic backend unavailable' }
        : { rows: outcome === 'rows' ? [{ sku: 'BACKEND-ONLY' }] : [], filteredSummary: {} }), {
        status: outcome === 'error' ? 500 : 200,
        headers: { 'content-type': 'application/json' },
      })
    })
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    try {
      installAbcLiveDataBridge('synthetic-rows-token')
      await window.__vellaLoadLiveAbcReport?.()
      expect(window.__vellaAbcLiveRows?.map((row) => row.sku)).toEqual(['BACKEND-ONLY'])
      for (const next of ['empty', 'error'] as const) {
        outcome = next
        installAbcLiveDataBridge(`synthetic-${next}-token`)
        await window.__vellaLoadLiveAbcReport?.()
        expect(window.__vellaAbcLiveRows).toEqual([])
        expect(window.__vellaAbcLiveLoading).toBe(false)
      }
      expect(window.__vellaAbcLiveReport).toBeNull()
      expect(window.__vellaAbcLiveError).toBe('Synthetic backend unavailable')
      expect(requests).toHaveLength(3)
      expect(requests.every(({ url, method }) => method === 'GET'
        && url.includes('2026-09-01') && url.includes('2026-09-07'))).toBe(true)
      installAbcLiveDataBridge(null)
      await window.__vellaLoadLiveAbcReport?.()
      expect(window.__vellaAbcLiveRows).toEqual([])
      expect(window.__vellaAbcLiveAuthExpired).toBe(true)
      expect(requests).toHaveLength(3)
    } finally {
      warn.mockRestore()
      vi.unstubAllGlobals()
    }
  })

  it('never exposes legacy mock rows on backend-owned report tabs', () => {
    const html = read('public/vella-production.html')

    for (const tab of ['rnp', 'pnl', 'ads', 'stock']) {
      expect(html).not.toContain(`document.querySelector('#tab-${tab} tbody')`)
    }
  })

  it('binds the production RNP report to backend data instead of the secondary mock renderer', () => {
    const parityPage = read('src/features/vella-parity/VellaHtmlParityPage.tsx')
    const handlers = read('src/mocks/handlers.ts')

    expect(parityPage).toContain('currentRnpReportPath')
    expect(parityPage).toContain('data-vella-runtime-binding="backend-rnp"')
    // rnpCacheBrowser.test.ts proves real cache GET → populated/empty/error
    // rendering and session clearing; obsolete explanatory copy is not the binding.
    expect(parityPage).not.toContain('data-vella-island="rnp-diagnostics"')
    expect(parityPage).not.toContain('source.statusCode')
    expect(parityPage).not.toContain('source.rateLimit')
    expect(parityPage).not.toContain('<SecondaryReportTableBodyIsland tab="rnp"')
    expect(handlers).toContain("reportId === 'rnp' || reportId === 'pnl'")
  })

  it('reloads Ads and Stock from backend for the shared selected period without legacy mock rows', () => {
    const parityPage = read('src/features/vella-parity/VellaHtmlParityPage.tsx')

    // Actual Ads/Stock route effects, unrelated-event silence and exact own
    // period GETs are covered by wbReportPeriodScopeBrowser.test.ts.
    const syncRuntime = parityPage.slice(
      parityPage.indexOf('function syncLegacyWbPeriodRuntime'),
      parityPage.indexOf('function applyProductsPeriodState'),
    )
    expect(syncRuntime).not.toContain('renderSecondaryReports')
    const managerRuntime = parityPage.slice(
      parityPage.indexOf('function publishRuntimeManagersToVella'),
      parityPage.indexOf('function resolveParityRouteTarget'),
    )
    expect(managerRuntime).not.toContain('renderSecondaryReports')
  })

  it('resets the live week report to loading when the selected period changes', () => {
    const parityPage = read('src/features/vella-parity/VellaHtmlParityPage.tsx')
    const weekIsland = parityPage.slice(
      parityPage.indexOf('function WeekReportIsland'),
      parityPage.indexOf('function AbcHelpRowIsland'),
    )

    expect(weekIsland).toContain("setState({ status: 'loading', debug: [...debug] })")
    expect(weekIsland).not.toContain("setState((current) => current.status === 'ready' ? current : { status: 'loading' })")
  })

  it('preserves week cache failure evidence and a user-facing error', () => {
    const parityPage = read('src/features/vella-parity/VellaHtmlParityPage.tsx')
    const weekIsland = parityPage.slice(
      parityPage.indexOf('function WeekReportIsland'),
      parityPage.indexOf('function AbcHelpRowIsland'),
    )

    expect(parityPage).toContain('type WeekDebugStep')
    expect(parityPage).toContain('function weekApiErrorDebug')
    expect(weekIsland).toContain("weekApiErrorDebug('cache', cachePath, error)")
    expect(weekIsland).toContain('debug: [...debug, reportError]')
    // reportLoadingBrowser.test.ts proves the actual cache503 message and
    // distinct error state. Do not restore the obsolete two-request job/report flow.
  })

  it('runs period-scoped network effects only for the active WB surface', () => {
    const parityPage = read('src/features/vella-parity/VellaHtmlParityPage.tsx')

    for (const report of ['rnp', 'ads', 'stock']) {
      expect(parityPage).toContain(`const ${report}ReportActive = shouldLoadPeriodSurface(activeTab, '${report}')`)
      expect(parityPage).toContain(`if (!${report}ReportActive) return`)
    }
    expect(parityPage).toContain("const pnlReportActive = shouldLoadPeriodSurface(activeTab, 'pnl')")
    expect(parityPage).toContain('if (!pnlReportActive || !cabinetMe) return')
    expect(parityPage).toContain('const ActiveParityTabContext = createContext<string | null>(null)')
    expect(parityPage).toContain('const activeTab = useContext(ActiveParityTabContext)')
    // Actual route-mounted Ads/Stock and other report browser tests assert
    // unrelated API silence; provider variable naming is not its contract.

    const applyPeriod = parityPage.slice(
      parityPage.indexOf('function applyProductsPeriodState'),
      parityPage.indexOf('function productsPeriodRequest'),
    )
    expect(applyPeriod).toContain("shouldLoadPeriodSurface(activeTab, 'products')")
    expect(applyPeriod).not.toContain("dispatchEvent(new CustomEvent('vella:products-period-updated', { detail: next }))\n  void window.__vellaLoadLiveRepricerProducts?.()")

    const legacyPeriodSync = parityPage.slice(
      parityPage.indexOf('function syncLegacyWbPeriodRuntime'),
      parityPage.indexOf('function applyProductsPeriodState'),
    )
    expect(legacyPeriodSync).toContain("const abcActive = activeTab === 'abc'")
    expect(legacyPeriodSync).toContain("if (${JSON.stringify(abcActive)} && typeof renderAbcDemoRows === 'function')")

    // Actual report routes must not issue product/worker requests; the mounted
    // browser tests enforce that boundary without requiring an inner guard in
    // a products-only component or slicing up to a removed function name.
  })

  it('does not let the legacy secondary renderer retain fallback rows for live report roots', () => {
    const parityPage = read('src/features/vella-parity/VellaHtmlParityPage.tsx')
    const secondaryBridge = parityPage.slice(
      parityPage.indexOf('function installSecondaryReportRowsBridge'),
      parityPage.indexOf('function installNotificationsReactBridge'),
    )

    expect(secondaryBridge).toContain("const tabs = ['week']")
    // Actual protected-node identity, week capture and exception restoration
    // are verified by secondaryReportProtectionBrowser.test.ts, including repricer-stats.
  })

  it('keeps RNP toolbar chips wired to the live RNP filter model', () => {
    const parityPage = read('src/features/vella-parity/VellaHtmlParityPage.tsx')

    expect(parityPage).toContain('function applyRnpReportFilter')
    expect(parityPage).toContain('data-rnp-group={chip.toLowerCase()}')
    expect(parityPage).toContain('data-rnp-filter={rnpFilterKey(chip)}')
    expect(parityPage).toContain('rnpRowGroupTags(row)')
    expect(parityPage).toContain('rnpRowStatusTags(row)')
  })

  it('keeps RNP positive and negative deltas visibly color-coded in the live table', () => {
    const parityPage = read('src/features/vella-parity/VellaHtmlParityPage.tsx')

    expect(parityPage).toContain('#tab-rnp .subline.metric-up')
    expect(parityPage).toContain('#tab-rnp .subline.metric-down')
    expect(parityPage).toContain('color: #059669 !important')
    expect(parityPage).toContain('color: #DC2626 !important')
  })

  it('keeps React Vella foundation on the production HTML typography contract', () => {
    const html = read('public/vella-production.html')
    const globalCss = read('src/index.css')
    const foundationCss = read('src/components/vella/VellaFoundation.css')
    const visualScript = read('scripts/compare-vella-visual-contract.mjs')
    const packageJson = read('package.json')

    expect(html).toContain('family=Inter:wght@400;500;600;700')
    expect(html).toContain('family=Unbounded:wght@700;800')
    expect(html).toContain('family=JetBrains+Mono:wght@500;600')
    expect(globalCss).toContain('family=Inter:wght@400;500;600;700')
    expect(globalCss).toContain('family=Unbounded:wght@700;800')
    expect(globalCss).toContain('family=JetBrains+Mono:wght@500;600')
    expect(foundationCss).toContain("font-family: 'Inter', -apple-system, sans-serif")
    expect(foundationCss).toContain("--sidebar-bg: #0F172A")
    expect(foundationCss).toContain('--brand: #2563EB')
    expect(foundationCss).not.toContain('Golos Text')
    expect(globalCss).not.toContain('Golos+Text')
    expect(visualScript).toContain('fontFamily')
    expect(visualScript).toContain('fontSize')
    expect(visualScript).toContain('lineHeight')
    expect(visualScript).toContain('letterSpacing')
    expect(packageJson).toContain('visual:vella-contract')
    expect(packageJson).toContain('visual:vella-contract:strict')
  })

  it('keeps the reports demo inside canonical vella-production.html', () => {
    const html = read('public/vella-production.html')

    expect(html).toContain('WB REPORTS DEMO HARDENING')
    expect(html).toContain('REPORT_DEMO_ROWS')
    expect(html).toContain('THRESHOLD_PRESETS')
    expect(html).toContain('tab-report-rules')
    expect(html).not.toContain('tab-monitor')
    expect(html).toContain('data-digest-mode="now"')
    expect(html).toContain('id="subtabsContext"')
    expect(html).toContain('context-live-status')
    expect(html).toContain('desktop-only-fallback')
    expect(html).not.toContain('read-only демо')
    expect(html).not.toContain('Витрина SKU')
    expect(html).not.toContain('ещё колонки справа')
  })

  it('keeps team access settings in canonical Vella HTML with role-safe policy checks', () => {
    const html = read('public/vella-production.html')
    const access = htmlSection(html, 'tab-settings-access')

    expect(access).toContain('Команда и доступы')
    expect(access).toContain('Пользователи')
    expect(access).not.toContain('Матрица ролей')
    expect(access).not.toContain('Логика распределения')
    expect(access).not.toContain('access-kpis')
    expect(html).toContain('SETTINGS_ACCESS_USERS')
    expect(html).toContain('settingsAccessRoleSelect')
    expect(html).toContain("name: 'Мария Ф.'")
    expect(html).toContain("name: 'Максим Б.'")
    expect(html).toContain("name: 'Светлана В.'")
    expect(html).toContain("name: 'Анна П.'")
    expect(html).toContain('renderSettingsAccess')
    expect(html).toContain('changeSettingsRoleFromTable')
    expect(html).toContain('finance_view')
    expect(html).toContain('wallet_topup')
    expect(html).toContain('orders_manual_create')
    expect(html).toContain('Ручное добавление заказов')
    expect(html).toContain("{ key: 'orders_manual_create', label: 'Добавление заказов' }")
    expect(html).toContain("ecom: { modules: ['repricer', 'reports', 'ads', 'reviews', 'avito'], rights: ['finance', 'price_send', 'orders_manual_create'] }")
    expect(html).toContain("production: { modules: ['production'], rights: ['orders_manual_create'] }")
    expect(html).toContain("finance: { modules: ['reports', 'avito'], rights: ['finance'] }")
    expect(html).toContain('updateOrdersManualAddButtonState')
    expect(html).toContain('role_admin')
    expect(html).toContain('token_admin')
    expect(html).toContain('xml_publish')
    expect(html).toContain('Повышение роли требует approval владельца')
  })

  it('keeps the personal account page in canonical Vella HTML', () => {
    const html = read('public/vella-production.html')
    const profile = htmlSection(html, 'tab-settings-profile')

    expect(profile).toContain('Личный кабинет')
    expect(profile).toContain('Профиль')
    expect(profile).toContain('Доступы')
    expect(profile).toContain('Безопасность')
    expect(profile).toContain('Сессии')
    expect(profile).not.toContain('Интерфейс')
    expect(profile).not.toContain('Компактная таблица')
    expect(profile).toContain('settingsProfileName')
    expect(profile).toContain('settingsProfileTelegram')
    expect(profile).toContain('Открыть роли')
    expect(html).toContain('renderSettingsProfileAccess')
    expect(html).toContain('saveSettingsProfile')
    expect(profile).not.toContain('shadcn')
  })

  it('keeps marketplace-ops UX hardening in the production repricer shell', () => {
    const html = read('public/vella-production.html')
    const products = htmlSection(html, 'tab-products')

    expect(products).toContain('id="savedViewsPanel"')
    expect(html).toContain('const SAVED_VIEWS')
    expect(html).toContain("name:'SKU ниже порогов'")
    expect(html).toContain("name:'Без P_min'")
    expect(html).toContain("name:'Маржа ниже 0'")
    expect(html).toContain("name:'Изменения 24ч'")
    expect(html).toContain('id="m-saveView"')
    expect(html).toContain('SavedView')
    expect(html).toContain('filters:{ status:')
    expect(html).toContain("sort:[{ key:")
    expect(html).toContain('columns:{ visible:')
    expect(html).toContain("period:'7'")
    expect(html).toContain('currentSavedViewSnapshot')
    expect(html).toContain('SAVED_VIEWS.push(view)')
    expect(html).toContain('function productMatchesSearch')
    expect(html).toContain('function productIdentityMatchesSearch')
    expect(html).toContain('__vellaHandleProductsSearchInput')
    expect(html).toContain('p.nmId')

    expect(html).toContain('id="m-bulkPmin"')
    expect(html).toContain('id="bulkPminRows"')
    expect(html).toContain('id="m-bulkStrategy"')
    expect(html).toContain('renderBulkStrategyPreview')
    expect(html).toContain('ручная проверка')
    expect(html).toContain('затронутые SKU')

    expect(html).toContain('worklist-filter')
    expect(html).toContain('на проверку')
    expect(html).toContain('ожидает данных')
    expect(html).toContain('заблокировано защитой')
    expect(html).toContain('в работе')
    expect(html).toContain('Почему в очереди')
    expect(html).toContain('Статус правила')

    expect(html).toContain('Основание решения')
    expect(html).toContain('Сигнал')
    expect(html).toContain('Данные')
    expect(html).toContain('Защита')

    expect(html).toContain('Состав события')
    expect(html).toContain('кто, когда, действие, объект, изменение, причина и охват')
    expect(html).toContain('audit-diff')
    expect(html).toContain('audit-scope')

    expect(html).toContain('id="m-shortcuts"')
    expect(html).toContain('Alt + 1...6')
  })

  it('keeps strategy CRUD honest in the production shell', () => {
    const html = read('public/vella-production.html')
    const templates = htmlSection(html, 'tab-templates')

    expect(html).not.toContain('Скопировать с')
    expect(html).not.toContain('Скопируйте параметры существующей стратегии')
    expect(templates).toContain('id="strategyGrid"')
    expect(html).toContain('const STRATEGIES')
    expect(html).toContain('function renderStrategies')
    expect(html).toContain('function openCreateStrategy')
    expect(html).toContain('function openEditStrategy')
    expect(html).toContain('id="bulkStrategyMenuItems"')
    expect(html).toContain('data-bulk-strategy')
    expect(html).toContain('Редактировать стратегию')
    expect(html).toContain('strategySaveBtn')
    expect(html).not.toContain('00:00–04:00')
    expect(html).toContain('23:00–06:00 МСК')
    expect(html).toContain("nightWindow:'23:00–06:00 МСК'")
  })

  it('keeps the 8 May reports decisions visible in Vella UI', () => {
    const html = read('public/vella-production.html')
    const staticPage = read('src/features/vella-static/VellaStaticPage.tsx')
    const digest = htmlSection(html, 'tab-digest')
    const pnl = htmlSection(html, 'tab-pnl')

    expect(digest).not.toContain('Оперативный монитор')
    expect(digest).not.toContain('Темп плана')
    expect(digest).not.toContain('Операционные данные свежие')
    expect(digest).not.toContain('Состояние данных')
    expect(digest).not.toContain('Детализация')
    expect(digest).not.toContain('Статусы ниже порогов')
    expect(digest).not.toContain('Риски периода')
    expect(digest).toContain('Баланс за неделю')
    expect(digest).toContain('Критичные события и очередь действий')
    expect(digest).toContain('Выполнение плана')
    expect(digest).toContain('Периоды')
    expect(digest).toContain('Марж. прибыль')
    expect(html).toContain('DIGEST_BALANCE_BASE_DAYS')
    expect(html).toContain('setDigestBalanceScenario')
    expect(digest).not.toContain('Воронка WB: путь от показа до денег')
    expect(digest).toContain('План-факт маржинальной прибыли')
    expect(html).toContain('DIGEST_PLANFACT_BASE_ROWS')
    expect(html).toContain('setDigestPlanFactScenario')
    expect(html).toContain('Планы менеджеров')
    expect(html).toContain('нет SKU mapping')
    expect(html).toContain('Бренды: Все')
    expect(digest).not.toContain('Новинки')
    expect(digest).not.toContain('Проблемные SKU')
    expect(html).toContain('Фильтр обновляет KPI')
    expect(html).toContain('data-report-open-sku')
    expect(html).toContain('openSkuRnpFromDrawer')
    expect(html).toContain('РНП по SKU')
    expect(html).toContain('function resolveInitialSku')
    expect(html).toContain("/^\\/wb\\/repricer\\/sku\\/([^/]+)$/")
    expect(html).toContain('resolveDrawerProduct')
    expect(html).toContain('cloneReportRowAsDrawerProduct')
    expect(staticPage).toContain("else if (searchParams.get('sku')) query.set('sku', searchParams.get('sku') || '')")
    expect(pnl).toContain('Себестоимость')
    expect(pnl).not.toContain('COGS')
    expect(pnl).not.toContain('ждём Максима')
    expect(pnl).not.toContain('ожидаем подтверждения')
    expect(pnl).not.toContain('Финансовая логика')
    expect(html).toContain('остаток WB + от клиента')
    expect(html).toContain('был ОС')
  })

  it('keeps repricer price bounds tied to the SPP basis contract', () => {
    const html = read('public/vella-production.html')

    expect(html).toContain('priceBoundsBasis')
    expect(html).toContain('before_spp')
    expect(html).toContain('after_spp')
    expect(html).toContain('drawerPminBeforeInput')
    expect(html).toContain('drawerPminAfterInput')
    expect(html).toContain('Маржа считается от цены после СПП')
  })

  it('keeps report copy threshold-based instead of interpretive', () => {
    const html = read('public/vella-production.html')
    const sourceFiles = [
      { path: 'public/vella-production.html', text: html },
      ...readSourceFiles('src/features/wb-reports'),
    ]
    const forbidden = [
      'Корзины и показы есть',
      'просели',
      'следим',
      'динамика нормальная',
      'разобрать маржу',
      'требуют приоритета',
      'поднять цену',
      'в ликвидацию',
      'стоп РК',
      'Склады требуют',
      'Причина: логистика',
      'предварительная рекомендация',
      'предварительных рекомендаций',
      'требует внимания',
      'требуют разбора',
      'требует разбора',
      'ручной разбор',
      'в РНП',
      'пополнить WB',
      'плавное снижение',
      'оставить рекламу',
      'Проверить, почему рост корзин',
      'Нужен разбор логистики',
      'не усиливать рекламу',
    ]
    const offenders = sourceFiles.flatMap(({ path, text }) =>
      forbidden.filter((phrase) => text.toLocaleLowerCase('ru-RU').includes(phrase.toLocaleLowerCase('ru-RU'))).map((phrase) => `${path}: ${phrase}`),
    )

    expect(offenders).toEqual([])
    expect(html).toContain('Заказы или выкуп ниже порога выбранного профиля')
    // Both the encoded comparison and the current plain-language wording
    // express the same threshold; do not require one obsolete spelling.
    expect(html).toMatch(/[Дд]ней до OOS (?:&lt;|ниже) порога/)
    expect(html).toMatch(/[Лл]огистика и ДРР выше порогов(?: профиля)?/)
    expect(html).toContain('кандидат · черновик')
    expect(html).toContain('Статус правила')
    expect(html).toContain('Основание')
  })

  it('renders the stabilized 8 May report decisions after Vella JS initialization', async () => {
    const browser = await chromium.launch({ headless: true })
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1512, height: 982 } })
    await page.route(/^https?:\/\//, route => route.abort())
    page.setDefaultTimeout(5_000)
    const source = pathToFileURL(join(root, 'public/vella-production.html')).toString()

    try {
      const cases = [
        { query: 'tab=digest&mode=now', expected: ['Баланс за неделю', 'Критичные события и очередь действий', 'Выполнение плана', 'Периоды'], absent: ['INDEEPA', 'Свежесть данных', 'Baseline 30 дней', 'Список товаров', 'Оперативный день', 'Детализация', 'Статусы ниже порогов', 'Оперативный монитор', 'Последние пересчёты'] },
        { query: 'tab=digest&mode=period', expected: ['Бренды: Все', 'План-факт маржинальной прибыли', 'Выручка', 'Маржа'], absent: ['Воронка WB: путь от показа до денег', 'Состояние данных', 'Оперативный день', 'Детализация', 'Статусы ниже порогов', 'Критичные события и очередь действий', 'Оперативный монитор'] },
        { query: 'tab=abc', expected: ['ABC-анализ', 'Себестоимость', 'Чистая прибыль по статусам', 'Порог фиксирован: 20/30/50', '+12% к периоду'], absent: ['COGS', 'ОБЦ'] },
        { query: 'tab=rnp', expected: ['Ниже порога', 'Позиция', 'Комментарий', 'Правило', 'Добавить'], absent: ['Склад Казань', 'Комментарии SKU', 'Логи действий'] },
        { query: 'tab=pnl', expected: ['Налог', 'Себестоимость', 'Позиция', 'Комментарий'], absent: ['Финансовая логика ждёт подтверждения', 'ждёт подтверждения', 'COGS', 'ждём Максима', 'placeholder'] },
        { query: 'tab=ads', expected: ['Все менеджеры', 'Все товары', 'РК', 'Тип РК', 'Позиция', 'Комментарий', 'детализация до ключевых фраз'], absent: ['Поиск · Футболка', 'draft', 'API discovery'] },
        { query: 'tab=stock', expected: ['Остаток WB', 'От клиента', 'К клиенту', 'Доступно', 'Средний КТР', 'Позиция', 'Комментарий'], absent: ['Мария подтвердила', 'по таблице локализации Марии'] },
        { query: 'tab=week', expected: ['SKU ниже порогов', 'был ОС', 'Наличие 7 дней', 'Позиция', 'Комментарий'], absent: ['Неделя-к-неделе: включаемые метрики'] },
        { query: 'tab=settings-profile', expected: ['Личный кабинет', 'Мария Ф.', 'maria@ogni.example', 'Безопасность', 'Сессии', 'Интерфейс'], absent: ['shadcn'] },
        { query: 'tab=settings-access', expected: ['Команда и доступы', 'Пользователи', 'Мария Ф.', 'Максим Б.', 'Руководитель ecom', 'Статус'], absent: ['shadcn', 'Логика распределения', 'Матрица ролей', 'WB scope', 'Avito scope'] },
      ]

      for (const scenario of cases) {
        await page.goto(`${source}?${scenario.query}`, { waitUntil: 'domcontentloaded' })
        await page.waitForTimeout(1200)
        const text = await page.locator('body').innerText()
        const normalized = text.toLocaleLowerCase('ru-RU')
        for (const expected of scenario.expected) expect(normalized).toContain(expected.toLocaleLowerCase('ru-RU'))
        if (scenario.query === 'tab=pnl') expect((await page.locator('#tab-pnl thead').innerText()).toLocaleLowerCase('ru-RU')).toContain('налог')
        for (const absent of scenario.absent) expect(normalized).not.toContain(absent.toLocaleLowerCase('ru-RU'))
        for (const forbidden of ['Корзины и показы есть', 'просели', 'следим', 'динамика нормальная', 'разобрать маржу', 'требуют приоритета', 'поднять цену', 'в ликвидацию', 'стоп РК']) {
          expect(normalized).not.toContain(forbidden.toLocaleLowerCase('ru-RU'))
        }
      }

      await page.goto(`${source}?tab=digest&mode=now`, { waitUntil: 'domcontentloaded' })
      await page.waitForTimeout(1200)
      expect(await page.locator('#subtabsContext').innerText()).toContain('Сегодня · обновлено')
      expect(await page.locator('#globalPeriod').isVisible()).toBe(true)
      expect(await page.locator('#ddExport').isVisible()).toBe(true)
      expect(await page.locator('#subtabsContext').innerText()).toContain('Бренды: Все')
      expect(await page.locator('#tab-digest').innerText()).not.toContain('Новинки')
      expect(await page.locator('#tab-digest').innerText()).not.toContain('Проблемные SKU')
      await page.locator('#digestBrandBtn').click()
      expect(await page.locator('#digestBrandFilter .brand-menu').isVisible()).toBe(true)
      await page.locator('#digestBrandFilter [data-brand-option="anomie"]').click()
      expect(await page.locator('#digestBrandBtn').innerText()).toContain('Бренд: Anomie')
      const balanceBox = await page.locator('#tab-digest .balance-chart-card').boundingBox()
      const criticalBox = await page.locator('#tab-digest [data-digest-mode="now"] .report-card-title', { hasText: 'Критичные события и очередь действий' }).boundingBox()
      const managerBox = await page.locator('#tab-digest [data-digest-mode="now"] .manager-signal-panel').boundingBox()
      const periodBox = await page.locator('#tab-digest [data-digest-mode="now"] .digest-period-panel').boundingBox()
      const lowerGrid = await page.locator('#tab-digest [data-digest-mode="now"] .monitor-lower-grid').boundingBox()
      expect(await page.locator('#tab-digest [data-digest-mode="now"] .report-card-title', { hasText: 'План-факт маржинальной прибыли' }).count()).toBe(0)
      expect(await page.locator('#tab-digest [data-digest-mode="now"] #digestManagerSignalGrid .manager-signal-card').count()).toBe(4)
      expect(await page.locator('#tab-digest [data-digest-mode="now"] #digestManagerSignalGrid .manager-signal-card').first().innerText()).toContain('Компания')
      expect(await page.locator('#tab-digest [data-digest-mode="now"] #digestManagerSignalGrid').innerText()).toContain('Факт')
      expect(await page.locator('#tab-digest [data-digest-mode="now"] #digestManagerSignalGrid').innerText()).toContain('Прогноз')
      expect(await page.locator('#tab-digest [data-digest-mode="now"] .digest-period-card').count()).toBe(4)
      expect(await page.locator('#tab-digest [data-digest-mode="now"] .digest-period-panel').innerText()).toContain('Пред. 30 дней')
      expect(await page.locator('#tab-digest [data-digest-mode="now"] .digest-period-panel').innerText()).toContain('Марж. прибыль')
      expect(await page.locator('#tab-digest [data-digest-mode="now"]').innerText()).not.toContain('Оперативный монитор')
      expect(await page.locator('#tab-digest [data-digest-mode="now"]').innerText()).not.toContain('Последние пересчёты')
      expect(managerBox?.y ?? Number.POSITIVE_INFINITY).toBeLessThan(periodBox?.y ?? 0)
      expect(periodBox?.y ?? Number.POSITIVE_INFINITY).toBeLessThan(lowerGrid?.y ?? 0)
      expect(Math.abs((balanceBox?.y ?? 0) - (criticalBox?.y ?? 0))).toBeLessThan(32)
      expect(Math.abs((balanceBox?.x ?? 0) - (lowerGrid?.x ?? 0))).toBeLessThan(1)
      expect((criticalBox?.x ?? 0)).toBeGreaterThan((balanceBox?.x ?? 0) + (balanceBox?.width ?? 0))
      expect(Math.abs((lowerGrid?.x ?? 0) - (managerBox?.x ?? 0))).toBeLessThan(1)
      expect(Math.abs((lowerGrid?.width ?? 0) - (managerBox?.width ?? 0))).toBeLessThan(1)
      expect(Math.abs((lowerGrid?.x ?? 0) - (periodBox?.x ?? 0))).toBeLessThan(1)
      expect(Math.abs((lowerGrid?.width ?? 0) - (periodBox?.width ?? 0))).toBeLessThan(1)
      expect(await page.locator('#tab-digest [data-digest-mode="now"] .balance-chart-card .chart-hit-zone').count()).toBe(7)
      await page.locator('#tab-digest [data-digest-mode="now"] .balance-chart-card .chart-hit-zone').first().hover()
      await page.locator('#g-tip.show').waitFor({ timeout: 2_000 })
      const firstTip = await page.locator('#g-tip').innerText()
      expect(firstTip).toContain('02.05.2026')
      expect(firstTip).toContain('Выкуп')
      await page.locator('#tab-digest [data-digest-mode="now"] .balance-chart-card .chart-hit-zone').nth(4).hover()
      await page.locator('#g-tip.show').waitFor({ timeout: 2_000 })
      const tip = await page.locator('#g-tip').innerText()
      expect(tip).toContain('06.05.2026')
      expect(tip).toContain('Продажи')
      expect(tip).toContain('Заказы')
      expect(tip).toContain('Возвраты')

      await page.setViewportSize({ width: 1280, height: 900 })
      await page.goto(`${source}?tab=digest&mode=now`, { waitUntil: 'domcontentloaded' })
      await page.waitForTimeout(1200)
      const narrowPlanBox = await page.locator('#tab-digest [data-digest-mode="now"] .manager-signal-panel').boundingBox()
      const narrowPeriodBox = await page.locator('#tab-digest [data-digest-mode="now"] .digest-period-panel').boundingBox()
      expect(narrowPlanBox?.y ?? Number.POSITIVE_INFINITY).toBeLessThan(900)
      expect(narrowPeriodBox?.y ?? Number.POSITIVE_INFINITY).toBeLessThan(900)
      expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false)

      await page.goto(`${source}?tab=digest&mode=period`, { waitUntil: 'domcontentloaded' })
      await page.waitForTimeout(1200)
      expect(await page.locator('#globalPeriod').isVisible()).toBe(true)
      expect(await page.locator('#ddExport').isVisible()).toBe(true)
      const periodText = await page.locator('#tab-digest [data-digest-mode="period"]').innerText()
      expect(periodText).not.toContain('Прогноз выполнения плана')
      expect(periodText).not.toContain('Детализация')
      expect(periodText).not.toContain('Статусы ниже порогов')
      expect(periodText).not.toContain('Риски периода')
      expect(periodText).not.toContain('ABC-анализ')
      expect(periodText).not.toContain('РНП и неделя')
      expect(periodText).toContain('Нужно в день')
      expect(periodText).toContain('План')
      expect(periodText).toContain('Прогноз')

      await page.goto(`${source}?tab=products`, { waitUntil: 'domcontentloaded' })
      await page.waitForTimeout(1200)
      expect(await page.locator('#subtabsContext').isVisible()).toBe(true)
      expect(await page.locator('#ddExport').isVisible()).toBe(true)
      expect(await page.locator('#globalPeriod').isVisible()).toBe(true)

      await page.goto(`${source}?tab=templates`, { waitUntil: 'domcontentloaded' })
      await page.waitForTimeout(1200)
      expect(await page.locator('#subtabsContext').isVisible()).toBe(false)
      expect(await page.locator('#ddExport').isVisible()).toBe(false)

      for (const tab of ['algo', 'liq', 'promos']) {
        await page.goto(`${source}?tab=${tab}`, { waitUntil: 'domcontentloaded' })
        await page.waitForTimeout(800)
        expect(await page.locator('#subtabsContext').isVisible()).toBe(false)
        expect(await page.locator('#globalPeriod').isVisible()).toBe(false)
        expect(await page.locator('#ddExport').isVisible()).toBe(false)
      }

      await page.goto(`${source}?tab=rnp`, { waitUntil: 'domcontentloaded' })
      await page.waitForTimeout(1200)
      // RNP demo row generation is retired. The active backend table renders
      // source explanations (covered by rnpCacheBrowser), not a demo history action.
      expect(await page.locator('#tab-rnp tr[data-report-row]').count()).toBe(0)
      expect(await page.locator('#tab-rnp .report-comment-btn').count()).toBe(0)

      await page.setViewportSize({ width: 375, height: 812 })
      await page.goto(`${source}?tab=digest&mode=period`, { waitUntil: 'domcontentloaded' })
      await page.waitForTimeout(1500)
      expect(await page.locator('.desktop-only-fallback').innerText()).toContain('Отчёты доступны в desktop-версии')
    } finally {
      await browser.close()
    }
  }, 45_000)

  it('filters report tables by chip, search, and manager in the static Vella shell', async () => {
    const browser = await chromium.launch({ headless: true })
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 980, height: 574 } })
    await page.route(/^https?:\/\//, route => route.abort())
    const source = pathToFileURL(join(root, 'public/vella-production.html')).toString()

    const visibleRows = (tab: string) => page.locator(`#tab-${tab} tbody tr[data-report-row]:visible`).count()
    // Populate only the test DOM: the production demo renderer stays retired.
    // Event wiring/filtering remain real; identity, thresholds and owners are explicit.
    const supplyRows = async (tab: string) => {
      await page.waitForFunction((tabId) => {
        const surface = document.getElementById(`tab-${tabId}`)
        return surface?.querySelector<HTMLElement>('.chips .chip')?.dataset.demoChipBound === '1'
          && surface?.querySelector<HTMLElement>('.search input')?.dataset.demoSearchBound === '1'
      }, tab)
      return page.evaluate((tabId) => {
        const surface = document.getElementById(`tab-${tabId}`)!
        const body = surface.querySelector('tbody')!
        body.replaceChildren()
        for (let index = 0; index < 12; index++) {
          const row = document.createElement('tr')
          const warehouse = index === 2 ? 'Екатеринбург' : 'Казань'
          Object.assign(row.dataset, {
            reportRow: tabId, search: `Synthetic ${index < 2 ? 'FBBT_42' : 'OTHER'} ${warehouse} ${index}`,
            reportTags: tabId === 'pnl' ? 'операционный' : '',
            manager: index % 2 === 0 ? 'МД' : 'АП',
            daysToOos: index === 0 ? '6' : index === 1 ? '61' : '20',
            availableUnits: '10', ktr: index === 2 ? '1.6' : '1', warehouse,
          })
          const cell = document.createElement('td')
          cell.textContent = row.dataset.search!
          row.appendChild(cell)
          body.appendChild(row)
        }
        ;(window as any).applyGenericReportFilter(surface)
      }, tab)
    }

    try {
      await page.goto(`${source}?tab=stock`, { waitUntil: 'domcontentloaded' })
      expect(await page.locator('#tab-stock tr[data-report-row]').count()).toBe(0)
      await supplyRows('stock')
      await page.waitForSelector('#tab-stock tbody tr[data-report-row]')
      const reportsMenu = await page.locator('[data-nav-group="reports"]').evaluate((group) => {
        const week = group.querySelector('[data-tab="week"]')
        const rules = group.querySelector('[data-tab="report-rules"]')
        const groupRect = group.getBoundingClientRect()
        const weekRect = week?.getBoundingClientRect()
        const rulesRect = rules?.getBoundingClientRect()
        return {
          text: group.textContent || '',
          weekInside: !!weekRect && weekRect.bottom <= groupRect.bottom + 1,
          rulesInside: !!rulesRect && rulesRect.bottom <= groupRect.bottom + 1,
        }
      })
      expect(reportsMenu.text).toContain('Неделя-к-неделе')
      expect(reportsMenu.text).toContain('Правила')
      expect(reportsMenu.weekInside).toBe(true)
      expect(reportsMenu.rulesInside).toBe(true)

      const stockAll = await visibleRows('stock')
      expect(stockAll).toBe(12)

      await page.locator('#tab-stock .chip', { hasText: 'OOS риск' }).click()
      const stockOos = await visibleRows('stock')
      expect(stockOos).toBeGreaterThan(0)
      expect(stockOos).toBeLessThan(stockAll)
      expect(await page.locator('#tab-stock tbody tr[data-report-row]:visible').evaluateAll((rows) =>
        rows.every((row) => Number((row as HTMLElement).dataset.daysToOos) < 7),
      )).toBe(true)

      await page.locator('#tab-stock .chip', { hasText: 'Избыток' }).click()
      const stockExcess = await visibleRows('stock')
      expect(stockExcess).toBeGreaterThan(0)
      expect(stockExcess).toBeLessThan(stockAll)

      await page.locator('#tab-stock .chip', { hasText: 'КТР высокий' }).click()
      const stockHighKtr = await visibleRows('stock')
      expect(stockHighKtr).toBeGreaterThan(0)
      expect(await page.locator('#tab-stock tbody tr[data-report-row]:visible').evaluateAll((rows) =>
        rows.every((row) => Number((row as HTMLElement).dataset.ktr) >= 1.5),
      )).toBe(true)

      await page.locator('#tab-stock .search input').fill('Екатеринбург')
      expect(await visibleRows('stock')).toBe(1)
      await page.locator('#tab-stock select.adv-select').selectOption('СВ')
      expect(await visibleRows('stock')).toBe(0)
      expect(await page.locator('#tab-stock [data-report-empty]').isVisible()).toBe(true)
      await page.locator('#tab-stock [data-report-empty] button', { hasText: 'Сбросить фильтры' }).click()
      expect(await visibleRows('stock')).toBe(stockAll)

      for (const tab of ['rnp', 'pnl', 'ads', 'week']) {
        await page.goto(`${source}?tab=${tab}`, { waitUntil: 'domcontentloaded' })
        await page.waitForSelector(`#tab-${tab}.active`)
        await supplyRows(tab)
        await page.waitForFunction((tabId) => document.querySelectorAll(`#tab-${tabId} tbody tr[data-report-row]`).length > 0, tab)
        const total = await visibleRows(tab)
        expect(total, tab).toBe(12)
        await page.locator(`#tab-${tab} .search input`).fill('FBBT_42')
        expect(await visibleRows(tab)).toBe(2)
        await page.locator(`#tab-${tab} .search input`).fill('')
        await page.locator(`#tab-${tab} select.adv-select`).first().selectOption('МД')
        expect(await visibleRows(tab)).toBe(6)
        const managerRows = await page.locator(`#tab-${tab} tbody tr[data-report-row]:visible`).evaluateAll((rows) =>
          rows.every((row) => (row as HTMLElement).dataset.manager === 'МД'),
        )
        expect(managerRows).toBe(true)
      }
    } finally {
      await browser.close()
    }
  }, 45_000)

  it('scales digest plan-fact managers across sparse, dense, and incomplete data states', async () => {
    const browser = await chromium.launch({ headless: true })
    const page = await browser.newPage({ viewport: { width: 1512, height: 982 } })
    const source = pathToFileURL(join(root, 'public/vella-production.html')).toString()

    try {
      await page.goto(`${source}?tab=digest&mode=period`, { waitUntil: 'domcontentloaded' })
      await page.waitForTimeout(1200)
      await page.evaluate(() => (window as any).setDigestMode('period', { silent: true, updateUrl: false }))

      const expectScenario = async (scenario: string, visibleCards: number, overflowText?: string) => {
        await page.evaluate((name) => (window as any).setDigestPlanFactScenario(name), scenario)
        expect(await page.locator('#digestPlanFactGrid .planfact-card').count()).toBe(visibleCards)
        if (overflowText) {
          expect(await page.locator('#digestPlanFactOverflow').innerText()).toContain(overflowText)
        } else {
          expect(await page.locator('#digestPlanFactOverflow').getAttribute('class')).not.toContain('visible')
        }
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)
        expect(overflow).toBe(false)
      }

      await expectScenario('zero', 1)
      expect(await page.locator('#digestPlanFactEmpty').innerText()).toContain('Планы менеджеров не заданы')
      await expectScenario('one', 2)
      await expectScenario('three', 4)
      await expectScenario('four', 5)
      await expectScenario('eight', 9)
      await expectScenario('twelve', 9, 'Все менеджеры · 12')

      await page.locator('#digestPlanFactOverflow button', { hasText: 'Все менеджеры' }).click()
      expect(await page.locator('#m-planfactManagers.open').isVisible()).toBe(true)
      expect(await page.locator('#m-planfactManagers [data-planfact-modal-row]').count()).toBe(13)
      await page.locator('#m-planfactManagers button', { hasText: 'Закрыть' }).click()

      await page.evaluate(() => (window as any).setDigestPlanFactScenario('missing'))
      const digestText = await page.locator('#tab-digest').innerText()
      expect(digestText).toContain('нет плана')
      expect(digestText).toContain('нет факта')
      expect(digestText).toContain('нет SKU mapping')
      expect(digestText).toContain('факт устарел')
      expect(await page.locator('#digestManagerSignalGrid .manager-signal-card').count()).toBe(4)

      await page.setViewportSize({ width: 1280, height: 900 })
      await page.evaluate(() => (window as any).setDigestPlanFactScenario('twelve'))
      await page.waitForTimeout(300)
      const narrowOverflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)
      expect(narrowOverflow).toBe(false)
      expect(await page.locator('#digestPlanFactGrid .planfact-card').count()).toBe(9)
      expect(await page.locator('#digestManagerSignalOverflow').innerText()).toContain('Все менеджеры · 12')
    } finally {
      await browser.close()
    }
  }, 45_000)

  it('renders legacy digest balance scenarios without retired local period controls', async () => {
    const browser = await chromium.launch({ headless: true })
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1512, height: 982 } })
    await page.route(/^https?:\/\//, route => route.abort())
    const source = pathToFileURL(join(root, 'public/vella-production.html')).toString()

    try {
      await page.goto(`${source}?tab=digest`, { waitUntil: 'domcontentloaded' })
      await page.waitForTimeout(1200)
      expect(await page.locator('[data-digest-balance-period]').count()).toBe(0)

      const expectBalance = async (scenario: string, hitZones: number, emptyVisible = false) => {
        await page.evaluate((name) => (window as any).setDigestBalanceScenario(name), scenario)
        expect(await page.locator('#digestBalanceChart .chart-hit-zone').count()).toBe(hitZones)
        expect(await page.locator('#digestBalanceEmpty').getAttribute('class')).toContain(emptyVisible ? 'visible' : 'balance-empty')
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)
        expect(overflow).toBe(false)
      }

      await expectBalance('empty', 0, true)
      await expectBalance('one', 1)
      expect(await page.locator('#digestBalanceTitle').innerText()).toBe('Баланс за день')
      await expectBalance('seven', 7)
      expect(await page.locator('#digestBalanceTitle').innerText()).toBe('Баланс за неделю')
      await expectBalance('fourteen', 14)
      expect(await page.locator('#digestBalanceTitle').innerText()).toBe('Баланс за период')
      await expectBalance('thirty', 5)
      expect(await page.locator('#digestBalanceChart').evaluate((node) => node.textContent || '')).toContain('НЕД')
      await page.evaluate(() => (window as any).setDigestBalanceScenario('fourteen'))
      expect(await page.locator('#digestBalanceChart .chart-hit-zone').count()).toBe(14)
      expect(await page.locator('#digestBalanceTitle').innerText()).toBe('Баланс за период')

      await page.locator('#digestBalanceChart .chart-hit-zone').first().hover()
      await page.locator('#g-tip.show').waitFor({ timeout: 2_000 })
      const tip = await page.locator('#g-tip').innerText()
      expect(tip).toContain('Продажи')
      expect(tip).toContain('Заказы')
      expect(tip).toContain('Возвраты')

      await page.setViewportSize({ width: 1280, height: 900 })
      await page.evaluate(() => (window as any).setDigestBalanceScenario('fourteen'))
      await page.waitForTimeout(300)
      expect(await page.locator('#digestBalanceChart .chart-hit-zone').count()).toBe(14)
      const narrowOverflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)
      expect(narrowOverflow).toBe(false)
    } finally {
      await browser.close()
    }
  }, 30_000)

  it('supports create and edit flows for strategy cards in the HTML shell', async () => {
    const browser = await chromium.launch({ headless: true })
    const page = await browser.newPage({ viewport: { width: 1512, height: 982 } })
    const source = pathToFileURL(join(root, 'public/vella-production.html')).toString()
    await page.route('**/*', route => {
      // This is a local presentation-state test, never a backend price action.
      if (route.request().isNavigationRequest() && route.request().url() === `${source}?tab=templates`) return route.continue()
      return route.abort()
    })

    try {
      await page.goto(`${source}?tab=templates`, { waitUntil: 'domcontentloaded' })
      await page.waitForTimeout(800)
      expect(await page.locator('body').innerText()).not.toContain('Скопировать с')

      await page.locator('#tab-templates button').filter({ hasText: 'Создать стратегию' }).click()
      expect(await page.locator('#m-createTpl.open #strategyModalTitle').innerText()).toBe('Создать стратегию')
      expect(await page.locator('#strategySaveBtn').isDisabled()).toBe(true)

      await page.locator('#strategyName').fill('Балансный')
      // Negative steps are invalid. Seven is accepted by current backend
      // contracts; this local form test must not invent a universal 6% ceiling.
      await page.locator('#strategyStep').fill('-1')
      expect(await page.locator('#strategySaveBtn').isDisabled()).toBe(true)
      await page.locator('#strategyStep').fill('4')
      await page.locator('#strategyBasketsDown').fill('45')
      expect(await page.locator('#strategySaveBtn').isDisabled()).toBe(true)
      await page.locator('#strategyBasketsUp').fill('50')
      expect(await page.locator('#strategySaveBtn').isEnabled()).toBe(true)
      await page.locator('#strategySaveBtn').click()

      expect(await page.locator('[data-strategy-card]').filter({ hasText: 'Балансный' }).isVisible()).toBe(true)
      expect(await page.locator('[data-bulk-strategy="Балансный"]').count()).toBe(1)

      await page.locator('[data-strategy-card="aggr"]').click()
      expect(await page.locator('#m-createTpl.open #strategyModalTitle').innerText()).toBe('Редактировать стратегию')
      await page.locator('#strategyMargin').fill('22')
      await page.locator('#strategySaveBtn').click()
      expect(await page.locator('[data-strategy-card="aggr"]').innerText()).toContain('22%')
    } finally {
      await browser.close()
    }
  }, 20_000)

  // Legacy Reviews drawer/guard/Escape/settings parity moved, without dropping
  // those assertions, to legacyReviewsBrowser.test.ts with explicit synthetic
  // rows and an initial empty-shell proof. Runtime REVIEWS must remain empty.

  it('keeps QA hardening for mobile fallback, period validation, and modal stacking', async () => {
    const browser = await chromium.launch({ headless: true })
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 375, height: 812 } })
    await page.route(/^https?:\/\//, route => route.abort())
    const source = pathToFileURL(join(root, 'public/vella-production.html')).toString()

    try {
      const mobileCases = [
        { tab: 'products', text: 'Репрайсер доступен в desktop-версии' },
        { tab: 'abc', text: 'ABC-анализ доступен в desktop-версии' },
        { tab: 'pnl', text: 'P&L доступен в desktop-версии' },
        { tab: 'promos', text: 'Акции WB доступны в desktop-версии' },
        { tab: 'liq', text: 'Ликвидация доступна в desktop-версии' },
        { tab: 'reviews', text: 'Отзывы WB доступны в desktop-версии' },
      ]

      for (const scenario of mobileCases) {
        await page.goto(`${source}?tab=${scenario.tab}`, { waitUntil: 'domcontentloaded' })
        await page.waitForTimeout(1000)
        expect(await page.locator('.desktop-only-fallback').isVisible()).toBe(true)
        expect(await page.locator('.desktop-only-fallback').innerText()).toContain(scenario.text)
      }

      await page.setViewportSize({ width: 1512, height: 982 })
      await page.goto(`${source}?tab=week`, { waitUntil: 'domcontentloaded' })
      await page.waitForTimeout(1200)
      const period = page.locator('#globalPeriodRange')
      const before = await period.inputValue()
      await period.evaluate((input) => {
        const el = input as HTMLInputElement
        el.value = '31.02.2026'
        el.dispatchEvent(new Event('change', { bubbles: true }))
      })
      expect(await period.inputValue()).toBe(before)

      await page.evaluate(() => {
        ;(window as unknown as { openModal: (name: string) => void }).openModal('problemQueue')
        ;(window as unknown as { openModal: (name: string) => void }).openModal('thresholdPreview')
      })
      expect(await page.locator('.modal-overlay.open').count()).toBe(1)
      expect(await page.locator('#m-thresholdPreview').getAttribute('class')).toContain('open')
    } finally {
      await browser.close()
    }
  }, 30_000)

  it('keeps WB and Avito picking sheets flat and source-scoped', () => {
    const app = read('src/App.tsx')
    const html = read('public/vella-production.html')
    const orders = htmlSection(html, 'tab-orders-print')

    expect(app).toContain('<Route path="/orders" element={<OrdersPrintListPage />} />')
    expect(app).toContain('<Route path="/orders/archive" element={<VellaHtmlParityPage />} />')
    expect(app).toContain('<Route path="/avito/orders" element={<VellaHtmlParityPage />} />')
    expect(app).toContain('<Route path="/avito/orders/archive" element={<VellaHtmlParityPage />} />')
    expect(orders).toContain('id="ordersPickingRows"')
    expect(orders).toContain('class="orders-picking-table"')
    expect(orders).toContain('orders-mode-tabs')
    expect(orders).toContain('ordersModeCurrentTab')
    expect(orders).toContain('ordersModeArchiveTab')
    expect(orders).toContain('ordersArchiveDateInput')
    expect(orders).toContain('ordersArchiveDateTrigger')
    expect(orders).toContain('ordersCalendarPopover')
    expect(orders).toContain('ordersArchiveDateText')
    expect(orders).toContain('ordersDeliveryState')
    expect(orders).toContain('PDF отправлен')
    expect(orders).toContain('Повторить отправку')
    expect(orders).toContain('ordersManualAddButton')
    expect(orders).toContain('Добавить заказ')
    expect(orders).toContain('Архив')
    expect(orders).toContain('Редактирование')
    expect(orders.indexOf('Наименование')).toBeLessThan(orders.indexOf('Кол-во'))
    expect(orders.indexOf('Кол-во')).toBeLessThan(orders.indexOf('Размер'))
    expect(html).toContain('orders-drawer-photo-card')
    expect(html).toContain('ordersDrawerPhoto')
    expect(html).toContain('ordersDrawerPhotoTitle')
    expect(html).toContain('orders-sticker-actions')
    expect(html).toContain("printOrdersDrawerSticker('120x75')")
    expect(html).toContain("printOrdersDrawerSticker('58x40')")
    expect(html).toContain('ordersStickerPrintArea')
    expect(html).toContain('print-orders-sticker-mode')
    expect(html).toContain('ordersStickerPrintMarkup')
    expect(orders).toContain('Номер')
    expect(orders).toContain('№ задания')
    expect(orders).toContain('QR/Barcode Авито')
    expect(orders).toContain('Возврат')
    expect(orders).toContain('ordersReturnsSyncControl')
    expect(orders).not.toContain('orders-print-group')
    expect(html).toContain('ORDERS_PRINT_FALLBACK')
    expect(html).toContain('PickingListRow')
    expect(html).toContain('hydrateOrdersPrintList')
    expect(html).toContain("'/orders': 'orders-print'")
    expect(html).toContain("'/orders/archive': 'orders-print'")
    expect(html).toContain("'/avito/orders': 'orders-avito'")
    expect(html).toContain("'/avito/orders/archive': 'orders-avito'")
    expect(html).toContain('/api/v1/avito/orders?dateFrom=')
    expect(html).toContain('/api/v1/avito/orders/returns-sync')
    expect(html).toContain('/api/v1/avito/orders/returns-sync/run')
    expect(html).toContain('/api/v1/production/print-list')
    expect(html).toContain("'?source=' + encodeURIComponent(source)")
    expect(html).toContain('/api/v1/production/print-list/archive')
    expect(html).toContain('/api/v1/production/print-list/export.')
    expect(html).toContain('/api/v1/production/print-list/rows/')
    expect(html).toContain('/api/v1/production/print-list/rows')
    expect(html).toContain('ordersPickingOverrides:')
    expect(html).toContain('ordersPickingDelivery:')
    expect(html).toContain('ordersPickingManualRows:')
    expect(html).toContain('loadOrdersManualRows')
    expect(html).toContain('saveOrdersManualRows')
    expect(html).toContain('manualCreated: true')
    expect(html).toContain('manualCreatedAt')
    expect(html).toContain('comment')
    expect(html).toContain('openOrdersManualAddDrawer')
    expect(html).toContain('ordersManualDrawer')
    expect(html).toContain('ordersManualModeSingle')
    expect(html).toContain('ordersManualModeTable')
    expect(html).toContain("data-orders-manual-mode=\"single\"")
    expect(html).toContain("data-orders-manual-mode=\"table\"")
    expect(html).toContain('Добавление доступно только в текущий лист')
    expect(html).toContain('ordersCanManualCreate')
    expect(html).toContain('orders_manual_create')
    expect(html).toContain('Источник фиксируется текущей страницей')
    expect(html).toContain('openOrdersArchiveDay')
    expect(html).toContain('renderOrdersDeliveryState')
    expect(html).toContain('saveOrdersDeliveryOverride')
    expect(html).toContain('.orders-print-shell.is-archive')
    expect(html).toContain('function writeOrdersModeUrl(forceArchive)')
    expect(html).toContain("archive ? '/orders/archive' : '/orders'")
    expect(html).toContain("archive ? '/avito/orders/archive' : '/avito/orders'")
    expect(html).toContain("typeof options.archive === 'boolean' ? options.archive : isOrdersArchiveMode()")
    expect(html).toContain("source: 'wb'")
    expect(html).toContain("source: 'avito'")
    expect(html).toContain("const REPORT_TABS = ['digest', 'report-rules', 'abc', 'rnp', 'pnl', 'expenses', 'ads', 'stock', 'week']")
    expect(html).not.toContain('data-module="production" data-tab="orders-print"')
    expect(html).toContain('/api/v1/production/production-skus')
    expect(html).toContain('/api/v1/production/external-sku-mappings')
    expect(html).toContain('/api/v1/production/external-sku-mappings/import')
    expect(html).toContain('sellerArticle')
    expect(html).toContain('honestSign')
    expect(html).toContain('avitoInternalBarcodeQr')
    expect(html).toContain('reuseSuggestion')
    expect(html).toContain('В возврате есть такой же товар')
    expect(html).not.toContain('<th>Трекинг</th>')
    expect(html).not.toContain('tracking:')
    expect(html).not.toContain('Лист отправлен печатнику')
    expect(html).not.toContain('PDF печатнику')
    expect(html).not.toContain('Открыть день в архиве')
    expect(html).not.toContain('в сборке')
    expect(html).not.toContain('Сборка <small')
    expect(html).toContain('orders-picking-dash')
    expect(html).toContain('—')
    expect(html).toContain('startOrdersRowEdit')
    expect(html).toContain('saveOrdersRowEdit')
    expect(html).toContain('cancelOrdersRowEdit')
    expect(html).toContain('data-orders-edit-field')
    expect(html).toContain('orders-inline-input')
    expect(html).toContain('orders-inline-select')
    expect(html).toContain('productionSku')
    expect(html).toContain('avito_item_8098459618')
    expect(html).toContain('AV-QR-8098459618')
    expect(html).toContain('1085_Фбbt')
    expect(html).toContain("data-orders-marketplace")
    expect(html).toContain('data-orders-source')
    expect(html).toContain('data-orders-mapping')
    expect(html).toContain('saveOrdersSkuMapping')
    expect(html).toContain('importOrdersMappingFile')
    expect(html).toContain('ordersMappingImportInput')
    expect(html).toContain('orders-map-control')
    expect(html).toContain('auto_mapped')
    expect(html).toContain('listing_catalog')
    expect(html).not.toContain("status: 'needs_mapping'")
    expect(html).not.toContain('Нужен маппинг SKU')
    expect(html).toContain('не сопоставлено')
    expect(html).not.toContain('После сохранения строка уйдёт в группу принта')
    expect(html).not.toContain('wbArticleId')
  })

  it('does not bring back deprecated reports UI components in frontend source', () => {
    const forbidden = ['Report' + 'Layout', 'Kpi' + 'Strip', 'Vella' + 'Reports' + 'Page']
    const offenders = readSourceFiles('src').flatMap(({ path, text }) =>
      forbidden.filter((token) => text.includes(token)).map((token) => `${path}: ${token}`),
    )

    expect(offenders).toEqual([])
  })
})
