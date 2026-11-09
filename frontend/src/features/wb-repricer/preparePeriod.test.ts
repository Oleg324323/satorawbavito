import { afterEach, expect, test, vi } from 'vitest'
import { missingPeriodSources, preparePeriod } from './preparePeriod'

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })
const completeAds = { adsFetchedAt: 'now', adsCoverageState: 'ok', adsFreshnessState: 'fresh' }
test('requests an exact period funnel even when additive daily coverage is complete', () => {
  expect(missingPeriodSources({ ...completeAds, financeFetchedAt: 'now', periodStatsFetchedAt: 'now', basketsFetchedAt: 'now',
    basketsCoverageState: 'ok', basketsPeriodConversionState: 'no_data' })).toEqual(['baskets'])
})
test('revalidates stale observations instead of presenting them as fresh', () => {
  expect(missingPeriodSources({ ...completeAds, financeFetchedAt: 'now', periodStatsFetchedAt: 'old', basketsFetchedAt: 'now',
    periodStatsFreshnessState: 'stale' })).toEqual(['period-stats'])
})
test('only missing exact-period sources are requested', () => {
  expect(missingPeriodSources({ ...completeAds, financeFetchedAt: 'now', periodStatsFetchedAt: 'now', basketsCoverageState: 'no_data' })).toEqual(['baskets'])
  expect(missingPeriodSources({ ...completeAds, financeFetchedAt: 'now', periodStatsFetchedAt: 'now', basketsFetchedAt: 'now', basketsCoverageState: 'ok' })).toEqual([])
})
test.each([
  {},
  { ...completeAds, adsCoverageState: 'partial' },
  { ...completeAds, adsFreshnessState: 'stale' },
  { ...completeAds, periodCoverage: { ads: { state: 'partial' } } },
  { ...completeAds, adsDateFrom: '2026-09-02', adsDateTo: '2026-09-07' },
])('requests missing, incomplete, stale or mismatched campaign advertising: %j', ads => {
  const period = { dateFrom: '2026-09-01', dateTo: '2026-09-07' }
  const cache = { adsDateFrom: period.dateFrom, adsDateTo: period.dateTo,
    financeFetchedAt: 'now', periodStatsFetchedAt: 'now', basketsFetchedAt: 'now', ...ads }
  expect(missingPeriodSources(cache, period)).toEqual(['ads'])
})
test('reuses complete exact-period advertising, including a confirmed zero', () => {
  const period = { dateFrom: '2026-09-01', dateTo: '2026-09-07' }
  expect(missingPeriodSources({ ...completeAds, adsDateFrom: period.dateFrom, adsDateTo: period.dateTo,
    adsSpendKopecks: 0, financeFetchedAt: 'now', periodStatsFetchedAt: 'now', basketsFetchedAt: 'now' }, period)).toEqual([])
})
test('uses selected dates without onboarding or force; waits for its run', async () => {
  vi.useFakeTimers()
  const response = (data: unknown) => new Response(JSON.stringify(data), { headers: { 'content-type': 'application/json' } })
  const fetch = vi.fn().mockResolvedValueOnce(response({ runId: 'r', running: true }))
    .mockResolvedValueOnce(response({ runId: 'r', running: false, state: 'completed' }))
  vi.stubGlobal('fetch', fetch)
  const result = preparePeriod('fake', { dateFrom: '2026-09-01', dateTo: '2026-09-04', periodDays: 4 }, ['baskets'], () => true)
  await vi.advanceTimersByTimeAsync(3000)
  expect(await result).toBe(true)
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ dateFrom: '2026-09-01', dateTo: '2026-09-04', periodDays: 4, sources: ['baskets'], mode: 'manual', force: false })
})
test('a superseded view never starts another request', async () => {
  const fetch = vi.fn(); vi.stubGlobal('fetch', fetch)
  expect(await preparePeriod('fake', { periodDays: 7 }, ['finance'], () => false)).toBe(false)
  expect(fetch).not.toHaveBeenCalled()
})

test.each(['partial', 'failed'])('does not claim success for %s data', async state => {
  vi.useFakeTimers()
  const response = (data: unknown) => new Response(JSON.stringify(data), { headers: { 'content-type': 'application/json' } })
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(response({ runId: 'r', running: true }))
    .mockResolvedValueOnce(response({ runId: 'r', running: false, state })))
  const assertion = expect(preparePeriod('fake', { periodDays: 3 }, ['finance'], () => true)).rejects.toThrow('неполные данные')
  await vi.advanceTimersByTimeAsync(3000)
  await assertion
})

