import { ApiError, buildApiUrl } from '@/lib/api'
import { type CanonicalCompatibilityMeta, type CanonicalPeriod } from './canonicalAbcPnl'

export const XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
export const PNL_TABLE_EXPORT_HEADERS = ['Товар', 'Категория', 'Выручка', 'Себестоимость', 'Комиссия', 'Логистика', 'Хранение', 'Штрафы, ₽', 'Платная приёмка, ₽', 'Реклама', 'Налог', 'Прибыль', 'Финальная маржа', 'Комментарий']
type Cell = string | number | null
export type ReportTableExportPayload = {
  reportKind: 'abc' | 'pnl'
  marketplaceAccountId: number
  dateFrom: string
  dateTo: string
  headers: string[]
  rows: Cell[][]
  source: ReturnType<typeof reportTableSource>
}

export type LoadedPnlTableExportPayload = {
  reportKind: 'pnl'
  sourceKind: 'loaded-pnl'
  dateFrom: string
  dateTo: string
  headers: string[]
  rows: Cell[][]
  sourceState: string
  warning: string
  filterDescription: string
}
export type PnlTableExportPayload = ReportTableExportPayload | LoadedPnlTableExportPayload

type LoadedPnlReport = {
  meta?: { dateRange?: { from?: string | null; to?: string | null } | null; freshnessState?: string | null; sourceType?: string | null } | null
  filters?: { dateRange?: { from?: string | null; to?: string | null } | null } | null
  cache?: { requestedRange?: { from?: string | null; to?: string | null } | null; fresh?: boolean } | null
  sourceStatus?: string | null
  warning?: string | null
  blockerIds?: string[] | null
}

export function buildLoadedPnlTableExport({ report, rows, period, loadedPeriod, filterDescription }: {
  report: LoadedPnlReport; rows: PnlRow[]; period: CanonicalPeriod; loadedPeriod: CanonicalPeriod; filterDescription: string
}): LoadedPnlTableExportPayload {
  const range = report.filters?.dateRange ?? report.cache?.requestedRange ?? report.meta?.dateRange
  if (period.fromIso !== loadedPeriod.fromIso || period.toIso !== loadedPeriod.toIso
    || range?.from?.slice(0, 10) !== period.fromIso || range?.to?.slice(0, 10) !== period.toIso
    || report.meta?.sourceType && report.meta.sourceType !== 'financial') {
    throw new Error('Дождитесь загрузки P&L за выбранный период и повторите экспорт.')
  }
  return {
    reportKind: 'pnl', sourceKind: 'loaded-pnl', dateFrom: period.fromIso, dateTo: period.toIso,
    headers: PNL_TABLE_EXPORT_HEADERS, rows: buildPnlTableRows(rows),
    sourceState: report.cache?.fresh === false ? 'stale' : report.sourceStatus ?? report.meta?.freshnessState ?? 'unknown',
    warning: [report.warning, ...(report.blockerIds ?? [])].filter(Boolean).join(' · '), filterDescription,
  }
}

export function reportTableSource(meta: CanonicalCompatibilityMeta | null | undefined, accountId: number, period: CanonicalPeriod, filterDescription: string) {
  if (!meta?.snapshot || !['ready', 'partial', 'empty'].includes(meta.state)
    || meta.period.temporalState === 'future' || meta.marketplaceAccountId !== accountId
    || meta.period.dateFrom !== period.fromIso || meta.period.dateTo !== period.toIso) {
    throw new Error('Экспорт недоступен: нужен загруженный canonical-отчёт выбранного аккаунта и периода.')
  }
  return {
    state: meta.state as 'ready' | 'partial' | 'empty',
    formulaVersion: meta.formulaVersion,
    financeFormulaVersion: meta.snapshot.formulaVersion,
    financeSnapshotChecksum: meta.snapshot.snapshotChecksum,
    financeObservedAt: meta.snapshot.lastObservedAt,
    advertisingSnapshotChecksum: meta.advertisingSnapshotChecksum,
    costLedgerRevision: meta.costLedgerRevision,
    economicsRevision: meta.economicsRevision,
    blockerIds: meta.blockerIds,
    filterDescription,
  }
}

type PnlRow = {
  articleId?: string | null; sku?: string | null; productName?: string | null; label?: string | null
  category?: string | null; nmId?: number | string | null; comment?: string | null
  revenueKopecks?: number | null; cogsKopecks?: number | null; commissionKopecks?: number | null
  logisticsKopecks?: number | null; storageKopecks?: number | null; adSpendKopecks?: number | null
  penaltyKopecks?: number | null; acceptanceKopecks?: number | null
  expenseState?: 'confirmed' | 'partial' | 'unavailable' | 'stale'
  taxKopecks?: number | null; netProfitKopecks?: number | null; marginPct?: number | null
  sourceStatus?: string | null; confidence?: string | null; blockerIds?: string[] | null
}

export function filterPnlTableRows<T extends PnlRow>(rows: T[], query: string, manager: string): T[] {
  const needle = query.trim().toLocaleLowerCase('ru-RU')
  // The P&L DTO has no manager assignment; its rows are unassigned.
  return rows.filter(row => (manager === 'all' || manager === 'unassigned')
    && (!needle || [row.articleId, row.sku, row.productName, row.label, row.category, row.nmId, row.comment]
      .join(' ').toLocaleLowerCase('ru-RU').includes(needle)))
}

const rubles = (value: number | null | undefined) => value == null ? null : value / 100

