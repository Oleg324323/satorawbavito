import { useEffect, useMemo, useRef, useState } from 'react'
import {
  ArrowUp,
  ArrowDown,
  ArrowUpDown,
  ChevronDown,
  Check,
  X,
  Search,
} from 'lucide-react'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '../../components/ui/dropdown-menu'
import type { SkuSettingsResponse, SkuStatus } from './schemas'
import { computePMinKopecks, marginStatusAt } from './pricing'
import { formatRub } from '../../lib/formatRub'
import { cn } from '../../lib/utils'
import { DateRangeControl, defaultDateRange } from '../wb-reports/DateRangeControl'
import type { DateRange } from '../wb-reports/types'

type Row = SkuSettingsResponse
type TypeFilter = 'all' | 'F' | 'H' | 'L'
type StatusFilter = 'all' | SkuStatus
type MarginFilter = 'all' | 'negative' | 'thin' | 'ok'
type AutoFilter = 'all' | 'on' | 'off'

type SortCol = 'problems' | 'article' | 'price' | 'pmin' | 'margin' | 'baskets'
type SortKey = 'problems' | `${Exclude<SortCol, 'problems'>}-${'asc' | 'desc'}`

const PAGE_SIZE = 50

// ── Image helpers ─────────────────────────────────────────────────────────────

function wbBasketUrl(nmId: number): string {
  const vol = Math.floor(nmId / 100000)
  const part = Math.floor(nmId / 1000)
  const basket =
    vol <= 143 ? String(Math.floor(vol / 14) + 1).padStart(2, '0')
    : vol <= 287 ? '11'
    : vol <= 431 ? '12'
    : vol <= 719 ? '13'
    : vol <= 1007 ? '14'
    : vol <= 1061 ? '15'
    : vol <= 1115 ? '16'
    : vol <= 1169 ? '17'
    : vol <= 1313 ? '18'
    : vol <= 1601 ? '19'
    : vol <= 1655 ? '20'
    : '21'
  return `https://basket-${basket}.wbbasket.ru/vol${vol}/part${part}/${nmId}/images/c516x688/1.webp`
}

function hashHue(s: string): number {
  let h = 0
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0
  return h % 360
}

function placeholderSvg(articleId: string): string {
  const type = articleId[0]?.toUpperCase() ?? '?'
  const color = articleId[1]?.toUpperCase() ?? 'B'
  const isWhite = color === 'B'
  const hue = hashHue(articleId)
  const bg = isWhite ? '#f3f4f6' : '#1f2937'
  const fg = isWhite ? '#4b5563' : '#e5e7eb'
  const printColor = `hsl(${hue} 70% ${isWhite ? 50 : 65}%)`
  const labelColor = isWhite ? '#6366f1' : '#a5b4fc'
  const silhouette =
    type === 'F'
      ? `<path d="M18 26 L30 20 L42 26 L46 32 L46 64 L14 64 L14 32 Z" fill="${fg}"/><path d="M26 22 Q30 18 34 22 L34 28 Q30 30 26 28 Z" fill="${bg}"/>`
      : type === 'H'
        ? `<path d="M16 32 L28 22 Q30 18 30 18 Q30 18 32 22 L44 32 L46 64 L14 64 Z" fill="${fg}"/><path d="M26 22 Q30 20 34 22 Q34 26 30 28 Q26 26 26 22 Z" fill="${bg}"/>`
        : type === 'L'
          ? `<path d="M18 24 L30 20 L42 24 L42 68 L18 68 Z M8 24 L18 24 L18 44 L8 44 Z M42 24 L52 24 L52 44 L42 44 Z" fill="${fg}"/>`
          : `<rect x="20" y="22" width="20" height="40" fill="${fg}"/>`
  const printX = 30
  const printY = type === 'F' ? 44 : type === 'H' ? 46 : 42
  const num = articleId.match(/\d+/)?.[0] ?? ''
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 60 76" width="60" height="76">
    <rect width="60" height="76" fill="${bg}"/>
    ${silhouette}
    <circle cx="${printX}" cy="${printY}" r="7" fill="${printColor}" opacity="0.8"/>
    <text x="${printX}" y="${printY + 3}" font-family="system-ui,sans-serif" font-size="8" font-weight="700" fill="${isWhite ? '#fff' : '#111'}" text-anchor="middle">${num.slice(-2)}</text>
    <text x="30" y="74" font-family="monospace" font-size="7" font-weight="600" fill="${labelColor}" text-anchor="middle">${type}${color}</text>
  </svg>`
  return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`
}

function skuImageUrl(meta: { articleId: string; nmId?: number | string | null }): string {
  const nm = typeof meta.nmId === 'string' ? Number(meta.nmId) : meta.nmId
  if (nm && Number.isFinite(nm) && nm > 200000000 && nm < 300000000) return wbBasketUrl(nm)
  return placeholderSvg(meta.articleId)
}

