import assert from 'node:assert/strict'
import test from 'node:test'

delete globalThis.SatornaAvitoItemSize
await import(`../src/item-size.js?test=${Date.now()}`).catch(() => {})

const parse = (text) => globalThis.SatornaAvitoItemSize?.parseExplicitListingSize?.(text)

test('parses combined numeric and letter size from characteristics', () => {
  assert.equal(parse('Характеристики\nРазмер: 54 (XL)\nЦвет: Чёрный'), '54 (XL)')
})

test('normalizes explicit letter and range sizes', () => {
  assert.equal(parse('Size: xl'), 'XL')
  assert.equal(parse('Размер одежды: 46–48'), '46-48')
})

test('rejects unrelated numeric dimensions and prose', () => {
  assert.equal(parse('Фото 96 × 96\nЦена 2 139 ₽\nМодель размера oversize'), null)
})
