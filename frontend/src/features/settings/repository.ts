import type {
  AccountCapability,
  CurrentUserProfile,
  DangerousPermission,
  Marketplace,
  MarketplaceScope,
  NotificationRoute,
  SessionInfo,
  SettingsAuditEvent,
  SettingsMutationResult,
  UserAccessProfile,
} from './types'

export const ROLE_LABELS = {
  owner: 'Владелец',
  admin: 'Администратор',
  ecom: 'Руководитель ecom',
  manager: 'Менеджер',
  finance: 'Финансы',
  ads: 'Реклама',
  production: 'Производство',
  reviews: 'Отзывы',
  viewer: 'Только просмотр',
} as const

export const DANGEROUS_PERMISSION_LABELS: Record<DangerousPermission, string> = {
  finance_view: 'Финансы',
  price_send: 'Отправка цен',
  xml_publish: 'Публикация XML',
  message_send: 'Отправка сообщений',
  review_send: 'Ответы на отзывы',
  wallet_topup: 'Пополнение кошельков',
  role_admin: 'Управление ролями',
  token_admin: 'Токены и интеграции',
}

const wbScope: MarketplaceScope = {
  marketplace: 'wb',
  status: 'full',
  modules: ['Репрайсер', 'Отчеты', 'P&L', 'Реклама', 'Отзывы', 'Ликвидация'],
  accountIds: ['wb-ogni-main'],
  dangerousPermissions: ['finance_view', 'price_send', 'review_send'],
  blockers: [],
}

const avitoScope: MarketplaceScope = {
  marketplace: 'avito',
  status: 'limited',
  modules: ['Аккаунты', 'Сообщения', 'Объявления', 'Статистика', 'Отзывы'],
  accountIds: ['avito-bless-msk', 'avito-anomie-msk', 'avito-bless-spb', 'avito-bless-kzn'],
  dangerousPermissions: ['message_send', 'review_send'],
  blockers: ['XML publish требует approval', '3 аккаунта без подтвержденного тарифа'],
}

const currentUser: CurrentUserProfile = {
  id: 'user-maria-f',
  name: 'Мария Ф.',
  shortName: 'Мария',
  initials: 'МФ',
  email: 'maria@ogni.example',
  telegram: '@maria_ogni',
  role: 'ecom',
  roleLabel: ROLE_LABELS.ecom,
  workspace: 'Огни',
  position: 'WB, отчеты и контроль Avito',
  scopes: [wbScope, avitoScope],
  unreadApprovals: 6,
}

let users: UserAccessProfile[] = [
  {
    id: 'user-maria-f',
    name: 'Мария Ф.',
    initials: 'МФ',
    email: 'maria@ogni.example',
    role: 'ecom',
    roleLabel: ROLE_LABELS.ecom,
    status: 'active',
    scopes: [wbScope, avitoScope],
    lastActiveAt: 'сегодня, 10:12',
  },
  {
    id: 'user-maxim-b',
    name: 'Максим Б.',
    initials: 'МБ',
    email: 'maxim@ogni.example',
    role: 'finance',
    roleLabel: ROLE_LABELS.finance,
    status: 'active',
    scopes: [
      { ...wbScope, status: 'limited', modules: ['Отчеты', 'P&L', 'Ликвидация'], dangerousPermissions: ['finance_view'] },
      { ...avitoScope, status: 'limited', modules: ['Кошельки', 'Статистика', 'Аккаунты'], dangerousPermissions: ['finance_view', 'wallet_topup'] },
    ],
    lastActiveAt: 'вчера, 18:44',
  },
  {
    id: 'user-print-lead',
    name: 'Светлана В.',
    initials: 'СВ',
    email: 'print@ogni.example',
    role: 'production',
    roleLabel: ROLE_LABELS.production,
    status: 'active',
    scopes: [
      {
        marketplace: 'wb',
        status: 'limited',
        modules: ['Заказы', 'Лист печати'],
        accountIds: ['wb-ogni-main'],
        dangerousPermissions: [],
        blockers: ['Финансы скрыты'],
      },
      {
        marketplace: 'avito',
        status: 'view_only',
        modules: ['Заказы', 'QR-возвраты'],
        accountIds: ['avito-bless-msk', 'avito-anomie-msk'],
        dangerousPermissions: [],
        blockers: ['Чаты и кошельки скрыты'],
      },
    ],
    lastActiveAt: 'сегодня, 08:02',
  },
  {
    id: 'user-reviews',
    name: 'Анна П.',
    initials: 'АП',
    email: 'reviews@ogni.example',
    role: 'reviews',
    roleLabel: ROLE_LABELS.reviews,
    status: 'invited',
    scopes: [
      { ...wbScope, status: 'limited', modules: ['Отзывы'], dangerousPermissions: ['review_send'], blockers: ['P&L скрыт'] },
      { ...avitoScope, status: 'limited', modules: ['Отзывы', 'Сообщения'], accountIds: ['avito-bless-msk'], dangerousPermissions: ['review_send'], blockers: [] },
    ],
    lastActiveAt: 'приглашение отправлено',
  },
]

