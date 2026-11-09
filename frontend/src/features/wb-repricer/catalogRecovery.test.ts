import { afterEach, expect, it, vi } from 'vitest'
import { ApiError, apiRequest } from '@/lib/api'
import { syncStatsSources } from './statsSourceSync'
import { readWithCatalogRecovery } from './catalogRecovery'

vi.mock('@/lib/api', async original => ({ ...await original<typeof import('@/lib/api')>(), apiRequest: vi.fn() }))
vi.mock('./statsSourceSync', () => ({ syncStatsSources: vi.fn() }))
vi.mock('@/features/auth/authApi', () => ({ authorizationHeaders: () => ({}) }))
afterEach(() => vi.resetAllMocks())
const read = (signal?: AbortSignal) => readWithCatalogRecovery('/same-period?page=2', 'synthetic', '2026-10-01', '2026-10-08', signal)

it('does not enqueue anything on a warm cache or a genuinely empty result', async () => {
  vi.mocked(apiRequest).mockResolvedValue({ items: [], total: 0 })
  expect(await read()).toEqual({ items: [], total: 0 })
  expect(syncStatsSources).not.toHaveBeenCalled()
})

it('recovers a missing catalog through the existing queue and retries the identical URL', async () => {
  vi.mocked(apiRequest).mockRejectedValueOnce(new ApiError('missing', 409, 'WB_CATALOG_CACHE_REQUIRED'))
    .mockResolvedValueOnce({ items: [{ nmId: 101 }], total: 1 })
  vi.mocked(syncStatsSources).mockResolvedValue({ state: 'completed' })
  expect(await read()).toMatchObject({ total: 1 })
  expect(vi.mocked(syncStatsSources).mock.calls[0].slice(0, 3)).toEqual(['synthetic', '2026-10-01', '2026-10-08'])
  expect(vi.mocked(syncStatsSources).mock.calls[0].at(-1)).toBe('catalog')
  expect(vi.mocked(apiRequest).mock.calls.map(([url]) => url)).toEqual(['/same-period?page=2', '/same-period?page=2'])
})

it('does not loop when recovery fails and does not recover authentication failures', async () => {
  const denied = new ApiError('denied', 403, 'FORBIDDEN')
  vi.mocked(apiRequest).mockRejectedValueOnce(denied)
  await expect(read()).rejects.toBe(denied)
  expect(syncStatsSources).not.toHaveBeenCalled()
  vi.mocked(apiRequest).mockRejectedValue(new ApiError('missing', 409, 'WB_CATALOG_CACHE_REQUIRED'))
  vi.mocked(syncStatsSources).mockRejectedValueOnce(new Error('WB unavailable'))
  await expect(read()).rejects.toThrow('WB unavailable')
  expect(syncStatsSources).toHaveBeenCalledOnce()
})

it('passes cancellation to collection and does not retry a cancelled read', async () => {
  const controller = new AbortController()
  vi.mocked(apiRequest).mockRejectedValueOnce(new ApiError('missing', 409, 'WB_CATALOG_CACHE_REQUIRED'))
  vi.mocked(syncStatsSources).mockImplementationOnce(async (_token, _from, _to, signal) => {
    expect(signal).toBe(controller.signal)
    controller.abort()
    throw new DOMException('cancelled', 'AbortError')
  })
  await expect(read(controller.signal)).rejects.toMatchObject({ name: 'AbortError' })
  expect(apiRequest).toHaveBeenCalledOnce()
})
