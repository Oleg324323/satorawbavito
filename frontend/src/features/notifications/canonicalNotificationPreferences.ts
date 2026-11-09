import { z } from 'zod'
import { buildApiUrl } from '@/lib/api'

const flags = {
  email: z.object({ enabled: z.boolean(), dailyDigest: z.boolean(), criticalAlerts: z.boolean() }).strict(),
  telegram: z.object({ enabled: z.boolean() }).strict(),
}
const version = z.string().regex(/^[1-9][0-9]*$/).max(128)
const preferencesSchema = z.object({ schemaVersion: z.literal('notification-preferences-v1'), version, ...flags }).strict()
const updateSchema = z.object({ schemaVersion: z.literal('notification-preferences-update-v1'), expectedVersion: version, ...flags }).strict()
export type CanonicalNotificationPreferences = z.infer<typeof preferencesSchema>
export type CanonicalPreferenceFlags = Pick<CanonicalNotificationPreferences, 'email' | 'telegram'>
export class NotificationPreferencesError extends Error {
  readonly kind: 'conflict' | 'unauthenticated' | 'no-access' | 'unavailable' | 'invalid-response'
  readonly unknownWrite: boolean
  constructor(kind: NotificationPreferencesError['kind'], unknownWrite = false) {
    super(kind === 'conflict' ? 'Настройки изменились. Перечитайте актуальную версию и повторно внесите изменения.'
      : kind === 'unauthenticated' ? 'Сессия завершена. Войдите снова.'
        : kind === 'no-access' ? 'Нет доступа к настройкам уведомлений.'
          : unknownWrite ? 'Результат сохранения неизвестен. Перечитайте настройки перед повтором.'
            : 'Настройки уведомлений недоступны. Повторите чтение.')
    this.kind = kind; this.unknownWrite = unknownWrite
  }
}
export function parseNotificationPreferences(input: unknown) {
  const parsed = preferencesSchema.safeParse(input)
  if (!parsed.success) throw new NotificationPreferencesError('invalid-response')
  return parsed.data
}
export function encodeNotificationPreferences(expectedVersion: string, values: CanonicalPreferenceFlags) {
  return JSON.stringify(updateSchema.parse({ schemaVersion: 'notification-preferences-update-v1', expectedVersion, ...values }))
}
export async function requestNotificationPreferences(accessToken: string, signal: AbortSignal,
  update?: { expectedVersion: string; values: CanonicalPreferenceFlags }, fetcher: typeof fetch = fetch) {
  const body = update ? encodeNotificationPreferences(update.expectedVersion, update.values) : undefined
  const controller = new AbortController()
  const abort = () => controller.abort()
  if (signal.aborted) controller.abort()
  signal.addEventListener('abort', abort, { once: true })
  const timeout = setTimeout(abort, 20_000)
  try {
    const response = await fetcher(buildApiUrl('/api/v2/notifications/preferences'), {
      method: update ? 'PUT' : 'GET', cache: 'no-store', credentials: 'omit', redirect: 'error', signal: controller.signal,
      headers: { Authorization: `Bearer ${accessToken}`, Accept: 'application/json', ...(body ? { 'Content-Type': 'application/json' } : {}) },
      ...(body ? { body } : {}),
    })
    if (response.status !== 200) {
      await response.body?.cancel()
      throw new NotificationPreferencesError(response.status === 409 ? 'conflict' : response.status === 401 ? 'unauthenticated' : response.status === 403 ? 'no-access' : 'unavailable', Boolean(update))
    }
    if (response.headers.get('content-type')?.split(';')[0].trim() !== 'application/json' || !response.body) {
      await response.body?.cancel(); throw new NotificationPreferencesError('invalid-response', Boolean(update))
    }
    const reader = response.body.getReader(), decoder = new TextDecoder('utf-8', { fatal: true })
    let bytes = 0, text = ''
    try {
      for (;;) {
        const chunk = await reader.read()
        if (controller.signal.aborted) { await reader.cancel(); throw new NotificationPreferencesError('unavailable', Boolean(update)) }
        if (chunk.done) break
        bytes += chunk.value.byteLength
        if (bytes > 4096) { await reader.cancel(); throw new NotificationPreferencesError('invalid-response', Boolean(update)) }
        text += decoder.decode(chunk.value, { stream: true })
      }
      text += decoder.decode()
    } finally { reader.releaseLock() }
    return parseNotificationPreferences(JSON.parse(text))
  } catch (error) {
    if (error instanceof NotificationPreferencesError) {
      if (update && !error.unknownWrite) throw new NotificationPreferencesError(error.kind, true)
      throw error
    }
    throw new NotificationPreferencesError('unavailable', Boolean(update))
  } finally { clearTimeout(timeout); signal.removeEventListener('abort', abort) }
}
