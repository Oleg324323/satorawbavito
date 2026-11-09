import { describe, expect, it, vi } from 'vitest'
import { buildAvitoReviewsPreviewPath, parseAvitoReviewsPreview } from './canonicalAvitoReviewsPreview'
import { createAvitoReviewsPreviewClient } from './canonicalAvitoReviewsPreviewClient'

const scope = { organizationId: 1, marketplaceAccountId: 12, provider: 'avito' as const, externalAccountId: '123', sessionKey: 'synthetic-session' }
const query = { offset: '0' }
// Exact projection from T2's synthetic test_account_avito_reviews.py; no private text.
const row = { reviewId: '92312343', score: 2 as number | null, stage: 'fell_through' as string | null, usedInScore: true as boolean | null,
  canAnswer: false as boolean | null, createdAt: '2026-07-28T09:20:00Z' as string | null, itemId: '9007199254740993' as string | null,
  answerId: '777' as string | null, answerStatus: 'moderation' as string | null, accountEvidence: 'credential_scope' }
const wire = () => ({ data: { marketplaceAccountId: scope.marketplaceAccountId, provider: 'avito', externalAccountId: scope.externalAccountId,
  offset: '0', limit: 50, coverageState: 'partial', total: '21' as string | null,
  rating: { isEnabled: true as boolean | null, score: '4.3' as string | null, reviewsCount: '21' as string | null, reviewsWithScoreCount: '12' as string | null }, rows: [{ ...row }] } })
const json = (value: unknown = wire(), status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })
const client = (fetcher: typeof fetch, current = () => true) => createAvitoReviewsPreviewClient('synthetic-only', scope, current, fetcher)

