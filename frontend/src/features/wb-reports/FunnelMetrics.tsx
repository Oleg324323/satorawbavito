import { useState } from 'react'

export type FunnelDay = { date?: string; label?: string; value?: number | null; compareValue?: number | null; ordersUnits?: number | null; salesUnits?: number | null; returnsUnits?: number | null; openCount?: number | null; cartCount?: number | null; coverageComplete?: boolean }
type CountKey = 'openCount' | 'cartCount' | 'ordersUnits' | 'salesUnits' | 'returnsUnits'
type Key = CountKey | 'cartCrPct'
const metrics: Array<{ key: Key; label: string; color: string }> = [
  { key: 'openCount', label: 'Переходы в карточку', color: '#64748b' },
  { key: 'cartCount', label: 'Добавили в корзину', color: '#0284c7' },
  { key: 'ordersUnits', label: 'Заказали', color: '#2563eb' },
  { key: 'salesUnits', label: 'Выкупили', color: '#16a34a' },
  { key: 'returnsUnits', label: 'Возвраты', color: '#7c3aed' },
  { key: 'cartCrPct', label: 'Конверсия корзины в заказ', color: '#0284c7' },
]
const dayMs = 86400000
const iso = (time: number) => new Date(time).toISOString().slice(0, 10)
const number = (value: number) => value.toLocaleString('ru-RU', { maximumFractionDigits: 1 })
export function funnelValue(day: FunnelDay, key: Key): number | null {
  if (day.coverageComplete === false) return null
  if (key === 'cartCrPct') {
    const orders = funnelValue(day, 'ordersUnits'), carts = funnelValue(day, 'cartCount')
    return orders !== null && carts !== null && carts > 0 ? orders / carts * 100 : null
  }
  const value = day[key] ?? (key === 'ordersUnits' ? day.value : key === 'salesUnits' ? day.compareValue : null)
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}
export function funnelTrend(days: FunnelDay[], key: Key, dateTo: string, today: string) {
  const end = Math.min(Date.parse(`${dateTo}T00:00:00Z`), Date.parse(`${today}T00:00:00Z`) - dayMs)
  const dates = Array.from({ length: 6 }, (_, index) => iso(end - (5 - index) * dayMs))
  const byDate = new Map(days.filter(day => day.date).map(day => [day.date, day]))
  // A day with zero carts has no daily rate, but still belongs to a complete
  // three-day window. Compute conversion from totals, not daily percentages.
  if (dates.some(date => !byDate.has(date))) return { text: 'Нет полных 6 дней', tone: '#64748b', dates }
  const prior = funnelTotal(dates.slice(0, 3).map(date => byDate.get(date)!), key)
  const recent = funnelTotal(dates.slice(3).map(date => byDate.get(date)!), key)
  if (prior === null || recent === null) return { text: 'Нет полных 6 дней', tone: '#64748b', dates }
  const delta = recent - prior
  const unit = key === 'cartCrPct' ? ' пп' : ''
  return { text: delta === 0 ? `→ 0${unit} (0%)` : `${delta > 0 ? '↑' : '↓'} ${number(Math.abs(delta))}${unit} (${prior === 0 ? 'база 0' : `${delta > 0 ? '+' : '−'}${number(Math.abs(delta / prior * 100))}%`})`, tone: delta > 0 ? '#16a34a' : delta < 0 ? '#dc2626' : '#64748b', dates }
}

export function funnelTotal(days: FunnelDay[], key: Key): number | null {
  if (!days.length) return null
  if (key === 'cartCrPct') {
    const orders = funnelTotal(days, 'ordersUnits'), carts = funnelTotal(days, 'cartCount')
    return orders !== null && carts !== null && carts > 0 ? orders / carts * 100 : null
  }
  const values = days.map(day => funnelValue(day, key))
  return values.every(value => value !== null) ? values.reduce<number>((sum, value) => sum + value!, 0) : null
}

