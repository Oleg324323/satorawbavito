import { z } from 'zod'

const maximum = 9223372036854775807n
export const productionId = z.string().regex(/^[1-9][0-9]{0,18}$/).refine(value => /^[1-9][0-9]{0,18}$/.test(value) && BigInt(value) <= maximum)
const positive = z.number().int().min(1).max(2147483647), quantity = z.number().int().min(0).max(2147483647)
// Match Python str.strip at the domain boundary; never normalize supplied text.
const edgeWhitespace = /^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]|[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]$/
const exactText = z.string().min(1).max(65536).refine(value => !edgeWhitespace.test(value) && !value.includes('\0')
  && !Array.from(value).some(character => { const point = character.codePointAt(0)!; return point >= 0xd800 && point <= 0xdfff }))
const utc = z.string().regex(/^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$/).refine(value => {
  const parsed = new Date(value)
  return !value.startsWith('0000-') && Number.isFinite(parsed.getTime()) && parsed.toISOString() === `${value.slice(0, 23)}Z`
})
export const productionScope = z.object({ organizationId: positive, marketplaceAccountId: positive, sessionKey: z.string().min(1).max(1024) }).strict()
export const productionCreateInput = z.object({ orderItemId: productionId, expectedSourceItemVersion: productionId }).strict()
export const productionAssignmentInput = z.object({ expectedVersion: productionId, catalogSkuId: positive, idempotencyKey: exactText, reason: exactText }).strict()
const quantities = { requiredQuantity: positive, plannedQuantity: quantity, remainingQuantity: quantity }
const coherent = (value: { requiredQuantity: number; plannedQuantity: number; remainingQuantity: number }) => value.remainingQuantity === value.requiredQuantity - value.plannedQuantity
export const productionItem = z.object({ workItemId: productionId, organizationId: positive, marketplaceAccountId: positive,
  orderId: productionId, orderItemId: productionId, sourceItemVersion: productionId, ...quantities, catalogSkuId: positive.nullable(), version: productionId,
  createdAt: utc, updatedAt: utc, currentAssignmentReceiptId: productionId.nullable() }).strict().refine(coherent).refine(value => value.updatedAt >= value.createdAt)
export const productionReadResponse = z.object({ schemaVersion: z.literal('production-work-item-v1'), item: productionItem }).strict()
export const productionCreateResponse = z.object({ schemaVersion: z.literal('production-create-v1'), workItemId: productionId, replayed: z.boolean() }).strict()
export const productionAssignmentResponse = z.object({ schemaVersion: z.literal('production-assignment-v1'), replayed: z.boolean(), result: z.object({
  workItemId: productionId, version: productionId, catalogSkuId: positive, ...quantities, sourceItemVersion: productionId,
}).strict().refine(coherent) }).strict()
export type ProductionScope = z.infer<typeof productionScope>
export type ProductionItem = z.infer<typeof productionItem>
export type ProductionCreateInput = z.infer<typeof productionCreateInput>
export type ProductionAssignmentInput = z.infer<typeof productionAssignmentInput>
