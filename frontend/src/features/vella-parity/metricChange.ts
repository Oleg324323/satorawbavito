// Trends compare observations, never infer growth from the magnitude of a value.
export function metricChange(current: number | null | undefined, previous: number | null | undefined, unit = '', digits = 1) {
  if (current == null || previous == null || !Number.isFinite(current) || !Number.isFinite(previous)) return { text: '—', tone: 'neutral' }
  const difference = current - previous
  const format = (n: number) => n.toLocaleString('ru-RU', { maximumFractionDigits: digits })
  if (Math.abs(difference) < 1e-9) return { text: `0${unit} (0%)`, tone: 'neutral' }
  const percent = previous === 0 ? 'нет базы' : `${difference > 0 ? '+' : '−'}${format(Math.abs(difference / previous * 100))}%`
  return { text: `${difference > 0 ? '↑' : '↓'} ${format(Math.abs(difference))}${unit} (${percent})`, tone: difference > 0 ? 'up' : 'down' }
}

export function completedThreeDayWindows(end: string, today: string) {
  const day = 86400000
  const last = Math.min(Date.parse(`${end.slice(0, 10)}T00:00:00Z`), Date.parse(`${today}T00:00:00Z`) - day)
  const iso = (offset: number) => new Date(last - offset * day).toISOString().slice(0, 10)
  return { current: { dateFrom: iso(2), dateTo: iso(0) }, previous: { dateFrom: iso(5), dateTo: iso(3) } }
}