const capabilities: AccountCapability[] = [
  {
    id: 'wb-ogni-main',
    marketplace: 'wb',
    accountName: 'WB · Огни основной',
    owner: 'Мария Ф.',
    status: 'partial',
    tariff: 'Base Token',
    health: 'Token активен, scope рекламы требует проверки',
    capabilities: [
      { key: 'repricer', label: 'Цены и P_min', status: 'ready', reason: 'Scope доступен' },
      { key: 'reports', label: 'Отчеты и P&L', status: 'ready', reason: 'Источник подключен' },
      { key: 'ads', label: 'Реклама', status: 'unknown', reason: 'Нужен live endpoint mapping' },
      { key: 'token', label: 'Токен', status: 'ready', reason: 'Истекает через 42 дня' },
    ],
    nextAction: 'Подтвердить WB Ads источник для РНП и P&L',
    updatedAt: 'сегодня, 09:55',
  },
  {
    id: 'avito-bless-msk',
    marketplace: 'avito',
    accountName: 'Bless T · Москва',
    owner: 'Максим Б.',
    status: 'ready',
    tariff: 'Максимальный',
    health: 'Чаты, статистика и объявления читаются',
    capabilities: [
      { key: 'messenger', label: 'Messenger', status: 'ready', reason: 'Read/write через approval' },
      { key: 'xml', label: 'XML/autoload', status: 'ready', reason: 'Publish заблокирован до validation' },
      { key: 'wallet', label: 'Кошелек', status: 'ready', reason: 'Balance read, top-up вручную' },
    ],
    nextAction: 'Проверить XML full-feed перед publish',
    updatedAt: 'сегодня, 09:42',
  },
  {
    id: 'avito-anomie-msk',
    marketplace: 'avito',
    accountName: 'Anomie studio · Москва',
    owner: 'Максим Б.',
    status: 'blocked',
    tariff: 'не подтвержден',
    health: 'Messenger API недоступен до тарифа',
    capabilities: [
      { key: 'messenger', label: 'Messenger', status: 'blocked', reason: 'Нужен тариф Максимальный' },
      { key: 'stats', label: 'Статистика', status: 'unknown', reason: 'Нужен live API UAT' },
      { key: 'wallet', label: 'Кошелек', status: 'unknown', reason: 'Balance endpoint не подтвержден' },
    ],
    nextAction: 'Подтвердить тариф и scope на аккаунте',
    updatedAt: 'вчера, 17:18',
  },
  {
    id: 'avito-bless-spb',
    marketplace: 'avito',
    accountName: 'Bless T · Санкт-Петербург',
    owner: 'Максим Б.',
    status: 'missing_access',
    tariff: 'ожидает подключения',
    health: 'Нет OAuth-сессии',
    capabilities: [
      { key: 'auth', label: 'OAuth', status: 'blocked', reason: 'Нужен reauth владельца' },
      { key: 'xml', label: 'XML/autoload', status: 'unknown', reason: 'После OAuth' },
    ],
    nextAction: 'Запросить безопасное подключение аккаунта',
    updatedAt: '2 дня назад',
  },
]

let notificationRoutes: NotificationRoute[] = [
  {
    id: 'route-wb-price',
    event: 'WB цена ниже P_min',
    marketplace: 'wb',
    priority: 'critical',
    recipients: ['Мария Ф.', 'Максим Б.'],
    channels: ['telegram', 'center'],
    workingHours: 'критичные сразу',
    status: 'active',
  },
  {
    id: 'route-avito-wallet',
    event: 'Авито баланс ниже порога',
    marketplace: 'avito',
    priority: 'warning',
    recipients: ['Максим Б.'],
    channels: ['telegram', 'email', 'center'],
    workingHours: '09:00-21:00',
    status: 'needs_setup',
  },
  {
    id: 'route-xml-block',
    event: 'XML validation blocked',
    marketplace: 'avito',
    priority: 'critical',
    recipients: ['Мария Ф.', 'Максим Б.'],
    channels: ['telegram', 'center'],
    workingHours: 'критичные сразу',
    status: 'active',
  },
]

let sessions: SessionInfo[] = [
  { id: 'session-current', device: 'MacBook Pro · Safari', location: 'Екатеринбург', ip: '95.***.***.18', lastActiveAt: 'сейчас', current: true, status: 'active' },
  { id: 'session-office', device: 'Chrome · Windows production', location: 'офис', ip: '188.***.***.42', lastActiveAt: 'сегодня, 08:11', current: false, status: 'active' },
  { id: 'session-old', device: 'iPhone · Telegram WebView', location: 'Москва', ip: '46.***.***.11', lastActiveAt: '5 дней назад', current: false, status: 'revoked' },
]