test('does not apply completion after the user changes the period', async () => {
  vi.useFakeTimers()
  let current = true
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ runId: 'r', running: true }), { headers: { 'content-type': 'application/json' } }))
  vi.stubGlobal('fetch', fetch)
  const result = preparePeriod('fake', { periodDays: 3 }, ['finance'], () => current)
  await vi.advanceTimersByTimeAsync(1)
  current = false
  await vi.advanceTimersByTimeAsync(3000)
  expect(await result).toBe(false)
  expect(fetch).toHaveBeenCalledTimes(1)
})

test('joins an identical running source job after 409 without starting it again', async () => {
  vi.useFakeTimers()
  const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'content-type': 'application/json' } })
  const fetch = vi.fn().mockResolvedValueOnce(response({ detail: 'WB_SYNC_RUNNING' }, 409))
    .mockResolvedValueOnce(response({ runId: 'shared', running: true, dateFrom: '2026-10-01', dateTo: '2026-10-08', sources: ['period-stats', 'baskets'] }))
    .mockResolvedValueOnce(response({ runId: 'shared', running: false, state: 'completed' }))
  vi.stubGlobal('fetch', fetch)
  const result = preparePeriod('fake', { dateFrom: '2026-10-01', dateTo: '2026-10-08' }, ['baskets'], () => true)
  await vi.advanceTimersByTimeAsync(6000)
  expect(await result).toBe(true)
  expect(fetch.mock.calls.filter(([, options]) => options.method === 'POST')).toHaveLength(1)
})

test('concurrent advertising requests join one exact-period job and refresh only their current views', async () => {
  vi.useFakeTimers()
  const period = { dateFrom: '2026-10-19', dateTo: '2026-10-25' }
  const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'content-type': 'application/json' } })
  let started = false
  let done = false
  const fetch = vi.fn().mockImplementation(async (_url, options) => {
    if (options.method === 'POST') {
      if (started) return response({ detail: 'WB_SYNC_RUNNING' }, 409)
      started = true
      return response({ runId: 'ads', running: true })
    }
    return response({ ...period, runId: 'ads', sources: ['ads'], running: !done, state: done ? 'completed' : 'running' })
  })
  vi.stubGlobal('fetch', fetch)
  const firstProgress = vi.fn(), secondProgress = vi.fn()
  const first = preparePeriod('fake', period, ['ads'], () => true, undefined, firstProgress)
  const second = preparePeriod('fake', period, ['ads'], () => true, undefined, secondProgress)
  await vi.advanceTimersByTimeAsync(3000)
  expect(firstProgress).toHaveBeenCalledOnce()
  expect(secondProgress).toHaveBeenCalledOnce()
  done = true
  await vi.advanceTimersByTimeAsync(3000)
  expect(await Promise.all([first, second])).toEqual([true, true])
  expect(fetch.mock.calls.filter(([, options]) => options.method === 'POST')).toHaveLength(2)
  for (const [, options] of fetch.mock.calls.filter(([, options]) => options.method === 'POST')) {
    expect(JSON.parse(options.body)).toEqual({ ...period, sources: ['ads'], mode: 'manual', force: false })
  }
})

test('does not publish advertising progress or completion when navigation occurs during a status read', async () => {
  vi.useFakeTimers()
  let current = true
  const progress = vi.fn()
  const response = (data: unknown) => new Response(JSON.stringify(data), { headers: { 'content-type': 'application/json' } })
  vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(response({ runId: 'ads', running: true }))
    .mockImplementationOnce(async () => { current = false; return response({ runId: 'ads', running: false, state: 'completed' }) }))
  const result = preparePeriod('fake', { dateFrom: '2026-10-19', dateTo: '2026-10-25' }, ['ads'], () => current, undefined, progress)
  await vi.advanceTimersByTimeAsync(3000)
  expect(await result).toBe(false)
  expect(progress).not.toHaveBeenCalled()
})

test.each([
  { dateFrom: '2026-10-01', dateTo: '2026-10-07', sources: ['baskets'] },
  { dateFrom: '2026-10-01', dateTo: '2026-10-08', sources: ['period-stats'] },
])('does not reuse another period or insufficient sources: %j', async other => {
  vi.useFakeTimers()
  const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'content-type': 'application/json' } })
  const fetch = vi.fn().mockResolvedValueOnce(response({ detail: 'WB_SYNC_RUNNING' }, 409))
    .mockResolvedValueOnce(response({ runId: 'other', running: false, state: 'completed', ...other }))
    .mockResolvedValueOnce(response({ runId: 'own', running: true }))
    .mockResolvedValueOnce(response({ runId: 'own', running: false, state: 'completed' }))
  vi.stubGlobal('fetch', fetch)
  const result = preparePeriod('fake', { dateFrom: '2026-10-01', dateTo: '2026-10-08' }, ['baskets'], () => true)
  await vi.advanceTimersByTimeAsync(6000)
  expect(await result).toBe(true)
  expect(fetch.mock.calls.filter(([, options]) => options.method === 'POST')).toHaveLength(2)
})
