import { z } from 'zod'

// Dormant boundary only: no requests, rollout, status mapping or print capability.
const id = z.number().int().min(1).max(2147483647)
// Match Python str.strip boundaries without changing an external identity.
// JS trim additionally removes BOM and misses U+001C..001F/U+0085.
const edgeWhitespace = /^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]|[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]$/
const text = z.string().min(1).refine(value => !edgeWhitespace.test(value))
const version = z.string().regex(/^[1-9][0-9]*$/).refine(value => value.length < 19 || value.length === 19 && value <= '9223372036854775807')
const instant = z.string().datetime({ offset: true })
function instantBefore(left: string, right: string): boolean {
  const fraction = /\.(\d+)/
  const leftSecond = Date.parse(left.replace(fraction, ''))
  const rightSecond = Date.parse(right.replace(fraction, ''))
  if (leftSecond !== rightSecond) return leftSecond < rightSecond
  // Python datetime carries microseconds; Date.parse alone truncates them.
  const leftFraction = left.match(fraction)?.[1] ?? ''
  const rightFraction = right.match(fraction)?.[1] ?? ''
  const width = Math.max(leftFraction.length, rightFraction.length)
  return leftFraction.padEnd(width, '0') < rightFraction.padEnd(width, '0')
}
const marketplace = z.enum(['wb', 'avito'])
const state = z.enum(['complete', 'partial', 'missing'])
export const orderStatuses = ['pending_confirmation', 'accepted', 'ready_for_fulfillment', 'in_delivery', 'delivered', 'closed', 'cancelled', 'returning', 'returned', 'disputed'] as const
export const orderMappingStates = ['mapped', 'unmapped', 'ambiguous'] as const
export const orderResolutionStates = ['resolved', 'unmapped', 'ambiguous', 'stale', 'manual_override'] as const
const exactFilter = (max: number) => text.refine(value => value.length <= max && !value.includes('\0')
  && !Array.from(value).some(char => { const point = char.codePointAt(0)!; return point >= 0xd800 && point <= 0xdfff }))
const filtersSchema = z.object({ marketplace: marketplace.optional(), canonical_status: z.enum(orderStatuses).optional(),
  mapping_state: z.enum(orderMappingStates).optional(), resolution_state: z.enum(orderResolutionStates).optional(),
  external_order_id: exactFilter(4096).optional(), raw_status: exactFilter(2048).optional(),
}).strict()
export type CanonicalOrderFilters = z.infer<typeof filtersSchema>
const scopeSchema = z.object({
  organizationId: id, accountIds: z.array(id).min(1).refine(values => new Set(values).size === values.length),
  snapshotId: version.optional(),
}).strict()
const requestSchema = scopeSchema.extend({
  queryChecksum: z.string().regex(/^[a-f0-9]{64}$/), cursor: text.optional(),
  limit: z.number().int().min(1).max(200).default(100),
  filters: filtersSchema.optional(),
}).refine(value => value.snapshotId === undefined || value.cursor === undefined)

