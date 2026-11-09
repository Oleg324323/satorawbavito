import { describe, expect, it } from 'vitest'
import { parseWbAccounts, parseWbConnection, parseWbCredential, parseWbProducts, parseWbSync } from './validation'
import { createWbWriteEpoch } from './writeEpoch'

const account = { marketplaceAccountId: 17, provider: 'wb', externalAccountId: 'seller', displayName: 'WB', status: 'active' }
const credential = { marketplaceAccountId: 17, status: 'active', updatedAt: null }
const source = { source: 'prices', state: 'queued', processed: 0, updatedAt: null, errorCode: null }
const sync = { marketplaceAccountId: 17, jobId: null, state: 'idle', sources: [source], updatedAt: null }
const product = {
  nmId: '100', vendorCode: null, title: null, brand: null, subjectId: null, subjectName: null,
  photoUrl: null, contentUpdatedAt: null, pricesUpdatedAt: null, sizesTruncated: false, truncatedFields: ['title'],
  sizes: [{ chrtId: '101', techSize: null, skus: null, skusTruncated: false, priceKopecks: '900719925474099399', discountedPriceKopecks: null, truncatedFields: ['techSize'] }],
}
const products = { marketplaceAccountId: 17, items: [product], nextCursor: null, readVersion: 'version', readiness: 'partial', sources: [source] }

describe('WB response validation before publication', () => {
  it.each(['2026-09-10T17:58:01.489071+03:00', '2026-09-10T09:58:01.489071-05:00', '2026-09-10T14:58:01.489071Z'])('accepts timezone-aware API timestamps without changing their instant: %s', (updatedAt) => {
    const parsed = parseWbProducts({ ...products,
      items: [{ ...product, pricesUpdatedAt: updatedAt, contentUpdatedAt: updatedAt }],
      sources: [{ ...source, updatedAt }],
    }, 17)
    expect(parsed.items[0].pricesUpdatedAt).toBe(updatedAt)
    expect(Date.parse(parsed.items[0].contentUpdatedAt!)).toBe(Date.parse('2026-09-10T14:58:01.489Z'))
    expect(parsed.sources[0].updatedAt).toBe(updatedAt)
  })

  it.each(['2026-09-10T17:58:01', '2026-09-10', '2026-09-10T17:58:01+25:00'])('rejects ambiguous or invalid timestamps: %s', (updatedAt) => {
    expect(() => parseWbSync({ ...sync, updatedAt }, 17)).toThrow('Некорректный ответ сервиса WB')
  })

  it('accepts bounded nullable responses and preserves truncation flags and lossless prices', () => {
    expect(parseWbAccounts([account])).toEqual([account])
    expect(parseWbAccounts([{ ...account, displayName: null }])[0].displayName).toBeNull()
    expect(parseWbConnection({ account, credential })).toEqual({ account, credential })
    expect(parseWbSync(sync, 17)).toEqual(sync)
    expect(parseWbProducts(products, 17)).toEqual(products)
    expect(parseWbProducts({ ...products, items: [{ ...product, sizes: [{ ...product.sizes[0], skus: [], skusTruncated: true }] }] }, 17).items[0].sizes[0].skus).toEqual([])
  })

  it.each([
    () => parseWbAccounts(null),
    () => parseWbAccounts([account, account]),
    () => parseWbAccounts([{ ...account, marketplaceAccountId: '17' }]),
    () => parseWbConnection({ account, credential: { ...credential, marketplaceAccountId: 18 } }),
    () => parseWbCredential(credential, 18),
    () => parseWbSync(sync, 18),
    () => parseWbSync({ ...sync, sources: null }, 17),
    () => parseWbSync({ ...sync, sources: [{ ...source, processed: -1 }] }, 17),
    () => parseWbSync({ ...sync, sources: [source, source] }, 17),
    () => parseWbSync({ ...sync, updatedAt: 'yesterday' }, 17),
    () => parseWbProducts(products, 18),
    () => parseWbProducts({ ...products, items: null }, 17),
    () => parseWbProducts({ ...products, items: Array(51).fill(product) }, 17),
    () => parseWbProducts({ ...products, items: [{ ...product, title: 'x'.repeat(4097) }] }, 17),
    () => parseWbProducts({ ...products, items: [{ ...product, sizes: Array(6).fill(product.sizes[0]) }] }, 17),
    () => parseWbProducts({ ...products, items: [{ ...product, sizes: [{ ...product.sizes[0], priceKopecks: 123 }] }] }, 17),
    () => parseWbProducts({ ...products, items: [{ ...product, sizes: [{ ...product.sizes[0], skusTruncated: true }] }] }, 17),
    () => parseWbProducts({ ...products, items: [{ ...product, sizes: [{ ...product.sizes[0], skus: ['unbounded-barcode'] }] }] }, 17),
  ])('rejects malformed, oversized or cross-account data with a safe error', (parse) => {
    expect(parse).toThrow('Некорректный ответ сервиса WB')
    try { parse() } catch (error) { expect(error).toMatchObject({ status: 502, code: 'INVALID_WB_RESPONSE' }) }
  })
})

describe('WB mutation publication epochs', () => {
  it('rejects the older A response after A → B → A without touching the new operation', () => {
    const epoch = createWbWriteEpoch()
    const firstA = epoch.capture()
    epoch.invalidate() // B
    epoch.invalidate() // A again
    const secondA = epoch.capture()
    let draft = 'new draft'; let busy = true; let published = 'new account state'
    if (firstA()) { draft = ''; busy = false; published = 'old response' }
    expect({ draft, busy, published }).toEqual({ draft: 'new draft', busy: true, published: 'new account state' })
    expect(secondA()).toBe(true)
  })

  it('invalidates callbacks on unmount and does not affect the next mount', () => {
    const oldMount = createWbWriteEpoch(); const oldWrite = oldMount.capture()
    oldMount.invalidate()
    const newMount = createWbWriteEpoch(); const newWrite = newMount.capture()
    expect(oldWrite()).toBe(false); expect(newWrite()).toBe(true)
  })

  it('does not allow a read begun before a mutation to overwrite its result', () => {
    const epoch = createWbWriteEpoch(); const initialRead = epoch.observe()
    const write = epoch.capture()
    expect(initialRead()).toBe(false); expect(write()).toBe(true)
    expect(epoch.observe()()).toBe(true)
  })
})
