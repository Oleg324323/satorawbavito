import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ApiError, apiRequest } from '@/lib/api'
import { loadLatestReportCache } from './VellaHtmlParityPage'
import { REPORT_LOADING_BUDGET_MS } from './backgroundReportPolling'

vi.mock('@/lib/api', async importOriginal => ({ ...await importOriginal<typeof import('@/lib/api')>(), apiRequest: vi.fn() }))
beforeEach(() => { vi.stubGlobal('document', { getElementById: () => null }) })
afterEach(() => { vi.mocked(apiRequest).mockReset(); vi.unstubAllGlobals(); vi.useRealTimers() })
const period = { fromIso: '2026-09-01', toIso: '2026-09-07' }

it('publishes saved data first and refreshes stale sources without clearing rows', async () => {
  vi.stubGlobal('document', { getElementById: () => null })
  const saved = { rows: [{ value: 1 }], cache: { fresh: false } }
  const latest = { rows: [{ value: 2 }], cache: { fresh: true } }
  vi.mocked(apiRequest).mockResolvedValueOnce(saved).mockResolvedValueOnce({ state: 'completed' }).mockResolvedValueOnce(latest)
  const onCached = vi.fn()
  const onJob = vi.fn()
  expect(await loadLatestReportCache('rnp', 'rnp', 'synthetic', 'sku', period, 'operational', { onCached, onJob })).toEqual(latest)
  expect(onCached).toHaveBeenCalledWith(saved)
  expect(onJob).not.toHaveBeenCalled() // Loading state must not hide the cached table.
  expect(apiRequest).toHaveBeenCalledTimes(3)
})

it('retains saved data when refresh fails and never retries automatically', async () => {
  vi.stubGlobal('document', { getElementById: () => null })
  const saved = { rows: [{ value: 1 }], cache: { fresh: false } }
  vi.mocked(apiRequest).mockResolvedValueOnce(saved).mockRejectedValueOnce(new Error('HTTP 429'))
  expect(await loadLatestReportCache('rnp', 'rnp', 'synthetic', 'sku', period)).toEqual(saved)
  expect(apiRequest).toHaveBeenCalledTimes(2)
})

it('does not start a collection for a fresh cache', async () => {
  const saved = { cache: { fresh: true } }
  vi.mocked(apiRequest).mockResolvedValueOnce(saved)
  expect(await loadLatestReportCache('rnp', 'rnp', 'synthetic', 'sku', period)).toEqual(saved)
  expect(apiRequest).toHaveBeenCalledTimes(1)
})

it.each(['abc', 'pnl', 'rnp', 'week-over-week'] as const)('%s starts a missing period automatically and publishes the worker result', async reportId => {
  vi.mocked(apiRequest).mockRejectedValueOnce(new ApiError('REPORT_LATEST_CACHE_MISSING', 404))
    .mockResolvedValueOnce({ state: 'completed' })
    .mockResolvedValueOnce({ rows: [{ value: 0 }], cache: { fresh: true } })
  const reportKey = reportId === 'week-over-week' ? 'week' : reportId
  expect(await loadLatestReportCache(reportKey, reportId, 'synthetic', 'sku', period)).toEqual({ rows: [{ value: 0 }], cache: { fresh: true } })
  const posts = vi.mocked(apiRequest).mock.calls.filter(([, init]) => init?.method === 'POST')
  expect(posts).toHaveLength(1)
  expect(posts[0][0]).toContain(`/api/wb/reports/${reportId}/jobs?`)
})

it('discards a late cache response after the selected period was cancelled', async () => {
  const controller = new AbortController()
  const onCached = vi.fn()
  vi.mocked(apiRequest).mockImplementationOnce(async () => {
    controller.abort()
    return { rows: [{ value: 1 }], cache: { fresh: false } }
  })
  await expect(loadLatestReportCache('rnp', 'rnp', 'synthetic', 'sku', period, 'operational', {
    signal: controller.signal, onCached,
  })).rejects.toMatchObject({ name: 'AbortError' })
  expect(onCached).not.toHaveBeenCalled()
  expect(apiRequest).toHaveBeenCalledTimes(1)
})

it('does not replace the selected dates with a report from a different period', async () => {
  const onCached = vi.fn()
  vi.mocked(apiRequest).mockResolvedValueOnce({ dateRange: { from: '2026-08-01', to: '2026-08-31' }, cache: { fresh: true } })
  await expect(loadLatestReportCache('rnp', 'rnp', 'synthetic', 'sku', period, 'operational', { onCached }))
    .rejects.toMatchObject({ status: 409 })
  expect(onCached).not.toHaveBeenCalled()
  expect(apiRequest).toHaveBeenCalledTimes(1)
})

