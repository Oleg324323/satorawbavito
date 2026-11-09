import { z } from 'zod'
import { buildApiUrl } from '@/lib/api'

const dateOnly = z.string().regex(/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/).refine(value => {
  const parsed = new Date(`${value}T00:00:00Z`)
  return !value.startsWith('0000-') && Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value
})
const periodSchema = z.object({ dateFrom: dateOnly, dateTo: dateOnly }).strict().refine(value => {
  const days = (Date.parse(value.dateTo) - Date.parse(value.dateFrom)) / 86_400_000
  return days >= 0 && days < 270
})
const accountSchema = z.object({ marketplaceAccountId: z.number().int().positive().max(2147483647),
  provider: z.literal('avito'), externalAccountId: z.string().regex(/^[1-9][0-9]{0,127}$/) }).strict()
// Defensive client budgets, not advertised provider limits. Never coerce metrics to Number.
const metric = z.string().max(8192).regex(/^(0|[1-9][0-9]*)$/).nullable()
const metricsSchema = z.object({ impressions: metric, views: metric, contactsMessenger: metric, contacts: metric,
  contactsShowPhone: metric, contactsShowPhoneAndMessenger: metric, favorites: metric, spendKopecks: metric,
  orders: metric, buyouts: metric }).strict()
const dataSchema = accountSchema.extend({ dateFrom: dateOnly, dateTo: dateOnly, status: z.enum(['synced', 'partial']),
  rows: z.array(z.object({ itemId: z.string().max(160), sourceStatus: z.enum(['fresh', 'partial', 'stale']), metrics: metricsSchema }).strict()).length(1),
  daily: z.array(z.object({ date: dateOnly, metrics: metricsSchema }).strict()).max(270),
}).strict()
export type CanonicalAvitoStatisticsAccount = z.infer<typeof accountSchema>
export type CanonicalAvitoStatisticsPeriod = z.infer<typeof periodSchema>
export type CanonicalAvitoStatistics = z.infer<typeof dataSchema>
export class CanonicalAvitoStatisticsError extends Error {
  readonly kind: 'scope' | 'invalid' | 'unavailable' | 'stale' | 'timeout'
  constructor(kind: CanonicalAvitoStatisticsError['kind']) {
    super(kind === 'scope' ? 'Нет доступа к статистике выбранного аккаунта Авито.'
      : kind === 'invalid' ? 'Некорректный период или ответ статистики Авито. Данные не показаны.'
        : kind === 'stale' ? 'Запрос статистики отменён: выбор или сессия изменились.'
          : kind === 'timeout' ? 'Истекло время ожидания статистики Авито. Автоматического повтора нет.'
            : 'Статистика Авито недоступна. Автоматического повтора нет.')
    this.kind = kind
  }
}
export function buildCanonicalAvitoStatisticsPath(account: CanonicalAvitoStatisticsAccount, period: CanonicalAvitoStatisticsPeriod) {
  try { accountSchema.parse(account); periodSchema.parse(period) } catch { throw new CanonicalAvitoStatisticsError('invalid') }
  return `/api/v2/avito/accounts/${account.marketplaceAccountId}/statistics?${new URLSearchParams(period)}`
}
export function parseCanonicalAvitoStatistics(value: unknown, account: CanonicalAvitoStatisticsAccount, period: CanonicalAvitoStatisticsPeriod): CanonicalAvitoStatistics {
  try {
    accountSchema.parse(account); periodSchema.parse(period)
    const data = z.object({ data: dataSchema }).strict().parse(value).data
    if (data.marketplaceAccountId !== account.marketplaceAccountId || data.externalAccountId !== account.externalAccountId
      || data.dateFrom !== period.dateFrom || data.dateTo !== period.dateTo
      || data.rows[0].itemId !== `account:${account.externalAccountId}:totals`
      || new Set(data.daily.map(row => row.date)).size !== data.daily.length
      || data.daily.some(row => row.date < period.dateFrom || row.date > period.dateTo)) throw new Error()
    return data
  } catch { throw new CanonicalAvitoStatisticsError('invalid') }
}

