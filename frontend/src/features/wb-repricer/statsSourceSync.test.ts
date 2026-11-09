import { afterEach, describe, expect, it, vi } from 'vitest'
import { apiRequest } from '@/lib/api'
import { shouldReadStatsProgress, syncStatsSources } from './statsSourceSync'
vi.mock('@/lib/api', () => ({ apiRequest: vi.fn() }))
vi.mock('@/features/auth/authApi', () => ({ authorizationHeaders: () => ({}) }))
afterEach(() => { vi.resetAllMocks(); vi.useRealTimers() })
describe('on-demand statistics sources', () => {
  it.each([['operational', 180], ['repricer-finance', 720]] as const)('keeps %s polling bounded without changing the other source budget', async (source, count) => {
    vi.useFakeTimers()
    vi.mocked(apiRequest).mockResolvedValue({ state: 'running' })
    const pending = syncStatsSources('test', '2026-09-01', '2026-09-30', new AbortController().signal, () => {}, () => true, source)
    const assertion = expect(pending).rejects.toThrow()
    await vi.advanceTimersByTimeAsync(count * 5000 + 1)
    await assertion
    expect(apiRequest).toHaveBeenCalledTimes(count + 1)
    expect(String(vi.mocked(apiRequest).mock.calls[0][0]).includes('source=repricer-finance')).toBe(source === 'repricer-finance')
  })
  it('stops polling a replaced product query even if its shared route signal stays alive', async () => {
    vi.useFakeTimers()
    let current = true
    vi.mocked(apiRequest).mockResolvedValue({ state: 'queued' })
    const pending = syncStatsSources('test', '2026-09-01', '2026-09-07', new AbortController().signal, () => {}, () => current)
    const assertion = expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    await vi.advanceTimersByTimeAsync(1)
    current = false
    await vi.advanceTimersByTimeAsync(5000)
    await assertion
    expect(apiRequest).toHaveBeenCalledTimes(1)
  })
  it('always reads final data even when a finished job is marked reused', () => {
    expect(shouldReadStatsProgress({ state: 'completed', reused: true }, 1000, 1001)).toBe(true)
    expect(shouldReadStatsProgress({ state: 'running' }, 1000, 1001)).toBe(false)
    expect(shouldReadStatsProgress({ state: 'running' }, 1000, 31000)).toBe(true)
  })
  it('reuses covered dates without polling or refreshing providers', async () => {
    vi.mocked(apiRequest).mockResolvedValue({ state: 'completed', reused: true })
    const progress = vi.fn()
    await syncStatsSources('test', '2026-09-01', '2026-09-07', new AbortController().signal, progress)
    expect(apiRequest).toHaveBeenCalledTimes(1)
    expect(vi.mocked(apiRequest).mock.calls[0][0]).toContain('/stats/jobs?preset=custom&from=2026-09-01&to=2026-09-07')
    expect(progress).toHaveBeenCalledWith({ state: 'completed', reused: true })
  })
  it('polls bounded work and exposes partial progress before completion', async () => {
    vi.useFakeTimers()
    vi.mocked(apiRequest).mockResolvedValueOnce({ state: 'queued', percent: 0 }).mockResolvedValueOnce({ state: 'completed', percent: 100, reused: true })
    const progress = vi.fn()
    const pending = syncStatsSources('test', '2026-09-01', '2026-09-07', new AbortController().signal, progress)
    await vi.advanceTimersByTimeAsync(5000)
    await pending
    expect(progress.mock.calls.map(([job]) => job.percent)).toEqual([0, 100])
    expect(apiRequest).toHaveBeenCalledTimes(2)
  })
  it('stops on a provider error without an automatic retry loop', async () => {
    vi.mocked(apiRequest).mockResolvedValue({ state: 'failed', error: 'WB HTTP 429' })
    await expect(syncStatsSources('test', '2026-09-01', '2026-09-07', new AbortController().signal, vi.fn())).rejects.toThrow('WB HTTP 429')
    expect(apiRequest).toHaveBeenCalledTimes(1)
  })
  it('stops polling immediately after leaving the screen', async () => {
    vi.useFakeTimers()
    vi.mocked(apiRequest).mockResolvedValue({ state: 'running' })
    const controller = new AbortController()
    const pending = syncStatsSources('test', '2026-09-01', '2026-09-07', controller.signal, vi.fn())
    const assertion = expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    await vi.advanceTimersByTimeAsync(1)
    controller.abort()
    await assertion
    await vi.advanceTimersByTimeAsync(30000)
    expect(apiRequest).toHaveBeenCalledTimes(1)
  })
})
