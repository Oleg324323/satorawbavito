export const metricKeys = ['impressions', 'views', 'contacts', 'favorites', 'orders', 'buyouts', 'spendKopecks', 'conversionPct'] as const
export type MetricKey = typeof metricKeys[number]
export type StatsDay = { date: string } & Partial<Record<MetricKey, number | null>>
export type StatsSummary = Partial<Record<MetricKey, number | null>>
export type StatsPeriod = { dateFrom: string; dateTo: string }
export function statsPeriodSelection(period: StatsPeriod, requestSeq = 0) {
  return { ...period, dateFromDraft: period.dateFrom, dateToDraft: period.dateTo,
    forceRefresh: false, requestSeq: requestSeq + 1 }
}
export const metricNames: Record<MetricKey, string> = {
  impressions: 'Показы', views: 'Просмотры', contacts: 'Контакты', favorites: 'Избранное',
  orders: 'Заказы', buyouts: 'Выкупы', spendKopecks: 'Расходы', conversionPct: 'Конверсия',
}
export function moscowToday(now = new Date()) {
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'Europe/Moscow', year: 'numeric', month: '2-digit', day: '2-digit' }).format(now)
}
export function shiftDate(day: string, offset: number) {
  const date = new Date(`${day}T12:00:00Z`)
  date.setUTCDate(date.getUTCDate() + offset)
  return date.toISOString().slice(0, 10)
}
export function quickPeriod(preset: '7' | '30' | '3m', today = moscowToday()): StatsPeriod {
  if (preset !== '3m') return { dateFrom: shiftDate(today, 1 - Number(preset)), dateTo: today }
  const date = new Date(`${today}T12:00:00Z`)
  const day = date.getUTCDate()
  date.setUTCDate(1)
  date.setUTCMonth(date.getUTCMonth() - 3)
  const lastDay = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 1, 0)).getUTCDate()
  date.setUTCDate(Math.min(day, lastDay))
  return { dateFrom: shiftDate(date.toISOString().slice(0, 10), 1), dateTo: today }
}
export function metricValue(row: StatsSummary, key: MetricKey): number | null {
  if (key === 'conversionPct') {
    if (row.conversionPct != null) return row.conversionPct
    return row.views != null && row.views > 0 && row.contacts != null ? row.contacts / row.views * 100 : null
  }
  const value = row[key]
  return typeof value === 'number' && Number.isFinite(value) ? key === 'spendKopecks' ? value / 100 : value : null
}
export function metricFormat(value: number | null, key: MetricKey): string {
  if (value == null) return '—'
  return `${value.toLocaleString('ru-RU', { maximumFractionDigits: key === 'conversionPct' ? 2 : 0 })}${key === 'spendKopecks' ? ' ₽' : key === 'conversionPct' ? '%' : ''}`
}
export function daysForPeriod(timeline: StatsDay[], period: StatsPeriod): StatsDay[] {
  const byDate = new Map(timeline.map(day => [day.date, day]))
  const result: StatsDay[] = []
  for (let date = period.dateFrom; date <= period.dateTo && result.length < 270; date = shiftDate(date, 1)) {
    result.push(byDate.get(date) ?? { date })
  }
  return result
}
export function threeDayTrend(timeline: StatsDay[], key: MetricKey, end: string, today = moscowToday()) {
  const completedEnd = end < today ? end : shiftDate(today, -1)
  const currentDates = [-2, -1, 0].map(offset => shiftDate(completedEnd, offset))
  const previousDates = [-5, -4, -3].map(offset => shiftDate(completedEnd, offset))
  const byDate = new Map(timeline.map(day => [day.date, day]))
  const sum = (dates: string[], metric: MetricKey) => {
    const values = dates.map(date => metricValue(byDate.get(date) ?? {}, metric))
    return values.every(value => value != null) ? values.reduce<number>((total, value) => total + value!, 0) : null
  }
  const aggregate = (dates: string[]) => {
    if (key !== 'conversionPct') return sum(dates, key)
    const views = sum(dates, 'views'), contacts = sum(dates, 'contacts')
    return views != null && views > 0 && contacts != null ? contacts / views * 100 : null
  }
  const current = aggregate(currentDates), previous = aggregate(previousDates)
  const rangeLabel = `${currentDates[0]} — ${currentDates[2]} / ${previousDates[0]} — ${previousDates[2]}`
  if (current == null || previous == null) return { direction: 'missing', label: 'Нет сравнения', rangeLabel } as const
  const delta = current - previous
  const direction = Math.abs(delta) < 1e-9 ? 'flat' : delta > 0 ? 'up' : 'down'
  if (direction === 'flat') return { direction, label: 'Без изменений', rangeLabel } as const
  const absolute = `${delta > 0 ? '+' : ''}${delta.toLocaleString('ru-RU', { maximumFractionDigits: key === 'conversionPct' ? 2 : key === 'spendKopecks' ? 2 : 0 })}`
  const label = key === 'conversionPct'
    ? `${absolute} п.п.${previous === 0 ? ' (с 0)' : ` (${delta > 0 ? '+' : ''}${(delta / previous * 100).toLocaleString('ru-RU', { maximumFractionDigits: 1 })}%)`}`
    : `${absolute}${key === 'spendKopecks' ? ' ₽' : ''}${previous === 0 ? ' (с 0)' : ` (${delta > 0 ? '+' : ''}${(delta / previous * 100).toLocaleString('ru-RU', { maximumFractionDigits: 1 })}%)`}`
  return { direction, label, rangeLabel } as const
}
