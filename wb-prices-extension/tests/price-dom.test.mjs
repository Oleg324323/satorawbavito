import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import test from 'node:test'
import { priceBlock, priceNode } from './price-dom-fixture.mjs'

const context = vm.createContext({ URL, Date, Object, Number })
vm.runInContext(readFileSync(new URL('../src/contract.js', import.meta.url), 'utf8'), context)
const C = context.WbPricesContract
const plain = value => JSON.parse(JSON.stringify(value))
const read = nodes => C.readPriceBlock(priceBlock(nodes), node => node.visible)

test('three prices are separated by semantic roles regardless of visual order', () => {
  const nodes = [priceNode('price-block__wallet-price', '1 397 ₽'),
    priceNode('price-block__final-price', '1\u00a0426 ₽'), priceNode('price-block__old-price', '4\u202f300 ₽', true, 'del')]
  const expected = { buyerPriceNoWalletKopecks: 142600, originalPriceKopecks: 430000, buyerPriceWithWalletKopecks: 139700 }
  assert.deepEqual(plain(read(nodes)), expected)
  assert.deepEqual(plain(read(nodes.reverse())), expected)
  assert.equal(C.amountKopecks('1\u202f426,50 ₽'), 142650)
  assert.equal(C.amountKopecks('1 397 ₽ 1 426 ₽'), null)
})

test('wallet, crossed-out, hidden and conflicting normal prices cannot become regular buyer prices', () => {
  assert.equal(read([priceNode('price-block__final-price price-block__wallet-price', '1 397 ₽'),
    priceNode('price-block__old-price', '4 300 ₽', true, 'del')]), null)
  assert.equal(read([priceNode('price-block__final-price', '1 426 ₽', false)]), null)
  assert.equal(read([priceNode('price-block__final-price', '1 426 ₽'), priceNode('price-block__price', '1 500 ₽')]), null)
  assert.equal(read([priceNode('price-block__final-price', '5 000 ₽'), priceNode('price-block__old-price', '4 300 ₽')]), null)
})

test('API base, buyer and wallet hint stay separate through snapshot serialization', () => {
  const observations = C.parsePublicDetail({ products: [{ id: 123, sizes: [{ optionId: 456,
    price: { basic: 430000, product: 142600, wallet: 200 } }] }] }, [123])
  const snapshot = C.snapshotItems(observations, [{ nmId: 123, sizeId: 456, sellerPriceKopecks: 206400 }], '2026-10-10T13:00:00Z')
  assert.deepEqual(plain(snapshot.items[0]), { nmId: 123, sizeId: 456, sellerPriceKopecks: 206400,
    originalPriceKopecks: 430000, buyerPriceNoWalletKopecks: 142600, buyerPriceWithWalletKopecks: null, observedAt: '2026-10-10T13:00:00Z' })
})
