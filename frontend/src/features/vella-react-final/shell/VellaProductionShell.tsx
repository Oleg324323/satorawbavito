import {
  BarChart3,
  Bell,
  Database,
  FileSpreadsheet,
  Grid2X2,
  Megaphone,
  Package,
  Repeat2,
  Settings,
  ShoppingBasket,
  SlidersHorizontal,
  Star,
  User,
} from 'lucide-react'
import type { ReactNode } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import '../../../components/vella/VellaFoundation.css'
import '../vella-react-final.css'

type NavItem = {
  label: string
  path: string
  icon?: ReactNode
  count?: string | number
  tone?: 'warn' | 'alert'
}

const reportItems: NavItem[] = [
  { label: 'Дайджест', path: '/wb/reports', icon: <Grid2X2 size={15} /> },
  { label: 'ABC-анализ', path: '/wb/reports/abc', icon: <BarChart3 size={15} /> },
  { label: 'РНП', path: '/wb/reports/rnp', icon: <FileSpreadsheet size={15} /> },
  { label: 'P&L', path: '/wb/reports/pnl', icon: <span className="vella-final-icon-text">$</span> },
  { label: 'Расходы', path: '/wb/reports/expenses', icon: <FileSpreadsheet size={15} /> },
  { label: 'Реклама', path: '/wb/reports/ads', icon: <Megaphone size={15} /> },
  { label: 'Остатки', path: '/wb/reports/stock', icon: <Package size={15} /> },
  { label: 'Неделя-к-неделе', path: '/wb/reports/week-over-week', icon: <Repeat2 size={15} /> },
  { label: 'Правила', path: '/wb/reports/rules', icon: <SlidersHorizontal size={15} /> },
]

const repricerItems: NavItem[] = [
  { label: 'Репрайсер', path: '/wb/repricer', icon: <Package size={15} />, count: '1 482' },
  { label: 'Диагностика цен', path: '/wb/repricer/stats', icon: <BarChart3 size={15} />, count: 'черновик', tone: 'warn' },
  { label: 'Стратегии', path: '/wb/templates', icon: <FileSpreadsheet size={15} />, count: 5 },
  { label: 'История', path: '/wb/repricer/changelog', icon: <Repeat2 size={15} /> },
  { label: 'Ликвидация', path: '/wb/repricer/liquidation', icon: <BarChart3 size={15} />, count: 8, tone: 'alert' },
  { label: 'Акции WB', path: '/wb/repricer/promos', icon: <Megaphone size={15} />, count: 2, tone: 'alert' },
  { label: 'Правила', path: '/wb/algorithm', icon: <SlidersHorizontal size={15} /> },
]

const reportTabs: NavItem[] = [
  { label: 'Дайджест', path: '/wb/reports', icon: <Grid2X2 size={14} /> },
  { label: 'ABC', path: '/wb/reports/abc', icon: <BarChart3 size={14} /> },
  { label: 'РНП', path: '/wb/reports/rnp', icon: <FileSpreadsheet size={14} /> },
  { label: 'P&L', path: '/wb/reports/pnl', icon: <span className="vella-final-icon-text">$</span>, count: 'фин.' },
  { label: 'Расходы', path: '/wb/reports/expenses', icon: <FileSpreadsheet size={14} /> },
  { label: 'Реклама', path: '/wb/reports/ads', icon: <Megaphone size={14} /> },
  { label: 'Остатки', path: '/wb/reports/stock', icon: <Package size={14} /> },
  { label: 'Неделя', path: '/wb/reports/week-over-week', icon: <Repeat2 size={14} /> },
]

const repricerTabs: NavItem[] = [
  { label: 'Репрайсер', path: '/wb/repricer', icon: <Package size={14} />, count: '1 482' },
  { label: 'Диагностика цен', path: '/wb/repricer/stats', icon: <BarChart3 size={14} />, count: 'черновик', tone: 'warn' },
  { label: 'Стратегии', path: '/wb/templates', icon: <FileSpreadsheet size={14} />, count: 5 },
  { label: 'История', path: '/wb/repricer/changelog', icon: <Repeat2 size={14} /> },
  { label: 'Ликвидация', path: '/wb/repricer/liquidation', icon: <BarChart3 size={14} />, count: 8, tone: 'alert' },
  { label: 'Акции WB', path: '/wb/repricer/promos', icon: <Megaphone size={14} />, count: 2, tone: 'alert' },
  { label: 'Правила', path: '/wb/algorithm', icon: <SlidersHorizontal size={14} /> },
]

function candidatePath(path: string, currentPath: string) {
  if (!currentPath.startsWith('/internal/vella-react-final')) return path
  return `/internal/vella-react-final${path}`
}

