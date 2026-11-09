import { describe, it, expect, vi, afterEach } from 'vitest'
import { createReadCache } from './readCache'
afterEach(() => vi.useRealTimers())
describe('page-scoped WB reads', () => {
  it('coalesces matching consumers, caches repeats and isolates session/period keys', async () => {
    const cache = createReadCache<number>()
    const read = vi.fn().mockResolvedValue(42)
    expect(await Promise.all([cache.get('account1:day1', read), cache.get('account1:day1', read)])).toEqual([42, 42])
    await cache.get('account1:day1', read)
    expect(read).toHaveBeenCalledTimes(1)
    await cache.get('account2:day1', read)
    await cache.get('account1:day2', read)
    expect(read).toHaveBeenCalledTimes(3)
    cache.clear(); await cache.get('account1:day1', read)
    expect(read).toHaveBeenCalledTimes(4)
  })
  it('does not cancel a shared read when one screen leaves', async () => {
    const cache = createReadCache<number>()
    let finish!: (n: number) => void
    let requestSignal!: AbortSignal
    const read = (signal: AbortSignal) => { requestSignal = signal; return new Promise<number>(resolve => { finish = resolve }) }
    const owner = new AbortController()
    const first = cache.get('one', read, owner.signal)
    const second = cache.get('one', read)
    await Promise.resolve()
    owner.abort()
    await expect(first).rejects.toMatchObject({ name: 'AbortError' })
    expect(requestSignal.aborted).toBe(false)
    finish(5); expect(await second).toBe(5)
  })
  it('bounds timeouts and never loops or caches 429/errors', async () => {
    vi.useFakeTimers()
    const cache = createReadCache<number>(1000, 30_000)
    const hang = vi.fn((signal: AbortSignal) => new Promise<number>((_, reject) => signal.addEventListener('abort', () => reject(signal.reason))))
    const pending = cache.get('one', hang)
    const assertion = expect(pending).rejects.toThrow('30 секунд')
    await vi.advanceTimersByTimeAsync(30_000)
    await assertion
    expect(hang).toHaveBeenCalledTimes(1)
    const rateLimited = vi.fn().mockRejectedValue(new Error('HTTP 429'))
    await expect(cache.get('one', rateLimited)).rejects.toThrow('429')
    await expect(cache.get('one', rateLimited)).rejects.toThrow('429')
    expect(rateLimited).toHaveBeenCalledTimes(2)
  })
})
