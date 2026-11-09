import { useMemo, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import {
  AlertTriangle,
  Bell,
  ChevronLeft,
  ChevronRight,
  Database,
  Download,
  FileClock,
  HelpCircle,
  Info,
  LogOut,
  MonitorSmartphone,
  ShieldAlert,
  ShieldCheck,
  Store,
  Tag,
  UserCog,
  Users,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Separator } from '@/components/ui/separator'
import { SidebarTrigger } from '@/components/ui/sidebar'
import { ThemeToggle } from '@/components/ThemeToggle'

import { findNavItemByPath, findNavGroupForPath, NAVIGATION, type NavItem } from '@/config/navigation'
import { ROUTE_TO_WIKI } from '@/wiki/wikiRoutes'
import { cn } from '@/lib/utils'
import {
  NOTIFICATION_CATEGORY_LABELS,
  NOTIFICATION_SEVERITY_LABELS,
  filterNotifications,
  getUnreadCount,
} from '@/features/notifications/repository'
import { useNotifications } from '@/features/notifications/notificationContext'
import type { NotificationEvent, NotificationSeverity } from '@/features/notifications/types'
import { useAuth } from '@/features/auth/authContext'

type Crumb = { label: string; path?: string; mono?: boolean }

function buildBreadcrumbs(pathname: string): Crumb[] {
  const crumbs: Crumb[] = []

  const group = findNavGroupForPath(pathname)
  if (group?.label) {
    crumbs.push({ label: group.label })
  }

  const exact = findNavItemByPath(pathname)
  if (exact) {
    const parent = group?.items.find((i: NavItem) =>
      i.children?.some((c) => c.path === pathname),
    )
    if (parent) {
      // Parent = Репрайсер (/wb/repricer), exact может быть сам Репрайсер (тот же путь).
      // Если путь совпадает — вместо дубля показываем child с тем же path (например «Товары»).
      if (parent.path === exact.path) {
        const child = parent.children?.find((c) => c.path === pathname)
        crumbs.push({ label: parent.label, path: parent.path })
        crumbs.push({ label: child?.label ?? exact.label })
        return crumbs
      }
      crumbs.push({ label: parent.label, path: parent.path })
    }
    crumbs.push({ label: exact.label })
    return crumbs
  }

  for (const g of NAVIGATION) {
    for (const item of g.items) {
      if (item.path !== '/' && pathname.startsWith(item.path + '/')) {
        if (g.label) crumbs.push({ label: g.label })
        crumbs.push({ label: item.label, path: item.path })
        const tail = pathname.slice(item.path.length + 1).split('/')
        if (tail.length === 2 && tail[0] === 'sku' && tail[1]) {
          crumbs.push({ label: tail[1], mono: true })
        }
        return crumbs
      }
    }
  }

  return crumbs
}

function notificationIconClass(severity: NotificationSeverity) {
  if (severity === 'critical') return 'border-red-200 bg-red-50 text-red-600 dark:border-red-900 dark:bg-red-950/30 dark:text-red-300'
  if (severity === 'warning') return 'border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-300'
  return 'border-blue-200 bg-blue-50 text-blue-700 dark:border-blue-900 dark:bg-blue-950/30 dark:text-blue-300'
}

function NotificationIcon({ severity }: { severity: NotificationSeverity }) {
  if (severity === 'critical') return <ShieldAlert className="size-3.5" aria-hidden="true" />
  if (severity === 'warning') return <AlertTriangle className="size-3.5" aria-hidden="true" />
  return <Info className="size-3.5" aria-hidden="true" />
}

function HeaderNotifications({ items }: { items: NotificationEvent[] }) {
  const navigate = useNavigate()
  const unreadCount = getUnreadCount(items)
  const recent = useMemo(
    () => filterNotifications(items, { period: '7d', category: 'all', severity: 'all', manager: 'all', read: 'unread', query: '' }).slice(0, 5),
    [items],
  )

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className="relative h-8 w-8"
          title="Уведомления"
          aria-label="Уведомления"
        >
          <Bell className="size-4" />
          {unreadCount > 0 && (
            <span className="absolute right-1 top-1 flex min-w-3.5 h-3.5 items-center justify-center rounded-full bg-red-500 px-0.5 text-[9px] font-semibold leading-none text-white">
              {unreadCount > 9 ? '9+' : unreadCount}
            </span>
          )}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-[360px] p-0">
        <div className="flex items-center justify-between border-b px-3 py-2">
          <DropdownMenuLabel className="p-0 text-sm">Уведомления</DropdownMenuLabel>
          <span className="text-xs text-muted-foreground">{unreadCount} новых</span>
        </div>
        {recent.length === 0 ? (
          <div className="px-3 py-8 text-center text-sm text-muted-foreground">Новых уведомлений нет</div>
        ) : (
          <div className="max-h-[340px] overflow-auto py-1">
            {recent.map((item) => (
              <DropdownMenuItem
                key={item.id}
                className="items-start gap-2 px-3 py-2"
                onSelect={() => navigate('/notifications')}
              >
                <span className={cn('mt-0.5 inline-flex size-6 shrink-0 items-center justify-center rounded-md border', notificationIconClass(item.severity))}>
                  <NotificationIcon severity={item.severity} />
                </span>
                <span className="min-w-0">
                  <span className="block truncate text-sm font-semibold">{item.title}</span>
                  <span className="mt-0.5 block text-xs text-muted-foreground">
                    {NOTIFICATION_SEVERITY_LABELS[item.severity]} · {NOTIFICATION_CATEGORY_LABELS[item.category]} · {item.manager}
                  </span>
                </span>
              </DropdownMenuItem>
            ))}
          </div>
        )}
        <DropdownMenuSeparator className="m-0" />
        <DropdownMenuItem className="justify-center py-2 font-semibold text-primary" onSelect={() => navigate('/notifications')}>
          Все уведомления
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function HeaderProfileMenu() {
  const navigate = useNavigate()
  const { logout, profile } = useAuth()
  const [logoutPending, setLogoutPending] = useState(false)
  const [logoutError, setLogoutError] = useState(false)

  if (!profile) {
    return (
      <button
        type="button"
        disabled
        className="ml-1 inline-flex h-8 items-center gap-2 rounded-md px-1.5"
        aria-label="Профиль загружается"
        title="Профиль загружается"
      >
        <span className="flex size-7 items-center justify-center rounded-full bg-muted text-xs font-semibold text-muted-foreground">···</span>
      </button>
    )
  }

  const wbScope = profile.scopes.find((scope) => scope.marketplace === 'wb')
  const avitoScope = profile.scopes.find((scope) => scope.marketplace === 'avito')

  async function handleLogout() {
    if (logoutPending) return
    setLogoutPending(true)
    setLogoutError(false)
    try {
      await logout()
    } catch {
      setLogoutError(true)
    } finally {
      setLogoutPending(false)
    }
  }

  const menuItems = [
    { label: 'Личный кабинет', path: '/settings/profile', icon: UserCog },
    { label: 'Команда и доступы', path: '/settings/access', icon: Users },
    { label: 'Маркетплейсы', path: '/settings/marketplaces', icon: Store },
    { label: 'Импорт данных', path: '/settings/imports', icon: Database },
    { label: 'Уведомления', path: '/settings/notifications', icon: Bell },
    { label: 'Сессии', path: '/settings/sessions', icon: MonitorSmartphone },
    { label: 'Audit', path: '/settings/audit', icon: FileClock },
  ]

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          className="ml-1 inline-flex h-8 items-center gap-2 rounded-md px-1.5 hover:bg-muted focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2"
          aria-label={`Профиль ${profile.name}`}
          title={`Профиль ${profile.name}`}
        >
          <span className="flex size-7 items-center justify-center rounded-full bg-primary text-xs font-semibold text-primary-foreground">{profile.initials}</span>
          <span className="hidden text-sm font-semibold text-foreground xl:inline">{profile.shortName}</span>
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-[380px] p-0">
        <div className="grid grid-cols-[44px_minmax(0,1fr)] gap-3 border-b px-3 py-3">
          <span className="flex size-11 items-center justify-center rounded-full bg-primary text-sm font-semibold text-primary-foreground">{profile.initials}</span>
          <div className="min-w-0">
            <DropdownMenuLabel className="truncate p-0 text-sm">{profile.name}</DropdownMenuLabel>
            <div className="mt-1 truncate text-xs text-muted-foreground">{profile.roleLabel} · {profile.email}</div>
            <div className="mt-1 truncate text-xs text-muted-foreground">{profile.telegram}</div>
          </div>
        </div>

        <div className="grid gap-2 border-b px-3 py-3">
          <div className="flex items-start gap-2 rounded-md bg-muted/50 p-2">
            <ShieldCheck className="mt-0.5 size-4 shrink-0 text-emerald-600" />
            <div className="min-w-0 text-xs">
              <div className="font-semibold text-foreground">WB: {wbScope?.status === 'full' ? 'полный доступ' : 'ограниченный доступ'}</div>
              <div className="mt-0.5 truncate text-muted-foreground">{wbScope?.modules.join(', ')}</div>
            </div>
          </div>
          <div className="flex items-start gap-2 rounded-md bg-muted/50 p-2">
            <Store className="mt-0.5 size-4 shrink-0 text-amber-600" />
            <div className="min-w-0 text-xs">
              <div className="font-semibold text-foreground">Авито: {avitoScope?.status === 'limited' ? 'ограничено' : 'просмотр'}</div>
              <div className="mt-0.5 truncate text-muted-foreground">{avitoScope?.blockers.join(' · ') || 'Блокеров нет'}</div>
            </div>
          </div>
        </div>

        <div className="py-1">
          {menuItems.map((item) => {
            const Icon = item.icon
            return (
              <DropdownMenuItem key={item.path} className="gap-2 px-3 py-2" onSelect={() => navigate(item.path)}>
                <Icon className="size-4 text-muted-foreground" />
                <span>{item.label}</span>
              </DropdownMenuItem>
            )
          })}
        </div>
        <DropdownMenuSeparator className="m-0" />
        <DropdownMenuItem
          disabled={logoutPending}
          className="gap-2 px-3 py-2 text-destructive"
          onSelect={(event) => {
            event.preventDefault()
            void handleLogout()
          }}
        >
          <LogOut className="size-4" />
          <span>{logoutPending ? 'Выходим...' : 'Выйти'}</span>
          {logoutError ? (
            <span role="alert" aria-live="assertive" className="ml-auto text-xs text-muted-foreground">
              Не удалось выйти. Повторите попытку.
            </span>
          ) : (
            <span className="ml-auto text-xs text-muted-foreground">auth</span>
          )}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

export function AppHeader() {
  const { pathname } = useLocation()
  const navigate = useNavigate()
  const crumbs = buildBreadcrumbs(pathname)
  const wikiPath = ROUTE_TO_WIKI[pathname]
  const { items: notifications } = useNotifications()

  return (
    <header className="sticky top-0 z-30 flex h-[50px] shrink-0 items-center gap-2 border-b bg-background px-4">
      <SidebarTrigger />
      <Separator orientation="vertical" className="mx-1 h-5" />

      <div className="hidden items-center gap-1 md:flex">
        <Button variant="ghost" size="icon" className="h-7 w-7 text-muted-foreground" disabled aria-label="Назад">
          <ChevronLeft className="size-4" />
        </Button>
        <Button variant="ghost" size="icon" className="h-7 w-7 text-muted-foreground" disabled aria-label="Вперёд">
          <ChevronRight className="size-4" />
        </Button>
      </div>

      <nav
        aria-label="Хлебные крошки"
        className="flex min-w-0 flex-1 items-center gap-2 overflow-hidden whitespace-nowrap text-sm"
      >
        {crumbs.length === 0 ? (
          <span className="font-medium text-foreground truncate">Огни</span>
        ) : (
          crumbs.map((c, idx) => (
            <span key={idx} className="flex items-center gap-2 min-w-0 shrink-0 last:shrink">
              {idx > 0 && <span className="text-muted-foreground/50">/</span>}
              {c.path && idx < crumbs.length - 1 ? (
                <Link
                  to={c.path}
                  className="text-muted-foreground hover:text-foreground max-w-[18ch] truncate"
                >
                  {c.label}
                </Link>
              ) : (
                <span
                  className={[
                    idx === crumbs.length - 1
                      ? 'font-medium text-foreground truncate'
                      : 'text-muted-foreground max-w-[18ch] truncate',
                    c.mono ? 'font-mono text-xs' : '',
                  ]
                    .filter(Boolean)
                    .join(' ')}
                >
                  {c.label}
                </span>
              )}
            </span>
          ))
        )}
      </nav>

      <div className="ml-auto flex shrink-0 items-center gap-2">
        {wikiPath && (
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8"
            onClick={() => navigate(wikiPath)}
            title="Открыть вики"
            aria-label="Открыть вики"
          >
            <HelpCircle className="size-4" />
          </Button>
        )}
        <ThemeToggle />
        <HeaderNotifications items={notifications} />
        <Button variant="outline" size="sm" className="hidden h-8 gap-2 font-semibold lg:inline-flex">
          <Tag className="size-3.5" />
          Акции WB
        </Button>
        <Button variant="outline" size="sm" className="hidden h-8 gap-2 font-semibold md:inline-flex">
          <Download className="size-3.5" />
          Экспорт
        </Button>
        <HeaderProfileMenu />
      </div>
    </header>
  )
}
