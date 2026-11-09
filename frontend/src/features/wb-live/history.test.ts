import { afterEach, describe, expect, it, vi } from 'vitest'
import { prepareWbHistory, startWbHistory, validWbHistoryDate, wbHistorySource } from './history'
const sync = { marketplaceAccountId: 11, jobId: 'synthetic-job', state: 'queued', updatedAt: null,
  sources: [{ source: wbHistorySource, state: 'queued', processed: 0, updatedAt: null, errorCode: null }] }
const response = (data: unknown) => new Response(JSON.stringify({ data }), { headers: { 'Content-Type': 'application/json' } })
afterEach(() => vi.useRealTimers())
describe('explicit WB history initialization', () => {
  it('does not submit an already-cancelled owner action', async () => {
    const controller = new AbortController(), fetcher = vi.fn<typeof fetch>()
    controller.abort()
    await expect(startWbHistory('synthetic', 11, prepareWbHistory('2026-01-01'), controller.signal, fetcher)).rejects.toMatchObject({ name: 'AbortError' })
    expect(fetcher).not.toHaveBeenCalled()
  })
  it.each(['', '2026-02-29', '0000-01-01', '2026-04-31', ' 2026-01-01', '2026-01-01T00:00:00Z'])('rejects missing/invalid date-only UI input without inventing a range: %s', date => {
    expect(validWbHistoryDate(date)).toBe(false); expect(() => prepareWbHistory(date)).toThrow()
  })
  it('preserves chosen native date and immutable idempotency body/key', async () => {
    const intent = prepareWbHistory('2024-02-29'), fetcher = vi.fn<typeof fetch>().mockResolvedValue(response(sync))
    expect(Object.isFrozen(intent)).toBe(true)
    expect(await startWbHistory('synthetic', 11, intent, new AbortController().signal, fetcher)).toEqual(sync)
    expect(fetcher.mock.calls[0][0]).toBe('/api/v2/wb/accounts/11/history')
    expect(fetcher.mock.calls[0][1]).toMatchObject({ method: 'POST', body: '{"dateFrom":"2024-02-29"}', headers: { 'Idempotency-Key': intent.idempotencyKey }, credentials: 'omit', redirect: 'error' })
  })
  it.each([401, 403, 409, 503])('never auth-refreshes/replays POST for HTTP %s', async status => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response('{"code":"TOKEN_EXPIRED"}', { status, headers: { 'Content-Type': 'application/json' } }))
    await expect(startWbHistory('synthetic', 11, prepareWbHistory('2026-01-01'), new AbortController().signal, fetcher)).rejects.toMatchObject({ status })
    expect(fetcher).toHaveBeenCalledOnce()
  })
  it('does not accept a cross-account or malformed sync envelope', async () => {
    for (const data of [{ ...sync, marketplaceAccountId: 12 }, { ...sync, sources: {} }, { ...sync, sources: [] }]) {
      await expect(startWbHistory('synthetic', 11, prepareWbHistory('2026-01-01'), new AbortController().signal, vi.fn<typeof fetch>().mockResolvedValue(response(data)))).rejects.toThrow()
    }
  })
  it('retains byte-identical body/key only for an explicit caller retry', async () => {
    const intent = prepareWbHistory('2026-01-01'), fetcher = vi.fn<typeof fetch>().mockRejectedValueOnce(new Error('lost')).mockResolvedValueOnce(response(sync))
    await expect(startWbHistory('synthetic', 11, intent, new AbortController().signal, fetcher)).rejects.toThrow()
    expect(fetcher).toHaveBeenCalledOnce()
    await startWbHistory('synthetic', 11, intent, new AbortController().signal, fetcher)
    expect(fetcher.mock.calls[0][1]?.body).toBe(fetcher.mock.calls[1][1]?.body)
    expect(fetcher.mock.calls[0][1]?.headers).toEqual(fetcher.mock.calls[1][1]?.headers)
  })
  it('rejects late mutation result after owner abort even if fetch ignores cancellation', async () => {
    let finish!: (value: Response) => void
    const controller = new AbortController(), fetcher = vi.fn<typeof fetch>().mockImplementation(() => new Promise(resolve => { finish = resolve }))
    const pending = startWbHistory('synthetic', 11, prepareWbHistory('2026-01-01'), controller.signal, fetcher)
    controller.abort(); finish(response(sync))
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    expect(fetcher).toHaveBeenCalledOnce()
  })
})