describe('exact Avito Reviews metadata-only preview', () => {
  it('matches the actual synthetic backend projection and preserves large string IDs', () => {
    expect(parseAvitoReviewsPreview(wire(), scope, query)).toEqual(wire().data)
    expect(Object.keys(parseAvitoReviewsPreview(wire(), scope, query).rows[0])).toEqual(Object.keys(row))
  })
  it('preserves null unknowns without default five/false/page-size totals', () => {
    const value = wire(); value.data.total = null
    value.data.rating = { isEnabled: null, score: null, reviewsCount: null, reviewsWithScoreCount: null }
    value.data.rows = [{ reviewId: '000-review', score: null, stage: null, usedInScore: null, canAnswer: null, createdAt: null, itemId: null, answerId: null, answerStatus: null, accountEvidence: 'credential_scope' }]
    expect(parseAvitoReviewsPreview(value, scope, query)).toEqual(value.data)
  })
  it('preserves measured zeros/false/empty page independently of unknowns', () => {
    const value = wire(); value.data.total = '0'; value.data.rows = []
    value.data.rating = { isEnabled: false, score: '0', reviewsCount: '0', reviewsWithScoreCount: '0' }
    expect(parseAvitoReviewsPreview(value, scope, query)).toEqual(value.data)
  })
  it.each(['0', '0.00000000000000000000', '4.3', '4.30', '4.99999999999999999999', '5', '5.00000000000000000000'])('preserves exact rating scale %s without float conversion', score => {
    const value = wire(); value.data.rating.score = score
    expect(parseAvitoReviewsPreview(value, scope, query).rating.score).toBe(score)
  })
  it.each(['-0', '-1', '5.00000000000000000001', '0.000000000000000000001', '4e0', '4.', '.5', '04.3', 'NaN', 'Infinity', 4.3, true])('rejects invalid rating scalar %s', score => {
    const value = wire() as any; value.data.rating.score = score
    expect(() => parseAvitoReviewsPreview(value, scope, query)).toThrow()
  })
  it('accepts int64 maximum count/offset as strings without inferring another page', () => {
    const value = wire(); value.data.offset = '9223372036854775807'; value.data.total = '9223372036854775807'
    expect(parseAvitoReviewsPreview(value, scope, { offset: value.data.offset }).total).toBe(value.data.total)
    expect(buildAvitoReviewsPreviewPath(scope, { offset: value.data.offset })).toBe('/api/v2/avito/accounts/12/reviews/preview?offset=9223372036854775807')
  })
  it.each(['-1', '00', '1.0', '1e3', '1\n', '9223372036854775808', 0, false, null])('rejects invalid offset %s before fetching', async offset => {
    const fetcher = vi.fn<typeof fetch>()
    await expect(client(fetcher).load({ offset } as any)).rejects.toMatchObject({ kind: 'invalid' })
    expect(fetcher).not.toHaveBeenCalled()
  })
  it('rejects missing/extraneous query fields and malformed counts without coercion', () => {
    for (const request of [{}, { offset: '0', limit: 50 }, { offset: '0', cursor: 'next' }]) expect(() => buildAvitoReviewsPreviewPath(scope, request as any)).toThrow()
    for (const total of [0, false, '01', '-1', '9223372036854775808']) {
      const value = wire() as any; value.data.total = total
      expect(() => parseAvitoReviewsPreview(value, scope, query)).toThrow()
    }
  })
  it('preserves opaque IDs and leading zeros but rejects numeric aliases, controls and invalid scalars', () => {
    for (const reviewId of ['000123', '0', 'opaque-review-id', '🙂'.repeat(512), '١٢٣']) {
      const value = wire(); value.data.rows[0].reviewId = reviewId
      expect(parseAvitoReviewsPreview(value, scope, query).rows[0].reviewId).toBe(reviewId)
    }
    for (const reviewId of ['2E4', '-1', '+1', '1.0', '١.٢', ' bad', 'bad\n', 'bad\u007f', '\ud800', '🙂'.repeat(513)]) {
      const value = wire(); value.data.rows[0].reviewId = reviewId
      expect(() => parseAvitoReviewsPreview(value, scope, query)).toThrow()
    }
  })
  it('canAnswer remains nullable metadata, not a new client action or permission', () => {
    for (const canAnswer of [null, true, false]) {
      const value = wire(); value.data.rows[0].canAnswer = canAnswer
      expect(parseAvitoReviewsPreview(value, scope, query).rows[0].canAnswer).toBe(canAnswer)
    }
    expect(Object.keys(client(vi.fn<typeof fetch>()))).toEqual(['invalidate', 'dispose', 'load'])
  })
  it.each([{ marketplaceAccountId: 13 }, { provider: 'wb' }, { externalAccountId: '0123' }, { offset: '1' }, { limit: 49 }, { coverageState: 'complete' }, { hasMore: false }])('rejects cross-scope or invented response fields', change => {
    const value = wire(); Object.assign(value.data, change)
    expect(() => parseAvitoReviewsPreview(value, scope, query)).toThrow()
  })
  it('rejects private content/answer policy or lossy row fields instead of dropping them silently', () => {
    for (const change of [{ text: 'private' }, { buyerName: 'private' }, { itemTitle: 'private' }, { images: [] }, { answerText: 'private' },
      { sendPolicy: true }, { score: 0 }, { score: 6 }, { score: '2' }, { usedInScore: 1 }, { canAnswer: 'false' }, { stage: '' }, { accountEvidence: 'provider_account_id' }]) {
      const value = wire(); Object.assign(value.data.rows[0], change)
      expect(() => parseAvitoReviewsPreview(value, scope, query)).toThrow()
    }
  })
  it('rejects invalid or rewritten time formats and duplicate/oversized pages', () => {
    for (const createdAt of ['2026-02-30T00:00:00Z', '0000-01-01T00:00:00Z', '2026-07-28T09:20:00.000Z', '2026-07-28T12:20:00+03:00', '2026-07-28', 1785230400]) {
      const value = wire() as any; value.data.rows[0].createdAt = createdAt
      expect(() => parseAvitoReviewsPreview(value, scope, query)).toThrow()
    }
    const duplicate = wire(); duplicate.data.rows.push({ ...row }); expect(() => parseAvitoReviewsPreview(duplicate, scope, query)).toThrow()
    const oversized = wire(); oversized.data.rows = Array.from({ length: 51 }, (_, n) => ({ ...row, reviewId: String(n) }))
    expect(() => parseAvitoReviewsPreview(oversized, scope, query)).toThrow()
  })
})

