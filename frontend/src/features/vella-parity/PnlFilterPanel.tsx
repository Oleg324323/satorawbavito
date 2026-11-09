import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import './pnlFilters.css'
import { createPnlFilterMatcher, defaultPnlFilters, pnlAttrs, pnlFilterError, pnlRanges, pnlSegments, pnlStatuses, pnlStocks, pnlTypes,
  type PnlFilters, type PnlFilterMetadata, type PnlFilterRow } from './pnlFilters'

export function pnlManagerOptions(metadata: PnlFilterMetadata | null) {
  return [...new Map(Object.values(metadata?.products ?? {}).filter(p => p.managerId)
    .map(p => [p.managerId!, p.managerName || p.managerId!] as const)).entries()].sort((a, b) => a[1].localeCompare(b[1], 'ru'))
}

export function PnlFilterPanel({ filters, rows, metadata, currentManagerId, loading, error, onApply, onClear, onClose, onRetry,
  tab = 'pnl', label = 'Фильтры товаров P&L', description = 'Сохранённые атрибуты товаров и показатели выбранного периода P&L.', ranges = pnlRanges }: {
  filters: PnlFilters; rows: PnlFilterRow[]; metadata: PnlFilterMetadata | null; currentManagerId: string
  loading: boolean; error: string; onApply: (filters: PnlFilters) => void; onClear: () => void; onClose: () => void
  onRetry?: () => void
  tab?: string; label?: string; description?: string; ranges?: readonly (typeof pnlRanges)[number][]
}) {
  const [draft, setDraft] = useState(() => structuredClone(filters))
  const panel = useRef<HTMLDivElement>(null)
  const validation = pnlFilterError(draft)
  useEffect(() => {
    panel.current?.focus()
    const outside = (event: PointerEvent) => {
      const target = event.target as Node
      if (!panel.current?.contains(target) && !document.getElementById(`${tab}-filters-trigger`)?.contains(target)) onClose()
    }
    document.addEventListener('pointerdown', outside)
    return () => document.removeEventListener('pointerdown', outside)
  }, [onClose, tab])
  const choose = (key: keyof PnlFilters, value: string) => setDraft(current => ({ ...current, [key]: value }))
  const counts = useMemo(() => new Map<string, number>(), [rows, metadata, currentManagerId])
  const count = (key: keyof PnlFilters, value: string) => {
    const cacheKey = JSON.stringify([key, value])
    if (!counts.has(cacheKey)) {
      const matches = createPnlFilterMatcher({ ...defaultPnlFilters(), [key]: value }, currentManagerId)
      counts.set(cacheKey, rows.filter(row => row.nmId && matches(row, metadata?.products[String(row.nmId)])).length)
    }
    return counts.get(cacheKey)!
  }
  const choices = (key: keyof PnlFilters, options: readonly (readonly [string, string])[]) => options.map(([value, label]) => (
    <button key={value} type="button" data-vella-react-handlers="onclick" className={`chip${draft[key] === value ? ' active' : ''}`} aria-pressed={draft[key] === value}
      onClick={() => choose(key, value)}>{label}<span className="chip-count">{count(key, value)}</span></button>
  ))
  const multi = (key: 'types' | 'attrs', options: readonly (readonly [string, string])[]) => options.map(([value, label]) => (
    <button key={value} type="button" data-vella-react-handlers="onclick" className={`chip${draft[key].includes(value) ? ' active' : ''}`} aria-pressed={draft[key].includes(value)}
      onClick={() => setDraft(current => ({ ...current, [key]: current[key].includes(value) ? current[key].filter(item => item !== value) : [...current[key], value] }))}>{label}</button>
  ))
  const section = (label: string, content: ReactNode, wide = false) => <div className={`products-filter-section${wide ? ' products-filter-section-wide' : ''}`} role="group" aria-label={label}>
    <span>{label}</span><div className="products-filter-popover-grid">{content}</div></div>
  const brands = [...new Set(Object.values(metadata?.products ?? {}).map(p => p.brand).filter((value): value is string => !!value))].sort((a, b) => a.localeCompare(b, 'ru'))
  const strategies = [...new Map(Object.values(metadata?.products ?? {}).filter(p => p.strategyId).map(p => [p.strategyId!, p.strategyName || p.strategyId!] as const)).entries()]
  return <div id={`${tab}-product-filters`} ref={panel} tabIndex={-1} role="region" aria-label={label}
    className="products-advanced-filters-panel report-product-filters" onKeyDown={event => { if (event.key === 'Escape') { event.stopPropagation(); onClose() } }}>
    <div className="products-filter-popover-head"><div><b>Фильтры товаров</b><span>{description}</span></div>
      <div className="products-filter-head-actions">
        <button type="button" data-vella-react-handlers="onclick" className="btn btn-ghost btn-sm" onClick={onClear}>Очистить</button>
        <button type="button" data-vella-react-handlers="onclick" className="btn btn-primary btn-sm" disabled={!!validation || loading || !!error} onClick={() => onApply(draft)}>Применить</button>
        <button type="button" data-vella-react-handlers="onclick" className="btn btn-ghost btn-sm" aria-label="Закрыть фильтры" onClick={onClose}>×</button>
      </div></div>
    {loading && <p role="status">Читаем сохранённые атрибуты товаров…</p>}
    {error && <p role="alert">{error} {onRetry && <button type="button" className="btn btn-default btn-sm" disabled={loading} onClick={onRetry}>Повторить загрузку фильтров</button>}</p>}
    {validation && <p role="alert">{validation}</p>}
    <p className="muted" style={{ fontSize: 12 }}>Счётчики — по всем строкам товаров отчёта, без поиска и других фильтров. Неизвестные значения не равны нулю. Нераспределённые расходы WB не относятся к товарам и исключаются при фильтре по товару.</p>
    <div className="products-advanced-filter-grid">
      {section('Сегменты', choices('segment', pnlSegments), true)}
      {section('Ответственный', choices('manager', [['all', 'Все менеджеры'], ['mine', 'Мои товары'], ['unassigned', 'Без ответственного'], ...pnlManagerOptions(metadata)]))}
      {section('Состояние товара', choices('status', pnlStatuses))}
      {section('Акции', <>{choices('promo', [['all', 'Все акции'], ['yes', 'В акции'], ['no', 'Без акции']])}
        {metadata && !Object.values(metadata.products).some(p => p.promotion != null) && <small>Нет сохранённых данных об акциях. Это не означает, что товары без акции.</small>}</>)}
      {section('Бренд', choices('brand', [['all', 'Все бренды'], ...brands.map(brand => [brand, brand] as const)]))}
      <div className="products-filter-section products-filter-section-wide"><span>Показатели товара</span><div className="stats-ranges">
        {ranges.map(([key, label]) => <div className="products-filter-section" key={key}><span>{label}</span><div className="stats-range">
          {(['from', 'to'] as const).map((bound, index) => <span key={bound} style={{ display: 'contents' }}>{index ? <span>—</span> : null}<input type="text" inputMode="decimal" placeholder={bound === 'from' ? 'от' : 'до'} aria-label={`${label} ${bound === 'from' ? 'от' : 'до'}`} value={draft.ranges[key][bound]}
            onChange={event => setDraft(current => ({ ...current, ranges: { ...current.ranges, [key]: { ...current.ranges[key], [bound]: event.target.value } } }))} /></span>)}
        </div></div>)}
      </div></div>
      {section('Тип одежды', multi('types', pnlTypes))}
      {section('Пол / цвет', multi('attrs', pnlAttrs))}
      {section('Стратегия', choices('strategy', [['all', 'Все стратегии'], ...strategies]))}
      {section('Остаток', choices('stock', pnlStocks))}
    </div>
  </div>
}
