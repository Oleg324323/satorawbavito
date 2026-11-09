import { afterEach, expect, it, vi } from 'vitest'
import { buildLoadedPnlTableExport, downloadReportTableXlsx, filterPnlTableRows, PNL_TABLE_EXPORT_HEADERS, XLSX_MIME } from './reportTableExport'

afterEach(() => vi.unstubAllGlobals())
const period = { fromIso: '2026-10-01', toIso: '2026-10-08' }
const report = { filters: { dateRange: { from: period.fromIso, to: period.toIso } },
  meta: { sourceType: 'financial', freshnessState: 'pending_financial' },
  warning: 'Предварительные данные', blockerIds: ['WB_PNL_EXPENSES_UNCONFIRMED'] }
const rows = Array.from({ length: 160 }, (_, i) => ({ nmId: 1000 + i, sku: `SKU-${i}`,
  revenueKopecks: i * 100, penaltyKopecks: -123, acceptanceKopecks: i ? null : 0,
  netProfitKopecks: null, expenseState: 'partial' as const }))
const build = (selected = period, loadedPeriod = period, data = report) => buildLoadedPnlTableExport({
  report: data, rows, period: selected, loadedPeriod, filterDescription: 'Все товары',
})

it('exports the loaded non-canonical P&L, every row and typed fees for the applied inclusive period', () => {
  const payload = build()
  expect(payload).toMatchObject({ sourceKind: 'loaded-pnl', reportKind: 'pnl', dateFrom: '2026-10-01', dateTo: '2026-10-08', sourceState: 'pending_financial' })
  expect(payload.rows).toHaveLength(160)
  expect(payload.rows[159][0]).toContain('WB 1159')
  expect(payload.rows[0].slice(7, 9)).toEqual([-1.23, 0])
  expect(payload.rows[1][8]).toBeNull()
  expect(payload.warning).toContain('WB_PNL_EXPENSES_UNCONFIRMED')
  expect(payload).not.toHaveProperty('marketplaceAccountId')
})

it('exports a different applied period and its filtered rows without a hardcoded range', () => {
  const selected = { fromIso: '2026-09-11', toIso: '2026-09-13' }
  const payload = buildLoadedPnlTableExport({ period: selected, loadedPeriod: selected,
    report: { ...report, filters: { dateRange: { from: selected.fromIso, to: selected.toIso } } },
    rows: filterPnlTableRows(rows, 'SKU-159', 'all'), filterDescription: 'SKU-159',
  })
  expect(payload).toMatchObject({ dateFrom: selected.fromIso, dateTo: selected.toIso })
  expect(payload.rows).toHaveLength(1)
  expect(payload.headers).toEqual(PNL_TABLE_EXPORT_HEADERS)
  expect(payload.rows[0][0]).toContain('SKU-159')
})

it('rejects an immediate period change, stale-period rows and unbound reports', () => {
  const changed = { ...period, toIso: '2026-10-09' }
  expect(() => build(changed)).toThrow('выбранный период')
  expect(() => build(changed, changed)).toThrow('выбранный период')
  expect(() => build(period, period, { ...report, filters: { dateRange: { from: '', to: '' } } })).toThrow()
})

it('allows cached partial data but marks staleness and never supplies zero for missing amounts', () => {
  const payload = buildLoadedPnlTableExport({ report: { ...report, cache: { fresh: false } },
    rows: [{ nmId: 1 }], period, loadedPeriod: period, filterDescription: '',
  })
  expect(payload.sourceState).toBe('stale')
  expect(payload.rows[0].slice(2, 13)).toEqual(Array(11).fill(null))
})

it('uses the authenticated P&L export endpoint without needing a canonical snapshot', async () => {
  const fetcher = vi.fn(async () => new Response('xlsx', { headers: { 'content-type': XLSX_MIME } }))
  vi.stubGlobal('fetch', fetcher)
  const payload = build()
  expect(await (await downloadReportTableXlsx({ accessToken: 'synthetic', payload })).text()).toBe('xlsx')
  expect(fetcher.mock.calls[0]).toEqual(['/api/wb/reports/pnl/table.xlsx', expect.objectContaining({
    method: 'POST', body: JSON.stringify(payload), headers: expect.objectContaining({ Authorization: 'Bearer synthetic' }),
  })])
})
