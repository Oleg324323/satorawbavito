import { expect, it } from 'vitest'
import { buildAbcExportTable } from './VellaHtmlParityPage'
import { storageAttributionLabel } from './storageAttribution'

it.each([
  ['current_stock_allocation', 'текущему снимку'], ['current_wb_stock', 'текущему снимку'],
  ['historical_stock_allocation', 'истории остатков'], ['wb_finance_report', 'Прямые финансовые'],
  ['account_unallocated', 'Нераспределённые'], [undefined, 'не подтверждён'], ['new-unknown', 'не подтверждён'],
])('exports the actual storage provenance %s without changing money', (storageAttribution, phrase) => {
  const source = { sku: 'SYNTHETIC', nmId: 101, storageAttribution, storageKopecks: -12345,
    commissionKopecks: 0, revenueKopecks: 123456, netProfitKopecks: 65432 }
  const before = structuredClone(source)
  const exported = buildAbcExportTable([source])
  const columns = Object.fromEntries(exported.headers.map((name, index) => [name, exported.rows[0][index]]))
  expect(exported.rows[0].at(-1)).toContain(phrase)
  expect(columns['Хранение, ₽']).toBe('−123,45 ₽')
  expect(columns['Комиссия, ₽']).toBe('0 ₽')
  expect(source).toEqual(before)
  expect(storageAttributionLabel(source)).toContain(phrase)
})

it('keeps cabinet allocation separate even when a stale product provenance is present', () => {
  expect(storageAttributionLabel({ isUnallocated: true, storageAttribution: 'wb_finance_report' })).toContain('Нераспределённые')
})