it('ABC automatically builds the selected DB period without a manual refresh', async () => {
  const saved = { rows: [{ sku: 'SAVED' }], cache: { fresh: false } }
  const built = { rows: [{ sku: 'UPDATED' }], cache: { fresh: true } }
  vi.mocked(apiRequest).mockResolvedValueOnce(saved).mockResolvedValueOnce({ state: 'completed' }).mockResolvedValueOnce(built)
  const onCached = vi.fn()
  expect(await loadLatestReportCache('abc', 'abc', 'synthetic', 'sku', period, 'operational', { onCached })).toEqual(built)
  expect(onCached).toHaveBeenCalledWith(saved)
  expect(apiRequest).toHaveBeenNthCalledWith(2,
    expect.stringContaining('/api/wb/reports/abc/jobs?preset=custom&from=2026-09-01&to=2026-09-07'),
    expect.objectContaining({ method: 'POST' }))
  expect(vi.mocked(apiRequest).mock.calls.every(([path]) => !String(path).includes('refresh-sources-job'))).toBe(true)
})

it('ABC keeps the saved rows while a throttled job resumes automatically', async () => {
  vi.stubGlobal('window', {
    setTimeout: (handler: () => void) => setTimeout(handler, 0), clearTimeout,
  })
  const saved = { rows: [{ sku: 'SAVED' }], cache: { fresh: false } }
  const built = { rows: [{ sku: 'UPDATED' }], cache: { fresh: true } }
  vi.mocked(apiRequest).mockResolvedValueOnce(saved)
    .mockResolvedValueOnce({ state: 'queued', stage: 'rate_limit', retryAfterSeconds: 126 })
    .mockResolvedValueOnce({ state: 'completed' }).mockResolvedValueOnce(built)
  const onCached = vi.fn()
  expect(await loadLatestReportCache('abc', 'abc', 'synthetic', 'sku', period, 'operational', { onCached })).toEqual(built)
  expect(onCached).toHaveBeenCalledWith(saved)
  const calls = vi.mocked(apiRequest).mock.calls
  expect(calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1)
  expect(String(calls[2][0])).toContain('/api/wb/reports/abc/jobs?preset=custom&from=2026-09-01&to=2026-09-07')
})

it('stops a long-running report at the elapsed budget while retaining its cached rows', async () => {
  vi.useFakeTimers()
  vi.stubGlobal('window', { setTimeout, clearTimeout })
  const saved = { rows: [{ sku: 'SAVED' }], cache: { fresh: false } }
  vi.mocked(apiRequest).mockImplementation(async path => String(path).includes('/latest-cache') ? saved : { state: 'running' })
  const result = loadLatestReportCache('pnl', 'pnl', 'synthetic', 'sku', period)
  await vi.advanceTimersByTimeAsync(REPORT_LOADING_BUDGET_MS + 30_000)
  expect(await result).toEqual(saved)
  expect(vi.mocked(apiRequest).mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1)
  expect(vi.mocked(apiRequest).mock.calls.length).toBeLessThan(200)
})

it('P&L publishes finance first and replaces it after the campaign job completes for the same dates', async () => {
  vi.stubGlobal('window', { setTimeout: (handler: () => void) => setTimeout(handler, 0), clearTimeout })
  const saved = { rows: [{ revenueKopecks: 10000, adSpendKopecks: null, netProfitKopecks: null }], cache: { fresh: false } }
  const complete = { rows: [{ revenueKopecks: 10000, adSpendKopecks: 400, netProfitKopecks: 1200 }], cache: { fresh: true } }
  vi.mocked(apiRequest).mockResolvedValueOnce(saved)
    .mockResolvedValueOnce({ state: 'running', stage: 'ads-fullstats' })
    .mockResolvedValueOnce({ state: 'completed' }).mockResolvedValueOnce(complete)
  const onCached = vi.fn(), onJob = vi.fn()
  expect(await loadLatestReportCache('pnl', 'pnl', 'synthetic', 'sku', period, 'financial', { onCached, onJob })).toEqual(complete)
  expect(onCached).toHaveBeenCalledWith(saved)
  expect(onJob).not.toHaveBeenCalled()
  const calls = vi.mocked(apiRequest).mock.calls
  expect(calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1)
  expect(calls.every(([url]) => String(url).includes('from=2026-09-01&to=2026-09-07'))).toBe(true)
})

it('P&L does not publish a late campaign snapshot after its dates were cancelled', async () => {
  const controller = new AbortController(), onCached = vi.fn()
  vi.mocked(apiRequest).mockImplementationOnce(async () => {
    controller.abort()
    return { rows: [{ adSpendKopecks: 999 }], cache: { fresh: false } }
  })
  await expect(loadLatestReportCache('pnl', 'pnl', 'synthetic', 'sku', period, 'financial', {
    signal: controller.signal, onCached,
  })).rejects.toMatchObject({ name: 'AbortError' })
  expect(onCached).not.toHaveBeenCalled()
  expect(apiRequest).toHaveBeenCalledTimes(1)
})
