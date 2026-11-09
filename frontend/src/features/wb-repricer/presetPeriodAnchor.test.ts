import { describe, expect, it } from 'vitest'

import { isSelectableWbDay, lastClosedWbDay, resolvePresetPeriodRange } from './presetPeriodAnchor'

describe('WB report period anchoring', () => {
  it.each(['2026-10-07', '2026-10-08', '2026-10-09'])('allows uncached past day %s on October 10 in Moscow', iso => {
    expect(isSelectableWbDay(iso, '2026-07-12', new Date('2026-10-09T21:01:00Z'))).toBe(true)
  })
  it.each(['2026-10-11', '2026-11-01', '2026-02-30', 'invalid'])('rejects future/invalid day %s', iso => {
    expect(isSelectableWbDay(iso, '2026-07-12', new Date('2026-10-10T09:00:00Z'))).toBe(false)
  })
  it('uses the previous Moscow day after local midnight', () => {
    expect(lastClosedWbDay(new Date('2026-08-23T21:30:00Z'))).toBe('2026-08-23')
  })

  it.each([
    [1, { dateFrom: '2026-08-24', dateTo: '2026-08-24' }],
    [7, { dateFrom: '2026-08-18', dateTo: '2026-08-24' }],
    [14, { dateFrom: '2026-08-11', dateTo: '2026-08-24' }],
    [30, { dateFrom: '2026-07-26', dateTo: '2026-08-24' }],
  ])('keeps an inclusive %i-day range', (days, expected) => {
    expect(resolvePresetPeriodRange(days, new Date('2026-08-25T09:00:00Z'))).toEqual(expected)
  })

  it('crosses year boundaries without changing the day count', () => {
    expect(resolvePresetPeriodRange(7, new Date('2026-01-03T05:00:00Z')))
      .toEqual({ dateFrom: '2025-12-27', dateTo: '2026-01-02' })
  })
})
