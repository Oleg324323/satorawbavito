import { apiRequest, ApiError } from '@/lib/api'
import { authorizationHeaders } from '@/features/auth/authApi'
import { resetLiveRepricerParityCache, type LiveRepricerPeriodRequest, type LiveRepricerSyncStatus } from './liveParityData'

export function missingPeriodSources(cache?: Record<string, unknown>, period?: LiveRepricerPeriodRequest) {
  return (['period-stats', 'finance', 'baskets', 'ads'] as const).filter(source => {
    const key = source === 'period-stats' ? 'periodStats' : source
    const coverageKey = source === 'period-stats' ? 'orders' : source
    const coverage = cache?.periodCoverage as Record<string, { state?: string }> | undefined
    if (coverage?.[coverageKey]?.state && coverage[coverageKey].state !== 'ok') return true
    if (cache?.[`${key}FreshnessState`] === 'stale') return true
    if (source === 'ads' && period?.dateFrom && period.dateTo
      && (cache?.adsDateFrom !== period.dateFrom || cache?.adsDateTo !== period.dateTo)) return true
    if (source === 'baskets' && cache?.basketsPeriodConversionState === 'no_data') return true
    return !cache?.[`${key}FetchedAt`] || ['partial', 'no_data'].includes(String(cache?.[`${key}CoverageState`]))
  })
}

export async function preparePeriod(token: string, period: LiveRepricerPeriodRequest,
  sources: string[], isCurrent: () => boolean, signal?: AbortSignal,
  onProgress?: (status: LiveRepricerSyncStatus) => Promise<void> | void) {
  const options = { headers: authorizationHeaders(token), signal, cache: 'no-store' as const }
  const deadline = Date.now() + 10 * 60_000
  let runId: string | null | undefined
  let started = false
  let waitingForOther = false
  while (Date.now() < deadline) {
    signal?.throwIfAborted()
    if (!isCurrent()) return false
    if (!started && !waitingForOther) {
      try {
        const run = await apiRequest<LiveRepricerSyncStatus>('/api/v1/wb-repricer/sync/run', {
          ...options, method: 'POST', body: JSON.stringify({ ...period, sources, mode: 'manual', force: false }),
        })
        if (!run?.runId) throw new Error('Сервер не подтвердил запуск загрузки периода')
        runId = run.runId
        started = true
      } catch (error) {
        if (!(error instanceof ApiError) || error.status !== 409) throw error
        waitingForOther = true
      }
    }
    await new Promise(resolve => setTimeout(resolve, 3000))
    if (!isCurrent()) return false
    const status = await apiRequest<LiveRepricerSyncStatus>('/api/v1/wb-repricer/sync/status', options)
    signal?.throwIfAborted()
    if (!isCurrent()) return false
    if (!status) throw new Error('Не удалось получить статус загрузки периода')
    if (!status.running) waitingForOther = false
    // A 409 means another caller already owns the source sync. Join that run
    // when it covers this exact request, rather than launching it again later.
    if (!started && status.runId && period.dateFrom && period.dateTo
      && status.dateFrom === period.dateFrom && status.dateTo === period.dateTo
      && sources.every(source => status.sources?.includes(source))) {
      runId = status.runId
      started = true
    }
    if (started && runId && status.runId !== runId) throw new Error('Загрузка периода была заменена. Обновите выбранный период.')
    if (started && !status.running) {
      resetLiveRepricerParityCache(token)
      if (status.state !== 'completed') throw new Error('WB вернул неполные данные периода. Проверьте статус загрузки; неизвестные показатели не рассчитаны.')
      return true
    }
    if (started && status.running) await onProgress?.(status)
  }
  throw new Error('Загрузка WB ещё не завершена. Проверьте статус загрузки и обновите список позже.')
}
