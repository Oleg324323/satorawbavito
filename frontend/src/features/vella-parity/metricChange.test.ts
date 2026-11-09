import { expect, test } from 'vitest'
import { completedThreeDayWindows, metricChange } from './metricChange'

test('absolute and percentage change belong to an observation', () => {
  expect(metricChange(20, 60)).toEqual({ text: '↓ 40 (−66,7%)', tone: 'down' })
  expect(metricChange(60, 20).text).toBe('↑ 40 (+200%)')
  expect(metricChange(0, 0).text).toBe('0 (0%)')
  expect(metricChange(10, 0).text).toBe('↑ 10 (нет базы)')
  expect(metricChange(null, 60).tone).toBe('neutral')
  expect(metricChange(NaN, 60).text).toBe('—')
  expect(metricChange(10, 12, ' п.п.').text).toBe('↓ 2 п.п. (−16,7%)')
  expect(metricChange(.03, .01, ' п.п.', 2).text).toBe('↑ 0,02 п.п. (+200%)')
})
test('completed windows exclude today and respect historical end date', () => {
  expect(completedThreeDayWindows('2026-10-06', '2026-10-06')).toEqual({ current: { dateFrom: '2026-10-03', dateTo: '2026-10-05' }, previous: { dateFrom: '2026-09-30', dateTo: '2026-10-02' } })
  expect(completedThreeDayWindows('2026-09-30', '2026-10-06').current).toEqual({ dateFrom: '2026-09-28', dateTo: '2026-09-30' })
})