function getProductType(articleId: string): TypeFilter {
  const c = articleId[0]
  if (c === 'F' || c === 'H' || c === 'L') return c
  return 'all'
}

function problemScore(row: Row): number {
  const margin = marginStatusAt(
    row.meta.currentPriceKopecks,
    row.settings.cogsKopecks,
    row.settings.wbCommissionPct,
    row.settings.logisticsKopecks,
  )
  if (margin === 'negative') return 4
  if (row.meta.basketsLast7d < row.meta.basketNorm * 0.25) return 3
  if (row.meta.status === 'warmup') return 2
  if (row.meta.status === 'manual') return 1
  return 0
}

// Left-border priority colour class (Attio-style: тонкая полоска, не bg)
function rowPriorityClass(row: Row): string {
  const margin = marginStatusAt(
    row.meta.currentPriceKopecks,
    row.settings.cogsKopecks,
    row.settings.wbCommissionPct,
    row.settings.logisticsKopecks,
  )
  if (margin === 'negative') return 'border-l-2 border-l-orange-400 bg-orange-500/[0.03]'
  if (row.meta.status === 'warmup') return 'border-l-2 border-l-blue-400'
  return 'border-l-2 border-l-transparent'
}

// ── Sub-components ────────────────────────────────────────────────────────────

function StatusPill({
  status,
  warmupDaysLeft,
  automationEnabled,
  onToggle,
}: {
  status: SkuStatus
  warmupDaysLeft: number | null
  automationEnabled?: boolean
  onToggle?: () => void
}) {
  const cfg: Record<SkuStatus, { label: string; cls: string }> = {
    auto: { label: 'авто', cls: 'bg-emerald-500/10 text-emerald-700 border-emerald-500/20' },
    manual: { label: 'ручной', cls: 'bg-muted text-muted-foreground border-border' },
    warmup: {
      label: warmupDaysLeft ? `прогрев ${warmupDaysLeft}д` : 'прогрев',
      cls: 'bg-blue-500/10 text-blue-700 border-blue-500/20',
    },
    liquidation: { label: 'ликвидация', cls: 'bg-red-600/10 text-red-800 border-red-600/30 dark:text-red-400' },
  }
  const { label, cls } = cfg[status]
  const isToggleable = onToggle && (status === 'auto' || status === 'manual')

  const pillCls = `inline-flex items-center px-1.5 py-px text-[11px] font-medium rounded border whitespace-nowrap ${cls}`

  if (isToggleable) {
    return (
      <button
        type="button"
        onClick={onToggle}
        title={automationEnabled ? 'Выключить автоматику' : 'Включить автоматику'}
        className={`${pillCls} cursor-pointer hover:opacity-75 transition-opacity`}
      >
        {label}
      </button>
    )
  }
  return <span className={pillCls}>{label}</span>
}

function BasketCell({ baskets, norm, source }: { baskets: number; norm: number; source: 'manual' | 'auto' | 'fallback' }) {
  const ratio = norm > 0 ? baskets / norm : 0
  const color =
    ratio >= 1 ? 'text-emerald-600' : ratio >= 0.5 ? 'text-amber-600' : 'text-red-600'
  const barWidth = Math.min(100, ratio * 100)
  const dot =
    source === 'manual'
      ? { cls: 'bg-primary', title: 'Норма задана вручную' }
      : source === 'fallback'
        ? { cls: 'bg-amber-400', title: 'Fallback на период прогрева' }
        : { cls: 'bg-emerald-500/70', title: 'Норма рассчитана автоматически' }
  return (
    <span className="flex items-center gap-1.5">
      <span className="text-xs font-mono inline-flex items-center gap-1">
        <span className={`font-semibold ${color}`}>{baskets}</span>
        <span className="text-muted-foreground/60">/{norm}</span>
        <span className={`inline-block w-1.5 h-1.5 rounded-full ${dot.cls}`} title={dot.title} />
      </span>
      <span className="w-8 h-1 bg-muted rounded-full overflow-hidden flex-shrink-0">
        <span
          className={`h-full rounded-full block ${ratio >= 1 ? 'bg-emerald-500' : ratio >= 0.5 ? 'bg-amber-400' : 'bg-red-400'}`}
          style={{ width: `${barWidth}%` }}
        />
      </span>
    </span>
  )
}

function MarginCell({ row }: { row: Row }) {
  const status = marginStatusAt(
    row.meta.currentPriceKopecks,
    row.settings.cogsKopecks,
    row.settings.wbCommissionPct,
    row.settings.logisticsKopecks,
  )
  const net =
    row.meta.currentPriceKopecks * (1 - row.settings.wbCommissionPct / 100) -
    row.settings.logisticsKopecks -
    row.settings.cogsKopecks
  const pct = (net / row.meta.currentPriceKopecks) * 100
  const color =
    status === 'negative' ? 'text-red-600' : status === 'thin' ? 'text-amber-600' : 'text-emerald-600'
  const value = pct < 0 ? `−${Math.abs(pct).toFixed(1)}%` : `${pct.toFixed(1)}%`
  return (
    <span className={`text-xs font-mono font-semibold ${color}`}>
      {value}
    </span>
  )
}

