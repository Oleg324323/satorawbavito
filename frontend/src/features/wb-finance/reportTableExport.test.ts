import { afterEach, expect, it, vi } from 'vitest'
import { buildAbcTableRows, buildPnlTableRows, downloadReportTableXlsx, filterPnlTableRows, reportTableSource, XLSX_MIME } from './reportTableExport'
import type { CanonicalCompatibilityMeta } from './canonicalAbcPnl'

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

const meta: CanonicalCompatibilityMeta = {
  marketplaceAccountId: 31, state: 'partial', formulaVersion: 'wb-abc-pnl-finance-rrdate-v4',
  advertisingSource: null, advertisingEvidenceStatus: null, advertisingSnapshotChecksum: null,
  costLedgerRevision: 2, economicsRevision: 3, blockerIds: ['COST_MISSING'],
  period: { dateFrom: '2026-09-01', dateTo: '2026-09-07', timezone: 'Europe/Moscow', startAt: '2026-08-31T21:00:00Z', endExclusiveAt: '2026-09-07T21:00:00Z', days: 7, temporalState: 'complete' },
  snapshot: { syncRunId: 'synthetic', snapshotChecksum: 'finance-checksum', formulaVersion: 'finance-v1', operationCount: 65, capturedAt: '2026-09-08T00:00:00Z', lastObservedAt: '2026-09-08T01:00:00Z' },
}
const period = { fromIso: '2026-09-01', toIso: '2026-09-07' }
const source = () => reportTableSource(meta, 31, period, 'поиск: SKU-64')

it('filters all P&L rows before windowing and preserves manager semantics and order', () => {
  const rows = Array.from({ length: 65 }, (_, i) => ({ sku: `SKU-${i}`, revenueKopecks: i === 64 ? 0 : null }))
  expect(filterPnlTableRows(rows, 'SKU-64', 'all')).toEqual([rows[64]])
  expect(filterPnlTableRows(rows, '', 'unassigned')).toEqual(rows)
  expect(filterPnlTableRows(rows, '', 'someone')).toEqual([])
  const exported = buildPnlTableRows(filterPnlTableRows(rows, 'SKU-64', 'all'))
  expect(exported[0]).toHaveLength(14)
  expect(exported[0].slice(2, 13)).toEqual([0, null, null, null, null, null, null, null, null, null, null])
  expect(buildPnlTableRows([{ sku: 'negative', revenueKopecks: -123, netProfitKopecks: 0 }])[0][2]).toBe(-1.23)
})

it('selects all ABC snapshot indices in displayed order without parsing money', () => {
  const raw = Array.from({ length: 65 }, (_, i) => ({ sku: `SKU-${i}`, nmId: i + 1, netProfitKopecks: i === 64 ? 0 : -123 }))
  const display = raw.map(row => ({ sku: row.sku, wb: row.nmId, net: '999 ₽', canonical: true }))
  const snapshot = { count: 65, rows: display.map((row, originalIndex) => ({ row, originalIndex })).reverse() }
  const rows = buildAbcTableRows(raw, display, snapshot, ['wb', 'net', 'cogs'])
  expect(rows).toHaveLength(65)
  expect(rows[0]).toEqual([65, 0, null])
  expect(rows[64]).toEqual([1, -1.23, null])
  expect(buildAbcTableRows(raw, display, { count: 0, rows: [] }, ['net'])).toEqual([])
  expect(() => buildAbcTableRows(raw, display, { count: 1, rows: [{ row: display[0], originalIndex: 64 }] }, ['net'])).toThrow()
})