export function buildPnlTableRows(rows: PnlRow[]): Cell[][] {
  return rows.map(row => {
    const article = row.articleId ?? row.sku
    return [
      [row.productName || row.label || row.category || 'Товар без названия', article ? `Артикул: ${article}` : null, row.nmId ? `WB ${row.nmId}` : null].filter(Boolean).join('\n'),
      row.category ?? null,
      ...[row.revenueKopecks, row.cogsKopecks, row.commissionKopecks, row.logisticsKopecks, row.storageKopecks, row.penaltyKopecks, row.acceptanceKopecks, row.adSpendKopecks, row.taxKopecks, row.netProfitKopecks].map(rubles),
      row.marginPct ?? null, [row.comment, pnlExpenseNote(row)].filter(Boolean).join(' · '),
    ]
  })
}

export function pnlExpenseNote(row: Pick<PnlRow, 'expenseState'>) {
  return row.expenseState === 'confirmed' ? '' : row.expenseState === 'stale'
    ? 'Штрафы и приёмка: сохранённые данные требуют обновления' : row.expenseState === 'partial'
    ? 'Штрафы и приёмка: неполные данные' : 'Штрафы и приёмка: нет подтверждённых данных'
}

type AbcRawRow = { sku?: string | null; nmId?: number | string | null; sppPct?: number | null; cogsPerUnitKopecks?: number | null; adSpendKopecks?: number | null; netProfitKopecks?: number | null; commissionKopecks?: number | null; logisticsKopecks?: number | null; storageKopecks?: number | null; blockerIds?: string[] | null }
type AbcDisplayRow = Record<string, unknown>
export function buildAbcTableRows(rawRows: AbcRawRow[], displayRows: AbcDisplayRow[], snapshot: { count: number; rows: Array<{ row: AbcDisplayRow; originalIndex: number }> }, columns: string[]): Cell[][] {
  const seen = new Set<number>()
  if (snapshot.count !== snapshot.rows.length || rawRows.length !== displayRows.length) throw new Error('Таблица обновилась. Повторите экспорт.')
  return snapshot.rows.map(({ row, originalIndex }) => {
    const raw = rawRows[originalIndex]
    if (!Number.isInteger(originalIndex) || !raw || row !== displayRows[originalIndex] || seen.has(originalIndex)
      || String(raw.sku ?? '—') !== String(row.sku) || String(raw.nmId ?? '—') !== String(row.wb)) throw new Error('Таблица обновилась. Повторите экспорт.')
    seen.add(originalIndex)
    const feeDisplays = String(row.costs ?? '').split(' / ')
    const fee = (amount: number | null | undefined, index: number): Cell =>
      amount == null ? null : feeDisplays[index] || rubles(amount)
    // Only these fields exist in the canonical ABC adapter. Unsupported metrics remain blank.
    const cells: Record<string, Cell> = {
      position: [row.sku, row.name].filter(Boolean).join('\n'), wb: raw.nmId ?? null,
      status: String(row.status ?? ''), abc: String(row.abc ?? ''), action: String(row.action ?? ''),
      cogs: rubles(raw.cogsPerUnitKopecks), sales: String(row.sales ?? ''),
      spp: raw.sppPct ?? null,
      ads: raw.adSpendKopecks == null ? null : String(row.ads ?? ''), net: rubles(raw.netProfitKopecks),
      logistics: fee(raw.logisticsKopecks, 0), commission: fee(raw.commissionKopecks, 1), storage: fee(raw.storageKopecks, 2),
      comment: raw.blockerIds?.length ? raw.blockerIds.join(' · ') : 'canonical',
    }
    return columns.map(column => cells[column] ?? null)
  })
}

export async function downloadReportTableXlsx({ accessToken, payload, signal }: { accessToken: string; payload: PnlTableExportPayload; signal?: AbortSignal }): Promise<Blob> {
  signal?.throwIfAborted()
  if (!accessToken || payload.rows.length > 10000 || !payload.headers.length || payload.headers.length > 32
    || payload.headers.some(header => !header.length || header.length > 128)
    || payload.rows.some(row => row.length !== payload.headers.length || row.some(cell => cell !== null
      && (typeof cell === 'number' ? !Number.isFinite(cell) : typeof cell !== 'string' || cell.length > 4096)))) {
    throw new Error('Экспорт недоступен: превышен размер таблицы или найдены некорректные ячейки.')
  }
  const body = JSON.stringify(payload)
  if (new TextEncoder().encode(body).byteLength > 5 * 1024 * 1024) throw new Error('Экспорт превышает 5 МиБ. Уточните фильтры.')
  const endpoint = 'sourceKind' in payload && payload.sourceKind === 'loaded-pnl'
    ? '/api/wb/reports/pnl/table.xlsx' : '/api/v2/wb/reports/table.xlsx'
  const response = await fetch(buildApiUrl(endpoint), {
    method: 'POST', headers: { Authorization: `Bearer ${accessToken}`, 'Content-Type': 'application/json', Accept: XLSX_MIME },
    credentials: 'include', body, signal,
  })
  signal?.throwIfAborted()
  if (!response.ok) throw new ApiError(`Не удалось выгрузить XLSX (HTTP ${response.status}).`, response.status)
  if (response.headers.get('content-type') !== XLSX_MIME) throw new ApiError('Сервер вернул некорректный формат выгрузки.', 502, 'INVALID_API_RESPONSE')
  const blob = await response.blob()
  signal?.throwIfAborted()
  return blob
}
