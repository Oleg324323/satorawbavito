import { expect, it } from 'vitest'
import { compareWbValues, firstWbNumber, sortWbRows } from './tableSorting'
import { mapBackendAbcRowToParity, selectAdvertisingRows, sortAbcRows } from '../vella-parity/VellaHtmlParityPage'

it.each([['650 ₽ / 50%', 650], ['700 ₽ / 5%', 700], ['12 шт. / 24 000 ₽', 12],
  ['1 500 ₽ / 10%', 1500], ['−12 345,67 ₽ · +10%', -12345.67], ['0 ₽ / 99%', 0],
  ['— / 500 ₽', null], [null, null], ['SKU-99', null]])('reads only the first number in %s', (value, expected) => {
  expect(firstWbNumber(value)).toBe(expected)
})

it('compares negatives, decimals, dates and locale-aware text, with missing last in both directions', () => {
  for (const direction of ['asc', 'desc'] as const) {
    expect(compareWbValues(null, 0, direction)).toBeGreaterThan(0)
    expect(compareWbValues(NaN, -10, direction)).toBeGreaterThan(0)
    expect(compareWbValues('—', 0, direction)).toBeGreaterThan(0)
  }
  expect(compareWbValues(-1.2, 0, 'asc')).toBeLessThan(0)
  expect(compareWbValues('Товар 2', 'Товар 10', 'asc')).toBeLessThan(0)
  expect(compareWbValues(Date.parse('2026-02-01'), Date.parse('2026-01-31'), 'desc')).toBeLessThan(0)
})

it('extracts a sort key once per row and preserves source order for ties without mutating values', () => {
  const rows = [{ id: 1, kopecks: 70001 }, { id: 2, kopecks: 70000 }, { id: 3, kopecks: 70001 }]
  const before = structuredClone(rows)
  let calls = 0
  expect(sortWbRows(rows, { column: 0, direction: 'desc' }, row => { calls++; return row.kopecks }).map(row => row.id)).toEqual([1, 3, 2])
  expect(calls).toBe(rows.length)
  expect(rows).toEqual(before)
})

it('ABC sorts composites by original primary fields, never a secondary amount/percent or SKU fallback', () => {
  const rows = [
    mapBackendAbcRowToParity({ sku: 'A', marginKopecks: 65000, marginPct: 50, ordersComposite: { units: 12, kopecks: 2400000 }, salesComposite: { units: 5, kopecks: 9999999 }, adSpendKopecks: 65001, drrSalesPct: 99 }),
    mapBackendAbcRowToParity({ sku: 'B', marginKopecks: 70000, marginPct: 5, ordersComposite: { units: 13, kopecks: 100 }, salesComposite: { units: 6, kopecks: 1 }, adSpendKopecks: 65002, drrSalesPct: 1 }),
    mapBackendAbcRowToParity({ sku: 'C', marginKopecks: null, marginPct: 100 }),
  ]
  const before = structuredClone(rows)
  for (const key of ['margin', 'orders', 'sales', 'ads']) {
    expect(sortAbcRows(rows, [{ key, dir: 'desc' }]).map(row => row.sku)).toEqual(['B', 'A', 'C'])
    expect(sortAbcRows(rows, [{ key, dir: 'asc' }]).map(row => row.sku)).toEqual(['A', 'B', 'C'])
  }
  expect(sortAbcRows([rows[1], rows[0]], [{ key: 'not-a-column' }])).toEqual([rows[1], rows[0]])
  expect(rows).toEqual(before)
})

it('Ads uses order units, not revenue, and compares spend in original kopecks', () => {
  const rows = [
    { nmId: 1, orders: { units: 12, kopecks: 2400000 }, adSpendKopecks: 70001 },
    { nmId: 2, orders: { units: 13, kopecks: 100 }, adSpendKopecks: 70002 },
    { nmId: 3, orders: { units: null, kopecks: 9999999 }, adSpendKopecks: null },
  ]
  for (const column of [11, 12]) expect(selectAdvertisingRows(rows, { query: '', chip: 'все строки' }, { column, direction: -1 }).map(item => item.row.nmId)).toEqual([2, 1, 3])
})

it('Ads compares only the first campaign ID, retaining source order when it is equal', () => {
  const rows = [
    { nmId: 1, campaignIds: ['2', '999'] },
    { nmId: 2, campaignIds: ['2', '100'] },
    { nmId: 3, campaignId: '10' },
  ]
  expect(selectAdvertisingRows(rows, { query: '', chip: 'все строки' }, { column: 1, direction: 1 }).map(item => item.row.nmId)).toEqual([1, 2, 3])
})
