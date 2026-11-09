import { ApiError, buildApiUrl } from '@/lib/api'

export type AdsTableExportPayload = {
  reportKind: 'ads'; sourceKind: 'loaded-ads'; dateFrom: string; dateTo: string
  headers: string[]; rows: Array<Array<string | number | null>>
  sourceState: string; warning: string; filterDescription: string
}
type Period = { fromIso: string; toIso: string }
export const ADS_XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
export const ADS_ROMI_DESCRIPTION = 'ROMI по рекламным заказам = (сумма рекламных заказов − расход рекламы) / расход рекламы × 100%. Это отдача по заказам, не чистая прибыль по выкупам: себестоимость и комиссии здесь не вычитаются. При нулевом расходе или неполных данных — прочерк.'
export const ADS_DRR_DESCRIPTION = 'ДРР = расход рекламы / сумма рекламных заказов × 100%. Обе суммы за выбранный период. Итог считается по суммам, не как среднее процентов товаров.'

export function advertisingDrrPct(orderRevenueKopecks: number | null, spendKopecks: number | null): number | null {
  if (orderRevenueKopecks == null || spendKopecks == null || !Number.isFinite(orderRevenueKopecks)
    || !Number.isFinite(spendKopecks) || orderRevenueKopecks <= 0) return null
  const result = spendKopecks / orderRevenueKopecks * 100
  return Number.isFinite(result) ? result : null
}

export function campaignExternalId(value: unknown): string | null {
  const text = String(value ?? '').trim()
  return /^[1-9]\d*$/.test(text) ? text : null
}

export function adsCampaignIds(row: { campaignId?: unknown; campaignIds?: unknown; directCampaignIds?: unknown; attributedCampaignIds?: unknown }): string[] {
  // Older/cached product aggregates may carry only the separately verified
  // membership/attribution IDs. Do not lose them just because the union is absent.
  const ids = [row.campaignId, ...[row.campaignIds, row.directCampaignIds, row.attributedCampaignIds]
    .flatMap(values => Array.isArray(values) ? values : [])]
  return [...new Set(ids
    .map(campaignExternalId).filter((id): id is string => id !== null))].sort((a, b) => a.length - b.length || a.localeCompare(b))
}

// Match the existing Advertising report contract: return on attributed order
// revenue, not confirmed buyout profit. Missing data and zero spend are undefined.
export function advertisingRomiPct(orderRevenueKopecks: number | null, spendKopecks: number | null): number | null {
  if (orderRevenueKopecks == null || spendKopecks == null || !Number.isFinite(orderRevenueKopecks)
    || !Number.isFinite(spendKopecks) || spendKopecks <= 0) return null
  const result = (orderRevenueKopecks - spendKopecks) / spendKopecks * 100
  return Number.isFinite(result) ? result : null
}

export function observeAdsStickyProductWidth(wrap: HTMLElement) {
  const header = wrap.querySelector<HTMLElement>('thead th:first-child')
  if (!header) return () => undefined
  const sync = () => {
    const width = header.getBoundingClientRect().width
    if (width <= 0) return
    const value = `${width}px`
    if (wrap.style.getPropertyValue('--ads-product-width') !== value) wrap.style.setProperty('--ads-product-width', value)
  }
  sync()
  const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(sync)
  observer?.observe(header)
  window.addEventListener('resize', sync)
  return () => { observer?.disconnect(); window.removeEventListener('resize', sync) }
}

