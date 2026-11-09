import { describe, expect, it } from 'vitest'
import { encodeAvitoBrowserEvidence } from './avitoBrowserEvidence'

// Literal T3 4e010e216d1d9b875e563d6cccce96b20215a04b interoperability input.
const body = {
  schema_version: 'avito-browser-evidence-v1', idempotency_key: '11111111-1111-4111-8111-111111111111',
  captured_at: '2026-09-09T12:34:56.123456+03:00', orders: [{
    external_order_id: '000-synthetic-order', raw_status: 'synthetic-unknown-status', items: [
      { external_item_id: '000-synthetic-listing', stable_order_line_id: null, occurrence_index: 0, quantity: 3 },
      { external_item_id: '000-synthetic-listing', stable_order_line_id: null, occurrence_index: 1, quantity: 1 },
    ],
  }],
}
const decode = (value: unknown) => JSON.parse(new TextDecoder().decode(encodeAvitoBrowserEvidence(value)))

describe('new Avito browser evidence producer boundary', () => {
  it('preserves exact literal wire and unknown status without inventing authority', () => {
    expect(decode(body)).toEqual(body)
    expect(decode({ ...body, captured_at: '9999-12-31T23:59:59Z' }).captured_at).toBe('9999-12-31T23:59:59Z')
  })

  it.each(['organization_id', 'marketplace_account_id', 'complete', 'token', 'buyer_name'])('rejects extra %s', key => {
    expect(() => encodeAvitoBrowserEvidence({ ...body, [key]: 'synthetic' })).toThrow('AVITO_BROWSER_EVIDENCE_INVALID')
  })

  it.each(['0000-01-01T00:00:00Z', '2026-02-30T00:00:00Z', '2026-01-01T00:00:00-00:00',
    '2026-01-01', '2026-01-01T24:00:00Z', '2026-01-01T00:00:00.1234567Z'])('rejects invalid capture %s', captured_at => {
    expect(() => encodeAvitoBrowserEvidence({ ...body, captured_at })).toThrow()
  })

  it.each([null, ' leading', 'trailing\u0085', 'nul\0id', '\ud800'])('does not normalize bad order IDs', external_order_id => {
    const value = { ...body, orders: [{ ...body.orders[0], external_order_id }] }
    expect(() => encodeAvitoBrowserEvidence(value)).toThrow()
  })

  it('rejects missing occurrence instead of synthesizing array position', () => {
    const { occurrence_index: _unused, ...missing } = body.orders[0].items[0]
    expect(() => encodeAvitoBrowserEvidence({ ...body, orders: [{ ...body.orders[0], items: [missing] }] })).toThrow()
  })

  it('rejects duplicate order and item identities; stable line wins regardless of listing', () => {
    expect(() => encodeAvitoBrowserEvidence({ ...body, orders: [body.orders[0], body.orders[0]] })).toThrow()
    expect(() => encodeAvitoBrowserEvidence({ ...body, orders: [{ ...body.orders[0],
      items: [body.orders[0].items[0], body.orders[0].items[0]] }] })).toThrow()
    const items = body.orders[0].items.map((item, i) => ({ ...item, stable_order_line_id: 'same', external_item_id: String(i) }))
    expect(() => encodeAvitoBrowserEvidence({ ...body, orders: [{ ...body.orders[0], items }] })).toThrow()
  })

  it('retains valid Unicode and rejects oversize UTF-8 bytes', () => {
    const unicode = { ...body, orders: [{ ...body.orders[0], raw_status: 'Новый-😀-é' }] }
    expect(decode(unicode).orders[0].raw_status).toBe('Новый-😀-é')
    // Python str.strip does not remove BOM; JavaScript trim would change this contract.
    expect(decode({ ...body, orders: [{ ...body.orders[0], raw_status: '\ufeffunknown' }] })
      .orders[0].raw_status).toBe('\ufeffunknown')
    expect(() => encodeAvitoBrowserEvidence({ ...body, orders: [{ ...body.orders[0], raw_status: 'я'.repeat(524288) }] })).toThrow()
  })

  it.each([0, -1, 1.5, true, 2147483648])('rejects invalid quantity %s', quantity => {
    expect(() => encodeAvitoBrowserEvidence({ ...body, orders: [{ ...body.orders[0],
      items: [{ ...body.orders[0].items[0], quantity }] }] })).toThrow()
  })
})
