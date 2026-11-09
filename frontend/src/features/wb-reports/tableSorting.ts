import { useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent, type MouseEvent } from 'react'

export type WbSortValue = string | number | null | undefined
export type WbTableSort = { column: number; direction: 'asc' | 'desc'; key?: string } | null
// Remember the user's selection when a loading state temporarily unmounts a
// table. Mounted React state remains the only controller of row ordering.
const rememberedSorts = new Map<string, WbTableSort>()
const collator = new Intl.Collator('ru', { numeric: true, sensitivity: 'base' })
const missing = (value: WbSortValue) => value == null || typeof value === 'string' && /^(?:\s*|—|–|-|нет данных|н\/д)$/i.test(value.trim()) || typeof value === 'number' && !Number.isFinite(value)

export function compareWbValues(left: WbSortValue, right: WbSortValue, direction: 'asc' | 'desc') {
  if (missing(left) || missing(right)) return missing(left) === missing(right) ? 0 : missing(left) ? 1 : -1
  const result = typeof left === 'number' && typeof right === 'number' ? left - right : collator.compare(String(left), String(right))
  return direction === 'asc' ? result : -result
}

// Compatibility only for legacy formatted fields. Never join digits from the
// secondary amount, percentage, trend, date or SKU in the same cell.
export function firstWbNumber(value: unknown): number | null {
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  const match = String(value ?? '').trim().match(/^[↑↓]?\s*([+−–-]?\s*\d[\d\s\u00a0\u202f]*(?:[.,]\d+)?)/)
  if (!match) return null
  const number = Number(match[1].replace(/[\s\u00a0\u202f]/g, '').replace(/[−–]/g, '-').replace(',', '.'))
  return Number.isFinite(number) ? number : null
}

export function sortWbRows<T>(rows: readonly T[], sort: WbTableSort, value: (row: T, column: number) => WbSortValue): T[] {
  if (!sort) return [...rows]
  // Extract once per row, not repeatedly during O(n log n) comparisons.
  return rows.map((row, index) => ({ row, index, value: value(row, sort.column) }))
    .sort((a, b) => compareWbValues(a.value, b.value, sort.direction) || a.index - b.index).map(item => item.row)
}

export function isWbHeaderHelp(target: EventTarget | null) {
  return target instanceof Element && !!target.closest('.stat-tip, .tip-i, .tip-box, .report-help-tip, .rnp-help-tip, button, input, a')
}

export function useWbTableSorting<T>(source: readonly T[], value: (row: T, column: number) => WbSortValue, scope?: string, columnKeys?: readonly string[]) {
  const [selectedSort, setSort] = useState<WbTableSort>(() => scope ? rememberedSorts.get(scope) ?? null : null)
  const column = selectedSort?.key && columnKeys ? columnKeys.indexOf(selectedSort.key) : selectedSort?.column
  const sort = useMemo(() => selectedSort && column != null && column >= 0 ? { ...selectedSort, column } : null, [selectedSort, column])
  const headerRef = useRef<HTMLTableSectionElement>(null)
  const rows = useMemo(() => sortWbRows(source, sort, value), [source, sort, value])
  useLayoutEffect(() => {
    headerRef.current?.querySelectorAll<HTMLTableCellElement>('th').forEach((header, column) => {
      const label = header.cloneNode(true) as HTMLElement
      label.querySelectorAll('.stat-tip, .tip-i, .tip-box, .sort-arrow').forEach(node => node.remove())
      const key = columnKeys?.[column] ?? label.textContent?.trim() ?? ''
      header.dataset.wbSortKey = key
      header.dataset.sortBound = 'react'
      header.tabIndex = 0
      const active = sort?.column === column
      header.setAttribute('aria-sort', active ? sort.direction === 'asc' ? 'ascending' : 'descending' : 'none')
      header.classList.toggle('sorted', active)
      let arrow = header.querySelector<HTMLElement>('.sort-arrow')
      if (!arrow) { arrow = document.createElement('span'); arrow.className = 'sort-arrow'; arrow.setAttribute('aria-hidden', 'true'); header.append(' ', arrow) }
      arrow.textContent = active ? sort.direction === 'asc' ? '↑' : '↓' : '↕'
    })
  })
  const activate = (event: MouseEvent<HTMLTableSectionElement> | KeyboardEvent<HTMLTableSectionElement>) => {
    if ('key' in event && event.key !== 'Enter' && event.key !== ' ') return
    if (isWbHeaderHelp(event.target)) return
    const header = (event.target as Element).closest('th')
    if (!header || header.parentElement?.parentElement !== event.currentTarget) return
    event.preventDefault(); event.stopPropagation()
    const column = header.cellIndex
    setSort(current => {
      const key = header.dataset.wbSortKey
      const next: WbTableSort = { column, key, direction: current?.key === key && current?.direction === 'desc' ? 'asc' : 'desc' }
      if (scope) rememberedSorts.set(scope, next)
      return next
    })
  }
  return { rows, sort, headerProps: { ref: headerRef, 'data-wb-sort-owner': 'react', onClickCapture: activate, onKeyDownCapture: activate } }
}
