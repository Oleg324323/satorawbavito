import { afterEach, expect, it, vi } from 'vitest'
import { apiRequest } from '@/lib/api'
import { reportRequest } from './reportRequest'

vi.mock('@/lib/api', async original => ({ ...await original<typeof import('@/lib/api')>(), apiRequest: vi.fn() }))
afterEach(() => { vi.useRealTimers(); vi.mocked(apiRequest).mockReset() })

it('bounds an unresolved request, aborts its fetch and leaves a useful error', async () => {
  vi.useFakeTimers()
  let signal: AbortSignal | null | undefined
  vi.mocked(apiRequest).mockImplementation(async (_, init) => {
    signal = init?.signal
    return new Promise(() => {})
  })
  const pending = reportRequest('/api/wb/reports/sales-funnel', {}, 100)
  const check = expect(pending).rejects.toMatchObject({ status: 504 })
  await vi.advanceTimersByTimeAsync(100)
  await check
  expect(signal?.aborted).toBe(true)
  expect(vi.getTimerCount()).toBe(0)
})

it('cancels on date changes even when the request ignores abort', async () => {
  const controller = new AbortController()
  vi.mocked(apiRequest).mockImplementation(async () => new Promise(() => {}))
  const pending = reportRequest('/api/wb/reports/rnp/latest-cache', { signal: controller.signal })
  const check = expect(pending).rejects.toMatchObject({ name: 'AbortError' })
  controller.abort()
  await check
})

it('cleans up a successful request without changing its result', async () => {
  vi.useFakeTimers()
  const result = { rows: [{ value: 0 }] }
  vi.mocked(apiRequest).mockResolvedValue(result)
  expect(await reportRequest('/api/wb/reports/pnl/latest-cache')).toBe(result)
  expect(vi.getTimerCount()).toBe(0)
})

it('does not send a request for an already cancelled scope', async () => {
  const controller = new AbortController()
  controller.abort()
  await expect(reportRequest('/api/wb/reports/rnp/jobs', { signal: controller.signal })).rejects.toMatchObject({ name: 'AbortError' })
  expect(apiRequest).not.toHaveBeenCalled()
})
