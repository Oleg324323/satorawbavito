import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, expect, it, vi } from 'vitest'
import { PnlLiveTableShellIsland } from './VellaHtmlParityPage'
import { buildPnlTableRows, PNL_TABLE_EXPORT_HEADERS } from '@/features/wb-finance/reportTableExport'

afterEach(() => vi.restoreAllMocks())

it('renders the same signed product fees as XLSX, without recomputing profit', () => {
  vi.spyOn(console, 'error').mockImplementation(() => undefined) // SSR layout effects do not run
  const rows = [
    { nmId: 101, label: 'Product 101', penaltyKopecks: -123, acceptanceKopecks: 456, netProfitKopecks: 700, expenseState: 'confirmed' as const },
    { nmId: 102, label: 'Product 102', penaltyKopecks: 0, acceptanceKopecks: null, netProfitKopecks: null, expenseState: 'partial' as const },
    { label: 'Нераспределённые расходы WB', penaltyKopecks: 789, acceptanceKopecks: 0, expenseState: 'confirmed' as const },
  ]
  const html = renderToStaticMarkup(createElement(PnlLiveTableShellIsland, {
    replacementKey: 'pnl-fees', state: { status: 'ready', report: { rows } }, rows,
  }))
  expect(html).toContain('Штрафы, ₽')
  expect(html).toContain('Платная приёмка, ₽')
  expect(html).toContain('-1,23 ₽')
  expect(html).toContain('4,56 ₽')
  expect(html).toContain('7 ₽')
  expect(html).toContain('0 ₽')
  expect(html).toContain('нет данных')
  expect(html).toContain('Нераспределённые расходы WB')
  expect(html).toContain('Штрафы и приёмка: неполные данные')
  const exported = buildPnlTableRows(rows)
  expect(exported[0][PNL_TABLE_EXPORT_HEADERS.indexOf('Штрафы, ₽')]).toBe(-1.23)
  expect(exported[0][PNL_TABLE_EXPORT_HEADERS.indexOf('Платная приёмка, ₽')]).toBe(4.56)
  expect(exported[0][PNL_TABLE_EXPORT_HEADERS.indexOf('Прибыль')]).toBe(7)
  expect(exported[1][7]).toBe(0)
  expect(exported[1][8]).toBeNull()
  expect(exported[1][13]).toContain('неполные данные')
  expect(PNL_TABLE_EXPORT_HEADERS).not.toContain('Статус')
  expect(html).not.toContain('>Статус<')
  expect(exported[2][7]).toBe(7.89)
})

it('exports fees for every filtered product, not only the render window', () => {
  const rows = Array.from({ length: 160 }, (_, index) => ({ nmId: 1000 + index,
    penaltyKopecks: index, acceptanceKopecks: -index, expenseState: 'confirmed' as const }))
  const exported = buildPnlTableRows(rows)
  expect(exported).toHaveLength(160)
  expect(exported[159][7]).toBe(1.59)
  expect(exported[159][8]).toBe(-1.59)
})
