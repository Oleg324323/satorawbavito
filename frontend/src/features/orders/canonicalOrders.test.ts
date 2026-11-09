import { describe, expect, it } from 'vitest'
import { buildCanonicalOrdersReadPath, parseCanonicalOrdersPage } from './canonicalOrders'
import backendWire from './__fixtures__/canonicalOrdersBackendWire.json'

const checksum = 'a'.repeat(64)
const scope = { organizationId: 7, accountIds: [11] }
function wbPage(): any {
  const input: any = page(), observed = input.rows[0].observation
  observed.identity.marketplace = 'wb'
  observed.source_kind = 'wb-statistics-supplier-orders'
  input.account_coverage[0].source_kind = observed.source_kind
  observed.status = { raw_status: null, canonical_status: 'cancelled', mapping_state: 'mapped', mapping_version: 'wb-statistics-status-v1', evidence_source: observed.source_kind }
  observed.wb_is_cancelled = true
  observed.wb_cancel_evidence_present = true
  observed.items[0].stable_unit_id = '001'
  observed.items[0].identity.source_line_key = 'wb:unit:3:001'
  return input
}
function page() {
  const identity = { organization_id: 7, marketplace_account_id: 11, marketplace: 'avito', external_order_id: '00042' }
  const item = { order_identity: identity, source_line_key: 'avito:order:5:00042:item:5:00008:occurrence:0', external_item_id: '00008', occurrence_index: 0 }
  return {
    organization_id: 7, marketplace_account_ids: [11], snapshot_id: '9223372036854775807',
    high_water_mark: 'synthetic-hwm', published_at: '2026-09-09T00:00:00Z', coverage_state: 'partial', next_cursor: null,
    account_coverage: [{ marketplace_account_id: 11, source_kind: 'avito-order-management', source_version: 'adapter-v1',
      state: 'partial', source_snapshot: 'synthetic-snapshot', requested_from: null, requested_to: null }],
    rows: [{ observation: { identity, source_kind: 'avito-order-management', adapter_version: 'adapter-v1',
      source_revision: null, effective_at: null, observed_at: '2026-09-09T00:00:00Z',
      status: { raw_status: 'unknown-future-status', canonical_status: null, mapping_state: 'unmapped',
        mapping_version: 'avito-order-status-v1', evidence_source: 'avito-order-management' },
      items: [{ identity: item, quantity: 2, stable_order_line_id: null, stable_unit_id: null }],
      wb_is_cancelled: null, wb_cancel_evidence_present: null },
    item_identity: item, row_version: '9007199254740993',
    resolution: { state: 'unmapped', marketplace_product_id: null, marketplace_offer_id: null, catalog_sku_id: null, evidence_version: 'catalog-v1' },
    readiness_blockers: ['source_readiness_unproven', 'coverage_unproven'], deadlines: [] }],
  }
}

