import { buildApiUrl } from '@/lib/api'
import {
  encodeReviewNotificationAction, parseReviewNotificationCapabilities, parseReviewNotificationReceipts,
  parseReviewNotificationVisible, reviewNotificationPath, reviewNotificationReceiptsPath,
  parseReviewNotificationList, reviewNotificationListPath,
  type ReviewNotificationAction, type ReviewNotificationScope,
} from './canonicalReviewNotifications'

export type ReviewNotificationFailure = { state: 'invalid-request' | 'unauthenticated' | 'no-access'
  | 'not-found' | 'conflict' | 'unavailable' | 'invalid-response' | 'stale'; outcome: 'not-submitted' | 'unknown' }
export type ReviewNotificationResponse<T> = { state: 'ready'; data: T } | ReviewNotificationFailure

/** Dormant client. Create per session/membership/account epoch, including A→B→A.
 * No local storage, global cache, token refresh, implicit retry or auto-mark.
 * After any unknown receipt result, explicitly reload visible IDs before deciding.
 */
export function createCanonicalReviewNotificationsClient(options: {
  scope: ReviewNotificationScope; recipientMembershipId?: number; accessToken: string
  isCurrent: () => boolean; maxVisibleIds: number; maxResponseBytes: number; fetch?: typeof fetch; timeoutMs?: number
}) {
  const scope = { ...options.scope }, member = options.recipientMembershipId ?? 0, token = options.accessToken
  const current = options.isCurrent, fetcher = options.fetch ?? globalThis.fetch
  const maxIds = options.maxVisibleIds, maxBytes = options.maxResponseBytes
  const timeoutMs = options.timeoutMs ?? 20_000
  if (![maxIds, maxBytes].every(n => Number.isInteger(n) && n > 0 && n <= 2147483647)
    || !Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 60_000
    || typeof current !== 'function') throw new Error('CANONICAL_REVIEW_NOTIFICATION_CONFIGURATION_INVALID')
  const pending = new Set<AbortController>()
  let disposed = false, sequence = 0
  const active = () => !disposed && current()
  const failure = (state: ReviewNotificationFailure['state'], submitted = false): ReviewNotificationFailure => ({
    state, outcome: submitted ? 'unknown' : 'not-submitted',
  })
  const ids = (values: string[]) => {
    if (!Array.isArray(values) || !values.length || values.length > maxIds) throw new Error('CANONICAL_REVIEW_NOTIFICATION_INVALID')
    const copied = [...values]
    reviewNotificationPath(scope, copied)
    return copied
  }
  async function request<T>(path: string, parse: (input: unknown) => T, body?: string, discovery = false): Promise<ReviewNotificationResponse<T>> {
    const requestSequence = ++sequence
    if (!active()) return failure('stale')
    if ((!discovery && (!Number.isInteger(member) || member < 1 || member > 2147483647)) || typeof token !== 'string'
      || !token || /[^\x21-\x7e]/.test(token)) return failure('unauthenticated')
    const controller = new AbortController(), submitted = body !== undefined
    pending.add(controller)
    const timeout = setTimeout(() => controller.abort(), timeoutMs)
    const stale = () => !active() || sequence !== requestSequence
    try {
      const response = await fetcher(buildApiUrl(path), { method: submitted ? 'POST' : 'GET', cache: 'no-store',
        credentials: 'omit', redirect: 'error', signal: controller.signal,
        headers: { Authorization: `Bearer ${token}`, Accept: 'application/json',
          ...(submitted ? { 'Content-Type': 'application/json' } : {}) }, ...(submitted ? { body } : {}),
      })
      if (stale()) { await response.body?.cancel(); return failure('stale', submitted) }
      if (response.status !== 200) {
        await response.body?.cancel()
        const states: Partial<Record<number, ReviewNotificationFailure['state']>> = {
          400: 'invalid-request', 401: 'unauthenticated', 403: 'no-access', 404: 'not-found', 409: 'conflict',
        }
        return failure(states[response.status] ?? 'unavailable', submitted)
      }
      if (response.headers.get('content-type')?.split(';')[0].trim().toLowerCase() !== 'application/json' || !response.body) {
        await response.body?.cancel()
        return failure('invalid-response', submitted)
      }
      const reader = response.body.getReader(), chunks: Uint8Array[] = []
      let size = 0
      try {
        for (;;) {
          const part = await reader.read()
          if (stale()) { await reader.cancel(); return failure('stale', submitted) }
          if (part.done) break
          size += part.value.byteLength
          if (size > maxBytes) { await reader.cancel(); return failure('invalid-response', submitted) }
          chunks.push(part.value)
        }
      } finally { reader.releaseLock() }
      const bytes = new Uint8Array(size)
      let offset = 0
      for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength }
      let data: T
      try { data = parse(JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes))) }
      catch { return failure(stale() ? 'stale' : 'invalid-response', submitted) }
      return stale() ? failure('stale', submitted) : { state: 'ready', data }
    } catch { return failure(stale() ? 'stale' : 'unavailable', submitted) }
    finally { clearTimeout(timeout); pending.delete(controller) }
  }
  return {
    dispose() { disposed = true; for (const controller of pending) controller.abort(); pending.clear() },
    async list(cursor: string | null = null) {
      let path: string
      try { path = reviewNotificationListPath(scope, cursor) } catch { return failure('invalid-request') }
      return request(path, input => {
        const result = parseReviewNotificationList(input, scope)
        if (result.items.length > maxIds) throw new Error('CANONICAL_REVIEW_NOTIFICATION_INVALID')
        return result
      }, undefined, true)
    },
    async visible(eventIds: string[]) {
      let selected: string[]
      try { selected = ids(eventIds) } catch { return failure('invalid-request') }
      return request(reviewNotificationPath(scope, selected), input => parseReviewNotificationVisible(input,
        { ...scope, recipientMembershipId: member, eventIds: selected }))
    },
    async capabilities(eventIds: string[]) {
      let selected: string[]
      try { selected = ids(eventIds) } catch { return failure('invalid-request') }
      return request(reviewNotificationPath(scope, selected, true), input => parseReviewNotificationCapabilities(input,
        { ...scope, recipientMembershipId: member, eventIds: selected }))
    },
    async mark(eventIds: string[], action: ReviewNotificationAction) {
      let selected: string[], body: string
      try { selected = ids(eventIds); body = encodeReviewNotificationAction(scope, selected, action) }
      catch { return failure('invalid-request') }
      return request(reviewNotificationReceiptsPath, input => parseReviewNotificationReceipts(input,
        { ...scope, recipientMembershipId: member, eventIds: selected }, action), body)
    },
  }
}
