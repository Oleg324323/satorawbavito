import type {
  NotificationCategory,
  NotificationEvent,
  NotificationFilters,
  NotificationPeriod,
  NotificationReadFilter,
  NotificationSeverity,
  NotificationsResponse,
} from './types.js'

export const NOTIFICATION_CATEGORY_LABELS: Record<NotificationCategory, string> = {
  reports: 'Отчёты',
  prices: 'Цены',
  orders: 'Заказы',
  avito: 'Авито',
  ai: 'AI',
  system: 'Система',
}

export const NOTIFICATION_SEVERITY_LABELS: Record<NotificationSeverity, string> = {
  critical: 'Критично',
  warning: 'Внимание',
  info: 'Инфо',
}

export const DEFAULT_NOTIFICATION_FILTERS: NotificationFilters = {
  period: '7d',
  category: 'all',
  severity: 'all',
  manager: 'all',
  read: 'all',
  query: '',
}

const ITEMS: NotificationEvent[] = [
  {
    id: 'orders-sla-001',
    title: 'FBS SLA: 9 заказов менее 18ч до дедлайна',
    details: 'Заказы видны в очереди, но утренний PDF ещё не подтверждён производством.',
    severity: 'critical',
    category: 'orders',
    source: 'Очередь производства',
    manager: 'Производство',
    createdAt: '2026-05-08T08:32:00.000+05:00',
    readAt: null,
    entityType: 'order',
    entityId: 'fbs-sla-batch-2026-05-08',
    route: '/orders',
    blockedActions: ['Автоперенос в конец очереди'],
    freshness: {
      label: 'Заказы WB+Авито',
      state: 'partial',
      updatedAt: '2026-05-08T08:29:00.000+05:00',
    },
  },
  {
    id: 'price-pmin-001',
    title: 'LBBT_03 торгуется ниже P_min',
    details: 'Текущая цена ниже безопасного минимума. Автоснижение заблокировано до ручной проверки ликвидации.',
    severity: 'critical',
    category: 'prices',
    source: 'Цены WB',
    manager: 'Воробьева',
    createdAt: '2026-05-08T08:21:00.000+05:00',
    readAt: null,
    entityType: 'sku',
    entityId: 'LBBT_03',
    route: '/wb/repricer/sku/LBBT_03',
    blockedActions: ['Отправка цены', 'Автоснижение'],
    freshness: {
      label: 'Цены WB',
      state: 'fresh',
      updatedAt: '2026-05-08T08:12:00.000+05:00',
    },
  },
  {
    id: 'pnl-freshness-001',
    title: 'Финансовый источник P&L устарел',
    details: 'Оперативный P&L доступен, но финальный финансовый отчёт WB ещё не сформирован.',
    severity: 'warning',
    category: 'reports',
    source: 'Финансовый отчёт WB',
    manager: 'Финансы',
    createdAt: '2026-05-08T08:15:00.000+05:00',
    readAt: null,
    entityType: 'report',
    entityId: 'pnl',
    route: '/wb/reports/pnl',
    blockedActions: ['Финальное решение по P&L'],
    freshness: {
      label: 'Финансовый отчёт WB',
      state: 'pending',
      updatedAt: '2026-05-06T10:20:00.000+05:00',
    },
  },
  {
    id: 'report-export-ready-001',
    title: 'Excel-отчёт готов к скачиванию',
    details: 'Реклама WB · 25.04—01.05 · 1 482 SKU. Файл можно скачать повторно без нового расчёта.',
    severity: 'info',
    category: 'reports',
    source: 'Экспорт отчётов',
    manager: 'Отчёты',
    createdAt: '2026-05-08T08:16:00.000+05:00',
    readAt: null,
    entityType: 'report',
    entityId: 'wb-ads-export-2026-05-08-0816',
    route: '/wb/reports/ads',
    blockedActions: [],
    freshness: {
      label: 'Файл сформирован',
      state: 'fresh',
      updatedAt: '2026-05-08T08:16:00.000+05:00',
    },
    reportFile: {
      id: 'wb-ads-export-2026-05-08-0816',
      report: 'Реклама WB',
      period: '25.04—01.05',
      rows: 1482,
      size: '684 КБ',
      format: 'XLS',
      fileName: 'wb-ads-25-04_01-05.xls',
      generatedAt: '08.05, 08:16',
    },
  },
  {
    id: 'abc-cc',
    title: 'LBBT_03 попал в CC',
    details: 'Слабые продажи и почти нулевая чистая прибыль после рекламы, логистики и хранения.',
    severity: 'critical',
    category: 'reports',
    source: 'Сводный отчёт WB',
    manager: 'Воробьева',
    createdAt: '2026-05-07T08:05:00.000+05:00',
    readAt: null,
    entityType: 'sku',
    entityId: 'LBBT_03',
    route: '/wb/reports/abc',
    blockedActions: ['Автоматическое повышение цены'],
    freshness: {
      label: 'ABC-анализ',
      state: 'fresh',
      updatedAt: '2026-05-07T08:00:00.000+05:00',
    },
  },
  {
    id: 'ads-drr',
    title: 'ДРР по FBBT_55 выше нормы',
    details: 'Новинка растёт в корзинах, но рекламные расходы уже выше чистой прибыли за период.',
    severity: 'warning',
    category: 'reports',
    source: 'Реклама WB',
    manager: 'Дудина',
    createdAt: '2026-05-07T08:04:00.000+05:00',
    readAt: null,
    entityType: 'sku',
    entityId: 'FBBT_55',
    route: '/wb/reports/ads',
    blockedActions: ['Автопродление рекламной кампании'],
    freshness: {
      label: 'Реклама WB',
      state: 'fresh',
      updatedAt: '2026-05-07T08:00:00.000+05:00',
    },
  },
  {
    id: 'ai-review-001',
    title: 'ИИ не уверен в ответе на отзыв 2★',
    details: 'Ответ сохранён в очередь проверки и не будет отправлен без подтверждения менеджера.',
    severity: 'warning',
    category: 'ai',
    source: 'Автоответы WB',
    manager: 'Отзывы',
    createdAt: '2026-05-06T16:40:00.000+05:00',
    readAt: '2026-05-07T09:12:00.000+05:00',
    entityType: 'review',
    entityId: 'wb-review-8821',
    route: '/wb/reviews',
    blockedActions: ['Автоотправка ответа'],
    freshness: {
      label: 'Отзывы WB',
      state: 'fresh',
      updatedAt: '2026-05-06T16:38:00.000+05:00',
    },
  },
  {
    id: 'avito-balance-001',
    title: 'Баланс Авито ниже порога на 3 аккаунтах',
    details: 'Автопополнение ожидает окно ЮKassa и дневной лимит. Продвижение может остановиться.',
    severity: 'warning',
    category: 'avito',
    source: 'Кошельки Авито',
    manager: 'Авито',
    createdAt: '2026-05-05T11:20:00.000+05:00',
    readAt: '2026-05-05T11:35:00.000+05:00',
    entityType: 'account',
    entityId: 'avito-wallets',
    route: '/avito/wallets',
    blockedActions: ['Автоподнятие объявлений после нулевого баланса'],
    freshness: {
      label: 'Кошельки Авито',
      state: 'stale',
      updatedAt: '2026-05-05T08:00:00.000+05:00',
    },
  },
  {
    id: 'system-digest-001',
    title: 'Ежедневный WB-дайджест сформирован',
    details: 'WB-дайджест сформирован за выбранный период.',
    severity: 'info',
    category: 'system',
    source: 'Сводный дайджест',
    manager: 'Отчёты',
    createdAt: '2026-05-04T08:15:00.000+05:00',
    readAt: '2026-05-04T08:30:00.000+05:00',
    entityType: 'report',
    entityId: 'wb-digest',
    route: '/wb/reports',
    blockedActions: [],
    freshness: {
      label: 'Сводный дайджест',
      state: 'fresh',
      updatedAt: '2026-05-04T08:15:00.000+05:00',
    },
  },
  {
    id: 'system-api-001',
    title: 'Пустой период в отчёте WB',
    details: 'Это штатный ответ: данных за выбранный период нет, ошибка не требуется.',
    severity: 'info',
    category: 'system',
    source: 'Отчёты WB',
    manager: 'Система',
    createdAt: '2026-04-30T12:10:00.000+05:00',
    readAt: '2026-04-30T12:15:00.000+05:00',
    entityType: 'system',
    entityId: 'wb-report-empty-204',
    route: '/wb/reports',
    blockedActions: [],
    freshness: {
      label: 'Отчёты WB',
      state: 'fresh',
      updatedAt: '2026-04-30T12:10:00.000+05:00',
    },
  },
]

