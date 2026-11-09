import { afterEach, expect, it, vi } from 'vitest'
import { loadLiveRepricerParityProducts, loadLiveRepricerStats, resetLiveRepricerParityCache } from './liveParityData'

afterEach(() => { resetLiveRepricerParityCache(); vi.useRealTimers(); vi.unstubAllGlobals() })

it.each(['stats', 'sku'] as const)('price-only %s read never starts catalog recovery or source collection', async view => {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: { code: 'WB_CATALOG_CACHE_REQUIRED', message: 'Missing saved catalog' } }),
    { status: 409, headers: { 'content-type': 'application/json' } }))
  vi.stubGlobal('fetch', fetch)
  const period = { dateFrom: '2026-10-05', dateTo: '2026-10-11' }
  await expect(view === 'stats'
    ? loadLiveRepricerStats('synthetic', undefined, period, { savedOnly: true })
    : loadLiveRepricerParityProducts('synthetic', undefined, period, true, { savedOnly: true })).rejects.toThrow()
  expect(fetch).toHaveBeenCalledTimes(1)
  expect(fetch.mock.calls[0][1].method ?? 'GET').toBe('GET')
})

it.each(['stats', 'sku'] as const)('rejects empty, malformed and wrong-period %s refreshes instead of erasing visible data', async view => {
  for (const response of [
    new Response(null, { status: 204 }),
    new Response(JSON.stringify({ total: 0 }), { headers: { 'content-type': 'application/json' } }),
    new Response(JSON.stringify({ items: [], dateFrom: '2026-09-01', dateTo: '2026-09-07' }), { headers: { 'content-type': 'application/json' } }),
  ]) {
    const fetch = vi.fn().mockResolvedValue(response)
    vi.stubGlobal('fetch', fetch)
    const period = { dateFrom: '2026-10-05', dateTo: '2026-10-11' }
    await expect(view === 'stats'
      ? loadLiveRepricerStats('synthetic', undefined, period, { savedOnly: true })
      : loadLiveRepricerParityProducts('synthetic', undefined, period, true, { savedOnly: true })).rejects.toThrow()
    expect(fetch).toHaveBeenCalledTimes(1)
  }
})

it.each(['stats', 'sku'] as const)('bounds a stalled %s refresh without retries or price submission', async view => {
  vi.useFakeTimers()
  const fetch = vi.fn((_url: string, init: RequestInit) => new Promise<Response>((_resolve, reject) => {
    init.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true })
  }))
  vi.stubGlobal('fetch', fetch)
  const period = { dateFrom: '2026-10-05', dateTo: '2026-10-11' }
  const result = view === 'stats'
    ? loadLiveRepricerStats('synthetic', undefined, period, { savedOnly: true })
    : loadLiveRepricerParityProducts('synthetic', undefined, period, true, { savedOnly: true })
  const failed = expect(result).rejects.toThrow('Сервер не ответил вовремя')
  await vi.advanceTimersByTimeAsync(30_000)
  await failed
  expect(fetch).toHaveBeenCalledTimes(1)
  expect(fetch.mock.calls[0][1].signal?.aborted).toBe(true)
})
