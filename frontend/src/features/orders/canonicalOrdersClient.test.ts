import { describe, expect, it, vi } from 'vitest'
import wire from './__fixtures__/canonicalOrdersBackendWire.json'
import { buildCanonicalOrdersReadPath, buildSavedOrdersSnapshotPath, parseSavedOrdersSnapshot } from './canonicalOrders'
import { createCanonicalOrdersClient } from './canonicalOrdersClient'
import { parseCanonicalOrderAccounts } from './CanonicalOrdersPage'

const scope = { organizationId: 101, accountIds: [1001] }
const checksum = 'a'.repeat(64)
const { rows: ignoredRows, next_cursor: ignoredCursor, ...meta } = wire
void ignoredRows; void ignoredCursor
const snapshot = { ...meta, selection_kind: 'saved_snapshot', query_checksum: checksum, row_count: '9007199254740993' }
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })
describe('saved Orders view discovery and bounded read transport', () => {
  it('accepts WB and Avito metadata without treating disconnected accounts as action permission', () => {
    const data = [{ marketplaceAccountId: 1, provider: 'wb', externalAccountId: '0001', displayName: null, status: 'disconnected' },
      { marketplaceAccountId: 2, provider: 'avito', externalAccountId: 'avito-2', displayName: 'Avito', status: 'active' }]
    expect(parseCanonicalOrderAccounts(data)).toEqual(data)
    expect(() => parseCanonicalOrderAccounts([data[0], data[0]])).toThrow()
    expect(() => parseCanonicalOrderAccounts([{ ...data[1], provider: 'unknown' }])).toThrow()
    expect(() => parseCanonicalOrderAccounts([{ ...data[0], credentialRef: 'must-not-be-here' }])).toThrow()
  })
  it('uses only discovered snapshot/checksum with exact account scope', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(json(snapshot)).mockResolvedValueOnce(json(wire))
    const client = createCanonicalOrdersClient('synthetic-only', () => true, fetcher)
    const saved = await client.discover(scope)
    expect(saved.row_count).toBe('9007199254740993')
    expect((await client.page({ ...scope, snapshotId: saved.snapshot_id, queryChecksum: saved.query_checksum }, saved.snapshot_id)).rows).toHaveLength(1)
    expect(fetcher.mock.calls[0][0]).toBe('/api/v2/orders/snapshots/latest?account_id=1001')
    expect(fetcher.mock.calls[1][0]).toContain(`snapshot_id=${wire.snapshot_id}`)
    expect(fetcher.mock.calls[1][1]).toMatchObject({ cache: 'no-store', credentials: 'omit', redirect: 'error' })
  })
  it('sorts exact repeated account IDs and preserves all six AND filters losslessly', () => {
    expect(buildSavedOrdersSnapshotPath({ ...scope, accountIds: [2, 1] })).toBe('/api/v2/orders/snapshots/latest?account_id=1&account_id=2')
    const path = buildCanonicalOrdersReadPath({ ...scope, queryChecksum: checksum, cursor: 'opaque', filters: {
      marketplace: 'avito', canonical_status: 'accepted', mapping_state: 'mapped', resolution_state: 'unmapped', external_order_id: '000123', raw_status: 'UnknownExact' } })
    const query = new URL(path, 'https://synthetic.invalid').searchParams
    expect(query.get('external_order_id')).toBe('000123'); expect(query.get('raw_status')).toBe('UnknownExact')
    expect(query.get('mapping_state')).toBe('mapped'); expect(query.has('snapshot_id')).toBe(false)
  })
  it.each([' leading', 'trailing ', '\0', '\ud800', 'x'.repeat(4097)])('does not normalize or accept invalid exact IDs', value => {
    expect(() => buildCanonicalOrdersReadPath({ ...scope, queryChecksum: checksum, filters: { external_order_id: value } })).toThrow()
  })
  it.each([{ organization_id: 999 }, { marketplace_account_ids: [1002] }, { coverage_state: 'complete' }, { snapshot_id: 42 }, { row_count: 3 }, { selection_kind: 'all_orders' }])('rejects malformed/cross-scope metadata', change => {
    expect(() => parseSavedOrdersSnapshot({ ...snapshot, ...change }, scope)).toThrow()
  })
  it.each([401, 403, 409, 503])('does not retry or fall back for HTTP %s', async status => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(json({ detail: 'private must not echo' }, status))
    await expect(createCanonicalOrdersClient('synthetic-only', () => true, fetcher).discover(scope)).rejects.not.toThrow('private')
    expect(fetcher).toHaveBeenCalledOnce()
  })
  it('bounds response before decode', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(json({ value: 'x'.repeat(1_048_577) }))
    await expect(createCanonicalOrdersClient('synthetic-only', () => true, fetcher).discover(scope)).rejects.toMatchObject({ kind: 'invalid' })
  })
  it('discards earlier request, disposed session and A→B→A stale response even when fetch ignores abort', async () => {
    let done!: (response: Response) => void
    const fetcher = vi.fn<typeof fetch>().mockImplementationOnce(() => new Promise(resolve => { done = resolve })).mockResolvedValueOnce(json(snapshot))
    const client = createCanonicalOrdersClient('synthetic-only', () => true, fetcher)
    const old = client.discover(scope)
    expect(await client.discover(scope)).toEqual(snapshot)
    done(json(snapshot)); await expect(old).rejects.toMatchObject({ kind: 'stale' })
    client.dispose(); await expect(client.discover(scope)).rejects.toMatchObject({ kind: 'stale' })
    expect(fetcher).toHaveBeenCalledTimes(2)
  })
})