function PMinCell({ row }: { row: Row }) {
  const pMin = computePMinKopecks(
    row.settings.cogsKopecks,
    row.settings.wbCommissionPct,
    row.settings.logisticsKopecks,
    row.settings.minMarginPct,
  )
  if (!Number.isFinite(pMin)) {
    return <span className="text-amber-500 text-xs">⚠ невалидно</span>
  }
  return <span className="text-xs font-mono text-muted-foreground">{formatRub(pMin)}</span>
}

function PromotionDot({ value, label, promotionId, reason }: {
  value?: 'yes' | 'no' | 'unknown'
  label?: string | null
  promotionId?: string | number | null
  reason?: string | null
}) {
  if (value !== 'yes' && value !== 'no') {
    return (
      <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground" title={reason || 'Нет подтверждённых данных об участии в акциях'}>
        <span className="size-2 rounded-full bg-slate-400" />Нет данных
      </span>
    )
  }
  const active = value === 'yes'
  const labelText = label?.trim()
  const rawLabel = labelText && !/^Участвует:/i.test(labelText) ? labelText : null
  const fallbackLabel = promotionId != null ? `Акция ${promotionId}` : ''
  const promoLabel = active
    ? (rawLabel && rawLabel !== 'В акции' ? rawLabel : fallbackLabel || rawLabel || 'да')
    : (labelText === 'Нет в обычных' || /^Не участвует:/i.test(labelText || '') ? labelText || 'нет' : 'нет')
  const tooltip = reason || (active ? promoLabel : promoLabel === 'Нет в обычных'
    ? 'Не участвует в проверенных обычных акциях. Автоакции не проверены.' : 'Не участвует в акции')
  return (
    <span className="inline-flex max-w-[180px] items-center gap-1.5 text-xs" title={tooltip}>
      <span className={`size-2 rounded-full ${active ? 'bg-emerald-500' : 'bg-red-500'}`} />
      <span className="truncate">{promoLabel}</span>
    </span>
  )
}

// ── Sort header cell ──────────────────────────────────────────────────────────

function SortHeader({
  label,
  col,
  sort,
  onSort,
  className,
}: {
  label: string
  col: SortCol
  sort: SortKey
  onSort: (col: SortCol) => void
  className?: string
}) {
  const isActive = sort !== 'problems' && sort.startsWith(col)
  const isAsc = sort === `${col}-asc`

  return (
    <th
      className={cn(
        'px-3 py-2 text-[11px] font-semibold text-muted-foreground uppercase tracking-wide cursor-pointer select-none hover:text-foreground transition-colors',
        className,
      )}
      onClick={() => onSort(col)}
    >
      <span className="inline-flex items-center gap-1">
        {label}
        {isActive ? (
          isAsc ? (
            <ArrowUp size={11} className="text-primary" />
          ) : (
            <ArrowDown size={11} className="text-primary" />
          )
        ) : (
          <ArrowUpDown size={11} className="opacity-30" />
        )}
      </span>
    </th>
  )
}

// ── Filter chip ───────────────────────────────────────────────────────────────

