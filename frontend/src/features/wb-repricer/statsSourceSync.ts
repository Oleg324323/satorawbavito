import { apiRequest } from '@/lib/api'
import { authorizationHeaders } from '@/features/auth/authApi'

export type StatsSourceJob = { state: string; label?: string; error?: string; percent?: number; updatedAt?: string; reused?: boolean }
export function shouldReadStatsProgress(job: StatsSourceJob, lastRead: number, now = Date.now()) {
  // Reused describes the job, not the revision of data already on screen.
  return job.state === 'completed' || now - lastRead >= 30000
}
function pause(signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    const abort = () => { clearTimeout(timer); signal.removeEventListener('abort', abort); reject(new DOMException('Загрузка отменена', 'AbortError')) }
    const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve() }, 5000)
    signal.addEventListener('abort', abort, { once: true })
    if (signal.aborted) abort()
  })
}

// Only the mounted statistics screen owns polling; leaving it immediately
// cancels HTTP reads. Already persisted provider batches remain reusable.
export async function syncStatsSources(token: string, from: string, to: string, signal: AbortSignal,
  onProgress: (job: StatsSourceJob) => Promise<void> | void, isCurrent: () => boolean = () => true, source = 'operational') {
  const query = new URLSearchParams({ preset: 'custom', from, to })
  if (source !== 'operational') query.set('source', source)
  const url = `/api/wb/reports/stats/jobs?${query}`
  const request = async (method = 'GET') => {
    if (!isCurrent() || signal.aborted) throw new DOMException('Загрузка заменена', 'AbortError')
    const controller = new AbortController()
    const abort = () => controller.abort(signal.reason)
    signal.addEventListener('abort', abort, { once: true })
    if (signal.aborted) abort()
    let timedOut = false
    const timer = setTimeout(() => { timedOut = true; controller.abort() }, 30000)
    try {
      return await apiRequest<StatsSourceJob>(url, { method, headers: authorizationHeaders(token), cache: 'no-store', signal: controller.signal })
    } catch (error) {
      if (timedOut) throw new Error('Сервер не ответил за 30 секунд. Полученные данные сохранены; повторите проверку.')
      throw error
    } finally {
      clearTimeout(timer)
      signal.removeEventListener('abort', abort)
    }
  }
  let job = await request('POST')
  // Finance is paged at WB's minute-scale rate limit. Keep its wait bounded,
  // but allow a cold month to finish without changing other screens' budget.
  const maxPolls = source === 'repricer-finance' ? 720 : 180
  for (let poll = 0; poll < maxPolls; poll++) {
    if (signal.aborted || !isCurrent()) throw new DOMException('Загрузка отменена', 'AbortError')
    if (!job) throw new Error('Сервер не подтвердил запуск загрузки статистики')
    if (['failed', 'stale', 'paused', 'waiting_daily_detail'].includes(job.state)) throw new Error(job.error || job.label || 'Загрузка не завершена. Повторите недостающую часть.')
    await onProgress(job)
    if (job.state === 'completed') return job
    await pause(signal)
    job = await request()
  }
  throw new Error(`Загрузка продолжается дольше ${source === 'repricer-finance' ? 60 : 15} минут. Полученные данные сохранены; повторите проверку состояния.`)
}
