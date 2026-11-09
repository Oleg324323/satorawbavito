import { apiData, apiRequest } from '@/lib/api'
import {
  authorizationHeaders,
  type CabinetMeView,
  type PermissionProfile,
  type TeamUserView,
  type UserPreferencesView,
} from '@/features/auth/authApi'
import type {
  AccessRole,
  AccountCapability,
  DangerousPermission,
  Marketplace,
  MarketplaceScope,
  SettingsAuditEvent,
  UserAccessProfile,
} from './types'

type IntegrationStatus = 'disconnected' | 'connected' | 'error'

type IntegrationView = {
  integrationId: number
  organizationId: number
  provider: Marketplace
  status: IntegrationStatus
  externalAccountId: string | null
  tokenRef: string | null
  metadata: Record<string, unknown>
  updatedByUserId: string | null
  updatedAt: string
}

export type UserWbTokenView = {
  userId: string
  hasToken: boolean
  tokenMasked: string | null
  updatedAt: string | null
}

export type UserAvitoCredentialsView = {
  userId: string
  hasCredentials: boolean
  clientIdMasked: string | null
  clientSecretMasked: string | null
  accessTokenExpiresAt: string | null
  updatedAt: string | null
}

type AuditEventView = {
  eventId: number
  actorUserId: string | null
  action: string
  objectType: string
  objectId: string
  details: Record<string, unknown> | null
  reason: string | null
  createdAt: string
}

type PaginatedEnvelope<T> = {
  items: T[]
  total: number
  limit: number
  offset: number
  timestamp: string
}

export type SettingsUserAccessProfile = UserAccessProfile & {
  permissionProfile: PermissionProfile
  permissions: string[]
}

export type SettingsSnapshot = {
  users: SettingsUserAccessProfile[]
  accounts: AccountCapability[]
  auditEvents: SettingsAuditEvent[]
  preferences: UserPreferencesView
  wbToken: UserWbTokenView
  avitoCredentials: UserAvitoCredentialsView
  approvalsPending: number
}

export type SettingsShellSnapshot = Pick<
  SettingsSnapshot,
  'users' | 'wbToken' | 'avitoCredentials'
> & {
  approvalsPending?: number
}

export const DANGEROUS_PERMISSION_LABELS: Record<DangerousPermission, string> = {
  finance_view: 'Финансы',
  price_send: 'Отправка цен',
  xml_publish: 'Публикация XML',
  message_send: 'Сообщения',
  review_send: 'Ответы на отзывы',
  wallet_topup: 'Кошельки',
  role_admin: 'Роли и доступы',
  token_admin: 'Интеграции и токены',
}

const PROFILE_ROLE_LABELS: Record<PermissionProfile, string> = {
  viewer: 'Только просмотр',
  settings_editor: 'Редактор настроек',
  price_sender: 'Отправка цен',
  finance_viewer: 'Финансы',
  admin: 'Администратор',
  custom: 'Индивидуальный профиль',
}

const PROFILE_ROLE_MAP: Record<PermissionProfile, AccessRole> = {
  viewer: 'viewer',
  settings_editor: 'manager',
  price_sender: 'manager',
  finance_viewer: 'finance',
  admin: 'admin',
  custom: 'manager',
}

const EDITABLE_PERMISSION_PROFILES: PermissionProfile[] = ['viewer', 'settings_editor', 'price_sender', 'finance_viewer', 'admin']

function hasPermission(permissions: string[], permission: string) {
  return permissions.includes(permission)
}

function initialsFromName(fullName: string) {
  const parts = fullName.split(/\s+/).map((part) => part.trim()).filter(Boolean)
  if (parts.length === 0) return 'OG'
  return parts.slice(0, 2).map((part) => part[0]?.toUpperCase() ?? '').join('')
}

