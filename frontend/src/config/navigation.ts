import {
  Tag,
  Flame,
  FileBarChart,
  Star,
  MessageSquare,
  Bot,
  Repeat,
  BarChart3,
  Wallet,
  ClipboardList,
  QrCode,
  Settings,
  Bell,
  History,
  Percent,
  Layers,
  Settings2,
  LayoutTemplate,
  BookOpen,
  Database,
  type LucideIcon,
} from 'lucide-react'

export type Role = 'owner' | 'admin' | 'ecom' | 'manager' | 'finance' | 'ads' | 'production' | 'reviews' | 'viewer'

export type NavItem = {
  id: string
  label: string
  icon?: LucideIcon
  path: string
  children?: NavItem[]
  requiredRole?: Role[]
  badge?: 'soon' | number
}

export type NavGroup = {
  id: string
  label: string | null
  items: NavItem[]
}

export const CURRENT_ROLE: Role = 'admin'

export const NAVIGATION: NavGroup[] = [
  {
    id: 'wb',
    label: 'WB',
    items: [
      {
        id: 'wb-repricer',
        label: 'Репрайсер',
        icon: Tag,
        path: '/wb/repricer',
        children: [
          { id: 'wb-repricer-list', label: 'Репрайсер', icon: Layers, path: '/wb/repricer' },
          { id: 'wb-repricer-stats', label: 'Диагностика цен', icon: BarChart3, path: '/wb/repricer/stats' },
          { id: 'wb-repricer-templates', label: 'Стратегии', icon: LayoutTemplate, path: '/wb/templates' },
          { id: 'wb-repricer-changelog', label: 'История', icon: History, path: '/wb/repricer/changelog' },
          { id: 'wb-liquidation', label: 'Ликвидация', icon: Flame, path: '/wb/liquidation' },
          { id: 'wb-repricer-algorithm', label: 'Правила', icon: Settings2, path: '/wb/algorithm' },
          { id: 'wb-repricer-work-status', label: 'Статус работы', icon: ClipboardList, path: '/wb/repricer/work-status' },
        ],
      },
      { id: 'wb-promotions', label: 'Акции WB', icon: Percent, path: '/wb/promotions' },
      {
        id: 'wb-reports',
        label: 'Отчёты',
        icon: FileBarChart,
        path: '/wb/reports',
        children: [
          { id: 'wb-reports-digest', label: 'Воронка продаж', path: '/wb/reports' },
          { id: 'wb-reports-abc', label: 'ABC-анализ', path: '/wb/reports/abc' },
          { id: 'wb-reports-rnp', label: 'РНП', path: '/wb/reports/rnp' },
          { id: 'wb-reports-pnl', label: 'P&L', path: '/wb/reports/pnl', requiredRole: ['finance', 'admin'] },
          { id: 'wb-reports-ads', label: 'Реклама', path: '/wb/reports/ads', requiredRole: ['ads', 'finance', 'admin'] },
          { id: 'wb-reports-stock', label: 'Остатки', path: '/wb/reports/stock' },
          { id: 'wb-reports-wow', label: 'Неделя-к-неделе', path: '/wb/reports/week-over-week' },
          { id: 'wb-reports-rules', label: 'Правила', icon: Settings2, path: '/wb/reports/rules' },
        ],
      },
      { id: 'wb-sources', label: 'Источники и загрузки', icon: Database, path: '/wb/sources', requiredRole: ['owner', 'admin'] },
      { id: 'wb-reviews', label: 'Отзывы WB', icon: Star, path: '/wb/reviews', badge: 'soon', requiredRole: ['reviews', 'manager', 'admin'] },
    ],
  },
  {
    id: 'avito',
    label: 'АВИТО',
    items: [
      { id: 'avito-chats', label: 'Сообщения', icon: MessageSquare, path: '/avito/chats', requiredRole: ['owner', 'admin', 'ecom', 'manager', 'reviews'] },
      { id: 'avito-chat-bot', label: 'Бот-автоответчик', icon: Bot, path: '/avito/chat-bot', badge: 'soon', requiredRole: ['owner', 'admin', 'ecom', 'manager', 'reviews'] },
      { id: 'avito-repricer', label: 'Репрайсер Авито', icon: Repeat, path: '/avito/repricer', badge: 'soon', requiredRole: ['owner', 'admin', 'ecom', 'manager'] },
      { id: 'avito-reviews', label: 'Отзывы Авито', icon: Star, path: '/avito/reviews', badge: 'soon', requiredRole: ['owner', 'admin', 'ecom', 'reviews', 'manager'] },
      { id: 'avito-stats', label: 'Статистика', icon: BarChart3, path: '/avito/stats', requiredRole: ['owner', 'admin', 'ecom', 'manager', 'finance', 'ads'] },
      { id: 'avito-wallets', label: 'Кошельки', icon: Wallet, path: '/avito/wallets', badge: 'soon', requiredRole: ['owner', 'finance', 'admin'] },
      { id: 'avito-notifications', label: 'Уведомления', icon: Bell, path: '/avito/notifications', requiredRole: ['owner', 'admin', 'ecom', 'manager', 'finance', 'ads', 'production', 'reviews'] },
    ],
  },
  {
    id: 'production',
    label: 'ПРОИЗВОДСТВО',
    items: [
      { id: 'orders', label: 'Лист печати', icon: ClipboardList, path: '/orders', requiredRole: ['production', 'manager', 'admin'] },
      { id: 'orders-returns', label: 'QR-возвраты', icon: QrCode, path: '/orders/returns', badge: 'soon', requiredRole: ['production', 'manager', 'admin'] },
    ],
  },
  {
    id: 'system',
    label: 'СИСТЕМА',
    items: [
      { id: 'notifications', label: 'Уведомления', icon: Bell, path: '/notifications' },
      {
        id: 'settings',
        label: 'Настройки',
        icon: Settings,
        path: '/settings',
        requiredRole: ['owner', 'admin'],
        children: [
          { id: 'settings-profile', label: 'Профиль', path: '/settings/profile', requiredRole: ['owner', 'admin'] },
          { id: 'settings-access', label: 'Команда и доступы', path: '/settings/access', requiredRole: ['owner', 'admin'] },
          { id: 'settings-marketplaces', label: 'Маркетплейсы', path: '/settings/marketplaces', requiredRole: ['owner', 'admin'] },
          { id: 'settings-imports', label: 'Импорт данных', path: '/settings/imports', requiredRole: ['owner', 'admin'] },
          { id: 'settings-notifications', label: 'Уведомления', path: '/settings/notifications', requiredRole: ['owner', 'admin'] },
          { id: 'settings-sessions', label: 'Сессии', path: '/settings/sessions', requiredRole: ['owner', 'admin'] },
          { id: 'settings-audit', label: 'Audit', path: '/settings/audit', requiredRole: ['owner', 'admin'] },
        ],
      },
      {
        id: 'wiki',
        label: 'Вики',
        icon: BookOpen,
        path: '/wiki',
        children: [
          { id: 'wiki-wb-repricer', label: 'WB Репрайсер', icon: Tag, path: '/wiki/wb-repricer' },
          { id: 'wiki-liquidation', label: 'Ликвидация', icon: Flame, path: '/wiki/liquidation' },
          { id: 'wiki-promotions', label: 'Акции WB', icon: Percent, path: '/wiki/promotions' },
          { id: 'wiki-algorithm', label: 'Алгоритм', icon: Settings2, path: '/wiki/algorithm' },
          { id: 'wiki-templates', label: 'Шаблоны', icon: LayoutTemplate, path: '/wiki/templates' },
        ],
      },
    ],
  },
]

