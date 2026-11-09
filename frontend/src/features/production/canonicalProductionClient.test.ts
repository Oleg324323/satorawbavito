import { describe, expect, it, vi } from 'vitest'
import { createCanonicalProductionClient } from './canonicalProductionClient'
import { productionAssignmentInput, productionId, productionReadResponse } from './canonicalProduction'

const scope = { organizationId: 7, marketplaceAccountId: 11, sessionKey: 'synthetic-session' }
const at = '2026-09-10T10:11:12.123456Z'
const item = { workItemId: '9007199254740993', organizationId: 7, marketplaceAccountId: 11, orderId: '9007199254740994', orderItemId: '9007199254740995',
  sourceItemVersion: '9007199254740996', requiredQuantity: 2147483647, plannedQuantity: 2, remainingQuantity: 2147483645,
  catalogSkuId: null as number | null, version: '9007199254740997', createdAt: at, updatedAt: at, currentAssignmentReceiptId: null as string | null }
const createInput = { orderItemId: item.orderItemId, expectedSourceItemVersion: item.sourceItemVersion }
const assignInput = { catalogSkuId: 12, idempotencyKey: 'Exact-Key/🙂', reason: 'Ручной выбор\nSKU 🙂' }
const readWire = (value = item) => ({ schemaVersion: 'production-work-item-v1', item: value })
const createWire = { schemaVersion: 'production-create-v1', workItemId: item.workItemId, replayed: false }
const assignWire = () => ({ schemaVersion: 'production-assignment-v1', replayed: false, result: { workItemId: item.workItemId,
  version: String(BigInt(item.version) + 1n), catalogSkuId: 12, requiredQuantity: item.requiredQuantity, plannedQuantity: item.plannedQuantity,
  remainingQuantity: item.remainingQuantity, sourceItemVersion: item.sourceItemVersion } })
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })
const api = (fetcher: typeof fetch, isCurrent = () => true) => createCanonicalProductionClient('synthetic-only', scope, isCurrent, fetcher)
const fetchQueue = (...values: unknown[]) => { const fetcher = vi.fn<typeof fetch>(); for (const value of values) fetcher.mockResolvedValueOnce(json(value)); return fetcher }

describe('Production exact codecs and captured scope', () => {
  it('preserves signed-bigint maximum and int32 quantities without numeric conversion of IDs', () => {
    expect(productionId.parse('9223372036854775807')).toBe('9223372036854775807')
    expect(productionReadResponse.parse(readWire()).item).toEqual(item)
  })
  it.each(['9223372036854775808', '0', '01', '-1', '1.0', 9007199254740993, null])('rejects invalid identifier %s', value => {
    expect(() => productionId.parse(value)).toThrow()
  })
  it.each(['1\n', '1\r', '1\r\n', '1\u2028', '1\u2029'])('rejects a trailing line terminator in Production identifiers %j', value => {
    expect(() => productionId.parse(value)).toThrow()
    const fetcher = vi.fn<typeof fetch>(), client = api(fetcher)
    expect(() => client.prepareCreate({ ...createInput, orderItemId: value })).toThrow()
    expect(fetcher).not.toHaveBeenCalled()
  })
  it.each([' key', 'key ', '\u0085key', 'key\u001c', '', 'key\0inside', 'key\ud800'])('rejects exact-domain whitespace/NUL/surrogate input without trimming', value => {
    for (const field of ['idempotencyKey', 'reason']) expect(() => productionAssignmentInput.parse({ ...assignInput, expectedVersion: '1', [field]: value })).toThrow()
  })
  it('accepts domain-valid exact text, including FEFF not stripped by Python', () => {
    expect(productionAssignmentInput.parse({ ...assignInput, expectedVersion: '1', reason: '\ufeffexact\ufeff' }).reason).toBe('\ufeffexact\ufeff')
  })
  it.each([
    { organizationId: 8 }, { marketplaceAccountId: 12 }, { workItemId: '1' }, { remainingQuantity: 0 },
    { plannedQuantity: 2147483648 }, { requiredQuantity: true }, { version: 1 }, { createdAt: '2026-09-10T10:11:12Z' }, { updatedAt: '2026-02-30T00:00:00.000000Z' },
  ])('rejects wrong scope/identity/quantities/timestamps', async change => {
    const fetcher = fetchQueue(readWire({ ...item, ...change } as any))
    await expect(api(fetcher).read(item.workItemId)).rejects.toMatchObject({ kind: 'invalid' })
  })
  it('never sends caller organization/member/session authorization in command body or URL', async () => {
    const fetcher = fetchQueue(createWire), client = api(fetcher)
    expect(fetcher).not.toHaveBeenCalled()
    expect(() => client.prepareCreate({ ...createInput, organizationId: 8 } as any)).toThrow()
    const handle = client.prepareCreate(createInput)
    await client.dispatch(handle)
    const [path, init] = fetcher.mock.calls[0]
    expect(path).toBe('/api/v2/production/accounts/11/work-items')
    expect(init).toMatchObject({ method: 'POST', credentials: 'omit', cache: 'no-store', redirect: 'error' })
    expect(JSON.parse(init!.body as string)).toEqual(createInput)
    expect(init!.headers).toEqual({ Authorization: 'Bearer synthetic-only', Accept: 'application/json', 'Content-Type': 'application/json' })
  })
})

