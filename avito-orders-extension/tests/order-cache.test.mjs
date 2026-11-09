import test from 'node:test'
import assert from 'node:assert/strict'
import '../src/order-cache.js'

const row = () => ({ orderId: 'order-1', accountId: 'a', shipmentNumber: '00123', items: [{ itemId: '123', lineIndex: 0,
  size: 'M', color: 'black', imageUrl: 'https://b00.img.avito.st/a.jpg', sources: { size: 'order_detail', color: 'order_detail' } }] })

test('repeated collection reuses fields and skips completed details', () => {
  const current = row(); delete current.shipmentNumber; current.items[0].size = null
  assert.equal(SatornaOrderCache.complete(current), false)
  SatornaOrderCache.reuse(current, [row()])
  assert.equal(SatornaOrderCache.complete(current), true)
})
test('account/product collisions and listing guesses never count as complete', () => {
  const current = row(); current.accountId = 'b'; current.items[0].size = null
  SatornaOrderCache.reuse(current, [row()])
  assert.equal(current.items[0].size, null)
  const guessed = row(); guessed.items[0].sources.size = 'description'
  assert.equal(SatornaOrderCache.complete(guessed), false)
})