export function flattenNav(groups: NavGroup[] = NAVIGATION): NavItem[] {
  const out: NavItem[] = []
  for (const group of groups) {
    for (const item of group.items) {
      out.push(item)
      if (item.children) out.push(...item.children)
    }
  }
  return out
}

export function findNavItemByPath(path: string, groups: NavGroup[] = NAVIGATION): NavItem | undefined {
  return flattenNav(groups).find((i) => i.path === path)
}

export function findNavGroupForPath(path: string, groups: NavGroup[] = NAVIGATION): NavGroup | undefined {
  for (const group of groups) {
    for (const item of group.items) {
      if (item.path === path) return group
      if (item.children?.some((c) => c.path === path)) return group
    }
  }
  return undefined
}

export function isItemVisible(item: NavItem, role: Role = CURRENT_ROLE): boolean {
  if (!item.requiredRole || item.requiredRole.length === 0) return true
  return item.requiredRole.includes(role)
}

export function visibleNavigationForRole(role: Role = CURRENT_ROLE, groups: NavGroup[] = NAVIGATION): NavGroup[] {
  return groups
    .map((group) => {
      const items = group.items.reduce<NavItem[]>((visibleItems, item) => {
          const children = item.children?.filter((child) => isItemVisible(child, role))
          if (!isItemVisible(item, role) && (!children || children.length === 0)) return visibleItems
          visibleItems.push({ ...item, children })
          return visibleItems
        }, [])

      return { ...group, items }
    })
    .filter((group) => group.items.length > 0)
}
