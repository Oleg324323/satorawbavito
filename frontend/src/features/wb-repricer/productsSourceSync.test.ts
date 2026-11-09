import { afterEach, expect, it, vi } from 'vitest'
import { syncProductsPeriod } from './productsSourceSync'
import { syncStatsSources } from './statsSourceSync'
import { resetLiveRepricerParityCache } from './liveParityData'
import { preparePeriod } from './preparePeriod'

vi.mock('./statsSourceSync', async original => ({ ...await original<typeof import('./statsSourceSync')>(), syncStatsSources: vi.fn() }))
vi.mock('./liveParityData', () => ({ resetLiveRepricerParityCache: vi.fn() }))
vi.mock('./preparePeriod', async original => ({ ...await original<typeof import('./preparePeriod')>(), preparePeriod: vi.fn() }))
afterEach(() => { vi.resetAllMocks(); vi.useRealTimers() })
const period = { dateFrom: '2026-09-01', dateTo: '2026-09-07' }
const completeAds = { adsFetchedAt: 'now', adsCoverageState: 'ok', adsFreshnessState: 'fresh',
  adsDateFrom: period.dateFrom, adsDateTo: period.dateTo }

it('prepares missing exact-period conversions through the existing source queue, preserving isolated finance', async () => {
  vi.mocked(preparePeriod).mockResolvedValue(true)
  vi.mocked(syncStatsSources).mockResolvedValue({state:'completed'})
  const read = vi.fn()
  const current = () => true
  const signal = new AbortController().signal
  await syncProductsPeriod('synthetic',period,signal,current,read,vi.fn(),{
    ...completeAds,
    periodStatsFetchedAt:'now', financeFetchedAt:'now', basketsFetchedAt:'now', basketsPeriodConversionState:'no_data',
  })
  expect(preparePeriod).toHaveBeenCalledWith('synthetic',period,['baskets'],current,signal,expect.any(Function))
  expect(read).toHaveBeenCalledOnce()
  expect(vi.mocked(syncStatsSources).mock.calls[0].at(-1)).toBe('repricer-finance')
})

it('uses the managed period job and reads the saved final batch even for a reused job', async () => {
  vi.mocked(syncStatsSources).mockImplementation(async (_token, _from, _to, _signal, progress) => {
    await progress({ state: 'running' })
    await progress({ state: 'completed', reused: true })
    return { state: 'completed' }
  })
  const read = vi.fn().mockResolvedValue(undefined)
  const status = vi.fn()
  await syncProductsPeriod('synthetic', period, new AbortController().signal, () => true, read, status,
    { ...completePeriod, financeFreshnessState: 'stale' })
  expect(syncStatsSources).toHaveBeenCalledTimes(1)
  expect(read).toHaveBeenCalledTimes(1)
  expect(resetLiveRepricerParityCache).toHaveBeenCalledWith('synthetic')
  expect(status.mock.calls.map(([job]) => job.state)).toEqual(['running', 'completed'])
})

it('never publishes an old period after navigation or cancellation', async () => {
  let current = true
  vi.mocked(syncStatsSources).mockImplementation(async (_token, _from, _to, _signal, progress) => {
    current = false
    await progress({ state: 'completed' })
    return { state: 'completed' }
  })
  const read = vi.fn()
  const status = vi.fn()
  await syncProductsPeriod('synthetic', period, new AbortController().signal, () => current, read, status)
  expect(read).not.toHaveBeenCalled()
  expect(status).not.toHaveBeenCalled()
  const controller = new AbortController(); controller.abort()
  await syncProductsPeriod('synthetic', period, controller.signal, () => true, read)
  expect(syncStatsSources).toHaveBeenCalledTimes(1)
})

it('propagates a provider failure without a repeat or clearing saved rows', async () => {
  vi.mocked(syncStatsSources).mockRejectedValue(new Error('WB HTTP 429'))
  const read = vi.fn()
  await expect(syncProductsPeriod('synthetic', period, new AbortController().signal, () => true, read)).rejects.toThrow('429')
  expect(syncStatsSources).toHaveBeenCalledTimes(1)
  expect(read).not.toHaveBeenCalled()
})

it('does not start any jobs when the required observations are complete and fresh', async () => {
  await syncProductsPeriod('synthetic', period, new AbortController().signal, () => true, vi.fn(), vi.fn(), {
    ...completeAds,
    periodStatsFetchedAt: 'now', financeFetchedAt: 'now', basketsFetchedAt: 'now',
    periodCoverage: { finance: { state: 'ok' } }, financeFreshnessState: 'fresh',
  })
  expect(syncStatsSources).not.toHaveBeenCalled()
  expect(preparePeriod).not.toHaveBeenCalled()
})

it('starts Finance without waiting for operational collection and reads both independent results', async () => {
  let release!: (value: boolean) => void
  vi.mocked(preparePeriod).mockImplementation(() => new Promise(resolve => { release = resolve }))
  vi.mocked(syncStatsSources).mockImplementation(async (_t, _f, _e, _s, progress) => {
    await progress({ state: 'completed' }); return { state: 'completed' }
  })
  const read = vi.fn()
  const job = syncProductsPeriod('synthetic', period, new AbortController().signal, () => true, read, vi.fn(), {
    periodStatsFetchedAt: 'now', basketsCoverageState: 'no_data',
  })
  await vi.waitFor(() => expect(syncStatsSources).toHaveBeenCalledOnce())
  await vi.waitFor(() => expect(read).toHaveBeenCalledOnce())
  release(true)
  await job
  expect(read).toHaveBeenCalledTimes(2)
})