describe('dormant canonical Orders boundary', () => {
  it('accepts actual T3 Pydantic JSON without renaming or flattening the wire', () => {
    const parsed = parseCanonicalOrdersPage(backendWire, { organizationId: 101, accountIds: [1001] })
    expect(parsed).toEqual(backendWire)
    expect(parsed.rows[0].observation.items).toHaveLength(2)
  })

  it('preserves WB cancellation evidence without treating the statistics feed as fulfillment-ready', () => {
    const parsed = parseCanonicalOrdersPage(wbPage(), scope)
    expect(parsed.rows[0].observation.status.raw_status).toBeNull()
    expect(parsed.rows[0].observation.status.canonical_status).toBe('cancelled')
    expect(parsed.rows[0].readiness_blockers).toContain('source_readiness_unproven')
  })

  it.each(['wb_is_cancelled', 'wb_cancel_evidence_present', 'stable_unit_id'])('rejects missing WB evidence: %s', field => {
    const input = wbPage(), observed = input.rows[0].observation
    if (field === 'stable_unit_id') observed.items[0][field] = null
    else observed[field] = null
    expect(() => parseCanonicalOrdersPage(input, scope)).toThrow('CANONICAL_ORDERS_RESPONSE_INVALID')
  })

  it('accepts a nonempty microsecond coverage interval without rounding it to milliseconds', () => {
    const input: any = page()
    input.account_coverage[0].requested_from = '2026-09-09T00:00:00.000001Z'
    input.account_coverage[0].requested_to = '2026-09-09T03:00:00.000002+03:00'
    expect(parseCanonicalOrdersPage(input, scope).account_coverage[0].requested_from).toBe('2026-09-09T00:00:00.000001Z')
  })

  it.each(['2026-09-09T03:00:00.000001+03:00', '2026-09-09T00:00:00Z', '2026-09-08T23:59:59.999999Z'])('rejects equal or reversed precise coverage: %s', end => {
    const input: any = page()
    input.account_coverage[0].requested_from = '2026-09-09T00:00:00.000001Z'
    input.account_coverage[0].requested_to = end
    expect(() => parseCanonicalOrdersPage(input, scope)).toThrow('CANONICAL_ORDERS_RESPONSE_INVALID')
  })
  it('preserves exact versions, external IDs and unknown status without readiness inference', () => {
    const parsed = parseCanonicalOrdersPage(page(), scope)
    expect(parsed.snapshot_id).toBe('9223372036854775807')
    expect(parsed.rows[0].row_version).toBe('9007199254740993')
    expect(parsed.rows[0].observation.identity.external_order_id).toBe('00042')
    expect(parsed.rows[0].observation.status.canonical_status).toBeNull()
    expect(parsed.rows[0].observation.status.raw_status).toBe('unknown-future-status')
    expect(parsed.rows[0].readiness_blockers).toEqual(['source_readiness_unproven', 'coverage_unproven'])
    expect(parsed.rows[0]).not.toHaveProperty('printable')
  })

  it.each([0, 1, 9007199254740992, '0', '01', '-1', '9223372036854775808', ' 1', '1e3'])('rejects invalid version %s', version => {
    const input = page() as unknown as { rows: { row_version: unknown }[] }
    input.rows[0].row_version = version
    expect(() => parseCanonicalOrdersPage(input, scope)).toThrow('CANONICAL_ORDERS_RESPONSE_INVALID')
  })

  it.each([
    ['owner', (p: any) => { p.organization_id = 8 }],
    ['account', (p: any) => { p.marketplace_account_ids = [12] }],
    ['row owner', (p: any) => { p.rows[0].observation.identity.organization_id = 8 }],
    ['item mismatch', (p: any) => { p.rows[0].item_identity = { ...p.rows[0].item_identity, source_line_key: 'different' } }],
    ['duplicate row', (p: any) => { p.rows.push(p.rows[0]) }],
    ['duplicate coverage', (p: any) => { p.account_coverage.push(p.account_coverage[0]) }],
    ['missing coverage', (p: any) => { p.account_coverage = [] }],
    ['false complete', (p: any) => { p.coverage_state = 'complete' }],
    ['source mismatch', (p: any) => { p.account_coverage[0].source_version = 'other' }],
    ['extra data', (p: any) => { p.private_payload = 'SENSITIVE_SENTINEL' }],
    ['unsafe ID', (p: any) => { p.rows[0].resolution.marketplace_product_id = 2147483648 }],
    ['coerced quantity', (p: any) => { p.rows[0].observation.items[0].quantity = '2' }],
    ['false mapped', (p: any) => { p.rows[0].observation.status.mapping_state = 'mapped' }],
    ['wrong source', (p: any) => { p.rows[0].observation.source_kind = 'wb-statistics-supplier-orders'; p.account_coverage[0].source_kind = 'wb-statistics-supplier-orders' }],
    ['WB evidence on Avito', (p: any) => { p.rows[0].observation.wb_is_cancelled = false }],
    ['duplicate source line', (p: any) => { p.rows[0].observation.items.push(p.rows[0].observation.items[0]) }],
    ['wrong unit evidence', (p: any) => { p.rows[0].observation.items[0].stable_unit_id = 'fake-wb-unit' }],
    ['false SKU', (p: any) => { p.rows[0].resolution.catalog_sku_id = 1 }],
    ['missing blockers', (p: any) => { p.rows[0].readiness_blockers = [] }],
    ['invalid deadline', (p: any) => { p.rows[0].deadlines = [{ kind: 'ship', source_at: null, computed_at: '2026-09-09T00:00:00Z', rule_id: null, rule_version: null, timezone: 'UTC', evidence_source: 'synthetic', observed_at: '2026-09-09T00:00:00Z' }] }],
    ['partial coverage bounds', (p: any) => { p.account_coverage[0].requested_from = '2026-09-09T00:00:00Z' }],
    ['missing coverage with snapshot', (p: any) => { p.account_coverage[0].state = 'missing' }],
  ])('rejects whole page: %s', (_name, mutate) => {
    const input = page(); (mutate as (p: unknown) => void)(input)
    try { parseCanonicalOrdersPage(input, scope); expect.fail('accepted invalid payload') }
    catch (error) { expect((error as Error).message).toBe('CANONICAL_ORDERS_RESPONSE_INVALID'); expect(JSON.stringify(error)).not.toContain('SENSITIVE_SENTINEL') }
  })

  it('rejects a different explicit snapshot', () => {
    expect(() => parseCanonicalOrdersPage(page(), { ...scope, snapshotId: '1' })).toThrow('CANONICAL_ORDERS_RESPONSE_INVALID')
  })

  it('preserves a leading BOM that Python exact-text validation does not strip', () => {
    const input = page()
    input.rows[0].observation.identity.external_order_id = '\uFEFF00042'
    expect(parseCanonicalOrdersPage(input, scope).rows[0].observation.identity.external_order_id).toBe('\uFEFF00042')
  })

  it('rejects Python whitespace separators that JavaScript trim misses', () => {
    const input = page()
    input.rows[0].observation.identity.external_order_id = '\u001c00042'
    expect(() => parseCanonicalOrdersPage(input, scope)).toThrow('CANONICAL_ORDERS_RESPONSE_INVALID')
  })

  it.each(['complete', 'missing'])('preserves valid %s empty evidence without inventing rows', state => {
    const input: any = page()
    input.rows = []
    input.coverage_state = state
    input.account_coverage[0].state = state
    if (state === 'missing') input.account_coverage[0].source_snapshot = null
    const parsed = parseCanonicalOrdersPage(input, scope)
    expect(parsed.coverage_state).toBe(state)
    expect(parsed.rows).toEqual([])
    expect(parsed.next_cursor).toBeNull()
  })

  it('rejects mixed observations of one order rather than choosing a winner', () => {
    const input: any = structuredClone(backendWire)
    const second = structuredClone(input.rows[0])
    second.item_identity = second.observation.items[1].identity
    second.observation.source_revision = 'different-revision'
    input.rows.push(second)
    expect(() => parseCanonicalOrdersPage(input, { organizationId: 101, accountIds: [1001] })).toThrow('CANONICAL_ORDERS_RESPONSE_INVALID')
  })

  it('emits repeated account selectors and exact decimal snapshot without Number conversion', () => {
    expect(buildCanonicalOrdersReadPath({ organizationId: 7, accountIds: [12, 11], queryChecksum: checksum, snapshotId: '9007199254740993' }))
      .toBe(`/api/v2/orders?account_id=11&account_id=12&query_checksum=${checksum}&limit=100&snapshot_id=9007199254740993`)
  })

  it('encodes opaque cursor only as a query value', () => {
    expect(buildCanonicalOrdersReadPath({ ...scope, queryChecksum: checksum, cursor: 'opaque+/=', limit: 1 }))
      .toBe(`/api/v2/orders?account_id=11&query_checksum=${checksum}&limit=1&cursor=opaque%2B%2F%3D`)
  })

  it.each([
    { accountIds: [] }, { accountIds: [11, 11] }, { accountIds: [2147483648] },
    { organizationId: 0 }, { queryChecksum: 'not-a-checksum' }, { limit: 0 }, { limit: 201 },
    { snapshotId: '01' }, { snapshotId: '1', cursor: 'opaque' }, { cursor: '' },
  ])('rejects invalid local request %j', bad => {
    expect(() => buildCanonicalOrdersReadPath({ ...scope, queryChecksum: checksum, ...bad })).toThrow('CANONICAL_ORDERS_REQUEST_INVALID')
  })
})
