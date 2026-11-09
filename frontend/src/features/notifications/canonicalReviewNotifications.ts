/** Exact-ID Review notification contract. Not a general inbox or UI cutover. */
import { z } from 'zod'

const id = z.number().int().positive().max(2147483647)
const uuid = z.string().regex(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/)
  .refine(value => value !== '00000000-0000-0000-0000-000000000000')
const version = z.string().regex(/^[1-9][0-9]*$/)
const instant = z.string().regex(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$/).refine(value => {
  const parsed = new Date(value)
  return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 23) === value.slice(0, 23)
})
const scopeSchema = z.object({ organizationId: id, marketplaceAccountId: id, marketplace: z.enum(['wb', 'avito']) }).strict()
const idsSchema = z.array(uuid).nonempty().refine(values => new Set(values).size === values.length)
const base = { ...scopeSchema.shape, recipientMembershipId: id, eventIds: idsSchema }
const copy = {
  approval_required: ['Ответ на отзыв требует подтверждения', 'Проверьте текущую версию черновика в разделе отзывов.', 'info'],
  send_blocked: ['Отправка ответа заблокирована', 'Проверьте актуальность источника, черновика и разрешений.', 'warning'],
  send_ambiguous: ['Результат отправки ответа требует проверки', 'Повторная отправка заблокирована до проверки результата.', 'warning'],
} as const
const eventSchema = z.object({ schemaVersion: z.literal('notification-event-v1'), eventId: uuid,
  organizationId: id, marketplaceAccountId: id, scope: z.literal('account'), producer: z.literal('reviews'),
  entityId: uuid, sourceVersion: version, kind: z.enum(['approval_required', 'send_blocked', 'send_ambiguous']),
  occurredAt: instant, dedupeKey: z.string().regex(/^[0-9a-f]{64}$/), title: z.string(), details: z.string(),
  severity: z.enum(['info', 'warning']),
}).strict().refine(value => value.title === copy[value.kind][0] && value.details === copy[value.kind][1]
  && value.severity === copy[value.kind][2])
const receiptSchema = z.object({ value: z.object({ schemaVersion: z.literal('notification-in-app-receipt-v1'),
  organizationId: id, marketplaceAccountId: id, eventId: uuid, recipientMembershipId: id,
  readAt: instant.nullable(), dismissedAt: instant.nullable(),
}).strict().refine(value => value.readAt !== null || value.dismissedAt !== null), version }).strict()
const visibleSchema = z.object({ schemaVersion: z.literal('review-notification-visible-v1'), ...base,
  items: z.array(z.object({ event: eventSchema, receipt: receiptSchema.nullable() }).strict()),
}).strict()
const receiptsSchema = z.object({ schemaVersion: z.literal('review-notification-receipts-v1'), ...base,
  action: z.enum(['read', 'dismiss']), items: z.array(receiptSchema),
}).strict()
const capabilitiesSchema = z.object({ schemaVersion: z.literal('review-notification-capabilities-v1'), ...base,
  canRead: z.literal(true), canMarkRead: z.literal(true), canDismiss: z.literal(true),
}).strict()

export type ReviewNotificationScope = z.infer<typeof scopeSchema>
export type ReviewNotificationAction = 'read' | 'dismiss'
export type ReviewNotificationVisible = z.infer<typeof visibleSchema>
export type ReviewNotificationReceipts = z.infer<typeof receiptsSchema>
export type ReviewNotificationCapabilities = z.infer<typeof capabilitiesSchema>
export type ReviewNotificationExpectation = ReviewNotificationScope & { recipientMembershipId: number; eventIds: string[] }

const listSchema = z.object({
  schemaVersion: z.literal('review-notification-list-v1'), ...scopeSchema.shape, recipientMembershipId: id,
  eventSetVersion: z.string().regex(/^(0|[1-9][0-9]*)$/),
  eventIds: z.array(uuid).max(100).refine(values => new Set(values).size === values.length),
  items: visibleSchema.shape.items.max(100), nextCursor: z.string().min(1).max(4096).nullable(),
  capabilities: z.object({ canRead: z.boolean(), canMarkRead: z.boolean(), canDismiss: z.boolean() }).strict(),
}).strict()
export type ReviewNotificationList = z.infer<typeof listSchema>

export function parseReviewNotificationList(input: unknown, expected: ReviewNotificationScope): ReviewNotificationList {
  const checked = scopeSchema.safeParse(expected), parsed = listSchema.safeParse(input)
  if (!checked.success || !parsed.success) return invalid()
  const value = parsed.data
  if (!owner(value, expected) || value.marketplace !== expected.marketplace
    || value.eventIds.length !== value.items.length || (!value.capabilities.canRead && value.items.length > 0)) return invalid()
  if (value.eventIds.length > 0) parseReviewNotificationVisible({
    schemaVersion: 'review-notification-visible-v1', ...expected,
    recipientMembershipId: value.recipientMembershipId, eventIds: value.eventIds, items: value.items,
  }, { ...expected, recipientMembershipId: value.recipientMembershipId, eventIds: value.eventIds })
  return value
}