const identity = z.object({
  organization_id: id, marketplace_account_id: id, marketplace, external_order_id: text,
}).strict()
const itemIdentity = z.object({
  order_identity: identity, source_line_key: text, external_item_id: text.nullable(),
  occurrence_index: z.number().int().min(0).max(2147483647),
}).strict()
const status = z.object({
  raw_status: text.nullable(),
  canonical_status: z.enum(['pending_confirmation', 'accepted', 'ready_for_fulfillment', 'in_delivery', 'delivered', 'closed', 'cancelled', 'returning', 'returned', 'disputed']).nullable(),
  mapping_state: z.enum(['mapped', 'unmapped', 'ambiguous']), mapping_version: text, evidence_source: text,
}).strict().refine(value => (value.mapping_state === 'mapped') === (value.canonical_status !== null))
const observedItem = z.object({
  identity: itemIdentity, quantity: id, stable_order_line_id: text.nullable(), stable_unit_id: text.nullable(),
}).strict()
const observation = z.object({
  identity, source_kind: z.enum(['avito-order-management', 'avito-browser', 'wb-statistics-supplier-orders']),
  adapter_version: text, source_revision: text.nullable(), effective_at: instant.nullable(), observed_at: instant,
  status, items: z.array(observedItem), wb_is_cancelled: z.boolean().nullable(), wb_cancel_evidence_present: z.boolean().nullable(),
}).strict()
const resolution = z.object({
  state: z.enum(['resolved', 'unmapped', 'ambiguous', 'stale', 'manual_override']),
  marketplace_product_id: id.nullable(), marketplace_offer_id: id.nullable(), catalog_sku_id: id.nullable(), evidence_version: text,
}).strict().refine(value => {
  if (value.marketplace_offer_id !== null && value.marketplace_product_id === null) return false
  if (value.state === 'resolved') return value.marketplace_product_id !== null && value.catalog_sku_id !== null
  if (value.state === 'manual_override') return value.catalog_sku_id !== null
  return value.catalog_sku_id === null
})
const deadline = z.object({
  kind: text, source_at: instant.nullable(), computed_at: instant.nullable(), rule_id: text.nullable(), rule_version: text.nullable(),
  timezone: text, evidence_source: text, observed_at: instant,
}).strict().refine(value => (value.source_at !== null || value.computed_at !== null)
  && (value.computed_at !== null ? value.rule_id !== null && value.rule_version !== null : value.rule_id === null && value.rule_version === null))
const coverage = z.object({
  marketplace_account_id: id, source_kind: text, source_version: text, state,
  source_snapshot: text.nullable(), requested_from: instant.nullable(), requested_to: instant.nullable(),
}).strict().refine(value => (value.state === 'missing') === (value.source_snapshot === null)
  && ((value.requested_from === null && value.requested_to === null)
    || (value.requested_from !== null && value.requested_to !== null && instantBefore(value.requested_from, value.requested_to))))
const row = z.object({
  observation, item_identity: itemIdentity, row_version: version, resolution,
  readiness_blockers: z.array(text).refine(values => new Set(values).size === values.length), deadlines: z.array(deadline),
}).strict()
const pageSchema = z.object({
  organization_id: id, marketplace_account_ids: z.array(id).min(1), snapshot_id: version,
  high_water_mark: text, published_at: instant, coverage_state: state, rows: z.array(row).max(200),
  next_cursor: text.nullable(), account_coverage: z.array(coverage),
}).strict()

export type CanonicalOrdersPage = z.infer<typeof pageSchema>
export type CanonicalOrdersScope = z.infer<typeof scopeSchema>
export type CanonicalOrdersReadRequest = Omit<z.infer<typeof requestSchema>, 'limit'> & { limit?: number }

function invalidResponse(): never { throw new Error('CANONICAL_ORDERS_RESPONSE_INVALID') }
const same = (left: unknown, right: unknown) => JSON.stringify(left) === JSON.stringify(right)
const sameIds = (left: number[], right: number[]) => same([...left].sort((a, b) => a - b), [...right].sort((a, b) => a - b))

export function buildCanonicalOrdersReadPath(input: CanonicalOrdersReadRequest): string {
  const result = requestSchema.safeParse(input)
  if (!result.success) throw new Error('CANONICAL_ORDERS_REQUEST_INVALID')
  const request = result.data
  const query = new URLSearchParams()
  for (const account of [...request.accountIds].sort((a, b) => a - b)) query.append('account_id', String(account))
  query.set('query_checksum', request.queryChecksum)
  query.set('limit', String(request.limit))
  if (request.snapshotId !== undefined) query.set('snapshot_id', request.snapshotId)
  if (request.cursor !== undefined) query.set('cursor', request.cursor)
  for (const [key, value] of Object.entries(request.filters ?? {})) if (value !== undefined) query.set(key, value)
  return `/api/v2/orders?${query}`
}

