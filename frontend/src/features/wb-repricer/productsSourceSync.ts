import { resetLiveRepricerParityCache, type LiveRepricerPeriodRequest } from './liveParityData'
import { shouldReadStatsProgress, syncStatsSources, type StatsSourceJob } from './statsSourceSync'
import { missingPeriodSources, preparePeriod } from './preparePeriod'

// Finance remains isolated. Missing operational and campaign observations use the existing
// source-selective queue for precisely the same dates, never a full-account sync.
export async function syncProductsPeriod(token: string, period: LiveRepricerPeriodRequest,
  signal: AbortSignal, isCurrent: () => boolean, readSaved: () => Promise<void>, onStatus: (job: StatsSourceJob) => void = () => {}, cache?: Record<string, unknown>) {
  if (!period.dateFrom || !period.dateTo || !isCurrent() || signal.aborted) return
  const sources = cache ? missingPeriodSources(cache, period).filter(source => source !== 'finance') : ['ads']
  if (cache?.storageStocksRequired === true) sources.push('stocks')
  // Independent sources must not hold Finance behind an operational queue.
  // Serialize only the saved-data reads to prevent an older refresh winning.
  let pendingRead = Promise.resolve()
  const read = () => {
    pendingRead = pendingRead.then(async () => {
      if (!isCurrent() || signal.aborted) return
      resetLiveRepricerParityCache(token)
      await readSaved()
    })
    return pendingRead
  }
  let lastRead = Date.now()
  let publishedCompleted = false
  const progress = async (job: StatsSourceJob) => {
    if (!isCurrent() || signal.aborted) return
    publishedCompleted = job.state === 'completed' && sources.length === 0
    onStatus(job.state === 'completed' && sources.length ? { ...job, state: 'running', label: 'Финансы сохранены; проверяем оперативные показатели…' } : job)
    if (shouldReadStatsProgress(job, lastRead)) {
      await read()
      lastRead = Date.now()
    }
  }
  const coverage = cache?.periodCoverage as Record<string, { state?: string }> | undefined
  const comparison = cache?.buyoutComparison as { state?: string; freshnessState?: string; dateFrom?: string; dateTo?: string } | undefined
  const finance = async () => {
    if (coverage?.finance?.state !== 'ok' || cache?.financeFreshnessState === 'stale') {
      await syncStatsSources(token, period.dateFrom!, period.dateTo!, signal, progress, isCurrent, 'repricer-finance')
    }
    // Current period first. The comparison requires Finance only, never a
    // second full operational/account synchronization.
    if (comparison && (comparison.state !== 'ok' || comparison.freshnessState === 'stale') && comparison.dateFrom && comparison.dateTo && isCurrent() && !signal.aborted) {
      await syncStatsSources(token, comparison.dateFrom, comparison.dateTo, signal, progress, isCurrent, 'repricer-finance')
    }
  }
  const operational = async () => {
    if (!sources.length) return
    if (!await preparePeriod(token, period, sources, isCurrent, signal, status => {
      const step = status.steps?.find(item => item.status === 'running')
      return progress({ state: 'running', updatedAt: status.updatedAt ?? undefined,
        label: step?.message ?? 'Загружаем недостающие показатели выбранного периода…' })
    })) return
    await read()
  }
  const results = await Promise.allSettled([finance(), operational()])
  const failure = results.find((result): result is PromiseRejectedResult => result.status === 'rejected')
  if (failure) throw failure.reason
  if (!publishedCompleted && isCurrent() && !signal.aborted) onStatus({ state: 'completed', label: 'Доступные показатели выбранного периода загружены' })
}
