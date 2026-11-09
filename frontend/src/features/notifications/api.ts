import { authorizationHeaders } from '@/features/auth/authApi'
import { apiData } from '@/lib/api'
import type { NotificationsResponse } from './types.js'

function requireLegacyInbox() {
  if (import.meta.env.VITE_CANONICAL_NOTIFICATIONS_ENABLED === 'true') throw new Error('Откройте уведомления выбранного аккаунта')
}

export async function fetchNotifications(accessToken: string) {
  requireLegacyInbox()
  const data = await apiData<NotificationsResponse>('/api/v1/notifications', {
    headers: authorizationHeaders(accessToken),
    cache: 'no-store',
  })
  return data.items
}

export async function markBackendNotificationRead(accessToken: string, notificationId: string) {
  requireLegacyInbox()
  const data = await apiData<NotificationsResponse>(`/api/v1/notifications/${encodeURIComponent(notificationId)}/read`, {
    method: 'POST',
    headers: authorizationHeaders(accessToken),
  })
  return data.items
}

export async function markAllBackendNotificationsRead(accessToken: string) {
  requireLegacyInbox()
  const data = await apiData<NotificationsResponse>('/api/v1/notifications/read-all', {
    method: 'POST',
    headers: authorizationHeaders(accessToken),
  })
  return data.items
}
