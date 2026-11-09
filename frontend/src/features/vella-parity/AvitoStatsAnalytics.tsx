import { useEffect, useMemo, useRef, useState } from 'react'
import { ArrowDownRight, ArrowUpRight, BarChart3, Check, ChevronRight, Eye, Heart, Minus, PackageCheck, Percent, Phone, ShoppingBag, Wallet } from 'lucide-react'
import { Chart, CategoryScale, LinearScale, LineController, LineElement, PointElement, Filler, Tooltip } from 'chart.js'
import { daysForPeriod, metricFormat, metricKeys, metricNames, metricValue, threeDayTrend, type MetricKey, type StatsDay, type StatsPeriod, type StatsSummary } from './avitoStatsMetrics'
import './avitoStatsAnalytics.css'

Chart.register(CategoryScale, LinearScale, LineController, LineElement, PointElement, Filler, Tooltip)
const icons = { impressions: BarChart3, views: Eye, contacts: Phone, favorites: Heart, orders: ShoppingBag, buyouts: PackageCheck, spendKopecks: Wallet, conversionPct: Percent }
const colors: Record<MetricKey, string> = { impressions: '#64748b', views: '#2563eb', contacts: '#0891b2', favorites: '#6366f1', orders: '#334155', buyouts: '#0f766e', spendKopecks: '#2563eb', conversionPct: '#2563eb' }
const unit = (key: MetricKey) => key === 'spendKopecks' ? 'Рубли' : key === 'conversionPct' ? 'Проценты' : 'Количество'
const shortDate = (date: string) => `${date.slice(8, 10)}.${date.slice(5, 7)}`

function MetricChart({ days, selected, compact = false }: { days: StatsDay[]; selected: MetricKey[]; compact?: boolean }) {
  const ref = useRef<HTMLCanvasElement>(null)
  const signature = selected.join(',')
  useEffect(() => {
    if (!ref.current) return
    const keys = signature.split(',') as MetricKey[]
    const theme = getComputedStyle(ref.current)
    const foreground = theme.getPropertyValue('--gray-500').trim() || '#64748b'
    const grid = theme.getPropertyValue('--gray-100').trim() || '#e2e8f0'
    const chart = new Chart(ref.current, {
      type: 'line',
      data: {
        labels: days.map(day => shortDate(day.date)),
        datasets: keys.map((key, index) => ({
          label: metricNames[key], data: days.map(day => metricValue(day, key)),
          borderColor: compact ? '#2563eb' : colors[key], backgroundColor: 'rgba(37,99,235,0.06)',
          fill: keys.length === 1, borderWidth: compact ? 1.8 : 2.5,
          borderDash: !compact && index % 2 === 1 ? [6, 4] : [],
          pointRadius: compact || days.length > 45 ? 0 : 2.5,
          pointHoverRadius: compact ? 0 : 5, tension: 0.2, spanGaps: false,
        })),
      },
      options: {
        animation: false, responsive: true, maintainAspectRatio: false,
        events: compact ? [] : ['mousemove', 'mouseout', 'click', 'touchstart', 'touchmove'],
        interaction: { mode: 'index', intersect: false },
        plugins: { tooltip: { enabled: !compact, padding: 12, callbacks: {
          title: items => days[items[0]?.dataIndex]?.date.split('-').reverse().join('.') ?? '',
          label: context => `${metricNames[keys[context.datasetIndex]]}: ${metricFormat(context.parsed.y, keys[context.datasetIndex])}`,
        } } },
        scales: {
          x: { display: !compact, grid: { display: false }, ticks: { color: foreground, maxTicksLimit: 12, maxRotation: 0, font: { size: 11 } } },
          y: { display: !compact, beginAtZero: true, title: { display: true, text: unit(keys[0]), color: foreground }, grid: { color: grid }, ticks: { color: foreground, maxTicksLimit: 6 } },
        },
      },
    })
    return () => chart.destroy()
  }, [days, signature, compact])
  return <canvas ref={ref} role={compact ? undefined : 'img'} aria-hidden={compact || undefined} aria-label={compact ? undefined : `Динамика по дням: ${selected.map(key => metricNames[key]).join(', ')}. Точные значения доступны в таблице под графиком.`} />
}

