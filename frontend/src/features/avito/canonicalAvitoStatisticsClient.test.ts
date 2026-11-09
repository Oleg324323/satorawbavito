import { describe, expect, it, vi } from 'vitest'
import { buildCanonicalAvitoStatisticsPath, createCanonicalAvitoStatisticsClient, parseCanonicalAvitoStatistics } from './canonicalAvitoStatisticsClient'

const account = { marketplaceAccountId: 12, provider: 'avito' as const, externalAccountId: '900719925474099312345' }
const period = { dateFrom: '2026-09-01', dateTo: '2026-09-10' }
const metrics = { impressions: '0', views: '90071992547409931234567890', contactsMessenger: null, contacts: '1',
  contactsShowPhone: null, contactsShowPhoneAndMessenger: '0', favorites: '2', spendKopecks: '900719925474099399999', orders: null, buyouts: '0' }
const wire = () => ({ data: { ...account, ...period, status: 'synced',
  rows: [{ itemId: `account:${account.externalAccountId}:totals`, sourceStatus: 'fresh', metrics: { ...metrics } }],
  daily: [{ date: '2026-09-03', metrics: { ...metrics } }] } })
const response = (value: unknown = wire()) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })
const client = (fetcher: typeof fetch, isCurrent = () => true) => createCanonicalAvitoStatisticsClient('synthetic-only', account, isCurrent, fetcher)

describe('canonical Avito statistics exact wire', () => {
  it('retains unknown versus zero, large integers/money, partial and stale source without deriving extra observations', () => {
    const value = wire(); value.data.status = 'partial'; value.data.rows[0].sourceStatus = 'stale'; value.data.daily = []
    const parsed = parseCanonicalAvitoStatistics(value, account, period)
    expect(parsed.rows[0].metrics).toEqual(metrics)
    expect(parsed.status).toBe('partial'); expect(parsed.rows[0].sourceStatus).toBe('stale'); expect(parsed.daily).toEqual([])
  })
  it.each([0, false, -1, '01', '-1', '1.2', '1e3', '', '9'.repeat(8193)])('rejects lossy/noncanonical metrics %s', value => {
    const payload = wire() as any; payload.data.rows[0].metrics.spendKopecks = value
    expect(() => parseCanonicalAvitoStatistics(payload, account, period)).toThrow()
  })
  it.each(['marketplaceAccountId', 'externalAccountId', 'provider', 'dateFrom', 'dateTo'])('rejects response mismatch for %s', field => {
    const payload = wire() as any; payload.data[field] = field === 'marketplaceAccountId' ? 13 : 'other'
    expect(() => parseCanonicalAvitoStatistics(payload, account, period)).toThrow()
  })
  it('requires all ten exact metrics, one account-total row, and unique in-period daily observations', () => {
    const variants: any[] = []
    let value: any = wire(); delete value.data.rows[0].metrics.orders; variants.push(value)
    value = wire(); value.data.rows[0].metrics.revenue = '100'; variants.push(value)
    value = wire(); value.data.rows = []; variants.push(value)
    value = wire(); value.data.rows.push(value.data.rows[0]); variants.push(value)
    value = wire(); value.data.rows[0].itemId = 'listing:123'; variants.push(value)
    value = wire(); value.data.daily.push(value.data.daily[0]); variants.push(value)
    value = wire(); value.data.daily[0].date = '2026-08-31'; variants.push(value)
    value = wire(); value.data.credentialRef = 'forbidden'; variants.push(value)
    for (const invalid of variants) expect(() => parseCanonicalAvitoStatistics(invalid, account, period)).toThrow()
  })
  it('accepts exactly 270 inclusive days, leap day and date-only years below 100', () => {
    for (const valid of [{ dateFrom: '2026-01-01', dateTo: '2026-09-27' }, { dateFrom: '2024-02-29', dateTo: '2024-02-29' },
      { dateFrom: '0001-01-01', dateTo: '0001-01-01' }]) expect(buildCanonicalAvitoStatisticsPath(account, valid)).toContain(`dateFrom=${valid.dateFrom}&dateTo=${valid.dateTo}`)
  })
  it.each([
    ['2026-01-01', '2026-09-28'], ['2026-09-10', '2026-09-09'], ['', '2026-09-10'],
    ['2026-02-29', '2026-03-01'], ['0000-01-01', '0000-01-01'], ['2026-09-01T00:00:00Z', '2026-09-10'], ['2026-9-01', '2026-09-10'],
  ])('rejects invalid period %s → %s before any request', async (dateFrom, dateTo) => {
    const fetcher = vi.fn<typeof fetch>()
    await expect(client(fetcher).load({ dateFrom, dateTo })).rejects.toMatchObject({ kind: 'invalid' })
    expect(fetcher).not.toHaveBeenCalled()
  })
  it('rejects WB, external-ID-as-internal-ID and aliased external IDs', () => {
    for (const invalid of [{ ...account, provider: 'wb' }, { ...account, marketplaceAccountId: '12' }, { ...account, externalAccountId: '0012' }]) {
      expect(() => createCanonicalAvitoStatisticsClient('synthetic-only', invalid as any, () => true)).toThrow()
    }
  })
})

