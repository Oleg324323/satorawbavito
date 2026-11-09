import { useEffect, useMemo, useRef, useState } from 'react'
import { loadLiveRepricerCacheCoverage, type LiveRepricerCacheCoverage } from './liveParityData'
import { isSelectableWbDay } from './presetPeriodAnchor'

type Range = { fromIso: string; toIso: string; label: string }
type Props = { applied: Range; minIso: string; maxIso: string; accessToken?: string | null;
  onApply: (fromIso: string, toIso: string) => void }

function isoDay(date: Date) { return date.toISOString().slice(0, 10) }
function monthStart(iso: string) { return iso.slice(0, 7) + '-01' }
function shiftMonth(iso: string, offset: number) {
  const date = new Date(iso + 'T00:00:00Z'); date.setUTCMonth(date.getUTCMonth() + offset); return isoDay(date)
}
function normalizedDate(text: string) {
  const parts = text.match(/^(\d{2})\.(\d{2})\.(\d{4})$/)
  return parts ? `${parts[3]}-${parts[2]}-${parts[1]}` : text
}

// React owns the popup and its buttons throughout its lifetime. Never move it
// outside the React root or replace its children with legacy innerHTML.
export function WbPeriodCalendar({ applied, minIso, maxIso, accessToken, onApply }: Props) {
  const [from, setFrom] = useState(applied.fromIso)
  const [to, setTo] = useState(applied.toIso)
  const [open, setOpen] = useState(false)
  const [boundary, setBoundary] = useState<'from' | 'to'>('from')
  const [month, setMonth] = useState(monthStart(applied.fromIso))
  const [coverage, setCoverage] = useState<LiveRepricerCacheCoverage | null>(null)
  const [coverageLoading, setCoverageLoading] = useState(false)
  const [coverageError, setCoverageError] = useState(false)
  const [error, setError] = useState('')
  const root = useRef<HTMLDivElement>(null)
  const fromIso = normalizedDate(from), toIso = normalizedDate(to)
  useEffect(() => { setFrom(applied.fromIso); setTo(applied.toIso); setError('') }, [applied])
  useEffect(() => {
    if (!open) return
    const outside = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false) }
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') setOpen(false) }
    document.addEventListener('pointerdown', outside)
    document.addEventListener('keydown', escape)
    return () => { document.removeEventListener('pointerdown', outside); document.removeEventListener('keydown', escape) }
  }, [open])
  useEffect(() => {
    if (!open || !accessToken) return
    const controller = new AbortController()
    setCoverage(null); setCoverageLoading(true); setCoverageError(false)
    void loadLiveRepricerCacheCoverage(accessToken, { dateFrom: minIso, dateTo: maxIso }, controller.signal)
      .then(value => { if (!controller.signal.aborted) setCoverage(value) })
      .catch(() => { if (!controller.signal.aborted) { setCoverage(null); setCoverageError(true) } })
      .finally(() => { if (!controller.signal.aborted) setCoverageLoading(false) })
    return () => controller.abort()
  }, [open, accessToken, minIso, maxIso])
  const coverageByDate = useMemo(() => new Map(coverage?.days.map(day => [day.date, day]) ?? []), [coverage])
  const days = useMemo(() => {
    const first = new Date(month + 'T00:00:00Z')
    first.setUTCDate(first.getUTCDate() - (first.getUTCDay() + 6) % 7)
    return Array.from({ length: 42 }, (_, offset) => {
      const date = new Date(first); date.setUTCDate(date.getUTCDate() + offset)
      return { iso: isoDay(date), label: date.getUTCDate() }
    })
  }, [month])
  const allowed = (iso: string) => isSelectableWbDay(iso, minIso) && iso <= maxIso
  const choose = (iso: string) => {
    if (!allowed(iso)) return
    setError('')
    if (boundary === 'from') { setFrom(iso); if (iso > toIso) setTo(iso); setBoundary('to') }
    else { setFrom(iso < fromIso ? iso : fromIso); setTo(iso < fromIso ? fromIso : iso); setBoundary('from') }
  }
  const apply = () => {
    if (!allowed(fromIso) || !allowed(toIso) || fromIso > toIso) {
      setError('Укажите корректный диапазон завершённых дней по Москве.'); return
    }
    onApply(fromIso, toIso)
    setFrom(fromIso); setTo(toIso); setOpen(false); setError('')
  }
  const toggle = () => {
    if (!open) { setBoundary('from'); setMonth(monthStart(allowed(fromIso) ? fromIso : maxIso)) }
    setOpen(!open)
  }
  return <div ref={root} className="wb-period-calendar" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
    {(['from', 'to'] as const).map(edge => <input key={edge} type="text" inputMode="numeric"
      className="period-range products-calendar-field" style={{ width: 136 }}
      id={edge === 'from' ? 'globalPeriodRange' : 'globalPeriodRangeTo'}
      aria-label={edge === 'from' ? 'Начало периода аналитики WB' : 'Конец периода аналитики WB'}
      placeholder="YYYY-MM-DD" value={edge === 'from' ? from : to}
      data-vella-react-handlers="onclick,onchange,oninput,onkeydown"
      onChange={event => { (edge === 'from' ? setFrom : setTo)(event.currentTarget.value); setError('') }}
      onKeyDown={event => { if (event.key === 'Enter') apply() }} />)}
    <div className={`products-cache-calendar-wrap${open ? ' is-open' : ''}`}>
      <button className="products-cache-calendar-trigger" type="button" aria-expanded={open}
        data-vella-react-handlers="onclick" onClick={toggle}>Календарь данных</button>
      {open && <div className="products-cache-calendar-popover" role="dialog" aria-label="Календарь данных WB">
        <div className="products-cache-calendar-top">
          <div className="products-cache-calendar-tabs" aria-label="Граница периода">
            <button type="button" className={`products-cache-calendar-tab${boundary === 'from' ? ' active' : ''}`}
              onClick={() => setBoundary('from')}>С {fromIso}</button>
            <button type="button" className={`products-cache-calendar-tab${boundary === 'to' ? ' active' : ''}`}
              onClick={() => setBoundary('to')}>По {toIso}</button>
          </div>
          <div className="products-cache-calendar-nav">
            <button type="button" className="icon-btn" aria-label="Предыдущий месяц"
              disabled={shiftMonth(month, -1) < monthStart(minIso)} onClick={() => setMonth(shiftMonth(month, -1))}>‹</button>
            <strong>{new Date(month + 'T00:00:00Z').toLocaleDateString('ru-RU', { month: 'long', year: 'numeric', timeZone: 'UTC' })}</strong>
            <button type="button" className="icon-btn" aria-label="Следующий месяц"
              disabled={shiftMonth(month, 1) > monthStart(maxIso)} onClick={() => setMonth(shiftMonth(month, 1))}>›</button>
          </div>
        </div>
        <div className="products-cache-calendar-weekdays" aria-hidden="true">
          {['Пн','Вт','Ср','Чт','Пт','Сб','Вс'].map(day => <span key={day}>{day}</span>)}
        </div>
        <div className="products-cache-calendar-grid">{days.map(day => {
          const state = coverageByDate.get(day.iso)?.state ?? 'missing'
          return <button key={day.iso} type="button" data-date={day.iso} aria-label={day.iso}
            disabled={!allowed(day.iso)} onClick={() => choose(day.iso)}
            className={['products-cache-calendar-day', `is-${state}`, day.iso.slice(0,7) !== month.slice(0,7) ? 'is-outside-month' : '',
              day.iso >= fromIso && day.iso <= toIso ? 'is-in-range' : '', day.iso === fromIso ? 'is-from' : '', day.iso === toIso ? 'is-to' : ''].join(' ')}>{day.label}</button>
        })}</div>
        <div className="products-cache-calendar-foot"><span><i className="is-complete" />есть</span>
          <span><i className="is-partial" />частично</span><span><i className="is-missing" />нет данных ещё</span></div>
        <div className="products-cache-calendar-summary" role="status">{coverageLoading ? 'Загружаем календарь данных…'
          : coverageError ? 'Сведения о наличии данных недоступны. Даты можно выбирать.' : 'Недостающие данные загрузятся после применения.'}</div>
      </div>}
    </div>
    <button className="btn btn-default btn-sm" type="button" data-vella-react-handlers="onclick" onClick={apply}>Применить</button>
    <span className="muted" style={{ fontSize: 11, whiteSpace: 'nowrap' }}>Применено: {applied.label}</span>
    {error && <span role="alert">{error}</span>}
  </div>
}
