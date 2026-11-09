import { describe, expect, it } from 'vitest'
import { renderToStaticMarkup } from 'react-dom/server'
import { createElement } from 'react'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { transpileModule } from 'typescript'
import { AvitoStatsAnalytics } from './AvitoStatsAnalytics'
import { daysForPeriod, metricValue, quickPeriod, statsPeriodSelection, threeDayTrend, type StatsDay } from './avitoStatsMetrics'

const days: StatsDay[] = Array.from({ length: 6 }, (_, i) => ({
  date: `2026-09-${25 + i}`, views: i < 3 ? 100 : 200, contacts: i < 3 ? 10 : 30,
  impressions: i < 3 ? 400 : 200, favorites: 10, orders: 0, buyouts: 0, spendKopecks: i < 3 ? 10000 : 15000,
}))

describe('real daily Avito statistics', () => {
  it('updates applied and calendar dates together through the actual state dispatcher (no browser)', () => {
    const source = readFileSync(new URL('./VellaHtmlParityPage.tsx', import.meta.url), 'utf8')
    const dispatcher = source.slice(source.indexOf('function updateAvitoStatsState('), source.indexOf('function installAvitoStatsReactBridge('))
    const window: { __vellaAvitoStatsState?: ReturnType<typeof statsPeriodSelection>; dispatchEvent: () => void } = {
      dispatchEvent: () => { notifications += 1 },
    }
    let notifications = 0
    const context = { window, CustomEvent: class {}, defaultAvitoStatsPeriod: () => ({ from: '2026-09-02', to: '2026-10-01' }) }
    const update = runInNewContext(transpileModule(dispatcher, {}).outputText + '\nupdateAvitoStatsState', context)
    for (const preset of ['3m', '7', '30', '7'] as const) {
      const period = quickPeriod(preset, '2026-10-01')
      update(statsPeriodSelection(period, window.__vellaAvitoStatsState?.requestSeq))
      expect(window.__vellaAvitoStatsState).toMatchObject({ ...period, dateFromDraft: period.dateFrom, dateToDraft: period.dateTo })
    }
    expect(window.__vellaAvitoStatsState?.requestSeq).toBe(4)
    expect(notifications).toBe(4)
  })
  it('compares two complete adjacent three-day windows and excludes today', () => {
    const data = [...days, { date: '2026-10-01', views: 90000 }]
    expect(threeDayTrend(data, 'views', '2026-10-01', '2026-10-01')).toMatchObject({ direction: 'up', label: '+300 (+100%)' })
    expect(threeDayTrend(data, 'impressions', '2026-10-01', '2026-10-01')).toMatchObject({ direction: 'down', label: '-600 (-50%)' })
    expect(threeDayTrend(data, 'favorites', '2026-10-01', '2026-10-01')).toMatchObject({ direction: 'flat' })
  })
  it('uses weighted conversion and percentage points, not the average of daily percentages', () => {
    expect(threeDayTrend(days, 'conversionPct', '2026-09-30', '2026-10-01')).toMatchObject({ direction: 'up', label: '+5 п.п. (+50%)' })
  })
  it('does not turn missing or incomplete days into zero or fabricated growth', () => {
    expect(threeDayTrend(days.slice(1), 'views', '2026-09-30', '2026-10-01').direction).toBe('missing')
    expect(threeDayTrend([...days.slice(0, 5), { date: '2026-09-30', views: null }], 'views', '2026-09-30', '2026-10-01').direction).toBe('missing')
    expect(threeDayTrend([], 'views', '2026-09-30', '2026-10-01').direction).toBe('missing')
  })
  it('handles zero baselines, falling to zero and genuine zero changes', () => {
    expect(threeDayTrend(days, 'orders', '2026-09-30', '2026-10-01').label).toBe('Без изменений')
    const fromZero = days.map((day, i) => ({ ...day, views: i < 3 ? 0 : 10 }))
    expect(threeDayTrend(fromZero, 'views', '2026-09-30', '2026-10-01').label).toBe('+30 (с 0)')
    expect(threeDayTrend(fromZero.map(day => ({ ...day, views: 10 - day.views })), 'views', '2026-09-30', '2026-10-01').label).toBe('-30 (-100%)')
  })
  it('converts kopecks exactly once and uses the same growth direction for expenses', () => {
    expect(metricValue({ spendKopecks: 12345 }, 'spendKopecks')).toBe(123.45)
    expect(threeDayTrend(days, 'spendKopecks', '2026-09-30', '2026-10-01')).toMatchObject({ direction: 'up', label: '+150 ₽ (+50%)' })
  })
  it('presets are inclusive and calendar-month arithmetic handles month ends', () => {
    expect(quickPeriod('7', '2026-10-01')).toEqual({ dateFrom: '2026-09-25', dateTo: '2026-10-01' })
    expect(quickPeriod('30', '2026-10-01')).toEqual({ dateFrom: '2026-09-02', dateTo: '2026-10-01' })
    expect(quickPeriod('3m', '2026-05-31')).toEqual({ dateFrom: '2026-03-01', dateTo: '2026-05-31' })
  })
  it('gaps stay null and comparison history is not included in the visible chart', () => {
    const selected = daysForPeriod(days, { dateFrom: '2026-09-30', dateTo: '2026-10-01' })
    expect(selected.map(day => day.date)).toEqual(['2026-09-30', '2026-10-01'])
    expect(metricValue(selected[1], 'views')).toBeNull()
  })
  it('renders eight metric buttons and a named chart without search or info badges (no browser)', () => {
    const html = renderToStaticMarkup(createElement(AvitoStatsAnalytics, { timeline: days,
      summary: { views: 900, contacts: 120 }, period: { dateFrom: '2026-09-25', dateTo: '2026-09-30' } }))
    expect((html.match(/class="avito-metric-card"/g) ?? []).length).toBe(8)
    expect(html).toContain('Динамика по дням')
    expect(html).toContain('Воронка конверсии')
    expect(html).toContain('Значения по дням')
    expect(html).not.toContain('stat-tip')
    expect(html).not.toContain('avitoStatsSearch')
    expect(html).not.toContain('Неделю назад')
  })
})