describe('explicit Avito statistics request lifecycle', () => {
  it('does not fetch until load and makes one exact GET without refresh, cache or replay', async () => {
    const result = response(), fetcher = vi.fn<typeof fetch>().mockResolvedValue(result), api = client(fetcher)
    expect(fetcher).not.toHaveBeenCalled()
    expect(await api.load(period)).toEqual(wire().data)
    expect(fetcher).toHaveBeenCalledOnce()
    expect(fetcher.mock.calls[0]).toEqual([`/api/v2/avito/accounts/12/statistics?dateFrom=2026-09-01&dateTo=2026-09-10`, expect.objectContaining({
      method: 'GET', cache: 'no-store', credentials: 'omit', redirect: 'error', headers: { Authorization: 'Bearer synthetic-only', Accept: 'application/json' },
    })])
    expect(result.body!.locked).toBe(false)
  })
  it.each([[401, 'scope'], [403, 'scope'], [422, 'invalid'], [503, 'unavailable'], [429, 'unavailable']])('does not retry HTTP %s or surface its body', async (status, kind) => {
    const cancel = vi.fn(() => Promise.reject(new Error('private cleanup detail')))
    const body = new ReadableStream({ cancel }), fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(body, { status: Number(status) }))
    await expect(client(fetcher).load(period)).rejects.toMatchObject({ kind })
    expect(fetcher).toHaveBeenCalledOnce(); expect(cancel).toHaveBeenCalledOnce(); expect(body.locked).toBe(false)
  })
  it.each([
    ['text/html', new TextEncoder().encode('private provider detail')], ['application/json', new Uint8Array([0xff])],
    ['application/json', new Uint8Array(1_048_577)],
  ])('cancels malformed MIME/UTF-8/oversize streams and unlocks', async (mime, chunk) => {
    const cancel = vi.fn(), body = new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(chunk as Uint8Array) }, cancel })
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(body, { headers: { 'Content-Type': mime as string } }))
    await expect(client(fetcher).load(period)).rejects.toMatchObject({ kind: 'invalid' })
    expect(cancel).toHaveBeenCalledOnce(); expect(body.locked).toBe(false); expect(fetcher.mock.calls[0][1]?.signal?.aborted).toBe(true)
  })
  it('rejects malformed JSON and read errors safely', async () => {
    const broken = new ReadableStream({ start(controller) { controller.error(new Error('private read detail')) } })
    for (const result of [new Response('{', { headers: { 'Content-Type': 'application/json' } }), new Response(broken, { headers: { 'Content-Type': 'application/json' } })]) {
      await expect(client(vi.fn<typeof fetch>().mockResolvedValue(result)).load(period)).rejects.toMatchObject({ kind: 'invalid' })
      expect(result.body!.locked).toBe(false)
    }
  })
  it('enforces timeout even when fetch ignores abort and cancels its late body', async () => {
    vi.useFakeTimers()
    try {
      let finish!: (response: Response) => void
      const fetcher = vi.fn<typeof fetch>().mockImplementation(() => new Promise(resolve => { finish = resolve }))
      const pending = client(fetcher).load(period), rejected = expect(pending).rejects.toMatchObject({ kind: 'timeout' })
      await vi.advanceTimersByTimeAsync(20_000); await rejected
      const cancel = vi.fn(), body = new ReadableStream({ cancel })
      finish(new Response(body)); await vi.advanceTimersByTimeAsync(0)
      expect(cancel).toHaveBeenCalledOnce(); expect(fetcher).toHaveBeenCalledOnce()
    } finally { vi.useRealTimers() }
  })
  it('cancels a stalled body on caller abort and releases its lock', async () => {
    const cancel = vi.fn(), body = new ReadableStream({ cancel }), controller = new AbortController()
    const pending = client(vi.fn<typeof fetch>().mockResolvedValue(new Response(body, { headers: { 'Content-Type': 'application/json' } }))).load(period, controller.signal)
    const rejected = expect(pending).rejects.toMatchObject({ kind: 'stale' })
    await vi.waitFor(() => expect(body.locked).toBe(true))
    controller.abort(); await rejected
    expect(cancel).toHaveBeenCalledOnce(); expect(body.locked).toBe(false)
  })
  it('fences A→B→A and disposed clients even when identity strings match again', async () => {
    let finish!: (response: Response) => void
    const fetcher = vi.fn<typeof fetch>().mockImplementationOnce(() => new Promise(resolve => { finish = resolve })).mockResolvedValueOnce(response())
    const api = client(fetcher), old = api.load(period), rejected = expect(old).rejects.toMatchObject({ kind: 'stale' })
    api.invalidate(); api.invalidate()
    expect(await api.load(period)).toEqual(wire().data)
    finish(response()); await rejected
    api.dispose(); await expect(api.load(period)).rejects.toMatchObject({ kind: 'stale' })
    expect(fetcher).toHaveBeenCalledTimes(2)
  })
  it('rejects session epochs that changed while response was pending and pre-aborted requests', async () => {
    let current = true, finish!: (response: Response) => void
    const fetcher = vi.fn<typeof fetch>().mockImplementation(() => new Promise(resolve => { finish = resolve }))
    const api = client(fetcher, () => current), pending = api.load(period)
    current = false; finish(response()); await expect(pending).rejects.toMatchObject({ kind: 'stale' })
    const controller = new AbortController(); controller.abort()
    await expect(api.load(period, controller.signal)).rejects.toMatchObject({ kind: 'stale' })
    expect(fetcher).toHaveBeenCalledOnce()
  })
  it('captures account and date inputs before awaiting and never logs raw thrown transport errors', async () => {
    const mutableAccount = { ...account }, mutablePeriod = { ...period }
    let finish!: (response: Response) => void
    const api = createCanonicalAvitoStatisticsClient('synthetic-only', mutableAccount, () => true, vi.fn<typeof fetch>().mockImplementation(() => new Promise(resolve => { finish = resolve })))
    const pending = api.load(mutablePeriod)
    mutableAccount.marketplaceAccountId = 13; mutablePeriod.dateTo = '2026-09-11'
    finish(response()); expect(await pending).toEqual(wire().data)
    const fail = vi.fn<typeof fetch>().mockImplementation(() => { throw new Error('private token transport detail') })
    await expect(client(fail).load(period)).rejects.toMatchObject({ kind: 'unavailable' })
  })
})