function parseTime(value: string) {
  return new Date(value).getTime()
}

function periodStart(period: NotificationPeriod) {
  const days = Number(period.replace('d', ''))
  return Date.now() - days * 24 * 60 * 60 * 1000
}

function matchesText(item: NotificationEvent, query: string) {
  const needle = query.trim().toLowerCase()
  if (!needle) return true
  return [
    item.title,
    item.details,
    item.source,
    item.manager,
    item.entityId,
    item.category,
    item.severity,
    item.freshness?.label ?? '',
  ].some((value) => value.toLowerCase().includes(needle))
}

export function getNotifications(): NotificationsResponse {
  return {
    items: ITEMS.map((item) => ({
      ...item,
      blockedActions: [...item.blockedActions],
      reportFile: item.reportFile ? { ...item.reportFile } : undefined,
    })),
  }
}

export function filterNotifications(
  items: NotificationEvent[],
  filters: NotificationFilters = DEFAULT_NOTIFICATION_FILTERS,
) {
  const start = periodStart(filters.period)
  return items
    .filter((item) => parseTime(item.createdAt) >= start)
    .filter((item) => filters.category === 'all' || item.category === filters.category)
    .filter((item) => filters.severity === 'all' || item.severity === filters.severity)
    .filter((item) => filters.manager === 'all' || item.manager === filters.manager)
    .filter((item) => filters.read === 'all' || (filters.read === 'unread' ? !item.readAt : Boolean(item.readAt)))
    .filter((item) => matchesText(item, filters.query))
    .sort((a, b) => parseTime(b.createdAt) - parseTime(a.createdAt))
}

export function getUnreadCount(items: NotificationEvent[]) {
  return items.filter((item) => !item.readAt).length
}

export function markNotificationRead(items: NotificationEvent[], id: string, readAt = new Date().toISOString()) {
  return items.map((item) => (item.id === id ? { ...item, readAt: item.readAt ?? readAt } : item))
}

export function markAllNotificationsRead(items: NotificationEvent[], readAt = new Date().toISOString()) {
  return items.map((item) => ({ ...item, readAt: item.readAt ?? readAt }))
}

export function getNotificationManagers(items: NotificationEvent[]) {
  return Array.from(new Set(items.map((item) => item.manager))).sort((a, b) => a.localeCompare(b, 'ru'))
}

export function getNotificationPeriodLabel(period: NotificationPeriod) {
  return period === '1d' ? '1 день' : period === '7d' ? '7 дней' : period === '14d' ? '14 дней' : '30 дней'
}

export function getNotificationReadLabel(read: NotificationReadFilter) {
  if (read === 'unread') return 'Новые'
  if (read === 'read') return 'Прочитанные'
  return 'Все статусы'
}