function formatDateTime(iso: string | null) {
  if (!iso) return '-'
  return new Intl.DateTimeFormat('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(iso))
}

function prettyProvider(provider: Marketplace) {
  return provider === 'wb' ? 'WB' : 'Авито'
}

function integrationStatusToAccountStatus(status: IntegrationStatus): AccountCapability['status'] {
  if (status === 'connected') return 'ready'
  if (status === 'error') return 'blocked'
  return 'missing_access'
}

function integrationStatusToScopeStatus(status: IntegrationStatus, canWrite: boolean): MarketplaceScope['status'] {
  if (status === 'error') return 'blocked'
  if (status === 'connected') return canWrite ? 'full' : 'limited'
  return 'view_only'
}

function effectiveWbIntegration(integrations: Map<Marketplace, IntegrationView>, wbToken: UserWbTokenView) {
  const existing = integrations.get('wb')
  if (existing) return existing
  if (!wbToken.hasToken) return undefined
  return {
    integrationId: 0,
    organizationId: 0,
    provider: 'wb' as const,
    status: 'connected' as const,
    externalAccountId: 'wb-main',
    tokenRef: wbToken.tokenMasked,
    metadata: {},
    updatedByUserId: null,
    updatedAt: wbToken.updatedAt ?? new Date().toISOString(),
  }
}

function effectiveAvitoIntegration(
  integrations: Map<Marketplace, IntegrationView>,
  avitoCredentials: UserAvitoCredentialsView,
) {
  const existing = integrations.get('avito')
  if (existing) return existing
  if (!avitoCredentials.hasCredentials) return undefined
  return {
    integrationId: 0,
    organizationId: 0,
    provider: 'avito' as const,
    status: 'connected' as const,
    externalAccountId: 'avito-main',
    tokenRef: avitoCredentials.clientIdMasked,
    metadata: {},
    updatedByUserId: null,
    updatedAt: avitoCredentials.updatedAt ?? new Date().toISOString(),
  }
}

function deriveMarketplaceScopes(
  permissions: string[],
  integrations: Map<Marketplace, IntegrationView>,
  wbToken: UserWbTokenView,
  avitoCredentials: UserAvitoCredentialsView,
): MarketplaceScope[] {
  const wbIntegration = effectiveWbIntegration(integrations, wbToken)
  const avitoIntegration = effectiveAvitoIntegration(integrations, avitoCredentials)
  const wbModules = ['Репрайсер', 'Отчеты']
  if (hasPermission(permissions, 'finance:read')) wbModules.push('P&L')
  if (hasPermission(permissions, 'reviews:read')) wbModules.push('Отзывы')
  if (hasPermission(permissions, 'settings:read')) wbModules.push('Настройки')

  const wbDangerousPermissions: DangerousPermission[] = []
  if (hasPermission(permissions, 'finance:read')) wbDangerousPermissions.push('finance_view')
  if (hasPermission(permissions, 'price:send')) wbDangerousPermissions.push('price_send')
  if (hasPermission(permissions, 'reviews:send') || hasPermission(permissions, 'reviews:write')) wbDangerousPermissions.push('review_send')

  return [
    {
      marketplace: 'wb',
      status: integrationStatusToScopeStatus(wbIntegration?.status ?? 'disconnected', hasPermission(permissions, 'price:send')),
      modules: wbModules,
      accountIds: wbIntegration?.externalAccountId ? [wbIntegration.externalAccountId] : [],
      dangerousPermissions: wbDangerousPermissions,
      blockers: wbIntegration ? [] : ['WB token не подключен'],
    },
    {
      marketplace: 'avito',
      status: integrationStatusToScopeStatus(avitoIntegration?.status ?? 'disconnected', hasPermission(permissions, 'integrations:write')),
      modules: hasPermission(permissions, 'integrations:read') ? ['Интеграции', 'Уведомления'] : ['Интеграции'],
      accountIds: avitoIntegration?.externalAccountId ? [avitoIntegration.externalAccountId] : [],
      dangerousPermissions: [],
      blockers: avitoIntegration ? [] : ['Avito client_id/client_secret не подключены'],
    },
  ]
}

function mapTeamUserToAccessProfile(
  user: TeamUserView,
  integrations: Map<Marketplace, IntegrationView>,
  wbToken: UserWbTokenView,
  avitoCredentials: UserAvitoCredentialsView,
): SettingsUserAccessProfile {
  const roleLabel = PROFILE_ROLE_LABELS[user.permissionProfile] ?? user.permissionProfile
  return {
    id: user.userId,
    name: user.fullName,
    initials: initialsFromName(user.fullName),
    email: user.email,
    role: PROFILE_ROLE_MAP[user.permissionProfile] ?? 'manager',
    roleLabel,
    status: user.isActive ? 'active' : 'blocked',
    scopes: deriveMarketplaceScopes(user.permissions ?? [], integrations, wbToken, avitoCredentials),
    lastActiveAt: formatDateTime(user.createdAt),
    permissionProfile: user.permissionProfile,
    permissions: user.permissions ?? [],
  }
}

function mapIntegrationToCapability(
  integration: IntegrationView,
  userNameById: Map<string, string>,
  wbToken: UserWbTokenView,
  avitoCredentials: UserAvitoCredentialsView,
): AccountCapability {
  const isWb = integration.provider === 'wb'
  const tokenReady = isWb ? wbToken.hasToken : avitoCredentials.hasCredentials || Boolean(integration.tokenRef)
  return {
    id: integration.externalAccountId ?? `${integration.provider}-${integration.integrationId}`,
    marketplace: integration.provider,
    accountName: `${prettyProvider(integration.provider)} · ${integration.externalAccountId ?? 'account'}`,
    owner: integration.updatedByUserId ? userNameById.get(integration.updatedByUserId) ?? integration.updatedByUserId : 'Система',
    status: integrationStatusToAccountStatus(integration.status),
    tariff: isWb ? 'Base Token' : 'API access',
    health: tokenReady ? 'Доступ подключен' : 'Доступ не подключен',
    capabilities: [
      { key: 'token', label: isWb ? 'Токен' : 'OAuth', status: tokenReady ? 'ready' : 'blocked', reason: tokenReady ? 'Доступ сохранён' : 'Нужны credentials' },
      { key: 'api', label: 'API', status: integration.status === 'connected' ? 'ready' : 'unknown', reason: integration.status },
    ],
    nextAction: tokenReady ? 'Проверить live endpoints' : 'Подключить доступ',
    updatedAt: formatDateTime(integration.updatedAt),
  }
}

function inferMarketplace(event: AuditEventView): Marketplace | 'system' {
  const provider = event.details?.provider
  if (provider === 'wb' || provider === 'avito') return provider
  if (event.objectType.includes('integration')) return 'system'
  return 'system'
}

function inferAuditResult(event: AuditEventView): SettingsAuditEvent['result'] {
  if (event.action.includes('blocked')) return 'blocked'
  if (event.action.includes('approval')) return 'approval_required'
  return 'success'
}

function mapAuditEvent(event: AuditEventView, userNameById: Map<string, string>): SettingsAuditEvent {
  return {
    id: String(event.eventId),
    createdAt: formatDateTime(event.createdAt),
    actor: event.actorUserId ? userNameById.get(event.actorUserId) ?? event.actorUserId : 'Система',
    action: event.action,
    marketplace: inferMarketplace(event),
    target: event.objectId || event.objectType,
    result: inferAuditResult(event),
    summary: event.reason || event.action,
  }
}

export function editablePermissionProfiles() {
  return EDITABLE_PERMISSION_PROFILES.map((profile) => ({
    value: profile,
    label: PROFILE_ROLE_LABELS[profile],
  }))
}

export async function updateSettingsUserPermissionProfile(
  accessToken: string,
  userId: string,
  permissionProfile: PermissionProfile,
) {
  return apiData<TeamUserView>(`/api/v1/cabinet/team/users/${userId}/permission-profile`, {
    method: 'PATCH',
    headers: authorizationHeaders(accessToken),
    body: JSON.stringify({ permissionProfile, reason: 'updated from settings ui' }),
  })
}

export async function createSettingsTeamUser(
  accessToken: string,
  payload: {
    email: string
    fullName: string
    password: string
    permissionProfile: PermissionProfile
  },
) {
  return apiData<TeamUserView>('/api/v1/cabinet/team/users', {
    method: 'POST',
    headers: authorizationHeaders(accessToken),
    body: JSON.stringify(payload),
  })
}

export async function upsertCurrentUserWbToken(accessToken: string, wbToken: string) {
  if (import.meta.env.VITE_WB_LIVE_ENABLED === 'true') throw new Error('Используйте подключение выбранного аккаунта WB')
  return apiData<UserWbTokenView>('/api/v1/cabinet/wb-token', {
    method: 'PUT', headers: authorizationHeaders(accessToken), body: JSON.stringify({ wbToken }),
  })
}

export async function deleteCurrentUserWbToken(accessToken: string) {
  if (import.meta.env.VITE_WB_LIVE_ENABLED === 'true') throw new Error('Используйте подключение выбранного аккаунта WB')
  return apiData<UserWbTokenView>('/api/v1/cabinet/wb-token', {
    method: 'DELETE', headers: authorizationHeaders(accessToken),
  })
}

export async function upsertCurrentUserAvitoCredentials(accessToken: string, clientId: string, clientSecret: string) {
  return apiData<UserAvitoCredentialsView>('/api/v1/cabinet/avito-credentials', {
    method: 'PUT',
    headers: authorizationHeaders(accessToken),
    body: JSON.stringify({ clientId, clientSecret }),
  })
}

export async function deleteCurrentUserAvitoCredentials(accessToken: string) {
  return apiData<UserAvitoCredentialsView>('/api/v1/cabinet/avito-credentials', {
    method: 'DELETE',
    headers: authorizationHeaders(accessToken),
  })
}

export async function loadSettingsShellSnapshot(accessToken: string): Promise<SettingsShellSnapshot> {
  const headers = authorizationHeaders(accessToken)
  const [teamUsers, wbToken, avitoCredentials] = await Promise.all([
    apiData<TeamUserView[]>('/api/v1/cabinet/team/users', { headers }),
    apiData<UserWbTokenView>('/api/v1/cabinet/wb-token', { headers }),
    apiData<UserAvitoCredentialsView>('/api/v1/cabinet/avito-credentials', { headers }),
  ])

  return {
    users: teamUsers.map((user) => mapTeamUserToAccessProfile(user, new Map(), wbToken, avitoCredentials)),
    wbToken,
    avitoCredentials,
  }
}

export async function loadSettingsSnapshot(accessToken: string, me: CabinetMeView): Promise<SettingsSnapshot> {
  const headers = authorizationHeaders(accessToken)
  const [teamUsers, integrations, auditEnvelope, preferences, wbToken, avitoCredentials] = await Promise.all([
    apiData<TeamUserView[]>('/api/v1/cabinet/team/users', { headers }),
    apiData<IntegrationView[]>('/api/v1/cabinet/integrations', { headers }),
    apiRequest<PaginatedEnvelope<AuditEventView>>('/api/v1/cabinet/audit/events?limit=100', { headers }),
    apiData<UserPreferencesView>('/api/v1/cabinet/preferences', { headers }),
    apiData<UserWbTokenView>('/api/v1/cabinet/wb-token', { headers }),
    apiData<UserAvitoCredentialsView>('/api/v1/cabinet/avito-credentials', { headers }),
  ])

  const integrationMap = new Map<Marketplace, IntegrationView>(integrations.map((item) => [item.provider, item]))
  const wbIntegration = effectiveWbIntegration(integrationMap, wbToken)
  if (wbIntegration) integrationMap.set('wb', wbIntegration)
  const avitoIntegration = effectiveAvitoIntegration(integrationMap, avitoCredentials)
  if (avitoIntegration) integrationMap.set('avito', avitoIntegration)

  const userNameById = new Map<string, string>(teamUsers.map((user) => [user.userId, user.fullName]))
  userNameById.set(me.user.userId, me.user.fullName)

  const users = teamUsers.map((user) => mapTeamUserToAccessProfile(user, integrationMap, wbToken, avitoCredentials))
  const accounts = Array.from(integrationMap.values()).map((integration) =>
    mapIntegrationToCapability(integration, userNameById, wbToken, avitoCredentials),
  )
  const auditEvents = (auditEnvelope?.items ?? []).map((event) => mapAuditEvent(event, userNameById))

  return {
    users,
    accounts,
    auditEvents,
    preferences,
    wbToken,
    avitoCredentials,
    approvalsPending: auditEvents.filter((item) => item.result === 'approval_required').length,
  }
}
