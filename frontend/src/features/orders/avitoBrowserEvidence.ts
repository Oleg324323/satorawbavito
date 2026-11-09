/** New producer boundary only; never converts legacy browser snapshots or sends. */
import { z } from 'zod'

const exact = z.string().min(1).refine(value => {
  // Match Python str.strip, including C1 NEXT LINE; reject, never normalize.
  const stripped = value.replace(/^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/gu, '')
  if (stripped !== value) return false
  return [...value].every(char => {
    const code = char.codePointAt(0)!
    return code >= 32 && code !== 127 && !(code >= 0xd800 && code <= 0xdfff)
  })
})
const uuid = exact.refine(value => /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value)
  && value !== '00000000-0000-0000-0000-000000000000')
const captured = exact.refine(value => {
  const found = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?(Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$/.exec(value)
  if (!found || value.endsWith('-00:00')) return false
  const [year, month, day, hour, minute, second] = found.slice(1, 7).map(Number)
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0)
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
  return year >= 1 && month >= 1 && month <= 12 && day >= 1 && day <= days[month - 1]
    && hour < 24 && minute < 60 && second < 60
})
const integer = z.number().int().min(0).max(2147483647)
const item = z.object({ external_item_id: exact.nullable(), stable_order_line_id: exact.nullable(),
  occurrence_index: integer, quantity: integer.refine(value => value > 0) }).strict()
  .refine(value => value.external_item_id !== null || value.stable_order_line_id !== null)
const order = z.object({ external_order_id: exact, raw_status: exact, items: z.array(item).min(1) }).strict()
  .refine(value => {
    const identities = value.items.map(row => JSON.stringify(row.stable_order_line_id !== null
      ? ['line', row.stable_order_line_id] : ['listing', row.external_item_id, row.occurrence_index]))
    return new Set(identities).size === identities.length
  })
const envelope = z.object({ schema_version: z.literal('avito-browser-evidence-v1'),
  idempotency_key: uuid, captured_at: captured, orders: z.array(order).min(1) }).strict()
  .refine(value => new Set(value.orders.map(row => row.external_order_id)).size === value.orders.length)

export type AvitoBrowserEvidence = z.infer<typeof envelope>
export const avitoBrowserEvidenceByteLimit = 1_048_576

/** Caller must supply observed stable occurrence evidence; array indices aren't IDs.
 * Retain returned bytes and original UUID on retry. No timestamp/ID is generated.
 * This neither authorizes capture/upload nor proves a complete account or source chronology.
 */
export function encodeAvitoBrowserEvidence(value: unknown): Uint8Array {
  const parsed = envelope.safeParse(value)
  if (!parsed.success) throw new Error('AVITO_BROWSER_EVIDENCE_INVALID')
  // UTF-8 wire need not use the backend's ASCII canonical checksum representation:
  // backend decodes JSON, then canonicalizes it independently. Do not hash raw bytes.
  const bytes = new TextEncoder().encode(JSON.stringify(parsed.data))
  if (bytes.byteLength > avitoBrowserEvidenceByteLimit) throw new Error('AVITO_BROWSER_EVIDENCE_INVALID')
  return bytes
}