export function AvitoStatsAnalytics({ summary, timeline = [], period, loading = false }: {
  summary?: StatsSummary; timeline?: StatsDay[]; period: StatsPeriod; loading?: boolean
}) {
  const [selected, setSelected] = useState<MetricKey[]>(['views'])
  const [mode, setMode] = useState('traffic')
  const days = useMemo(() => daysForPeriod(timeline, period), [timeline, period.dateFrom, period.dateTo])
  const hasDaily = days.some(day => selected.some(key => metricValue(day, key) != null))
  const selectMetric = (key: MetricKey) => {
    setMode('custom')
    setSelected(current => unit(current[0]) !== unit(key) ? [key] : current.includes(key)
      ? current.length === 1 ? current : current.filter(item => item !== key) : [...current, key])
  }
  const modes: Array<{ key: string; label: string; metrics: MetricKey[] }> = [
    { key: 'traffic', label: 'Трафик', metrics: ['views'] },
    { key: 'funnel', label: 'Воронка', metrics: ['contacts', 'orders', 'buyouts'] },
    { key: 'sales', label: 'Продажи', metrics: ['orders', 'buyouts'] },
  ]
  const funnel: MetricKey[] = ['impressions', 'views', 'contacts', 'orders', 'buyouts']
  return <div className="avito-analytics" aria-busy={loading} data-date-from={period.dateFrom} data-date-to={period.dateTo}>
    <section className="avito-analytics-panel">
      <header className="avito-analytics-heading">
        <div><h2><BarChart3 size={21} aria-hidden="true" /> Аналитика по метрикам</h2><p>Нажмите на карточку, чтобы добавить показатель на график.</p></div>
        <div className="avito-analytics-modes" aria-label="Группа показателей">
          {modes.map(item => <button type="button" aria-pressed={mode === item.key} key={item.key} onClick={() => { setMode(item.key); setSelected(item.metrics) }}>{item.label}</button>)}
        </div>
      </header>
      <div className="avito-analytics-cards">
        {metricKeys.map(key => {
          const Icon = icons[key], trend = threeDayTrend(timeline, key, period.dateTo)
          const Direction = trend.direction === 'up' ? ArrowUpRight : trend.direction === 'down' ? ArrowDownRight : Minus
          const hasSparkline = days.some(day => metricValue(day, key) != null)
          return <button type="button" className="avito-metric-card" data-metric={key} key={key} aria-pressed={selected.includes(key)} onClick={() => selectMetric(key)}>
            <span className="avito-metric-label"><span className="avito-metric-icon"><Icon size={18} aria-hidden="true" /></span>{metricNames[key]}{selected.includes(key) ? <Check className="avito-metric-check" size={15} aria-hidden="true" /> : null}</span>
            <strong id={`avitoStatsKpi${key === 'spendKopecks' ? 'Spend' : key === 'conversionPct' ? 'Conversion' : key[0].toUpperCase() + key.slice(1)}`}>{loading && !summary ? '…' : metricFormat(metricValue(summary ?? {}, key), key)}</strong>
            <span className={`avito-metric-trend ${trend.direction}`} title={trend.rangeLabel}><Direction size={15} aria-hidden="true" />{trend.label}</span>
            <span className="avito-metric-spark" aria-hidden="true">{hasSparkline ? <MetricChart days={days} selected={[key]} compact /> : <span>Нет дневных данных</span>}</span>
          </button>
        })}
      </div>
      <p className="avito-trend-explanation">Стрелки: последние 3 завершённых дня против предыдущих 3 дней. Рост — зелёный, снижение — красный; для расходов рост означает увеличение затрат.</p>
      <div className="avito-analytics-chart-heading">
        <div><h3>Динамика по дням</h3><p>{shortDate(period.dateFrom)} — {shortDate(period.dateTo)} · {unit(selected[0]).toLocaleLowerCase('ru-RU')}</p></div>
        <div className="avito-analytics-legend" aria-label="Линии графика">{selected.map((key, index) => <button type="button" key={key} onClick={() => selectMetric(key)} aria-label={`Убрать с графика: ${metricNames[key]}`} disabled={selected.length === 1}><span style={{ borderColor: colors[key], borderTopStyle: index % 2 ? 'dashed' : 'solid' }} />{metricNames[key]}</button>)}</div>
      </div>
      {hasDaily ? <div className="avito-analytics-chart"><MetricChart days={days} selected={selected} /></div> : <div className="avito-analytics-no-data">{loading ? 'Загружаем дневную статистику…' : 'Дневная статистика не получена. Обновите данные, чтобы построить график.'}</div>}
      <div className="avito-analytics-chart-note">Наведение на график — значения за день. Расходы и конверсия отображаются отдельно, в своих единицах.</div>
      {hasDaily ? <details className="avito-analytics-data"><summary>Значения по дням</summary><div><table><thead><tr><th>Дата</th>{selected.map(key => <th key={key}>{metricNames[key]}</th>)}</tr></thead><tbody>{days.map(day => <tr key={day.date}><td>{day.date.split('-').reverse().join('.')}</td>{selected.map(key => <td key={key}>{metricFormat(metricValue(day, key), key)}</td>)}</tr>)}</tbody></table></div></details> : null}
    </section>
    <section className="avito-analytics-panel avito-funnel-panel">
      <header className="avito-analytics-heading"><div><h2><ArrowUpRight size={21} aria-hidden="true" /> Воронка конверсии</h2><p>Показатели за выбранный период.</p></div></header>
      <div className="avito-funnel-cards">{funnel.map((key, index) => {
        const Icon = icons[key], value = metricValue(summary ?? {}, key), previous = index ? metricValue(summary ?? {}, funnel[index - 1]) : null
        const ratio = previous != null && previous > 0 && value != null ? value / previous * 100 : null
        return <div className="avito-funnel-card" key={key}><span className="avito-metric-label"><Icon size={18} aria-hidden="true" />{metricNames[key]}</span><strong>{metricFormat(value, key)}</strong><span className="avito-funnel-rate">{index === 0 ? 'Начало воронки' : `${ratio == null ? '—' : `${ratio.toLocaleString('ru-RU', { maximumFractionDigits: 2 })}%`} от «${metricNames[funnel[index - 1]]}»`}</span>{index < funnel.length - 1 ? <ChevronRight className="avito-funnel-next" size={18} aria-hidden="true" /> : null}</div>
      })}</div>
      <p className="avito-trend-explanation">Заказы и выкупы считаются по событиям периода: выкуп мог относиться к заказу, оформленному раньше.</p>
    </section>
  </div>
}