describe('Production explicit command and readback lifecycle', () => {
  it('creation receipt requires explicit scope/order/source readback and is not an optimistic item', async () => {
    const fetcher = fetchQueue(createWire, readWire()), client = api(fetcher), handle = client.prepareCreate(createInput)
    const result = await client.dispatch(handle)
    expect(result).toEqual({ receipt: createWire, readbackRequired: true })
    expect(fetcher).toHaveBeenCalledOnce()
    expect(() => client.prepareCreate(createInput)).toThrow()
    expect(await client.readback()).toEqual({ item, previousOutcome: 'receipt', commandOutcomeVerified: false })
    expect(fetcher.mock.calls[1][0]).toBe(`/api/v2/production/accounts/11/work-items/${item.workItemId}`)
    expect(fetcher.mock.calls[1][1]?.method).toBe('GET')
    expect(client.getRecovery()).toBeNull()
    await expect(client.dispatch(handle)).rejects.toMatchObject({ kind: 'invalid' })
  })
  it('creation readback cannot clear uncertainty for a different source line', async () => {
    const fetcher = fetchQueue(createWire, readWire({ ...item, orderItemId: '5' })), client = api(fetcher)
    await client.dispatch(client.prepareCreate(createInput))
    await expect(client.readback()).rejects.toMatchObject({ kind: 'invalid' })
    expect(client.getRecovery()?.workItemId).toBe(item.workItemId)
  })
  it.each([503, 429])('unknown creation HTTP %s cannot invent ID, query or retry', async status => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(json({ detail: { code: 'PRODUCTION_READBACK_REQUIRED' } }, status)), client = api(fetcher)
    const handle = client.prepareCreate(createInput)
    await expect(client.dispatch(handle)).rejects.toMatchObject({ kind: 'readback-required' })
    expect(client.getRecovery()).toMatchObject({ workItemId: null, outcome: 'unknown' })
    await expect(client.readback()).rejects.toMatchObject({ kind: 'readback-required' })
    await expect(client.dispatch(handle)).rejects.toMatchObject({ kind: 'readback-required' })
    expect(() => client.prepareCreate(createInput)).toThrow()
    expect(fetcher).toHaveBeenCalledOnce()
  })
  it('unknown assignment permits known-ID GET only; it does not claim that the key committed', async () => {
    const fetcher = fetchQueue(readWire()), client = api(fetcher)
    fetcher.mockRejectedValueOnce(new Error('private transport detail')).mockResolvedValueOnce(json(readWire()))
    const current = await client.read(item.workItemId), handle = client.prepareAssignment(current, assignInput)
    await expect(client.dispatch(handle)).rejects.toMatchObject({ kind: 'readback-required' })
    expect(client.getRecovery()).toMatchObject({ workItemId: item.workItemId, outcome: 'unknown' })
    expect((await client.readback()).commandOutcomeVerified).toBe(false)
    await expect(client.dispatch(handle)).rejects.toMatchObject({ kind: 'invalid' })
    expect(fetcher.mock.calls.map(call => call[1]?.method)).toEqual(['GET', 'POST', 'GET'])
    expect(JSON.parse(fetcher.mock.calls[1][1]!.body as string)).toEqual({ ...assignInput, expectedVersion: item.version })
  })
  it('assignment captures immutable body and validates exact version+1/SKU/source/quantities before accepting a receipt', async () => {
    const fetcher = fetchQueue(readWire(), assignWire()), client = api(fetcher), current = await client.read(item.workItemId)
    const input = { ...assignInput }, handle = client.prepareAssignment(current, input)
    input.reason = 'later edit'; input.idempotencyKey = 'different key'
    const result = await client.dispatch(handle)
    expect(result).toEqual({ receipt: assignWire(), readbackRequired: true })
    expect(JSON.parse(fetcher.mock.calls[1][1]!.body as string)).toEqual({ ...assignInput, expectedVersion: item.version })
    expect(client.getRecovery()?.outcome).toBe('receipt')
    expect(() => client.prepareAssignment(current, assignInput)).toThrow()
  })
  it.each([{ workItemId: '1' }, { version: item.version }, { catalogSkuId: 13 }, { sourceItemVersion: '1' },
    { requiredQuantity: 4, plannedQuantity: 2, remainingQuantity: 2 }])('malformed/cross-target assignment receipt leaves unknown outcome', async change => {
    const wire = assignWire(); Object.assign(wire.result, change)
    const fetcher = fetchQueue(readWire(), wire), client = api(fetcher), current = await client.read(item.workItemId)
    await expect(client.dispatch(client.prepareAssignment(current, assignInput))).rejects.toMatchObject({ kind: 'readback-required' })
    expect(client.getRecovery()?.outcome).toBe('unknown')
  })
  it('rejects forged/read-from-another-client item, handles, overridden version and changed prepared state', async () => {
    const fetcher = fetchQueue(readWire(), readWire()), client = api(fetcher)
    const current = await client.read(item.workItemId)
    expect(() => client.prepareAssignment({ ...current }, assignInput)).toThrow()
    expect(() => client.prepareAssignment(current, { ...assignInput, expectedVersion: '2' } as any)).toThrow()
    const handle = client.prepareAssignment(current, assignInput)
    await client.read(item.workItemId)
    await expect(client.dispatch(handle)).rejects.toMatchObject({ kind: 'invalid' })
    await expect(client.dispatch({ operation: 'create' })).rejects.toMatchObject({ kind: 'invalid' })
    expect(fetcher).toHaveBeenCalledTimes(2)
  })
  it.each([400, 401, 403, 404, 409])('known HTTP rejection %s is not retried or reported committed', async status => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(json({ detail: { code: 'VERSION_CONFLICT' } }, status)), client = api(fetcher)
    const handle = client.prepareCreate(createInput)
    await expect(client.dispatch(handle)).rejects.toMatchObject({ kind: 'rejected', status })
    await expect(client.dispatch(handle)).rejects.toMatchObject({ kind: 'invalid' })
    expect(fetcher).toHaveBeenCalledOnce(); expect(client.getRecovery()).toBeNull()
  })
  it('postcommit malformed UTF-8/MIME/oversize responses cancel streams and preserve unknown write outcome', async () => {
    for (const [mime, chunk] of [['text/html', new Uint8Array([1])], ['application/json', new Uint8Array([0xff])], ['application/json', new Uint8Array(131073)]] as const) {
      const cancel = vi.fn(() => Promise.reject(new Error('private cleanup error'))), body = new ReadableStream({ start(controller) { controller.enqueue(chunk) }, cancel })
      const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(body, { headers: { 'Content-Type': mime } })), client = api(fetcher)
      await expect(client.dispatch(client.prepareCreate(createInput))).rejects.toMatchObject({ kind: 'readback-required' })
      expect(cancel).toHaveBeenCalledOnce(); expect(body.locked).toBe(false)
    }
  })
  it('stale session and disposed ABA client do not publish late write receipts', async () => {
    let finish!: (response: Response) => void
    const fetcher = vi.fn<typeof fetch>().mockImplementation(() => new Promise(resolve => { finish = resolve })), client = api(fetcher)
    const pending = client.dispatch(client.prepareCreate(createInput)), rejected = expect(pending).rejects.toMatchObject({ kind: 'readback-required' })
    client.dispose(); finish(json(createWire)); await rejected
    expect(() => client.prepareCreate(createInput)).toThrow()
    await expect(client.read(item.workItemId)).rejects.toMatchObject({ kind: 'stale' })
  })
  it('20-second timeout stops an abort-ignoring write; late response body is canceled without replay', async () => {
    vi.useFakeTimers()
    try {
      let finish!: (response: Response) => void
      const fetcher = vi.fn<typeof fetch>().mockImplementation(() => new Promise(resolve => { finish = resolve })), client = api(fetcher)
      const pending = client.dispatch(client.prepareCreate(createInput)), rejected = expect(pending).rejects.toMatchObject({ kind: 'readback-required' })
      await vi.advanceTimersByTimeAsync(20_000); await rejected
      const cancel = vi.fn(), body = new ReadableStream({ cancel }); finish(new Response(body)); await vi.advanceTimersByTimeAsync(0)
      expect(cancel).toHaveBeenCalledOnce(); expect(fetcher).toHaveBeenCalledOnce(); expect(client.getRecovery()?.workItemId).toBeNull()
    } finally { vi.useRealTimers() }
  })
  it('denies session changes before dispatch without network', async () => {
    let current = true
    const fetcher = vi.fn<typeof fetch>(), client = api(fetcher, () => current), handle = client.prepareCreate(createInput)
    current = false
    await expect(client.dispatch(handle)).rejects.toMatchObject({ kind: 'stale' })
    expect(fetcher).not.toHaveBeenCalled()
  })
  it('requires readback at least as new as the accepted assignment receipt', async () => {
    const fetcher = fetchQueue(readWire(), assignWire(), readWire()), client = api(fetcher)
    const current = await client.read(item.workItemId)
    await client.dispatch(client.prepareAssignment(current, assignInput))
    await expect(client.readback()).rejects.toMatchObject({ kind: 'invalid' })
    expect(client.getRecovery()?.minimumVersion).toBe(assignWire().result.version)
  })
  it('does not allow a concurrent read to cancel a dispatched command', async () => {
    let finish!: (response: Response) => void
    const fetcher = vi.fn<typeof fetch>().mockImplementation(() => new Promise(resolve => { finish = resolve })), client = api(fetcher)
    const pending = client.dispatch(client.prepareCreate(createInput))
    await expect(client.read(item.workItemId)).rejects.toMatchObject({ kind: 'busy' })
    finish(json(createWire)); await pending
    expect(fetcher).toHaveBeenCalledOnce()
  })
  it('failed reread invalidates previously loaded authoring state', async () => {
    const fetcher = fetchQueue(readWire()), client = api(fetcher)
    fetcher.mockResolvedValueOnce(json({}, 503))
    const current = await client.read(item.workItemId)
    await expect(client.read(item.workItemId)).rejects.toMatchObject({ kind: 'unavailable' })
    expect(() => client.prepareAssignment(current, assignInput)).toThrow()
  })
  it('recovers unknown creation only through an explicit exact-source GET, never by retrying POST', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(json({}, 503)).mockResolvedValueOnce(json(readWire())), client = api(fetcher)
    const mutableInput = { ...createInput }, handle = client.prepareCreate(mutableInput)
    await expect(client.dispatch(handle)).rejects.toMatchObject({ kind: 'readback-required' })
    mutableInput.orderItemId = '1'; mutableInput.expectedSourceItemVersion = '2'
    expect(fetcher).toHaveBeenCalledOnce()
    const result = await client.readbackUnknownCreate()
    expect(result).toEqual({ item, previousOutcome: 'unknown', commandOutcomeVerified: false })
    expect(client.getRecovery()).toBeNull()
    expect(fetcher.mock.calls[1][0]).toBe(`/api/v2/production/accounts/11/work-items/by-order-item/${createInput.orderItemId}?expectedSourceItemVersion=${createInput.expectedSourceItemVersion}`)
    expect(fetcher.mock.calls[1][1]?.method).toBe('GET'); expect(fetcher.mock.calls[1][1]?.body).toBeUndefined()
    expect(() => client.prepareAssignment(result.item, assignInput)).not.toThrow()
    await expect(client.dispatch(handle)).rejects.toMatchObject({ kind: 'invalid' })
    expect(fetcher).toHaveBeenCalledTimes(2)
  })
  it.each([404, 409, 503])('unknown creation lookup HTTP %s retains uncertainty without retry or fallback', async status => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(json({}, 503)).mockResolvedValueOnce(json({}, status)), client = api(fetcher)
    await expect(client.dispatch(client.prepareCreate(createInput))).rejects.toMatchObject({ kind: 'readback-required' })
    const before = client.getRecovery()
    await expect(client.readbackUnknownCreate()).rejects.toMatchObject({ kind: status === 503 ? 'unavailable' : 'rejected' })
    expect(client.getRecovery()).toEqual(before)
    expect(() => client.prepareCreate(createInput)).toThrow()
    expect(fetcher.mock.calls.map(call => call[1]?.method)).toEqual(['POST', 'GET'])
  })
  it.each([{ organizationId: 8 }, { marketplaceAccountId: 12 }, { orderItemId: '1' }, { sourceItemVersion: '1' },
    { workItemId: '0' }, { version: '0' }, { requiredQuantity: 0 }])('unknown creation lookup rejects incoherent committed state', async change => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(json({}, 503)).mockResolvedValueOnce(json(readWire({ ...item, ...change }))), client = api(fetcher)
    await expect(client.dispatch(client.prepareCreate(createInput))).rejects.toMatchObject({ kind: 'readback-required' })
    await expect(client.readbackUnknownCreate()).rejects.toMatchObject({ kind: 'invalid' })
    expect(client.getRecovery()).toMatchObject({ operation: 'create', outcome: 'unknown', workItemId: null })
    expect(fetcher).toHaveBeenCalledTimes(2)
  })
  it('unknown-create lookup is unavailable without matching recovery or when creation already returned its ID', async () => {
    const fetcher = fetchQueue(createWire), client = api(fetcher)
    await expect(client.readbackUnknownCreate()).rejects.toMatchObject({ kind: 'readback-required' })
    expect(fetcher).not.toHaveBeenCalled()
    await client.dispatch(client.prepareCreate(createInput))
    await expect(client.readbackUnknownCreate()).rejects.toMatchObject({ kind: 'readback-required' })
    expect(fetcher).toHaveBeenCalledOnce()
  })
  it('unknown-create lookup rejects stale-session response and retains the captured recovery', async () => {
    let current = true, finish!: (response: Response) => void
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(json({}, 503)).mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    const client = api(fetcher, () => current)
    await expect(client.dispatch(client.prepareCreate(createInput))).rejects.toMatchObject({ kind: 'readback-required' })
    const pending = client.readbackUnknownCreate(); current = false; finish(json(readWire()))
    await expect(pending).rejects.toMatchObject({ kind: 'stale' })
    expect(client.getRecovery()?.outcome).toBe('unknown')
  })
})
