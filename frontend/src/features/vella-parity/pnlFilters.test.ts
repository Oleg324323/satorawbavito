import { expect, it } from 'vitest'
import { defaultPnlFilters, matchesPnlFilters, pnlActiveFilterCount, pnlFilterError, pnlRangeNumber, selectPnlRows, type PnlFilters, type PnlFilterProduct } from './pnlFilters'

const product: PnlFilterProduct = { managerId: 'me', brand: 'Brand', status: 'manual', promotion: true, garment: 'shirt',
  attrs: ['female', 'black'], strategyId: 'test', stockUnits: 0, currentPriceKopecks: 12345, pminKopecks: 0, changed24h: true, orders: 2, baskets: 0 }
const row = { nmId: 123, sku: 'TW55', marginPct: -12.34, revenueKopecks: 9876 }
const match = (patch: Partial<PnlFilters>, p = product) => matchesPnlFilters(row, p, { ...defaultPnlFilters(), ...patch }, 'me')

it.each([
  ['segment', 'all', true], ['segment', 'problem', true], ['segment', 'nopmin', true], ['segment', 'loss', true],
  ['segment', 'changes24h', true], ['segment', 'illiquid', false], ['manager', 'mine', true], ['manager', 'me', true],
  ['manager', 'unassigned', false], ['manager', 'other', false], ['status', 'manual', true], ['status', 'nopmin', true],
  ['status', 'new', false], ['status', 'loko', false], ['status', 'illiquid', false], ['status', 'liq', false],
  ['promo', 'yes', true], ['promo', 'no', false], ['brand', 'Brand', true], ['brand', 'Other', false],
  ['strategy', 'test', true], ['strategy', 'other', false], ['stock', 'ending', true], ['stock', 'low', false],
  ['stock', 'normal', false], ['stock', 'excess', false],
] as const)('filters %s=%s using the actual primary attribute', (key, value, expected) => {
  expect(match({ [key]: value })).toBe(expected)
})

it.each(['loko', 'new', 'illiquid', 'liq', 'manual'] as const)('matches state %s, independently of the other filters', status => {
  expect(match({ status }, { ...product, status })).toBe(true)
})
it.each([['ending', 5], ['low', 6], ['low', 20], ['normal', 21], ['normal', 100], ['excess', 101]] as const)('matches stock band %s=%s', (stock, units) => {
  expect(match({ stock }, { ...product, stockUnits: units })).toBe(true)
})
it('keeps unallocated rows unchanged by default, but never assigns them to product facets', () => {
  expect(matchesPnlFilters({}, undefined, defaultPnlFilters(), 'me')).toBe(true)
  expect(matchesPnlFilters({}, undefined, { ...defaultPnlFilters(), manager: 'unassigned' }, 'me')).toBe(false)
})
it('uses OR inside multiselects and AND across groups, without guessing unknown types', () => {
  expect(match({ types: ['shirt', 'hoodie'], attrs: ['black'], brand: 'Brand' })).toBe(true)
  expect(match({ types: ['hoodie'], attrs: ['black'] })).toBe(false)
  expect(match({ types: ['shirt'] }, {})).toBe(false)
})
it.each(['margin', 'orders', 'baskets', 'price'] as const)('checks inclusive, one-sided and zero boundaries for %s', key => {
  const value = { margin: '-12,34', orders: '2', baskets: '0', price: '123,45' }[key]
  for (const bounds of [{ from: value, to: value }, { from: value, to: '' }, { from: '', to: value }]) {
    expect(match({ ranges: { ...defaultPnlFilters().ranges, [key]: bounds } })).toBe(true)
  }
  expect(match({ ranges: { ...defaultPnlFilters().ranges, [key]: { from: value, to: value } } }, {})).toBe(key === 'margin')
})
it('unknown promo and stock never match false/zero, while confirmed zero does', () => {
  expect(match({ promo: 'no' }, { promotion: null })).toBe(false)
  expect(match({ promo: 'no' }, { promotion: false })).toBe(true)
  expect(match({ stock: 'ending' }, { stockUnits: null })).toBe(false)
  expect(match({ stock: 'ending' }, { stockUnits: 0 })).toBe(true)
})
it.each([['abc', '', 'корректное число'], ['5', '4', 'больше'], ['-1', '', 'отрицательным']])('validates ranges %s..%s', (from, to, message) => {
  expect(pnlFilterError({ ...defaultPnlFilters(), ranges: { ...defaultPnlFilters().ranges, price: { from, to } } })).toContain(message)
})
it('parses decimal comma, distinguishes empty and zero, and counts applied groups', () => {
  expect([pnlRangeNumber(''), pnlRangeNumber('0'), pnlRangeNumber('-1,25')]).toEqual([null, 0, -1.25])
  expect(pnlActiveFilterCount(defaultPnlFilters())).toBe(0)
  expect(pnlActiveFilterCount({ ...defaultPnlFilters(), manager: 'mine', stock: 'ending' })).toBe(2)
})
it('filters all rows, preserves row objects and financial amounts, and combines search', () => {
  const rows = Array.from({ length: 160 }, (_, index) => ({ ...row, nmId: index + 1, sku: `SKU-${index}` }))
  const metadata = { marketplaceAccountId: 12, dateFrom: '2026-10-05', dateTo: '2026-10-11', products: Object.fromEntries(rows.map(item => [item.nmId, product])) }
  const found = selectPnlRows(rows, 'SKU-159', { ...defaultPnlFilters(), manager: 'mine' }, metadata, 'me')
  expect(found).toEqual([rows[159]])
  expect(found[0]).toBe(rows[159])
  expect(selectPnlRows(rows, '', defaultPnlFilters(), null, '')).toEqual(rows)
})
