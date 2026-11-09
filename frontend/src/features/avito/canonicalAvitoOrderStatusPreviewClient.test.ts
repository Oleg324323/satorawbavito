import { describe, expect, it, vi } from 'vitest'
import { orderStatuses } from '@/features/orders/canonicalOrders'
import { buildAvitoOrderPreviewPath, parseAvitoOrderStatusPreview } from './canonicalAvitoOrderStatusPreview'
import { createAvitoOrderStatusPreviewClient } from './canonicalAvitoOrderStatusPreviewClient'

const scope = { organizationId: 1, marketplaceAccountId: 12, provider: 'avito' as const, externalAccountId: '9007199254740993123', sessionKey: 'synthetic-session' }
const request = { dateFrom: '2026-09-01', page: '9223372036854775807' }
const row = { orderId: '0000012345678901234567890', rawStatus: 'provider_future_status', canonicalStatus: null as string | null,
  mappingState: 'unmapped', mappingVersion: 'avito-order-status-v1', createdAt: null as string | null,
  updatedAt: '2026-09-10T11:12:13.123456789+03:00', accountEvidence: 'credential_scope' }
const wire = () => ({ data: { marketplaceAccountId: scope.marketplaceAccountId, provider: 'avito', externalAccountId: scope.externalAccountId,
  ...request, limit: 20, coverageState: 'partial', hasMore: null as boolean | null, rows: [{ ...row }] } })
const json = (value: unknown = wire(), status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })
const client = (fetcher: typeof fetch, current = () => true) => createAvitoOrderStatusPreviewClient('synthetic-only', scope, current, fetcher)

