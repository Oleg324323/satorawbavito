import { z } from 'zod'
import { orderStatuses } from '@/features/orders/canonicalOrders'

const id = z.number().int().positive().max(2147483647)
const dateOnly = z.string().regex(/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/).refine(value => {
  const parsed = new Date(`${value}T00:00:00Z`)
  return !value.startsWith('0000-') && Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value
})
const pageNumber = z.string().regex(/^[1-9][0-9]{0,18}$/).refine(value => !/[^0-9]/.test(value) && (value.length < 19 || value.length === 19 && value <= '9223372036854775807'))
const edgeWhitespace = /^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]|[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]$/
const exactText = (max: number) => z.string().min(1).max(max).refine(value => !edgeWhitespace.test(value)
  && !Array.from(value).some(character => { const point = character.codePointAt(0)!; return point < 32 || point === 127 || point >= 0xd800 && point <= 0xdfff }))
const timestamp = z.string().max(35).refine(value => {
  const match = /^([0-9]{4}-[0-9]{2}-[0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,9}))?(Z|[+-]([0-9]{2}):([0-9]{2}))$/.exec(value)
  return Boolean(match && match[0] === value && dateOnly.safeParse(match[1]).success && Number(match[2]) < 24 && Number(match[3]) < 60 && Number(match[4]) < 60
    && (match[6] === 'Z' || Number(match[7]) < 24 && Number(match[8]) < 60))
}).nullable()
export const avitoOrderPreviewScope = z.object({ organizationId: id, marketplaceAccountId: id, externalAccountId: z.string().regex(/^[1-9][0-9]{0,127}$/).refine(value => !/[^0-9]/.test(value)),
  provider: z.literal('avito'), sessionKey: z.string().min(1).max(1024) }).strict()
export const avitoOrderPreviewRequest = z.object({ dateFrom: dateOnly, page: pageNumber }).strict()
const row = z.object({ orderId: exactText(4096), rawStatus: exactText(2048), canonicalStatus: z.enum(orderStatuses).nullable(),
  mappingState: z.enum(['mapped', 'unmapped']), mappingVersion: z.literal('avito-order-status-v1'), createdAt: timestamp, updatedAt: timestamp,
  accountEvidence: z.enum(['provider_account_id', 'credential_scope']) }).strict().refine(value => (value.mappingState === 'mapped') === (value.canonicalStatus !== null))
const response = z.object({ data: z.object({ marketplaceAccountId: id, provider: z.literal('avito'), externalAccountId: z.string(), dateFrom: dateOnly,
  page: pageNumber, limit: z.literal(20), coverageState: z.literal('partial'), hasMore: z.boolean().nullable(), rows: z.array(row).max(20) }).strict() }).strict()
export type AvitoOrderPreviewScope = z.infer<typeof avitoOrderPreviewScope>
export type AvitoOrderPreviewRequest = z.infer<typeof avitoOrderPreviewRequest>
export type AvitoOrderStatusPreview = z.infer<typeof response>['data']

export function buildAvitoOrderPreviewPath(scope: AvitoOrderPreviewScope, request: AvitoOrderPreviewRequest) {
  const captured = avitoOrderPreviewScope.parse(scope), query = avitoOrderPreviewRequest.parse(request)
  return `/api/v2/avito/accounts/${captured.marketplaceAccountId}/orders/status-preview?${new URLSearchParams(query)}`
}
export function parseAvitoOrderStatusPreview(input: unknown, scope: AvitoOrderPreviewScope, request: AvitoOrderPreviewRequest): AvitoOrderStatusPreview {
  avitoOrderPreviewScope.parse(scope); avitoOrderPreviewRequest.parse(request)
  const value = response.parse(input).data
  if (value.marketplaceAccountId !== scope.marketplaceAccountId || value.externalAccountId !== scope.externalAccountId
    || value.dateFrom !== request.dateFrom || value.page !== request.page || new Set(value.rows.map(row => row.orderId)).size !== value.rows.length) throw new Error('AVITO_ORDER_PREVIEW_INVALID')
  // Do not map raw statuses locally, infer totals/quantity/readiness, fill unknown
  // timestamps, normalize exact source text, or turn hasMore:null into false.
  return value
}