function FilterChip<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string
  value: T
  options: { value: T; label: string }[]
  onChange: (v: T) => void
}) {
  const isActive = value !== 'all'
  const currentLabel = options.find((o) => o.value === value)?.label ?? label

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          className={cn(
            'inline-flex items-center gap-1 px-2.5 py-1 text-sm border rounded-md transition-all duration-150 whitespace-nowrap',
            isActive
              ? 'bg-primary/10 border-primary/30 text-primary'
              : 'border-border bg-background text-foreground hover:bg-muted/60',
          )}
        >
          {isActive ? currentLabel : label}
          <ChevronDown size={12} className="opacity-50 flex-shrink-0" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="min-w-[140px]">
        {options.map((opt) => (
          <DropdownMenuItem
            key={opt.value}
            onClick={() => onChange(opt.value)}
            className="flex items-center justify-between text-sm"
          >
            {opt.label}
            {value === opt.value && <Check size={12} className="text-primary ml-2" />}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

// ── Pagination helpers ────────────────────────────────────────────────────────

function getPageNumbers(current: number, total: number): (number | '...')[] {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1)
  const pages: (number | '...')[] = [1]
  if (current > 3) pages.push('...')
  for (let i = Math.max(2, current - 1); i <= Math.min(total - 1, current + 1); i++) pages.push(i)
  if (current < total - 2) pages.push('...')
  pages.push(total)
  return pages
}

// ── Main component ────────────────────────────────────────────────────────────

export function SkuListPage({ onOpenCard }: { onOpenCard: (articleId: string) => void }) {
  const [rows, setRows] = useState<Row[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const [typeFilter, setTypeFilter] = useState<TypeFilter>('all')
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all')
  const [marginFilter, setMarginFilter] = useState<MarginFilter>('all')
  const [autoFilter, setAutoFilter] = useState<AutoFilter>('all')
  const [dateRange, setDateRange] = useState<DateRange>(() => defaultDateRange())
  const [search, setSearch] = useState('')
  const [sort, setSort] = useState<SortKey>('problems')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [page, setPage] = useState(0)
  const [toast, setToast] = useState<string | null>(null)
  const toastTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const topScrollRef = useRef<HTMLDivElement | null>(null)
  const tableScrollRef = useRef<HTMLDivElement | null>(null)
  const tableRef = useRef<HTMLTableElement | null>(null)
  const [tableScrollWidth, setTableScrollWidth] = useState(1320)

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError(null)
    fetch('/api/v1/wb-repricer/sku', { signal: controller.signal })
      .then((r) => r.json())
      .then((d: { items: Row[] }) => {
        setRows(d.items)
        setLoading(false)
      })
      .catch((e: unknown) => {
        if (e instanceof Error && e.name === 'AbortError') return
        setError(e instanceof Error ? e.message : 'Ошибка загрузки')
        setLoading(false)
      })
    return () => controller.abort()
  }, [])

  useEffect(() => {
    const updateWidth = () => {
      const tableWidth = tableRef.current?.scrollWidth ?? 0
      const scrollerWidth = tableScrollRef.current?.scrollWidth ?? 0
      setTableScrollWidth(Math.max(1320, tableWidth, scrollerWidth))
    }

    updateWidth()
    const observer = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(updateWidth) : null
    if (observer) {
      if (tableRef.current) observer.observe(tableRef.current)
      if (tableScrollRef.current) observer.observe(tableScrollRef.current)
    }
    window.addEventListener('resize', updateWidth)
    return () => {
      observer?.disconnect()
      window.removeEventListener('resize', updateWidth)
    }
  }, [rows.length, page, sort])

  function syncHorizontalScroll(source: 'top' | 'table') {
    const top = topScrollRef.current
    const table = tableScrollRef.current
    if (!top || !table) return
    const from = source === 'top' ? top : table
    const to = source === 'top' ? table : top
    if (Math.abs(to.scrollLeft - from.scrollLeft) > 1) {
      to.scrollLeft = from.scrollLeft
    }
  }

  function showToast(msg: string) {
    setToast(msg)
    if (toastTimerRef.current) clearTimeout(toastTimerRef.current)
    toastTimerRef.current = setTimeout(() => setToast(null), 3000)
  }

  // Cycle sort: problems → col-desc → col-asc → problems
  function handleColSort(col: SortCol) {
    if (col === 'problems') {
      setSort('problems')
      setPage(0)
      return
    }
    if (sort === `${col}-desc`) {
      setSort(`${col}-asc` as SortKey)
    } else if (sort === `${col}-asc`) {
      setSort('problems')
    } else {
      setSort(`${col}-desc` as SortKey)
    }
    setPage(0)
  }

  const filtered = useMemo(() => {
    let list = rows.filter((row) => {
      if (typeFilter !== 'all' && getProductType(row.meta.articleId) !== typeFilter) return false
      if (statusFilter !== 'all' && row.meta.status !== statusFilter) return false
      if (marginFilter !== 'all') {
        const m = marginStatusAt(
          row.meta.currentPriceKopecks,
          row.settings.cogsKopecks,
          row.settings.wbCommissionPct,
          row.settings.logisticsKopecks,
        )
        if (m !== marginFilter) return false
      }
      const effectiveAuto =
        row.settings.automationEnabled &&
        row.meta.status !== 'warmup'
      if (autoFilter === 'on' && !effectiveAuto) return false
      if (autoFilter === 'off' && effectiveAuto) return false
      if (search) {
        const q = search.toLowerCase()
        if (
          !row.meta.articleId.toLowerCase().includes(q) &&
          !row.meta.name.toLowerCase().includes(q)
        )
          return false
      }
      return true
    })

    list = [...list].sort((a, b) => {
      switch (sort) {
        case 'problems':
          return problemScore(b) - problemScore(a)
        case 'article-asc':
          return a.meta.articleId.localeCompare(b.meta.articleId)
        case 'article-desc':
          return b.meta.articleId.localeCompare(a.meta.articleId)
        case 'price-asc':
          return a.meta.currentPriceKopecks - b.meta.currentPriceKopecks
        case 'price-desc':
          return b.meta.currentPriceKopecks - a.meta.currentPriceKopecks
        case 'pmin-asc': {
          const pMin = (r: Row) =>
            computePMinKopecks(r.settings.cogsKopecks, r.settings.wbCommissionPct, r.settings.logisticsKopecks, r.settings.minMarginPct)
          return pMin(a) - pMin(b)
        }
        case 'pmin-desc': {
          const pMin = (r: Row) =>
            computePMinKopecks(r.settings.cogsKopecks, r.settings.wbCommissionPct, r.settings.logisticsKopecks, r.settings.minMarginPct)
          return pMin(b) - pMin(a)
        }
        case 'margin-asc': {
          const pct = (r: Row) => {
            const net =
              r.meta.currentPriceKopecks * (1 - r.settings.wbCommissionPct / 100) -
              r.settings.logisticsKopecks -
              r.settings.cogsKopecks
            return net / r.meta.currentPriceKopecks
          }
          return pct(a) - pct(b)
        }
        case 'margin-desc': {
          const pct = (r: Row) => {
            const net =
              r.meta.currentPriceKopecks * (1 - r.settings.wbCommissionPct / 100) -
              r.settings.logisticsKopecks -
              r.settings.cogsKopecks
            return net / r.meta.currentPriceKopecks
          }
          return pct(b) - pct(a)
        }
        case 'baskets-asc':
          return a.meta.basketsLast7d - b.meta.basketsLast7d
        case 'baskets-desc':
          return b.meta.basketsLast7d - a.meta.basketsLast7d
        default:
          return 0
      }
    })

    return list
  }, [rows, typeFilter, statusFilter, marginFilter, autoFilter, search, sort])

  const pageRows = filtered.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE)
  const totalPages = Math.ceil(filtered.length / PAGE_SIZE)
  const allPageSelected = pageRows.length > 0 && pageRows.every((r) => selected.has(r.meta.articleId))

  function toggleAll() {
    setSelected((prev) => {
      const next = new Set(prev)
      if (allPageSelected) {
        pageRows.forEach((r) => next.delete(r.meta.articleId))
      } else {
        pageRows.forEach((r) => next.add(r.meta.articleId))
      }
      return next
    })
  }

  function toggleRow(articleId: string) {
    setSelected((prev) => {
      const next = new Set(prev)
      next.has(articleId) ? next.delete(articleId) : next.add(articleId)
      return next
    })
  }

  function bulkSetAuto(enabled: boolean) {
    const ids = Array.from(selected)
    const count = ids.length
    setRows((prev) =>
      prev.map((r) =>
        selected.has(r.meta.articleId)
          ? { ...r, settings: { ...r.settings, automationEnabled: enabled } }
          : r,
      ),
    )
    setSelected(new Set())
    Promise.all(
      ids.map((id) =>
        fetch(`/api/v1/wb-repricer/sku/${id}/automation`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ automationEnabled: enabled }),
        }),
      ),
    ).catch(() => showToast('Ошибка при сохранении автоматики'))
    showToast(`Автоматика ${enabled ? 'включена' : 'выключена'} для ${count} артикул${count === 1 ? 'а' : 'ов'}`)
  }

  function toggleAuto(articleId: string, value: boolean) {
    setRows((prev) =>
      prev.map((r) =>
        r.meta.articleId === articleId
          ? { ...r, settings: { ...r.settings, automationEnabled: value } }
          : r,
      ),
    )
    fetch(`/api/v1/wb-repricer/sku/${articleId}/automation`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ automationEnabled: value }),
    }).catch(() => showToast(`Ошибка сохранения · ${articleId}`))
    showToast(`Автоматика ${value ? 'включена' : 'выключена'} · ${articleId}`)
  }

  function resetFilters() {
    setSearch('')
    setTypeFilter('all')
    setStatusFilter('all')
    setMarginFilter('all')
    setAutoFilter('all')
    setSort('problems')
    setPage(0)
  }

  const hasActiveFilters =
    search || typeFilter !== 'all' || statusFilter !== 'all' || marginFilter !== 'all' || autoFilter !== 'all'

  if (loading) {
    return (
      <div className="p-8 flex items-center justify-center">
        <div className="text-sm text-muted-foreground">Загрузка артикулов…</div>
      </div>
    )
  }

  if (error) {
    return (
      <div className="p-8 flex flex-col items-center gap-3 text-center">
        <div className="text-sm font-medium text-red-600 dark:text-red-400">Ошибка загрузки артикулов</div>
        <div className="text-xs text-muted-foreground">{error}</div>
        <button
          type="button"
          onClick={() => {
            setError(null)
            setLoading(true)
            const controller = new AbortController()
            fetch('/api/v1/wb-repricer/sku', { signal: controller.signal })
              .then((r) => r.json())
              .then((d: { items: Row[] }) => {
                setRows(d.items)
                setLoading(false)
              })
              .catch((e: unknown) => {
                if (e instanceof Error && e.name === 'AbortError') return
                setError(e instanceof Error ? e.message : 'Ошибка загрузки')
                setLoading(false)
              })
          }}
          className="text-xs text-primary hover:underline mt-1"
        >
          Повторить
        </button>
      </div>
    )
  }

  return (
    <div className="flex flex-col h-full">
      {/* Toast */}
      {toast && (
        <div className="fixed bottom-20 left-1/2 -translate-x-1/2 z-50 bg-foreground text-background text-sm px-4 py-2 rounded-lg shadow-lg pointer-events-none">
          {toast}
        </div>
      )}

      <div className="border-b border-border px-4 py-3">
        <div className="flex flex-col gap-2 lg:flex-row lg:items-center lg:justify-between">
          <div>
            <div className="text-sm font-semibold text-foreground">Период аналитики</div>
            <div className="text-xs text-muted-foreground">
              ABC, акция, остатки, корзины, заказы и средняя цена считаются за выбранный период.
            </div>
          </div>
          <DateRangeControl value={dateRange} onChange={setDateRange} />
        </div>
      </div>

      {/* Toolbar */}
      <div className="px-4 py-2.5 border-b border-border flex items-center gap-2 flex-wrap">
        {/* Search */}
        <div className="relative">
          <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground/50 pointer-events-none" />
          <input
            type="search"
            placeholder="Артикул или название…"
            value={search}
            onChange={(e) => { setSearch(e.target.value); setPage(0) }}
            className="pl-8 pr-3 py-1 text-sm border border-border rounded-md w-48 focus:outline-none focus:ring-1 focus:ring-primary/40 focus:border-primary/40 bg-background"
          />
        </div>

        {/* Filter chips */}
        <FilterChip<TypeFilter>
          label="Тип"
          value={typeFilter}
          onChange={(v) => { setTypeFilter(v); setPage(0) }}
          options={[
            { value: 'all', label: 'Все типы' },
            { value: 'F', label: 'Футболки' },
            { value: 'H', label: 'Худи' },
            { value: 'L', label: 'Лонгсливы' },
          ]}
        />

        <FilterChip<StatusFilter>
          label="Статус"
          value={statusFilter}
          onChange={(v) => { setStatusFilter(v); setPage(0) }}
          options={[
            { value: 'all', label: 'Все статусы' },
            { value: 'auto', label: 'Авто' },
            { value: 'manual', label: 'Ручной' },
            { value: 'warmup', label: 'Прогрев' },
            { value: 'liquidation', label: 'Ликвидация' },
          ]}
        />

        <FilterChip<MarginFilter>
          label="Маржа"
          value={marginFilter}
          onChange={(v) => { setMarginFilter(v); setPage(0) }}
          options={[
            { value: 'all', label: 'Любая маржа' },
            { value: 'negative', label: 'Отрицательная' },
            { value: 'thin', label: 'Тонкая (0–10%)' },
            { value: 'ok', label: 'Нормальная (≥10%)' },
          ]}
        />

        <FilterChip<AutoFilter>
          label="Авто"
          value={autoFilter}
          onChange={(v) => { setAutoFilter(v); setPage(0) }}
          options={[
            { value: 'all', label: 'Авто: все' },
            { value: 'on', label: 'Включена' },
            { value: 'off', label: 'Выключена' },
          ]}
        />

        {hasActiveFilters && (
          <button
            type="button"
            onClick={resetFilters}
            className="inline-flex items-center gap-1 px-2 py-1 text-xs text-muted-foreground hover:text-foreground transition-colors"
          >
            <X size={11} />
            Сбросить
          </button>
        )}

        <span className="ml-auto text-xs text-muted-foreground">
          {filtered.length} из {rows.length}
        </span>
      </div>

      {/* Table */}
      <div
        ref={topScrollRef}
        onScroll={() => syncHorizontalScroll('top')}
        className="h-4 overflow-x-auto overflow-y-hidden border-t border-border bg-background"
        aria-label="Горизонтальная прокрутка таблицы товаров"
      >
        <div style={{ width: tableScrollWidth, height: 1 }} />
      </div>
      <div ref={tableScrollRef} onScroll={() => syncHorizontalScroll('table')} className="flex-1 overflow-auto">
        <table ref={tableRef} className="w-full text-left border-collapse min-w-[1320px]">
          <thead className="sticky top-0 bg-background z-10">
            <tr className="border-b border-border">
              <th className="px-4 py-2 w-8">
                <input type="checkbox" checked={allPageSelected} onChange={toggleAll} className="rounded" />
              </th>
              <SortHeader
                label="Артикул"
                col="article"
                sort={sort}
                onSort={handleColSort}
                className="w-44"
              />
              <th className="px-3 py-2 text-[11px] font-semibold text-muted-foreground uppercase tracking-wide">
                Название
              </th>
              <th
                className="px-3 py-2 text-[11px] font-semibold text-muted-foreground uppercase tracking-wide cursor-pointer select-none hover:text-foreground transition-colors w-32"
                onClick={() => handleColSort('problems')}
              >
                <span className="inline-flex items-center gap-1">
                Статус
                  {sort === 'problems' ? (
                    <ArrowDown size={11} className="text-primary" />
                  ) : (
                    <ArrowUpDown size={11} className="opacity-30" />
                  )}
                </span>
              </th>
              <th className="px-3 py-2 text-[11px] font-semibold text-muted-foreground uppercase tracking-wide w-16 text-center">
                ABC
              </th>
              <th className="px-3 py-2 text-[11px] font-semibold text-muted-foreground uppercase tracking-wide w-20 text-center">
                Акция
              </th>
              <SortHeader label="Цена" col="price" sort={sort} onSort={handleColSort} className="w-28 text-right" />
              <th className="px-3 py-2 text-[11px] font-semibold text-muted-foreground uppercase tracking-wide w-28 text-right">
                Ср. с СПП
              </th>
              <SortHeader label="Мин. цена" col="pmin" sort={sort} onSort={handleColSort} className="w-24 text-right" />
              <SortHeader label="Маржа" col="margin" sort={sort} onSort={handleColSort} className="w-20 text-right" />
              <th className="px-3 py-2 text-[11px] font-semibold text-muted-foreground uppercase tracking-wide w-24 text-right">
                Комиссия
              </th>
              <th className="px-3 py-2 text-[11px] font-semibold text-muted-foreground uppercase tracking-wide w-24 text-right">
                Выкуп
              </th>
              <th className="px-3 py-2 text-[11px] font-semibold text-muted-foreground uppercase tracking-wide w-28 text-right">
                Остаток WB
              </th>
              <th className="px-3 py-2 text-[11px] font-semibold text-muted-foreground uppercase tracking-wide w-24 text-right">
                Заказы
              </th>
              <SortHeader label="Корзины" col="baskets" sort={sort} onSort={handleColSort} className="w-36" />
            </tr>
          </thead>
          <tbody>
            {pageRows.length === 0 && (
              <tr>
                <td colSpan={15} className="px-6 py-12 text-center text-sm text-muted-foreground">
                  Ничего не найдено.{' '}
                  <button type="button" onClick={resetFilters} className="text-primary hover:underline">
                    Сбросить фильтры
                  </button>
                </td>
              </tr>
            )}
            {pageRows.map((row) => (
              <tr
                key={row.meta.articleId}
                className={cn(
                  'border-b border-border/60 hover:bg-muted/40 transition-colors duration-100',
                  rowPriorityClass(row),
                )}
              >
                <td className="px-4 py-1.5">
                  <input
                    type="checkbox"
                    checked={selected.has(row.meta.articleId)}
                    onChange={() => toggleRow(row.meta.articleId)}
                    className="rounded"
                  />
                </td>

                {/* Артикул + фото */}
                <td className="px-3 py-1.5">
                  <span className="flex items-center gap-2">
                    <button
                      type="button"
                      onClick={() => onOpenCard(row.meta.articleId)}
                      className="shrink-0 size-10 rounded border border-border bg-muted overflow-hidden hover:border-primary/40 transition-colors"
                      title={`Открыть ${row.meta.articleId}`}
                    >
                      <img
                        src={skuImageUrl(row.meta)}
                        alt={row.meta.name}
                        loading="lazy"
                        className="w-full h-full object-cover"
                        onError={(e) => {
                          const img = e.currentTarget as HTMLImageElement
                          const fallback = placeholderSvg(row.meta.articleId)
                          if (img.src !== fallback) img.src = fallback
                        }}
                      />
                    </button>
                    <span className="flex items-center gap-1 min-w-0">
                      <span className="font-mono text-xs text-foreground font-semibold truncate">
                        {row.meta.articleId}
                      </span>
                    </span>
                  </span>
                </td>

                {/* Название */}
                <td className="px-3 py-1.5">
                  <button
                    type="button"
                    onClick={() => onOpenCard(row.meta.articleId)}
                    className="text-sm text-foreground hover:text-primary transition-colors truncate block max-w-xs text-left"
                    title={row.meta.name}
                  >
                    {row.meta.name}
                  </button>
                </td>

                {/* Статус */}
                <td className="px-3 py-1.5">
                  <StatusPill
                    status={row.meta.status}
                    warmupDaysLeft={row.meta.warmupDaysLeft}
                    automationEnabled={row.settings.automationEnabled}
                    onToggle={() => toggleAuto(row.meta.articleId, !row.settings.automationEnabled)}
                  />
                </td>

                <td className="px-3 py-1.5 text-center">
                  <span className="inline-flex rounded border border-primary/20 bg-primary/10 px-1.5 py-px font-mono text-[11px] font-semibold text-primary">
                    {row.analytics?.abcCode ?? '—'}
                  </span>
                </td>

                <td className="px-3 py-1.5 text-center">
                  <PromotionDot value={row.analytics?.promotionStatus} label={row.analytics?.promotionStatusText || row.analytics?.promotionName} promotionId={row.analytics?.promotionId} reason={row.analytics?.promotionStatusReason} />
                </td>

                {/* Цена */}
                <td className="px-3 py-1.5 text-right">
                  <span className="text-xs font-mono font-semibold text-foreground">
                    {formatRub(row.meta.currentPriceKopecks)}
                  </span>
                </td>

                <td className="px-3 py-1.5 text-right">
                  <span className="text-xs font-mono text-muted-foreground">
                    {row.analytics ? formatRub(row.analytics.avgPriceWithSppKopecks) : '—'}
                  </span>
                </td>

                {/* P_min */}
                <td className="px-3 py-1.5 text-right">
                  <PMinCell row={row} />
                </td>

                {/* Маржа */}
                <td className="px-3 py-1.5 text-right">
                  {row.analytics ? (
                    <span className={`text-xs font-mono font-semibold ${row.analytics.marginPct < 10 ? 'text-amber-600' : 'text-emerald-600'}`}>
                      {row.analytics.marginPct.toFixed(1)}%
                    </span>
                  ) : (
                    <MarginCell row={row} />
                  )}
                </td>

                <td className="px-3 py-1.5 text-right text-xs font-mono text-muted-foreground">
                  {row.analytics ? `${row.analytics.wbCommissionPct.toFixed(1)}%` : `${row.settings.wbCommissionPct}%`}
                </td>

                <td className="px-3 py-1.5 text-right text-xs font-mono text-muted-foreground">
                  {row.analytics ? `${row.analytics.buyoutPct.toFixed(1)}%` : '—'}
                </td>

                <td className="px-3 py-1.5 text-right text-xs font-mono text-muted-foreground">
                  {row.analytics?.wbStockUnits.toLocaleString('ru-RU') ?? '—'}
                </td>

                <td className="px-3 py-1.5 text-right text-xs font-mono text-muted-foreground">
                  {row.analytics?.ordersUnits.toLocaleString('ru-RU') ?? '—'}
                </td>

                {/* Корзины */}
                <td className="px-3 py-1.5">
                  <BasketCell
                    baskets={row.analytics?.baskets ?? row.meta.basketsLast7d}
                    norm={
                      row.settings.basketNormMode === 'manual' && row.settings.basketNormManual !== null
                        ? row.settings.basketNormManual
                        : row.meta.basketNorm
                    }
                    source={row.settings.basketNormMode === 'manual' ? 'manual' : row.meta.basketNormSource}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="px-4 py-2.5 border-t border-border flex items-center gap-1.5 text-sm">
          <button
            type="button"
            onClick={() => setPage((p) => Math.max(0, p - 1))}
            disabled={page === 0}
            className="px-2 py-1 rounded border border-border disabled:opacity-40 hover:bg-muted/60 text-sm"
          >
            ←
          </button>
          {getPageNumbers(page + 1, totalPages).map((p, idx) =>
            p === '...'
              ? <span key={`ellipsis-${idx}`} className="px-2 py-1 text-muted-foreground text-sm">…</span>
              : <button
                  key={p}
                  type="button"
                  onClick={() => setPage((p as number) - 1)}
                  className={cn(
                    'px-2.5 py-1 rounded border text-sm',
                    (p as number) - 1 === page
                      ? 'bg-primary text-primary-foreground border-primary'
                      : 'border-border hover:bg-muted/60',
                  )}
                >{p}</button>
          )}
          <button
            type="button"
            onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
            disabled={page === totalPages - 1}
            className="px-2 py-1 rounded border border-border disabled:opacity-40 hover:bg-muted/60 text-sm"
          >
            →
          </button>
          <span className="ml-auto text-xs text-muted-foreground">
            {page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, filtered.length)} из {filtered.length}
          </span>
        </div>
      )}

      {/* Floating bulk action bar */}
      {selected.size > 0 && (
        <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-50 flex items-center gap-3 bg-foreground text-background px-4 py-2.5 rounded-xl shadow-xl">
          <span className="text-sm font-medium">
            Выбрано: {selected.size}
          </span>
          <div className="w-px h-4 bg-background/20" />
          <button
            type="button"
            onClick={() => bulkSetAuto(true)}
            className="text-sm px-3 py-1 rounded-md bg-emerald-500 text-white hover:bg-emerald-400 transition-colors"
          >
            Включить авто
          </button>
          <button
            type="button"
            onClick={() => bulkSetAuto(false)}
            className="text-sm px-3 py-1 rounded-md bg-background/10 hover:bg-background/20 transition-colors"
          >
            Выключить авто
          </button>
          <button
            type="button"
            onClick={() => setSelected(new Set())}
            className="ml-1 p-1 rounded hover:bg-background/20 transition-colors"
            title="Снять выделение"
          >
            <X size={14} />
          </button>
        </div>
      )}
    </div>
  )
}
