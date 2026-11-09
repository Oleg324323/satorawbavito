import { describe, it, expect } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { FunnelMetrics, funnelTrend, funnelValue, type FunnelDay } from './FunnelMetrics'
const days: FunnelDay[] = Array.from({ length: 6 }, (_, i) => ({ date: `2026-10-0${i + 1}`, ordersUnits: i < 3 ? 20 : 10 }))
describe('WB daily funnel', () => {
  it('compares exact completed 3-day windows and counts', () => {
    expect(funnelTrend(days, 'ordersUnits', '2026-10-06', '2026-10-07').text).toBe('↓ 30 (−50%)')
    expect(funnelTrend(days, 'ordersUnits', '2026-10-06', '2026-10-06').text).toBe('Нет полных 6 дней')
  })
  it('does not invent a percent or convert absent data to zero', () => {
    expect(funnelTrend(days.map((d, i) => ({ ...d, ordersUnits: i < 3 ? 0 : 10 })), 'ordersUnits', '2026-10-06', '2026-10-07').text).toBe('↑ 30 (база 0)')
    expect(funnelTrend(days.slice(1), 'ordersUnits', '2026-10-06', '2026-10-07').text).toBe('Нет полных 6 дней')
    expect(funnelValue({ ordersUnits: 10, coverageComplete: false }, 'ordersUnits')).toBeNull()
    expect(funnelValue({ ordersUnits: 0, value: 999 }, 'ordersUnits')).toBe(0)
    expect(funnelValue({}, 'openCount')).toBeNull()
  })
  it('renders metric selection, units, labeled values and a missing state', () => {
    const html = renderToStaticMarkup(<FunnelMetrics days={days} dateTo="2026-10-06" loading={false} />)
    expect(html).toContain('aria-pressed="true"')
    expect(html).toContain('Количество, шт.')
    expect(html).toContain('Заказали — 10 шт.')
    expect(html).toContain('Переходы в карточку')
  })
  it('weights conversion by window totals, including complete zero-cart days', () => {
    const rateDays = days.map((day, i) => ({ ...day, cartCount: i % 3 ? 100 : 0, ordersUnits: i % 3 ? (i < 3 ? 10 : 20) : 0 }))
    expect(funnelTrend(rateDays, 'cartCrPct', '2026-10-06', '2026-10-07').text).toBe('↑ 10 пп (+100%)')
    expect(funnelTrend(rateDays.map((day, i) => i === 0 ? { ...day, coverageComplete: false } : day), 'cartCrPct', '2026-10-06', '2026-10-07').text).toBe('Нет полных 6 дней')
  })
})