describe('exact Avito status preview wire', () => {
  it('retains unknown statuses, IDs with leading zeros, null observations and nanosecond offset text; never implies coverage', () => {
    expect(parseAvitoOrderStatusPreview(wire(), scope, request)).toEqual(wire().data)
    expect(Object.keys(parseAvitoOrderStatusPreview(wire(), scope, request).rows[0])).toEqual(Object.keys(row))
  })
  it.each(orderStatuses)('accepts existing canonical enum %s as mapped without inventing raw mapping', status => {
    const value = wire(); value.data.rows[0] = { ...row, canonicalStatus: status, mappingState: 'mapped', accountEvidence: 'provider_account_id' }
    expect(parseAvitoOrderStatusPreview(value, scope, request).rows[0].canonicalStatus).toBe(status)
  })
  it.each([true, false, null])('preserves hasMore %s even on an empty partial page without inferring completion', hasMore => {
    const value = wire(); value.data.hasMore = hasMore; value.data.rows = []
    expect(parseAvitoOrderStatusPreview(value, scope, request)).toMatchObject({ hasMore, coverageState: 'partial', rows: [] })
  })
  it.each(['0', '-1', '01', '1\n', '1.5', '1e2', '9223372036854775808', 2, null])('rejects invalid page %s without a request', async page => {
    const fetcher = vi.fn<typeof fetch>()
    await expect(client(fetcher).load({ ...request, page } as any)).rejects.toMatchObject({ kind: 'invalid' })
    expect(fetcher).not.toHaveBeenCalled()
  })
  it.each(['', '2026-02-29', '0000-01-01', '2026-9-01', '2026-09-01T00:00:00Z'])('rejects noncanonical date %s', dateFrom => {
    expect(() => buildAvitoOrderPreviewPath(scope, { ...request, dateFrom })).toThrow()
  })
  it('accepts leap day and minimum year without inventing a default page, limit or end date', () => {
    expect(buildAvitoOrderPreviewPath(scope, { dateFrom: '2024-02-29', page: '1' })).toBe('/api/v2/avito/accounts/12/orders/status-preview?dateFrom=2024-02-29&page=1')
    expect(buildAvitoOrderPreviewPath(scope, { dateFrom: '0001-01-01', page: '1' })).toContain('dateFrom=0001-01-01')
    for (const extra of [{ limit: 20 }, { dateTo: '2026-09-10' }, { cursor: 'opaque' }]) expect(() => buildAvitoOrderPreviewPath(scope, { ...request, ...extra })).toThrow()
  })
  it.each([{ marketplaceAccountId: 13 }, { provider: 'wb' }, { externalAccountId: '1' }, { dateFrom: '2026-09-02' }, { page: '1' },
    { limit: 19 }, { coverageState: 'complete' }, { hasMore: 0 }, { total: 1 }])('rejects scope/query/coverage/shape mismatch', change => {
    const value = wire(); Object.assign(value.data, change)
    expect(() => parseAvitoOrderStatusPreview(value, scope, request)).toThrow()
  })
  it.each([{ canonicalStatus: 'invented', mappingState: 'mapped' }, { canonicalStatus: 'closed' }, { mappingState: 'mapped' },
    { mappingState: 'ambiguous' }, { mappingVersion: 'v2' }, { accountEvidence: 'assumed' }, { quantity: 1 },
    { orderId: ' x' }, { orderId: 'x\u007f' }, { rawStatus: 'bad\nstatus' }, { rawStatus: '\ud800' }])('rejects malformed/unsupported row', change => {
    const value = wire(); Object.assign(value.data.rows[0], change)
    expect(() => parseAvitoOrderStatusPreview(value, scope, request)).toThrow()
  })
  it.each(['2026-02-30T00:00:00Z', '2026-09-01', '2026-09-01T24:00:00Z', '2026-09-01T12:60:00Z',
    '2026-09-01T12:00:60Z', '2026-09-01T12:00:00+00:99', '2026-09-01T12:00:00+24:00', '2026-09-01T12:00:00.1234567890Z', '2026-09-01T12:00:00Z\n'])('rejects invalid source timestamp %s without repair', updatedAt => {
    const value = wire(); value.data.rows[0].updatedAt = updatedAt
    expect(() => parseAvitoOrderStatusPreview(value, scope, request)).toThrow()
  })
  it('rejects duplicate IDs, over20 rows and omitted nullable fields', () => {
    const duplicate = wire(); duplicate.data.rows.push({ ...row })
    expect(() => parseAvitoOrderStatusPreview(duplicate, scope, request)).toThrow()
    const oversized = wire(); oversized.data.rows = Array.from({ length: 21 }, (_, id) => ({ ...row, orderId: String(id) }))
    expect(() => parseAvitoOrderStatusPreview(oversized, scope, request)).toThrow()
    const missing: any = wire(); delete missing.data.rows[0].createdAt
    expect(() => parseAvitoOrderStatusPreview(missing, scope, request)).toThrow()
  })
})