describe('explicit scoped Reviews metadata transport', () => {
  it('does nothing until load and makes one exact GET without automatic paging despite total', async () => {
    const response = json(), fetcher = vi.fn<typeof fetch>().mockResolvedValue(response), api = client(fetcher)
    expect(fetcher).not.toHaveBeenCalled(); expect(await api.load(query)).toEqual(wire().data)
    expect(fetcher).toHaveBeenCalledOnce()
    expect(fetcher.mock.calls[0]).toEqual(['/api/v2/avito/accounts/12/reviews/preview?offset=0', expect.objectContaining({
      method: 'GET', credentials: 'omit', cache: 'no-store', redirect: 'error', headers: { Authorization: 'Bearer synthetic-only', Accept: 'application/json' },
    })]); expect(response.body!.locked).toBe(false)
  })
  it.each([[401, 'scope'], [403, 'scope'], [422, 'invalid'], [429, 'unavailable'], [503, 'unavailable']])('does not retry HTTP %s or reveal its body', async (status, kind) => {
    const cancel = vi.fn(() => Promise.reject(new Error('private cleanup'))), body = new ReadableStream({ cancel })
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(body, { status: Number(status) }))
    await expect(client(fetcher).load(query)).rejects.toMatchObject({ kind })
    expect(cancel).toHaveBeenCalledOnce(); expect(body.locked).toBe(false); expect(fetcher).toHaveBeenCalledOnce()
  })
  it('cleans malformed MIME/UTF8/oversize/JSON/read failures without fallback data', async () => {
    for (const [mime, chunk] of [['text/html', new Uint8Array([1])], ['application/json', new Uint8Array([0xff])], ['application/json', new Uint8Array(1048577)]] as const) {
      const cancel = vi.fn(), body = new ReadableStream({ start(controller) { controller.enqueue(chunk) }, cancel })
      const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(body, { headers: { 'Content-Type': mime } }))
      await expect(client(fetcher).load(query)).rejects.toMatchObject({ kind: 'invalid' })
      expect(cancel).toHaveBeenCalledOnce(); expect(body.locked).toBe(false)
    }
    const broken = new ReadableStream({ start(controller) { controller.error(new Error('private transport')) } })
    for (const result of [new Response('{', { headers: { 'Content-Type': 'application/json' } }), new Response(broken, { headers: { 'Content-Type': 'application/json' } })]) {
      await expect(client(vi.fn<typeof fetch>().mockResolvedValue(result)).load(query)).rejects.toMatchObject({ kind: 'invalid' })
      expect(result.body!.locked).toBe(false)
    }
  })
  it('fences A→B→A and disposal even when the old fetch ignores abort', async () => {
    let finish!: (response: Response) => void
    const fetcher = vi.fn<typeof fetch>().mockImplementationOnce(() => new Promise(resolve => { finish = resolve })).mockResolvedValueOnce(json())
    const api = client(fetcher), pending = api.load(query), rejected = expect(pending).rejects.toMatchObject({ kind: 'stale' })
    api.invalidate(); api.invalidate(); expect(await api.load(query)).toEqual(wire().data)
    finish(json()); await rejected; api.dispose()
    await expect(api.load(query)).rejects.toMatchObject({ kind: 'stale' }); expect(fetcher).toHaveBeenCalledTimes(2)
  })
  it('captures immutable scope/query and rejects changed auth session or pre-aborted reads', async () => {
    let active = true, finish!: (response: Response) => void
    const mutableScope = { ...scope }, mutableQuery = { ...query }
    const fetcher = vi.fn<typeof fetch>().mockImplementation(() => new Promise(resolve => { finish = resolve }))
    const api = createAvitoReviewsPreviewClient('synthetic-only', mutableScope, () => active, fetcher)
    const pending = api.load(mutableQuery); mutableScope.marketplaceAccountId = 13; mutableQuery.offset = '1'
    finish(json()); expect(await pending).toEqual(wire().data)
    const second = api.load(query); active = false; finish(json()); await expect(second).rejects.toMatchObject({ kind: 'stale' })
    const aborted = new AbortController(); aborted.abort()
    await expect(api.load(query, aborted.signal)).rejects.toMatchObject({ kind: 'stale' }); expect(fetcher).toHaveBeenCalledTimes(2)
  })
  it('cancels a stalled stream on caller abort and releases its lock', async () => {
    const cancel = vi.fn(), body = new ReadableStream({ cancel }), abort = new AbortController()
    const pending = client(vi.fn<typeof fetch>().mockResolvedValue(new Response(body, { headers: { 'Content-Type': 'application/json' } }))).load(query, abort.signal)
    const rejected = expect(pending).rejects.toMatchObject({ kind: 'stale' })
    await vi.waitFor(() => expect(body.locked).toBe(true)); abort.abort(); await rejected
    expect(cancel).toHaveBeenCalledOnce(); expect(body.locked).toBe(false)
  })
  it('bounds a fetch ignoring abort to20seconds and cancels its eventual body without retry', async () => {
    vi.useFakeTimers()
    try {
      let finish!: (response: Response) => void
      const fetcher = vi.fn<typeof fetch>().mockImplementation(() => new Promise(resolve => { finish = resolve }))
      const pending = client(fetcher).load(query), rejected = expect(pending).rejects.toMatchObject({ kind: 'timeout' })
      await vi.advanceTimersByTimeAsync(20000); await rejected
      const cancel = vi.fn(); finish(new Response(new ReadableStream({ cancel }))); await vi.advanceTimersByTimeAsync(0)
      expect(cancel).toHaveBeenCalledOnce(); expect(fetcher).toHaveBeenCalledOnce()
    } finally { vi.useRealTimers() }
  })
})
