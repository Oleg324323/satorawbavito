import { expect, it } from 'vitest'
import { isWbTableRoute, stickyHeaderShift } from './wbStickyHeaders'

it('enables table pinning on WB only, not Avito or the shared profile', () => {
  for (const path of ['/wb', '/wb/repricer', '/wb/reports', '/wb/reports/ads', '/wb/reviews']) expect(isWbTableRoute(path)).toBe(true)
  for (const path of ['/avito', '/avito/reports', '/settings/profile', '/wb-other', '/internal/vella-parity']) expect(isWbTableRoute(path)).toBe(false)
})

it('follows the vertical scrollport without moving a header before it reaches the top', () => {
  expect(stickyHeaderShift(200, 60, 1000, 120, 700)).toBe(0)
  expect(stickyHeaderShift(20, 60, 1000, 120, 700)).toBe(100)
  expect(stickyHeaderShift(-80, 90, 1000, 140, 700)).toBe(220)
})

it('stops at the last row and does not float above a different table', () => {
  expect(stickyHeaderShift(-300, 60, 150, 120, 700)).toBe(390)
  expect(stickyHeaderShift(-300, 60, 110, 120, 700)).toBe(0)
  expect(stickyHeaderShift(750, 60, 1000, 120, 700)).toBe(0)
})