describe('explicit bounded status preview reads', () => {
  it('performs one exact GET only on load; hasMore does not trigger a next page', async () => {
    const value = wire(); value.data.hasMore = true
    const response = json(value), fetcher = vi.fn<typeof fetch>().mockResolvedValue(response), api = client(fetcher)
    expect(fetcher).not.toHaveBeenCalled()
    expect(await api.load(request)).toEqual(value.data)
    expect(fetcher).toHaveBeenCalledOnce()
    expect(fetcher.mock.calls[0]).toEqual([`/api/v2/avito/accounts/12/orders/status-preview?dateFrom=2026-09-01&page=9223372036854775807`, expect.objectContaining({
      method: 'GET', cache: 'no-store', credentials: 'omit', redirect: 'error', headers: { Authorization: 'Bearer synthetic-only', Accept: 'application/json' },
    })]); expect(response.body!.locked).toBe(false)
  })
  it.each([[401, 'scope'], [403, 'scope'], [422, 'invalid'], [429, 'unavailable'], [503, 'unavailable']])('never retries HTTP %s or propagates private body', async (status, kind) => {
    const cancel = vi.fn(() => Promise.reject(new Error('private cleanup detail'))), body = new ReadableStream({ cancel })
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(body, { status: Number(status) }))
    await expect(client(fetcher).load(request)).rejects.toMatchObject({ kind })
    expect(cancel).toHaveBeenCalledOnce(); expect(fetcher).toHaveBeenCalledOnce(); expect(body.locked).toBe(false)
  })
  it('cleans malformed MIME/UTF8/oversize/parse/read failures', async () => {
    for (const [mime, chunk] of [['text/html', new Uint8Array([1])], ['application/json', new Uint8Array([0xff])], ['application/json', new Uint8Array(1048577)]] as const) {
      const cancel = vi.fn(), body = new ReadableStream({ start(controller) { controller.enqueue(chunk) }, cancel })
      const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(body, { headers: { 'Content-Type': mime } }))
      await expect(client(fetcher).load(request)).rejects.toMatchObject({ kind: 'invalid' })
      expect(cancel).toHaveBeenCalledOnce(); expect(body.locked).toBe(false)
    }
    const failed = new ReadableStream({ start(controller) { controller.error(new Error('private read detail')) } })
    for (const result of [new Response('{', { headers: { 'Content-Type': 'application/json' } }), new Response(failed, { headers: { 'Content-Type': 'application/json' } })]) {
      await expect(client(vi.fn<typeof fetch>().mockResolvedValue(result)).load(request)).rejects.toMatchObject({ kind: 'invalid' })
      expect(result.body!.locked).toBe(false)
    }
  })
  it('monotonic invalidation fences A→B→A even if the old transport ignores abort', async () => {
    let finish!: (response: Response) => void
    const fetcher = vi.fn<typeof fetch>().mockImplementationOnce(() => new Promise(resolve => { finish = resolve })).mockResolvedValueOnce(json())
    const api = client(fetcher), old = api.load(request), rejected = expect(old).rejects.toMatchObject({ kind: 'stale' })
    api.invalidate(); api.invalidate(); expect(await api.load(request)).toEqual(wire().data)
    finish(json()); await rejected
    api.dispose(); await expect(api.load(request)).rejects.toMatchObject({ kind: 'stale' })
    expect(fetcher).toHaveBeenCalledTimes(2)
  })
  it('rejects changed session and pre-aborted reads without an automatic new request', async () => {
    let active = true, finish!: (response: Response) => void
    const fetcher = vi.fn<typeof fetch>().mockImplementation(() => new Promise(resolve => { finish = resolve })), api = client(fetcher, () => active)
    const pending = api.load(request); active = false; finish(json())
    await expect(pending).rejects.toMatchObject({ kind: 'stale' })
    const signal = new AbortController(); signal.abort()
    await expect(api.load(request, signal.signal)).rejects.toMatchObject({ kind: 'stale' }); expect(fetcher).toHaveBeenCalledOnce()
  })
  it('captures original scope/query independently of caller mutation', async () => {
    const mutable = { ...scope }, query = { ...request }
    let finish!: (response: Response) => void
    const api = createAvitoOrderStatusPreviewClient('synthetic-only', mutable, () => true, vi.fn<typeof fetch>().mockImplementation(() => new Promise(resolve => { finish = resolve })))
    const pending = api.load(query); mutable.marketplaceAccountId = 13; query.page = '1'
    finish(json()); expect(await pending).toEqual(wire().data)
  })
  it('enforces timeout on an abort-ignoring transport and cancels late response body', async () => {
    vi.useFakeTimers()
    try {
      let finish!: (response: Response) => void
      const fetcher = vi.fn<typeof fetch>().mockImplementation(() => new Promise(resolve => { finish = resolve }))
      const pending = client(fetcher).load(request), rejected = expect(pending).rejects.toMatchObject({ kind: 'timeout' })
      await vi.advanceTimersByTimeAsync(20000); await rejected
      const cancel = vi.fn(); finish(new Response(new ReadableStream({ cancel }))); await vi.advanceTimersByTimeAsync(0)
      expect(cancel).toHaveBeenCalledOnce(); expect(fetcher).toHaveBeenCalledOnce()
    } finally { vi.useRealTimers() }
  })
})