export function reviewNotificationListPath(scope: ReviewNotificationScope, cursor: string | null = null) {
  const checked = scopeSchema.safeParse(scope)
  if (!checked.success || (cursor !== null && (typeof cursor !== 'string' || !cursor || cursor.length > 4096))) return invalid()
  const query = new URLSearchParams({ marketplace_account_id: String(scope.marketplaceAccountId), marketplace: scope.marketplace, limit: '50' })
  if (cursor !== null) query.set('cursor', cursor)
  return `/api/v2/reviews/notifications?${query}`
}

function invalid(): never { throw new Error('CANONICAL_REVIEW_NOTIFICATION_INVALID') }
function owner(value: { organizationId: number; marketplaceAccountId: number }, expected: ReviewNotificationScope) {
  return value.organizationId === expected.organizationId && value.marketplaceAccountId === expected.marketplaceAccountId
}
function envelope(value: z.infer<typeof capabilitiesSchema> | ReviewNotificationVisible | ReviewNotificationReceipts,
  expected: ReviewNotificationExpectation) {
  const checked = z.object(base).strict().safeParse(expected)
  if (!checked.success || !owner(value, expected) || value.marketplace !== expected.marketplace
    || value.recipientMembershipId !== expected.recipientMembershipId
    || value.eventIds.length !== expected.eventIds.length
    || value.eventIds.some((item, index) => item !== expected.eventIds[index])) return invalid()
}
function receipt(value: z.infer<typeof receiptSchema>, expected: ReviewNotificationExpectation, eventId: string) {
  if (!owner(value.value, expected) || value.value.recipientMembershipId !== expected.recipientMembershipId
    || value.value.eventId !== eventId) return invalid()
}

export function parseReviewNotificationVisible(input: unknown, expected: ReviewNotificationExpectation): ReviewNotificationVisible {
  const parsed = visibleSchema.safeParse(input)
  if (!parsed.success) return invalid()
  const value = parsed.data
  envelope(value, expected)
  if (value.items.length !== expected.eventIds.length) return invalid()
  value.items.forEach((item, index) => {
    if (!owner(item.event, expected) || item.event.eventId !== expected.eventIds[index]) return invalid()
    if (item.receipt) receipt(item.receipt, expected, item.event.eventId)
  })
  return value
}

export function parseReviewNotificationReceipts(input: unknown, expected: ReviewNotificationExpectation,
  action: ReviewNotificationAction): ReviewNotificationReceipts {
  const parsed = receiptsSchema.safeParse(input)
  if (!parsed.success) return invalid()
  const value = parsed.data
  envelope(value, expected)
  if (value.action !== action || value.items.length !== expected.eventIds.length) return invalid()
  value.items.forEach((item, index) => {
    receipt(item, expected, expected.eventIds[index])
    if ((action === 'read' ? item.value.readAt : item.value.dismissedAt) === null) return invalid()
  })
  return value
}

export function parseReviewNotificationCapabilities(input: unknown,
  expected: ReviewNotificationExpectation): ReviewNotificationCapabilities {
  const parsed = capabilitiesSchema.safeParse(input)
  if (!parsed.success) return invalid()
  envelope(parsed.data, expected)
  return parsed.data
}

export const reviewNotificationReceiptsPath = '/api/v2/reviews/notifications/receipts'
export function reviewNotificationPath(scope: ReviewNotificationScope, eventIds: string[], capabilities = false) {
  const checked = scopeSchema.safeParse(scope), ids = idsSchema.safeParse(eventIds)
  if (!checked.success || !ids.success || typeof capabilities !== 'boolean') return invalid()
  const query = new URLSearchParams({ marketplace_account_id: String(checked.data.marketplaceAccountId), marketplace: checked.data.marketplace })
  ids.data.forEach(value => query.append('event_id', value))
  return `/api/v2/reviews/notifications/${capabilities ? 'capabilities' : 'visible'}?${query}`
}

export function encodeReviewNotificationAction(scope: ReviewNotificationScope, eventIds: string[], action: ReviewNotificationAction) {
  const checked = scopeSchema.safeParse(scope), ids = idsSchema.safeParse(eventIds)
  if (!checked.success || !ids.success || (action !== 'read' && action !== 'dismiss')) return invalid()
  // No member, arbitrary timestamp, mark-all-future or account-derived permission.
  return JSON.stringify({ schemaVersion: 'review-notification-action-v1', ...checked.data, eventIds: ids.data, action })
}