/** One client per auth/account epoch. Call load only on explicit user submission.
 * Discovery metadata is not authorization. The server decides cabinet:read and rollout access.
 * Dispose on identity change/unmount; invalidate when editable selection changes, even A→B→A.
 * No effects, cache, retries, token refresh, legacy fallback or provider calls here.
 */
export function createCanonicalAvitoStatisticsClient(token: string, account: CanonicalAvitoStatisticsAccount,
  isCurrent: () => boolean, fetcher: typeof fetch = fetch) {
  let captured: CanonicalAvitoStatisticsAccount
  try { captured = accountSchema.parse(account) } catch { throw new CanonicalAvitoStatisticsError('invalid') }
  let disposed = false, sequence = 0, pending: AbortController | undefined
  function invalidate() { sequence++; pending?.abort(); pending = undefined }
  return {
    invalidate,
    dispose() { disposed = true; invalidate() },
    async load(period: CanonicalAvitoStatisticsPeriod, signal?: AbortSignal) {
      // Validate and copy before awaiting; caller edits cannot change response identity checks.
      const path = buildCanonicalAvitoStatisticsPath(captured, period), expected = { ...period }
      invalidate()
      const request = sequence, controller = new AbortController(), abort = () => controller.abort()
      const current = () => !disposed && request === sequence && isCurrent() && !controller.signal.aborted
      if (!token || signal?.aborted || !current()) throw new CanonicalAvitoStatisticsError('stale')
      pending = controller
      signal?.addEventListener('abort', abort, { once: true })
      let timedOut = false, complete = false, response: Response | undefined, reader: ReadableStreamDefaultReader<Uint8Array> | undefined
      const timer = setTimeout(() => { timedOut = true; abort() }, 20_000)
      const cancelled = new Promise<never>((_, reject) => controller.signal.addEventListener('abort', () => reject(new CanonicalAvitoStatisticsError(timedOut ? 'timeout' : 'stale')), { once: true }))
      void cancelled.catch(() => { /* fetch may throw synchronously before the first race */ })
      try {
        const fetching = fetcher(buildApiUrl(path), { method: 'GET', cache: 'no-store', credentials: 'omit', redirect: 'error', signal: controller.signal,
          headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' } }).then(async result => {
          // A transport ignoring abort still must not retain a late response body.
          if (controller.signal.aborted) { try { await result.body?.cancel() } catch { /* preserve cancellation */ } }
          return result
        })
        response = await Promise.race([fetching, cancelled])
        if (!current()) throw new CanonicalAvitoStatisticsError('stale')
        if (response.status !== 200) throw new CanonicalAvitoStatisticsError(response.status === 401 || response.status === 403 ? 'scope'
          : response.status === 400 || response.status === 422 ? 'invalid' : 'unavailable')
        if (response.headers.get('content-type')?.split(';')[0].trim().toLowerCase() !== 'application/json' || !response.body) throw new CanonicalAvitoStatisticsError('invalid')
        reader = response.body.getReader()
        const decoder = new TextDecoder('utf-8', { fatal: true })
        let bytes = 0, text = ''
        while (true) {
          const chunk = await Promise.race([reader.read(), cancelled])
          if (!current()) throw new CanonicalAvitoStatisticsError('stale')
          if (chunk.done) break
          bytes += chunk.value.byteLength
          if (bytes > 1_048_576) throw new CanonicalAvitoStatisticsError('invalid')
          text += decoder.decode(chunk.value, { stream: true })
        }
        const data = parseCanonicalAvitoStatistics(JSON.parse(text + decoder.decode()), captured, expected)
        complete = true
        return data
      } catch (error) {
        if (error instanceof CanonicalAvitoStatisticsError) throw error
        throw new CanonicalAvitoStatisticsError(timedOut ? 'timeout' : !current() ? 'stale' : response ? 'invalid' : 'unavailable')
      } finally {
        if (!complete) {
          abort()
          try { if (reader) await reader.cancel(); else await response?.body?.cancel() } catch { /* preserve sanitized error */ }
        }
        try { reader?.releaseLock() } catch { /* preserve read result */ }
        clearTimeout(timer); signal?.removeEventListener('abort', abort)
        if (pending === controller) pending = undefined
      }
    },
  }
}