it('collects only missing comparison Finance after the selected period', async () => {
  vi.mocked(syncStatsSources).mockResolvedValue({ state: 'completed' })
  await syncProductsPeriod('synthetic', period, new AbortController().signal, () => true, vi.fn(), vi.fn(), {
    ...completeAds,
    periodStatsFetchedAt: 'now', basketsFetchedAt: 'now', financeFetchedAt: 'now',
    periodCoverage: { finance: { state: 'ok' } },
    buyoutComparison: { state: 'no_data', dateFrom: '2026-08-25', dateTo: '2026-08-31' },
  })
  expect(syncStatsSources).toHaveBeenCalledOnce()
  expect(vi.mocked(syncStatsSources).mock.calls[0].slice(1, 3)).toEqual(['2026-08-25', '2026-08-31'])
  expect(vi.mocked(syncStatsSources).mock.calls[0].at(-1)).toBe('repricer-finance')
  expect(preparePeriod).not.toHaveBeenCalled()
})

const completePeriod = { ...completeAds, periodStatsFetchedAt: 'now', financeFetchedAt: 'now', basketsFetchedAt: 'now',
  periodCoverage: { finance: { state: 'ok' } }, financeFreshnessState: 'fresh' }

it('loads only missing campaign spend for the selected period and reads the saved summary after completion', async () => {
  vi.mocked(preparePeriod).mockResolvedValue(true)
  const read = vi.fn()
  const status = vi.fn()
  const signal = new AbortController().signal
  const current = () => true
  await syncProductsPeriod('synthetic', period, signal, current, read, status, {
    ...completePeriod, adsFetchedAt: null, adsCoverageState: 'no_data',
  })
  expect(syncStatsSources).not.toHaveBeenCalled()
  expect(preparePeriod).toHaveBeenCalledOnce()
  expect(preparePeriod).toHaveBeenCalledWith('synthetic', period, ['ads'], current, signal, expect.any(Function))
  expect(read).toHaveBeenCalledOnce()
  expect(resetLiveRepricerParityCache).toHaveBeenCalledWith('synthetic')
  expect(status).toHaveBeenLastCalledWith(expect.objectContaining({ state: 'completed' }))
})

it('keeps saved finance visible and refreshes completed batches while advertising is still running', async () => {
  vi.useFakeTimers()
  let release!: (value: boolean) => void
  vi.mocked(preparePeriod).mockImplementation(async (_t, _p, _sources, _current, _signal, progress) => {
    await progress?.({ state: 'running', running: true, steps: [{ source: 'ads', status: 'running', message: 'WB Ads loading' }] })
    await vi.advanceTimersByTimeAsync(30000)
    await progress?.({ state: 'running', running: true })
    return new Promise(resolve => { release = resolve })
  })
  vi.mocked(syncStatsSources).mockImplementation(async (_t, _f, _e, _s, progress) => {
    await progress({ state: 'completed' }); return { state: 'completed' }
  })
  const read = vi.fn()
  const pending = syncProductsPeriod('synthetic', period, new AbortController().signal, () => true, read, vi.fn(), {
    ...completePeriod, financeFreshnessState: 'stale', adsCoverageState: 'partial',
  })
  await vi.waitFor(() => expect(read).toHaveBeenCalledTimes(2))
  release(true)
  await pending
  expect(read).toHaveBeenCalledTimes(3)
  vi.useRealTimers()
})

it('preserves the current-period guard after advertising finishes', async () => {
  let current = true
  vi.mocked(preparePeriod).mockImplementation(async () => { current = false; return true })
  const read = vi.fn(), status = vi.fn()
  await syncProductsPeriod('synthetic', period, new AbortController().signal, () => current, read, status, {
    ...completePeriod, adsCoverageState: 'partial',
  })
  expect(read).not.toHaveBeenCalled()
  expect(status).not.toHaveBeenCalled()
})

it('exposes advertising failures without rerunning sources or clearing saved data', async () => {
  vi.mocked(preparePeriod).mockRejectedValue(new Error('WB Ads HTTP 429'))
  const read = vi.fn(), status = vi.fn()
  await expect(syncProductsPeriod('synthetic', period, new AbortController().signal, () => true, read, status, {
    ...completePeriod, adsCoverageState: 'no_data',
  })).rejects.toThrow('WB Ads HTTP 429')
  expect(preparePeriod).toHaveBeenCalledOnce()
  expect(syncStatsSources).not.toHaveBeenCalled()
  expect(read).not.toHaveBeenCalled()
  expect(status).not.toHaveBeenCalled()
})

it('loads missing warehouse stock for storage once without refetching saved period sources', async () => {
  await syncProductsPeriod('synthetic', period, new AbortController().signal, () => true, vi.fn(), vi.fn(), {
    ...completePeriod, storageStocksRequired: true,
  })
  expect(preparePeriod).toHaveBeenCalledWith('synthetic', period, ['stocks'], expect.any(Function), expect.any(AbortSignal), expect.any(Function))
  expect(syncStatsSources).not.toHaveBeenCalled()
})