const savedSnapshotSchema = pageSchema.omit({ rows: true, next_cursor: true }).extend({
  selection_kind: z.literal('saved_snapshot'), query_checksum: z.string().regex(/^[0-9a-f]{64}$/),
  row_count: z.string().regex(/^(0|[1-9][0-9]*)$/).max(128),
}).strict()
export type CanonicalOrdersSavedSnapshot = z.infer<typeof savedSnapshotSchema>
export function buildSavedOrdersSnapshotPath(scope: CanonicalOrdersScope) {
  const checked = scopeSchema.parse(scope)
  const query = new URLSearchParams()
  for (const account of [...checked.accountIds].sort((a, b) => a - b)) query.append('account_id', String(account))
  return `/api/v2/orders/snapshots/latest?${query}`
}
export function parseSavedOrdersSnapshot(value: unknown, scope: CanonicalOrdersScope) {
  const result = savedSnapshotSchema.parse(value)
  const { selection_kind: ignoredKind, query_checksum: ignoredQuery, row_count: ignoredCount, ...page } = result
  void ignoredKind; void ignoredQuery; void ignoredCount
  parseCanonicalOrdersPage({ ...page, rows: [], next_cursor: null }, scope)
  return result
}

export function parseCanonicalOrdersPage(payload: unknown, expectedScope: CanonicalOrdersScope): CanonicalOrdersPage {
  const decoded = pageSchema.safeParse(payload), expected = scopeSchema.safeParse(expectedScope)
  if (!decoded.success || !expected.success) return invalidResponse()
  const page = decoded.data, scope = expected.data
  if (page.organization_id !== scope.organizationId || !sameIds(page.marketplace_account_ids, scope.accountIds)
    || scope.snapshotId !== undefined && page.snapshot_id !== scope.snapshotId) return invalidResponse()
  const coverageByAccount = new Map(page.account_coverage.map(value => [value.marketplace_account_id, value]))
  if (coverageByAccount.size !== page.account_coverage.length || !sameIds([...coverageByAccount.keys()], scope.accountIds)) return invalidResponse()
  const states = new Set(page.account_coverage.map(value => value.state))
  const aggregate = states.size === 1 && states.has('complete') ? 'complete' : states.size === 1 && states.has('missing') ? 'missing' : 'partial'
  if (aggregate !== page.coverage_state || aggregate === 'missing' && (page.rows.length !== 0 || page.next_cursor !== null)) return invalidResponse()
  const rowKeys = new Set<string>(), orderObservations = new Map<string, string>()
  for (const row of page.rows) {
    const observed = row.observation, owner = observed.identity, source = coverageByAccount.get(owner.marketplace_account_id)
    if (owner.organization_id !== scope.organizationId || !source || source.state === 'missing'
      || source.source_kind !== observed.source_kind || source.source_version !== observed.adapter_version) return invalidResponse()
    if ((owner.marketplace === 'wb') !== (observed.source_kind === 'wb-statistics-supplier-orders')) return invalidResponse()
    if (owner.marketplace === 'avito' && (observed.wb_is_cancelled !== null || observed.wb_cancel_evidence_present !== null)) return invalidResponse()
    if (owner.marketplace === 'wb' && (observed.wb_is_cancelled === null || observed.wb_cancel_evidence_present === null)) return invalidResponse()
    const orderKey = JSON.stringify(owner), encodedObservation = JSON.stringify(observed)
    if (orderObservations.has(orderKey) && orderObservations.get(orderKey) !== encodedObservation) return invalidResponse()
    orderObservations.set(orderKey, encodedObservation)
    const lines = new Set<string>()
    for (const item of observed.items) {
      if (!same(item.identity.order_identity, owner) || lines.has(item.identity.source_line_key)) return invalidResponse()
      if (owner.marketplace === 'wb' ? item.stable_order_line_id !== null || item.stable_unit_id === null : item.stable_unit_id !== null) return invalidResponse()
      lines.add(item.identity.source_line_key)
    }
    if (!observed.items.some(item => same(item.identity, row.item_identity))) return invalidResponse()
    const key = JSON.stringify([owner, row.item_identity.source_line_key])
    if (rowKeys.has(key)) return invalidResponse()
    rowKeys.add(key)
    const blockedStatus = observed.status.canonical_status === null || ['cancelled', 'returning', 'returned', 'disputed'].includes(observed.status.canonical_status)
    if ((owner.marketplace === 'wb' || blockedStatus || !['resolved', 'manual_override'].includes(row.resolution.state)) && !row.readiness_blockers.length) return invalidResponse()
  }
  return page
}
