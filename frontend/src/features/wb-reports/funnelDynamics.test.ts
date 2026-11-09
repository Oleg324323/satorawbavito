import { expect, it } from 'vitest'
import { formatAggregateFunnelDynamic, formatNativeFunnelDynamic, previousFunnelPeriod } from './funnelDynamics'

it.each([
  ['2026-10-10', '2026-10-15', '2026-10-04', '2026-10-09'],
  ['2026-10-01', '2026-10-01', '2026-09-30', '2026-09-30'],
  ['2026-01-01', '2026-01-07', '2025-12-25', '2025-12-31'],
  ['2024-03-01', '2024-03-02', '2024-02-28', '2024-02-29'],
  ['2026-03-29', '2026-03-30', '2026-03-27', '2026-03-28'],
])('compares an inclusive calendar interval %s–%s', (from, to, before, end) => {
  expect(previousFunnelPeriod(from, to)).toEqual({ dateFrom: before, dateTo: end })
})

it('does not invent native dynamics for missing, incomplete or invalid evidence', () => {
  for (const value of [null, undefined, NaN, Infinity, false, '-7']) {
    expect(formatNativeFunnelDynamic(value)).toBeNull()
  }
  expect(formatNativeFunnelDynamic(10, false)).toBeNull()
})

it('passes through WB zero, sign and above-100% changes without applying zero-baseline rules', () => {
  expect(formatNativeFunnelDynamic(-100)).toEqual({ direction: 'down', text: '↓ −100%' })
  expect(formatNativeFunnelDynamic(0)).toEqual({ direction: 'flat', text: '0%' })
  expect(formatNativeFunnelDynamic(-0)).toEqual({ direction: 'flat', text: '0%' })
  expect(formatNativeFunnelDynamic(900)?.text).toBe('↑ +900%')
  expect(formatNativeFunnelDynamic(-150)?.text).toBe('↓ −150%')
})

it('preserves provider rounding and supplied fractional precision', () => {
  expect(formatNativeFunnelDynamic(-7)?.text).toBe('↓ −7%')
  expect(formatNativeFunnelDynamic(-3)?.text).toBe('↓ −3%')
  expect(formatNativeFunnelDynamic(7.12345)?.text).toBe('↑ +7,12345%')
  expect(formatNativeFunnelDynamic(0.000001)?.text).toBe('↑ +0,000001%')
})

it('rounds only calculated aggregate percentages for presentation', () => {
  expect(formatAggregateFunnelDynamic(100 / 3)?.text).toBe('↑ +33,33%')
  expect(formatAggregateFunnelDynamic(-100 / 3)?.text).toBe('↓ −33,33%')
  expect(formatAggregateFunnelDynamic(-0)?.text).toBe('0%')
  expect(formatAggregateFunnelDynamic(-0.000001)?.text).toBe('0%')
  for (const value of [null, undefined, NaN, Infinity, false, '0']) expect(formatAggregateFunnelDynamic(value)).toBeNull()
  expect(formatAggregateFunnelDynamic(100, false)).toBeNull()
})
