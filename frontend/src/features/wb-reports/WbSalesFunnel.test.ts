import { expect, it } from 'vitest'
import { formatFunnelValue, funnelColumns } from './WbSalesFunnel'

it('keeps unknown, zero, rubles and percentages distinct', () => {
  expect(formatFunnelValue(null)).toBe('—')
  expect(formatFunnelValue(undefined, 'percent')).toBe('—')
  expect(formatFunnelValue(0, 'percent')).toBe('0%')
  expect(formatFunnelValue(2343415, 'amount').replaceAll('\u00a0', ' ')).toBe('23 434,15 ₽')
  expect(formatFunnelValue(28, 'percent')).toBe('28%')
  expect(funnelColumns.map(([key]) => key)).toEqual(['revenueShare', 'visits', 'carts', 'cartConversion', 'orders', 'orderConversion', 'orderAmount', 'buyouts', 'buyoutPercent', 'buyoutAmount'])
})
