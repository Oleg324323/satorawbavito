import { afterEach, beforeEach, expect, test, vi } from 'vitest'

vi.mock('./liveParityData', () => ({ resetLiveRepricerParityCache: vi.fn() }))
beforeEach(() => { vi.resetModules(); vi.useFakeTimers(); vi.setSystemTime('2026-11-02T10:00:00Z') })
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); vi.clearAllMocks() })
const stale = { promotionsRefreshRequired: true }
const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'content-type': 'application/json' } })
const success = () => response({ source: 'promotions', count: 1 })

test('fresh unsupported participation does not trigger another WB collection', async () => {
  const fetch = vi.fn(); vi.stubGlobal('fetch', fetch)
  const { syncProductsPromotions } = await import('./promotionsSourceSync')
  expect(await syncProductsPromotions('a', { promotionsRefreshRequired: false, promotionsRefreshAt: '2026-11-02T11:00:00Z' }, new AbortController().signal, () => true)).toBe(false)
  expect(fetch).not.toHaveBeenCalled()
})

test('defers missing membership and shares one account request across view consumers', async () => {
  let release!: (value: Response) => void
  const fetch = vi.fn((_url: unknown) => new Promise<Response>(resolve => { release = resolve })); vi.stubGlobal('fetch', fetch)
  const { syncProductsPromotions } = await import('./promotionsSourceSync')
  const first = syncProductsPromotions('a', stale, new AbortController().signal, () => true)
  const second = syncProductsPromotions('a', stale, new AbortController().signal, () => true)
  expect(fetch).not.toHaveBeenCalled()
  await vi.advanceTimersByTimeAsync(0)
  expect(fetch).toHaveBeenCalledOnce()
  expect(String(fetch.mock.calls[0][0])).toContain('refresh-promotions?onlyIfStale=true')
  release(success())
  expect(await Promise.all([first, second])).toEqual([true, true])
})

test('a saved page crossing its expiry refreshes even when its stored flag was false', async () => {
  const fetch = vi.fn().mockResolvedValue(success()); vi.stubGlobal('fetch', fetch)
  const { syncProductsPromotions } = await import('./promotionsSourceSync')
  const pending = syncProductsPromotions('a', { promotionsRefreshRequired: false, promotionsRefreshAt: '2026-11-02T09:59:59Z' }, new AbortController().signal, () => true)
  await vi.advanceTimersByTimeAsync(0)
  expect(await pending).toBe(true)
  expect(fetch).toHaveBeenCalledOnce()
})

test('navigation before the deferred task prevents the request', async () => {
  let current = true
  const fetch = vi.fn(); vi.stubGlobal('fetch', fetch)
  const { syncProductsPromotions } = await import('./promotionsSourceSync')
  const pending = syncProductsPromotions('a', stale, new AbortController().signal, () => current)
  current = false
  await vi.advanceTimersByTimeAsync(0)
  expect(await pending).toBe(false)
  expect(fetch).not.toHaveBeenCalled()
})

test('one cancelled view does not abort membership still needed by another view', async () => {
  let release!: (value: Response) => void
  const fetch = vi.fn((_url: unknown, _options: RequestInit) => new Promise<Response>(resolve => { release = resolve })); vi.stubGlobal('fetch', fetch)
  const { syncProductsPromotions } = await import('./promotionsSourceSync')
  const controller = new AbortController()
  const first = syncProductsPromotions('a', stale, controller.signal, () => true).catch(error => error.name)
  const second = syncProductsPromotions('a', stale, new AbortController().signal, () => true)
  await vi.advanceTimersByTimeAsync(0)
  controller.abort()
  expect(fetch.mock.calls[0][1].signal?.aborted).toBe(false)
  release(success())
  expect(await first).toBe('AbortError')
  expect(await second).toBe(true)
})

test('a failure preserves saved rows and throttles further automatic attempts without polling', async () => {
  const fetch = vi.fn().mockResolvedValueOnce(response({ detail: 'Synthetic WB rate limit' }, 429)).mockResolvedValueOnce(success()); vi.stubGlobal('fetch', fetch)
  const { syncProductsPromotions } = await import('./promotionsSourceSync')
  const { resetLiveRepricerParityCache } = await import('./liveParityData')
  const failed = syncProductsPromotions('a', stale, new AbortController().signal, () => true).catch(error => error.message)
  await vi.advanceTimersByTimeAsync(0)
  expect(await failed).toContain('rate limit')
  expect(resetLiveRepricerParityCache).not.toHaveBeenCalled()
  expect(await syncProductsPromotions('a', stale, new AbortController().signal, () => true)).toBe(false)
  await vi.advanceTimersByTimeAsync(60_001)
  expect(fetch).toHaveBeenCalledOnce()
  const retry = syncProductsPromotions('a', stale, new AbortController().signal, () => true)
  await vi.advanceTimersByTimeAsync(0)
  expect(await retry).toBe(true)
  expect(fetch).toHaveBeenCalledTimes(2)
})

test('a partial fallback is not cached as successful membership', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ source: 'promotions', count: 1, partial: true, warning: 'Synthetic incomplete pages' })))
  const { syncProductsPromotions } = await import('./promotionsSourceSync')
  const { resetLiveRepricerParityCache } = await import('./liveParityData')
  const result = syncProductsPromotions('a', stale, new AbortController().signal, () => true).catch(error => error.message)
  await vi.advanceTimersByTimeAsync(0)
  expect(await result).toContain('incomplete')
  expect(resetLiveRepricerParityCache).not.toHaveBeenCalled()
})

test('different authenticated accounts never share membership requests', async () => {
  const fetch = vi.fn().mockImplementation(async () => success()); vi.stubGlobal('fetch', fetch)
  const { syncProductsPromotions } = await import('./promotionsSourceSync')
  const pending = ['a', 'b'].map(token => syncProductsPromotions(token, stale, new AbortController().signal, () => true))
  await vi.advanceTimersByTimeAsync(0)
  expect(await Promise.all(pending)).toEqual([true, true])
  expect(fetch).toHaveBeenCalledTimes(2)
  expect(fetch.mock.calls.map(([, options]) => new Headers(options.headers).get('Authorization'))).toEqual(['Bearer a', 'Bearer b'])
})