export function VellaProductionShell({
  title,
  subtitle,
  mobileTitle,
  mobileCopy,
  topbarActions,
  tabs,
  module,
  children,
}: {
  title: string
  subtitle: string
  mobileTitle: string
  mobileCopy: string
  topbarActions?: ReactNode
  tabs?: ReactNode
  module?: 'reports' | 'repricer' | 'standalone'
  children: ReactNode
}) {
  const location = useLocation()
  const navigate = useNavigate()
  const currentPath = location.pathname.replace('/internal/vella-react-final', '')

  const go = (path: string) => navigate(candidatePath(path, location.pathname))
  const isActive = (path: string) => currentPath === path
  const activeModule = module === 'standalone'
    ? 'standalone'
    : module === 'repricer' || currentPath.startsWith('/wb/repricer') || currentPath === '/wb/templates' || currentPath === '/wb/algorithm'
    ? 'repricer'
    : module === 'reports' || currentPath.startsWith('/wb/reports')
      ? 'reports'
      : 'standalone'
  const isReportPath = activeModule === 'reports'
  const isRepricerPath = activeModule === 'repricer'
  const activeTabs = activeModule === 'repricer' ? repricerTabs : activeModule === 'reports' ? reportTabs : []

  return (
    <div className="vella-root vella-final-root" data-vella-shell>
      <div className="vella-shell">
        <aside className="vella-sidebar" data-vella-sidebar>
          <div className="vella-brand">
            <img className="vella-brand-logo" src="/brand/satorna-logo-white.svg" alt="Satorna" />
          </div>
          <div className="vella-nav-label">WB</div>
          <button className={`vella-nav-button vella-nav-parent ${isRepricerPath ? 'active' : ''}`} type="button" onClick={() => go('/wb/repricer')}>
            <span className="vella-nav-icon"><ShoppingBasket size={15} /></span>
            <span>Репрайсер</span>
            <span className={`vella-final-chevron ${isRepricerPath ? 'open' : ''}`}>{isRepricerPath ? '⌄' : '›'}</span>
          </button>
          {isRepricerPath ? (
            <div className="vella-final-nav-sub">
              {repricerItems.map((item) => (
                <button
                  className={`vella-nav-button ${isActive(item.path) ? 'active' : ''}`}
                  key={item.path}
                  type="button"
                  onClick={() => go(item.path)}
                >
                  <span className="vella-nav-icon">{item.icon}</span>
                  <span>{item.label}</span>
                  {item.count ? <span className={`vella-nav-count ${item.tone ?? ''}`}>{item.count}</span> : null}
                </button>
              ))}
            </div>
          ) : null}
          <button className={`vella-nav-button vella-nav-parent ${isReportPath ? 'active' : ''}`} type="button" onClick={() => go('/wb/reports/pnl')}>
            <span className="vella-nav-icon"><BarChart3 size={15} /></span>
            <span>Отчёты</span>
            <span className={`vella-final-chevron ${isReportPath ? 'open' : ''}`}>{isReportPath ? '⌄' : '›'}</span>
          </button>
          {isReportPath ? (
            <div className="vella-final-nav-sub">
              {reportItems.map((item) => (
                <button
                  className={`vella-nav-button ${isActive(item.path) ? 'active' : ''}`}
                  key={item.path}
                  type="button"
                  onClick={() => go(item.path)}
                >
                  <span className="vella-nav-icon">{item.icon}</span>
                  <span>{item.label}</span>
                  {item.count ? <span className={`vella-nav-count ${item.tone ?? ''}`}>{item.count}</span> : null}
                </button>
              ))}
            </div>
          ) : null}
          <button className={`vella-nav-button ${isActive('/wb/sources') ? 'active' : ''}`} type="button" onClick={() => go('/wb/sources')}>
            <span className="vella-nav-icon"><Database size={15} /></span>
            <span>Источники и загрузки</span>
            <span className="vella-nav-count warn">8</span>
          </button>
          <button className="vella-nav-button" type="button" onClick={() => go('/wb/reviews')}>
            <span className="vella-nav-icon"><Star size={15} /></span>
            <span>Отзывы WB</span>
            <span className="vella-nav-count alert">17</span>
          </button>
          <div className="vella-nav-label">Авито</div>
          <button className="vella-nav-button vella-nav-parent" type="button" onClick={() => go('/avito')}>
            <span className="vella-nav-icon"><Grid2X2 size={15} /></span>
            <span>Авито</span>
            <span className="vella-final-chevron">›</span>
          </button>
          <div className="vella-nav-label">Система</div>
          <button className="vella-nav-button" type="button" onClick={() => go('/settings/profile')}>
            <span className="vella-nav-icon"><User size={15} /></span>
            <span>Профиль</span>
          </button>
          <button className="vella-nav-button" type="button" onClick={() => go('/notifications')}>
            <span className="vella-nav-icon"><Bell size={15} /></span>
            <span>Уведомления</span>
            <span className="vella-nav-count alert">13</span>
          </button>
          <button className="vella-nav-button" type="button" onClick={() => go('/settings/access')}>
            <span className="vella-nav-icon"><Settings size={15} /></span>
            <span>Команда и доступы</span>
          </button>
        </aside>

        <main className="vella-main">
          <header className="vella-topbar" data-vella-topbar>
            <div className="vella-breadcrumb">WB <span>/</span> <b>{title}</b></div>
            <div className="vella-top-actions">{topbarActions}</div>
          </header>
          {tabs || activeTabs.length ? (
            <div className="vella-final-subtabs" aria-label={activeModule === 'repricer' ? 'Разделы репрайсера' : 'Разделы отчётов'}>
              {(tabs ? null : activeTabs.map((item) => (
                <button
                  className={`vella-final-subtab ${isActive(item.path) ? 'active' : ''}`}
                  key={item.path}
                  type="button"
                  onClick={() => go(item.path)}
                >
                  {item.icon}
                  {item.label}
                  {item.count ? <span className={`subtab-count ${item.tone ?? ''}`}>{item.count}</span> : null}
                </button>
              )))}
              {tabs}
            </div>
          ) : null}

          <section className="vella-content">
            <div className="desktop-only-fallback vella-final-mobile-fallback">
              <div className="desktop-only-card">
                <h1>{mobileTitle}</h1>
                <p>{mobileCopy} Полная версия доступна на ПК.</p>
                <a href={candidatePath(location.pathname.replace('/internal/vella-react-final', ''), location.pathname)}>Открыть раздел на ПК</a>
              </div>
            </div>
            <div className="vella-final-desktop">
              {subtitle ? <div className="vella-final-page-note">{subtitle}</div> : null}
              {children}
            </div>
          </section>
        </main>
      </div>
    </div>
  )
}