export function buildAdsTableExport(table: Pick<AdsTableExportPayload, 'headers' | 'rows'>, report: {
  filters?: { dateRange?: { from?: string; to?: string } }; cache?: { fresh?: boolean } | null; warning?: string
  meta?: { freshnessState?: string | null; lastUpdatedAt?: string | null } | null; performanceComplete?: boolean
  kpis?: Array<{ id?: string; hint?: string | null }> | null
}, period: Period, loadedPeriod: Period, filterDescription: string): AdsTableExportPayload {
  const range = report.filters?.dateRange
  if (period.fromIso !== loadedPeriod.fromIso || period.toIso !== loadedPeriod.toIso
    || range?.from?.slice(0, 10) !== period.fromIso || range?.to?.slice(0, 10) !== period.toIso) {
    throw new Error('Дождитесь загрузки рекламы за выбранный период и повторите экспорт.')
  }
  return { ...table, reportKind: 'ads', sourceKind: 'loaded-ads', dateFrom: period.fromIso, dateTo: period.toIso,
    sourceState: report.cache?.fresh === false ? 'stale' : report.performanceComplete !== true ? 'partial' : report.meta?.freshnessState ?? 'unknown',
    warning: [report.warning, report.meta?.lastUpdatedAt ? `Наблюдение: ${report.meta.lastUpdatedAt}` : null,
      'Текущие бюджеты и состав кампаний не являются исторической активностью. Атрибуция не подтверждает прямое участие.',
      ...(report.kpis ?? []).filter(kpi => ['campaign_budget', 'cabinet_balance'].includes(kpi.id ?? '')).map(kpi => kpi.hint),
      ADS_ROMI_DESCRIPTION, ADS_DRR_DESCRIPTION].filter(Boolean).join(' · '), filterDescription }
}

export async function downloadAdsTableXlsx(accessToken: string, payload: AdsTableExportPayload, signal?: AbortSignal) {
  signal?.throwIfAborted()
  if (!accessToken || payload.rows.length > 10000 || payload.headers.length > 32
    || payload.rows.some(row => row.length !== payload.headers.length || row.some(value => value !== null &&
      (typeof value === 'number' ? !Number.isFinite(value) : typeof value !== 'string' || value.length > 4096)))) {
    throw new Error('Некорректная или слишком большая таблица рекламы. Уточните фильтры.')
  }
  const body = JSON.stringify(payload)
  if (new TextEncoder().encode(body).byteLength > 5 * 1024 * 1024) throw new Error('Экспорт превышает 5 МиБ. Уточните фильтры.')
  const response = await fetch(buildApiUrl('/api/wb/reports/ads/table.xlsx'), { method: 'POST', credentials: 'include', signal,
    headers: { Authorization: `Bearer ${accessToken}`, 'Content-Type': 'application/json', Accept: ADS_XLSX_MIME }, body })
  if (!response.ok) throw new ApiError(`Не удалось выгрузить рекламу (HTTP ${response.status}).`, response.status)
  if (response.headers.get('content-type') !== ADS_XLSX_MIME) throw new Error('Сервер вернул не XLSX.')
  const blob = await response.blob()
  signal?.throwIfAborted()
  return blob
}

// Scoped to Advertising. Native sticky cells do not require scroll event
// transforms and work for all product/campaign rows and wrapped text.
export const ADS_STICKY_CSS = `
.vella-html-parity-root #tab-ads .report-table-wrap { position: relative; overflow-x: auto; }
.vella-html-parity-root #tab-ads .report-table-wrap table.report-mid { overflow: visible; }
.vella-html-parity-root #tab-ads table.report-mid th:first-child,
.vella-html-parity-root #tab-ads table.report-mid td:first-child,
.vella-html-parity-root #tab-ads table.report-mid th:nth-child(2),
.vella-html-parity-root #tab-ads table.report-mid td:nth-child(2) {
  position: sticky; left: 0; transform: none; z-index: 9;
  background: var(--white);
}
.vella-html-parity-root #tab-ads table.report-mid th:nth-child(2),
.vella-html-parity-root #tab-ads table.report-mid td:nth-child(2) {
  left: var(--ads-product-width, 360px); box-shadow: 8px 0 14px rgba(15,23,42,.06);
}
.vella-html-parity-root #tab-ads table.report-mid thead th:nth-child(-n+2) { z-index: 25; background: var(--gray-50); }
.vella-html-parity-root #tab-ads table.report-mid tr:hover td:nth-child(-n+2) { background: var(--gray-50); }
.vella-html-parity-root .vella-sticky-table-head[data-vella-sticky-tab="tab-ads"] table.report-mid th:nth-child(2) {
  z-index: 25; transform: translateX(var(--ads-scroll-left, 0px));
  box-shadow: 8px 0 14px rgba(15,23,42,.06), inset 0 -1px 0 var(--gray-200);
}
.vella-html-parity-root #tab-ads .ads-campaign-ids { min-width: 150px; max-width: 240px; white-space: normal; overflow-wrap: anywhere; }
`