it('retains snapshot provenance and rejects missing or mismatched account/period evidence', () => {
  expect(source()).toEqual({ state: 'partial', formulaVersion: 'wb-abc-pnl-finance-rrdate-v4', financeFormulaVersion: 'finance-v1', financeSnapshotChecksum: 'finance-checksum', financeObservedAt: '2026-09-08T01:00:00Z', advertisingSnapshotChecksum: null, costLedgerRevision: 2, economicsRevision: 3, blockerIds: ['COST_MISSING'], filterDescription: 'поиск: SKU-64' })
  for (const invalid of [null, { ...meta, snapshot: null }, { ...meta, state: 'missing' }, { ...meta, state: 'future' }, { ...meta, marketplaceAccountId: 32 }]) {
    expect(() => reportTableSource(invalid as typeof meta, 31, period, '')).toThrow()
  }
  expect(() => reportTableSource(meta, 31, { ...period, toIso: '2026-09-06' }, '')).toThrow()
  expect(reportTableSource({ ...meta, state: 'empty' }, 31, period, '').state).toBe('empty')
})

it('exports the same known ABC ruble expenses and percentages, preserving zero versus missing', () => {
  const raw = [{ sku: 'Fees', nmId: 1, logisticsKopecks: 1234, commissionKopecks: 0, storageKopecks: null }]
  const display = [{ sku: 'Fees', wb: 1, costs: '12,34 ₽ · 12% / 0 ₽ · 0% / — · —' }]
  expect(buildAbcTableRows(raw, display, { count: 1, rows: [{ row: display[0], originalIndex: 0 }] },
    ['logistics', 'commission', 'storage'])).toEqual([['12,34 ₽ · 12%', '0 ₽ · 0%', null]])
})

const payload = () => ({ reportKind: 'pnl' as const, marketplaceAccountId: 31, dateFrom: period.fromIso, dateTo: period.toIso, headers: ['Товар', 'Сумма'], rows: [['=text', 0]], source: source() })
it('posts bounded typed JSON with Bearer and returns only XLSX responses', async () => {
  const fetcher = vi.fn(async () => new Response('xlsx', { headers: { 'content-type': XLSX_MIME } }))
  vi.stubGlobal('fetch', fetcher)
  const signal = new AbortController().signal
  const blob = await downloadReportTableXlsx({ accessToken: 'synthetic', payload: payload(), signal })
  expect(await blob.text()).toBe('xlsx')
  expect(fetcher.mock.calls[0]).toEqual(['/api/v2/wb/reports/table.xlsx', expect.objectContaining({ method: 'POST', signal, body: JSON.stringify(payload()), headers: { Authorization: 'Bearer synthetic', 'Content-Type': 'application/json', Accept: XLSX_MIME } })])
})
it.each([[403, 'application/json'], [200, 'application/json'], [200, 'text/html'], [200, `${XLSX_MIME}; charset=utf-8`]])('rejects status %s and MIME %s', async (status, mime) => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status, headers: { 'content-type': mime } })))
  await expect(downloadReportTableXlsx({ accessToken: 'synthetic', payload: payload() })).rejects.toThrow()
})
it.each([
  { rows: Array.from({ length: 10001 }, () => ['x', 0]) },
  { rows: [['x'.repeat(4097), 0]] },
  { rows: [['x', NaN]] },
  { rows: [['x', true]] },
  { rows: [['x']] },
  { headers: ['x'.repeat(129), 'amount'] },
  { rows: Array.from({ length: 1500 }, () => ['ю'.repeat(2048), 0]) },
])('rejects invalid bounds before transport', async change => {
  const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher)
  await expect(downloadReportTableXlsx({ accessToken: 'synthetic', payload: { ...payload(), ...change } as ReturnType<typeof payload> })).rejects.toThrow()
  expect(fetcher).not.toHaveBeenCalled()
})
it('rejects an aborted successful response even if fetch ignores the signal', async () => {
  const controller = new AbortController()
  vi.stubGlobal('fetch', vi.fn(async () => { controller.abort(); return new Response('xlsx', { headers: { 'content-type': XLSX_MIME } }) }))
  await expect(downloadReportTableXlsx({ accessToken: 'synthetic', payload: payload(), signal: controller.signal })).rejects.toThrow()
})
