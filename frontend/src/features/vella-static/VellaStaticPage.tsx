import { useEffect } from 'react'
import { useLocation } from 'react-router-dom'

export function routeStateFromPath(pathname: string, search: string) {
  const searchParams = new URLSearchParams(search)
  const mode = searchParams.get('mode')
  if (pathname.endsWith('/settings/profile')) return 'settings-profile'
  if (pathname.endsWith('/settings/access')) return 'settings-access'
  if (pathname.endsWith('/settings/imports')) return 'settings-imports'
  if (pathname.endsWith('/avito/notifications')) return 'avito-notifications'
  if (pathname.endsWith('/avito/privacy')) return 'avito-privacy'
  if (pathname.endsWith('/avito/reviews')) return 'avito-reviews'
  if (pathname.endsWith('/notifications')) return 'notifications'
  if (pathname === '/avito' || pathname.endsWith('/avito/overview')) return 'avito-overview'
  if (pathname.endsWith('/avito/listings')) return 'avito-listings'
  if (pathname.endsWith('/avito/repricer')) return 'avito-repricer'
  if (pathname.endsWith('/avito/stats') || pathname.endsWith('/avito/statistics')) return 'avito-stats'
  if (pathname.endsWith('/avito/chats') || pathname.endsWith('/avito/inbox') || pathname.endsWith('/avito/messages')) return 'avito-inbox'
  if (pathname.endsWith('/avito/orders/archive') || pathname.endsWith('/avito/orders')) return 'orders-avito'
  if (pathname.endsWith('/orders/archive') || pathname.endsWith('/orders')) return 'orders-print'
  if (pathname.endsWith('/reports/monitor')) return { tab: 'digest', mode: 'now' }
  if (pathname.endsWith('/reports/rules')) return 'report-rules'
  if (pathname.endsWith('/reports/abc')) return 'abc'
  if (pathname.endsWith('/reports/rnp')) return 'rnp'
  if (pathname.endsWith('/reports/pnl')) return 'pnl'
  if (pathname.endsWith('/reports/expenses')) return 'expenses'
  if (pathname.endsWith('/reports/ads')) return 'ads'
  if (pathname.endsWith('/reports/sales')) return { tab: 'digest', mode: 'period' }
  if (pathname.endsWith('/reports/stock')) return 'stock'
  if (pathname.endsWith('/reports/week-over-week')) return 'week'
  if (pathname.endsWith('/reports')) return { tab: 'digest', mode: mode === 'period' ? 'period' : 'now' }
  if (pathname.endsWith('/wb/sources')) return 'sources'
  if (pathname.startsWith('/wb/reviews')) return 'reviews'
  if (pathname.endsWith('/algorithm')) return 'algo'
  if (pathname.endsWith('/templates')) return 'templates'
  if (pathname.includes('/repricer/sku/')) return 'products'
  if (pathname.endsWith('/repricer/work-status')) return 'work-status'
  if (pathname.endsWith('/repricer/stats')) return 'repricer-stats'
  if (pathname.endsWith('/repricer/changelog')) return 'history'
  if (pathname.endsWith('/repricer/liquidation')) return 'liq'
  if (pathname.endsWith('/repricer/promos')) return 'promos'
  if (pathname.endsWith('/liquidation')) return 'liq'
  if (pathname.endsWith('/promotions')) return 'promos'
  if (pathname.startsWith('/wb/reports/')) return 'digest'
  if (pathname.startsWith('/avito')) return 'avito-overview'
  return 'products'
}

export function vellaTitleFromPath(pathname: string) {
  return pathname.endsWith('/avito/privacy') ? 'Satorna — Конфиденциальность'
    : pathname.includes('/notifications')
    ? 'Satorna — Уведомления'
    : pathname.endsWith('/avito/orders/archive') ? 'Satorna — Архив заказов Авито'
    : pathname.endsWith('/avito/orders') ? 'Satorna — Заказы Авито'
    : pathname.endsWith('/orders/archive') ? 'Satorna — Архив листа WB'
    : pathname.endsWith('/orders') ? 'Satorna — Лист печати'
    : pathname.includes('/settings/profile') ? 'Satorna — Личный кабинет'
    : pathname.includes('/settings/access') ? 'Satorna — Команда и доступы'
    : pathname.includes('/settings/imports') ? 'Satorna — Импорт данных'
    : pathname.includes('/avito') ? 'Satorna — Авито'
      : pathname.includes('/wb/reviews') ? 'Satorna — Отзывы WB'
      : pathname.includes('/work-status') ? 'Satorna — Статус работы'
      : pathname.includes('/wb/reports') || pathname.includes('/internal/vella-preview/reports') ? 'Satorna — Отчёты WB' : 'Satorna — Репрайсер WB'
}

export function VellaStaticPage() {
  const { pathname, search } = useLocation()
  const routeState = routeStateFromPath(pathname, search)
  const tab = typeof routeState === 'string' ? routeState : routeState.tab
  const searchParams = new URLSearchParams(search)
  const query = new URLSearchParams({ tab })
  const skuMatch = pathname.match(/\/repricer\/sku\/([^/?#]+)/)
  if (skuMatch) query.set('sku', decodeURIComponent(skuMatch[1]))
  else if (searchParams.get('sku')) query.set('sku', searchParams.get('sku') || '')
  if (typeof routeState !== 'string') query.set('mode', routeState.mode)

  useEffect(() => {
    document.title = vellaTitleFromPath(pathname)
  }, [pathname])

  return (
    <iframe
      title="Satorna production UI"
      src={`/vella-production.html?${query.toString()}`}
      className="fixed inset-0 h-screen w-screen border-0 bg-[#0F172A]"
    />
  )
}