export function FunnelMetrics({ days, comparisonDays, dateTo, loading, error }: { days: FunnelDay[]; comparisonDays?: FunnelDay[]; dateTo: string; loading: boolean; error?: string | null }) {
  const [selected, setSelected] = useState<Key[]>(['ordersUnits', 'salesUnits'])
  const today = new Date().toLocaleDateString('sv-SE', { timeZone: 'Europe/Moscow' })
  const active = metrics.filter(metric => selected.includes(metric.key))
  const percent = selected.includes('cartCrPct')
  const unitLabel = percent ? 'Конверсия, %' : 'Количество, шт.'
  const max = Math.max(1, ...days.flatMap(day => active.map(metric => funnelValue(day, metric.key) ?? 0)))
  const left = 70, right = 960, top = 24, bottom = 245
  const x = (index: number) => left + (right - left) * index / Math.max(1, days.length - 1)
  const y = (value: number) => bottom - (bottom - top) * value / max
  const window = funnelTrend(comparisonDays ?? days, 'ordersUnits', dateTo, today).dates
  return <section className="report-panel report-panel-pad balance-chart-card" aria-label="Воронка продаж">
    <h2 style={{ fontSize: 19, margin: '0 0 16px' }}>Воронка продаж</h2>
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))', gap: 10 }}>
      {metrics.map(metric => {
        const total = funnelTotal(days, metric.key)
        const trend = funnelTrend(comparisonDays ?? days, metric.key, dateTo, today)
        const enabled = selected.includes(metric.key)
        return <button key={metric.key} type="button" aria-pressed={enabled} onClick={() => setSelected(old => enabled ? old.filter(key => key !== metric.key) : metric.key === 'cartCrPct' ? ['cartCrPct'] : [...old.filter(key => key !== 'cartCrPct'), metric.key])} style={{ textAlign: 'left', padding: 16, borderRadius: 10, border: `1px solid ${enabled ? '#2563eb' : '#e2e8f0'}`, background: enabled ? '#eff6ff' : 'var(--white)', color: 'var(--gray-900)', cursor: 'pointer' }}>
          <span style={{ display: 'block', fontSize: 12 }}>{metric.label}</span>
          <strong style={{ display: 'block', fontSize: 25, margin: '10px 0' }}>{total === null ? '—' : `${number(total)}${metric.key === 'cartCrPct' ? '%' : ''}`}</strong>
          <span style={{ color: trend.tone, fontSize: 12 }}>{trend.text}</span>
        </button>
      })}
    </div>
    <p style={{ color: 'var(--gray-500)', fontSize: 12 }}>Сравнение: {window[3]} — {window[5]} с {window[0]} — {window[2]}. Только завершённые дни. Нажмите на показатель для выбора линии.</p>
    {loading && <p role="status">Обновляем данные…</p>}
    {error && <p role="alert">{error}</p>}
    {!days.length ? <p>Дневные данные за этот период не получены.</p> : <>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 16, fontSize: 12 }}>{active.map(metric => <span key={metric.key} style={{ color: metric.color }}>━ {metric.label}</span>)}</div>
      <p style={{ fontSize: 12, color: 'var(--gray-500)' }}>{unitLabel}</p>
      <svg viewBox="0 0 1000 280" role="img" aria-label={`Дневная динамика: ${unitLabel}`} style={{ width: '100%', minHeight: 200 }}>
        {[0, .5, 1].map(ratio => <g key={ratio}><line x1={left} x2={right} y1={y(max * ratio)} y2={y(max * ratio)} stroke="#e2e8f0" /><text x={left - 10} y={y(max * ratio) + 4} textAnchor="end" fill="#64748b" fontSize="12">{number(max * ratio)}</text></g>)}
        {active.map(metric => <g key={metric.key}>{days.map((day, index) => {
          const value = funnelValue(day, metric.key), previous = index ? funnelValue(days[index - 1], metric.key) : null
          if (value === null) return null
          return <g key={day.date ?? index}>
            {previous !== null && <line x1={x(index - 1)} y1={y(previous)} x2={x(index)} y2={y(value)} stroke={metric.color} strokeWidth="2.5" />}
            <circle cx={x(index)} cy={y(value)} r="4" fill={metric.color}><title>{`${day.date ?? day.label}: ${metric.label} — ${number(value)} ${percent ? '%' : 'шт.'}`}</title></circle>
          </g>
        })}</g>)}
        {days.map((day, index) => index % Math.max(1, Math.ceil(days.length / 8)) === 0 || index === days.length - 1 ? <text key={index} x={x(index)} y={270} textAnchor="middle" fill="#64748b" fontSize="12">{day.date ? `${day.date.slice(8, 10)}.${day.date.slice(5, 7)}` : day.label}</text> : null)}
      </svg>
    </>}
  </section>
}
