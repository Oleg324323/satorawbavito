import { useCallback, useEffect, useMemo, useState } from 'react'
import { authorizationHeaders } from '@/features/auth/authApi'
import { reportRequest } from '@/features/wb-reports/reportRequest'
import { PnlFilterPanel } from './PnlFilterPanel'
import { createPnlFilterMatcher, defaultPnlFilters, pnlActiveFilterCount, pnlRanges,
  type PnlFilterMetadata, type PnlFilterRow, type PnlFilters } from './pnlFilters'

export type ReportFilterTab = 'pnl' | 'abc' | 'rnp' | 'ads' | 'stock' | 'week'

/** The Repricer product filter for any WB report tab: same attributes, same endpoint, client-side selection only. */
export function useReportProductFilters({ tab, rows, accessToken, enabled, dateFrom, dateTo, scope, currentManagerId,
  expectedAccountId, label, description, withMargin = true, onClear }: {
  tab: ReportFilterTab; rows: PnlFilterRow[]; accessToken: string | null | undefined; enabled: boolean
  dateFrom: string; dateTo: string; scope: string; currentManagerId: string; expectedAccountId?: number | null
  label: string; description: string; withMargin?: boolean; onClear?: () => void
}) {
  const [filters, setFilters] = useState(defaultPnlFilters)
  const [open, setOpen] = useState(false)
  const [published, setPublished] = useState<{ scope: string; data?: PnlFilterMetadata; error?: string } | null>(null)
  const triggerId = `${tab}-filters-trigger`
  const close = useCallback(() => { setOpen(false); document.getElementById(triggerId)?.focus() }, [triggerId])
  const activeCount = pnlActiveFilterCount(filters)
  const nmIds = JSON.stringify([...new Set(rows.map(row => Number(row.nmId)).filter(nm => Number.isSafeInteger(nm) && nm > 0))].sort((a, b) => a - b))
  const metadataScope = JSON.stringify([scope, dateFrom, dateTo, nmIds])
  const metadata = published?.scope === metadataScope ? published.data ?? null : null
  const metadataError = published?.scope === metadataScope ? published.error ?? '' : ''
  const needsMetadata = open || activeCount > 0
  const metadataLoading = needsMetadata && !metadata && !metadataError
  useEffect(() => {
    if (!enabled || !accessToken || !needsMetadata || published?.scope === metadataScope) return
    const controller = new AbortController()
    void reportRequest<PnlFilterMetadata>('/api/wb/reports/pnl/filter-metadata', {
      method: 'POST', headers: { ...authorizationHeaders(accessToken), 'Content-Type': 'application/json' },
      body: JSON.stringify({ dateFrom, dateTo, nmIds: JSON.parse(nmIds) }), signal: controller.signal,
    }).then(data => {
      if (!data || data.dateFrom !== dateFrom || data.dateTo !== dateTo
        || !Number.isSafeInteger(data.marketplaceAccountId) || data.marketplaceAccountId <= 0 || !data.products
        || expectedAccountId && data.marketplaceAccountId !== expectedAccountId) throw new Error('Атрибуты фильтров не соответствуют выбранному аккаунту и периоду.')
      if (!controller.signal.aborted) setPublished({ scope: metadataScope, data })
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setPublished({ scope: metadataScope, error: error instanceof Error ? error.message : 'Не удалось прочитать атрибуты фильтров.' })
    })
    return () => controller.abort()
  }, [accessToken, dateFrom, dateTo, enabled, expectedAccountId, metadataScope, needsMetadata, nmIds, published?.scope])
  const matches = useMemo(() => {
    const matcher = createPnlFilterMatcher(filters, currentManagerId)
    return (row: PnlFilterRow) => matcher(row, metadata?.products[String(row.nmId)])
  }, [currentManagerId, filters, metadata])
  const clear = () => { setFilters(defaultPnlFilters()); onClear?.(); close() }
  const trigger = <button id={triggerId} className="btn btn-default btn-sm products-filter-trigger" type="button"
    data-vella-react-handlers="onclick" data-tip="Фильтры товаров, как в репрайсере"
    aria-expanded={open} aria-controls={`${tab}-product-filters`} onClick={() => setOpen(current => !current)}>
    Фильтры{activeCount > 0 && <span className="products-filter-trigger-count">{activeCount}</span>}
  </button>
  const panel = <>
    {open && <PnlFilterPanel tab={tab} label={label} description={description} filters={filters} rows={rows} metadata={metadata}
      currentManagerId={currentManagerId} loading={metadataLoading} error={metadataError} onClose={close} onClear={clear} onRetry={() => setPublished(null)}
      ranges={withMargin ? pnlRanges : pnlRanges.filter(([key]) => key !== 'margin')}
      onApply={(next: PnlFilters) => { setFilters(next); close() }} />}
    {!open && needsMetadata && metadataError && <p role="alert" className="report-product-filters-status">{metadataError} <button type="button" className="btn btn-default btn-sm" onClick={() => setPublished(null)}>Повторить загрузку фильтров</button></p>}
    {!open && activeCount > 0 && metadataLoading && <p role="status" className="report-product-filters-status">Читаем сохранённые атрибуты для фильтрации…</p>}
  </>
  return { filters, setFilters, activeCount, metadata, metadataError, metadataLoading, matches, trigger, panel }
}