let auditEvents: SettingsAuditEvent[] = [
  {
    id: 'audit-1007',
    createdAt: 'сегодня, 09:58',
    actor: 'Мария Ф.',
    action: 'access.change.requested',
    marketplace: 'avito',
    target: 'Bless T · Москва',
    result: 'approval_required',
    summary: 'Запрошено право XML publish; требуется approval владельца',
  },
  {
    id: 'audit-1006',
    createdAt: 'сегодня, 09:44',
    actor: 'Система',
    action: 'capability.check',
    marketplace: 'avito',
    target: 'Anomie studio · Москва',
    result: 'blocked',
    summary: 'Messenger заблокирован: тариф Максимальный не подтвержден',
  },
  {
    id: 'audit-1005',
    createdAt: 'вчера, 18:02',
    actor: 'Максим Б.',
    action: 'finance.scope.updated',
    marketplace: 'wb',
    target: 'P&L',
    result: 'success',
    summary: 'Финансовый экспорт оставлен только для finance/owner',
  },
]

function pushAudit(event: Omit<SettingsAuditEvent, 'id' | 'createdAt'>) {
  const auditEvent: SettingsAuditEvent = {
    ...event,
    id: `audit-${Date.now()}`,
    createdAt: 'сейчас',
  }
  auditEvents = [auditEvent, ...auditEvents]
  return auditEvent
}

export function getCurrentUserProfile() {
  return currentUser
}

export function getUsers() {
  return users
}

export function getMarketplaceCapabilities() {
  return capabilities
}

export function getNotificationRoutes() {
  return notificationRoutes
}

export function getSessions() {
  return sessions
}

export function getSettingsAudit() {
  return auditEvents
}

export function getUserAccountScope(userId: string, marketplace: Marketplace) {
  const user = users.find((item) => item.id === userId)
  return user?.scopes.find((scope) => scope.marketplace === marketplace)?.accountIds ?? []
}

export function canUseDangerousPermission(userId: string, permission: DangerousPermission) {
  const user = users.find((item) => item.id === userId)
  return Boolean(user?.scopes.some((scope) => scope.dangerousPermissions.includes(permission)))
}

export function patchUserAccess(userId: string, patch: Partial<Pick<UserAccessProfile, 'role' | 'status'>>): SettingsMutationResult {
  const user = users.find((item) => item.id === userId)
  if (!user) {
    const audit = pushAudit({
      actor: currentUser.name,
      action: 'access.change.blocked',
      marketplace: 'system',
      target: userId,
      result: 'blocked',
      summary: 'Пользователь не найден',
    })
    return { status: 'blocked', message: 'Пользователь не найден', blockedReason: 'unknown_user', auditEventId: audit.id }
  }

  if (patch.role === 'owner' || patch.role === 'admin') {
    const audit = pushAudit({
      actor: currentUser.name,
      action: 'access.change.requested',
      marketplace: 'system',
      target: user.name,
      result: 'approval_required',
      summary: `Запрошена роль ${patch.role === 'owner' ? ROLE_LABELS.owner : ROLE_LABELS.admin}`,
    })
    return {
      status: 'approval_required',
      message: 'Повышение роли требует approval владельца и backend policy check',
      approvalId: `apr-${audit.id}`,
      auditEventId: audit.id,
    }
  }

  users = users.map((item) =>
    item.id === userId
      ? {
          ...item,
          ...patch,
          roleLabel: patch.role ? ROLE_LABELS[patch.role] : item.roleLabel,
        }
      : item,
  )
  const audit = pushAudit({
    actor: currentUser.name,
    action: 'access.change.saved',
    marketplace: 'system',
    target: user.name,
    result: 'success',
    summary: 'Изменение доступа сохранено в mock contract',
  })
  return { status: 'success', message: 'Доступ обновлен', auditEventId: audit.id }
}

export function updateNotificationRoutes(nextRoutes: NotificationRoute[]): SettingsMutationResult {
  notificationRoutes = nextRoutes
  const audit = pushAudit({
    actor: currentUser.name,
    action: 'notification.routes.updated',
    marketplace: 'system',
    target: 'Уведомления',
    result: 'success',
    summary: 'Маршруты уведомлений обновлены в mock contract',
  })
  return { status: 'success', message: 'Маршруты уведомлений обновлены', auditEventId: audit.id }
}

export function revokeSession(sessionId: string): SettingsMutationResult {
  const session = sessions.find((item) => item.id === sessionId)
  if (!session) {
    const audit = pushAudit({
      actor: currentUser.name,
      action: 'session.revoke.blocked',
      marketplace: 'system',
      target: sessionId,
      result: 'blocked',
      summary: 'Сессия не найдена',
    })
    return { status: 'blocked', message: 'Сессия не найдена', blockedReason: 'unknown_session', auditEventId: audit.id }
  }
  if (session.current) {
    const audit = pushAudit({
      actor: currentUser.name,
      action: 'session.revoke.blocked',
      marketplace: 'system',
      target: session.device,
      result: 'blocked',
      summary: 'Текущая сессия не отзывается из списка',
    })
    return { status: 'blocked', message: 'Текущую сессию нельзя отозвать здесь', blockedReason: 'current_session', auditEventId: audit.id }
  }
  sessions = sessions.map((item) => (item.id === sessionId ? { ...item, status: 'revoked' } : item))
  const audit = pushAudit({
    actor: currentUser.name,
    action: 'session.revoked',
    marketplace: 'system',
    target: session.device,
    result: 'success',
    summary: 'Сессия отозвана',
  })
  return { status: 'success', message: 'Сессия отозвана', auditEventId: audit.id }
}
