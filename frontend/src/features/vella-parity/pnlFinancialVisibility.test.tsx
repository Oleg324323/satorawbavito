import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it } from 'vitest'
import { PnlLiveSourceStripIsland, PnlLiveWorkbenchIsland } from './VellaHtmlParityPage'

const rows = [
  { nmId: 101, revenueKopecks: 10000, cogsKopecks: 1000, taxKopecks: 2200, netProfitKopecks: 2000 },
  { nmId: 102, revenueKopecks: 0, cogsKopecks: 0, taxKopecks: 0, netProfitKopecks: -500 },
  { nmId: 103, revenueKopecks: 0, cogsKopecks: 0, taxKopecks: null, netProfitKopecks: null },
]
const report = { rows, profitBasis: 'wb_financial_report' as const,
  blockerIds: ['WB_PNL_TAX_UNCONFIRMED', 'tax_mapping_missing'], cashFlow: { status: 'unavailable' as const } }

it('explains the actual unresolved subset instead of saying all financial results are missing', () => {
  const html = renderToStaticMarkup(createElement(PnlLiveSourceStripIsland, {
    replacementKey: 'pnl-subset', state: { status: 'ready', report },
    source: 'financial', period: { fromIso: '2026-10-12', toIso: '2026-10-18' },
  }))
  expect(html).toContain('Прибыль рассчитана для 2 из 3 строк')
  expect(html).toContain('Налог не определён в 1 из 3 строк')
  expect(html).not.toContain('Уточните себестоимость')
  expect(html).not.toContain('Для подтверждения прибыли нужны данные')
})

it('shows only an explicitly partial known-profit sum without fabricating a report margin', () => {
  const html = renderToStaticMarkup(createElement(PnlLiveWorkbenchIsland, {
    replacementKey: 'pnl-partial', state: { status: 'ready', report }, rows,
  }))
  expect(html).toContain('Прибыль WB — известная часть')
  expect(html).toContain('15 ₽ · итог неполный')
  expect(html).toContain('Общий итог пока не подтверждён')
  expect(html).not.toContain('15%')
  expect(rows[2].netProfitKopecks).toBeNull()
})

it('does not replace a completely missing result with zero profit', () => {
  const unavailable = [{ revenueKopecks: 10000, netProfitKopecks: null }]
  const html = renderToStaticMarkup(createElement(PnlLiveWorkbenchIsland, {
    replacementKey: 'pnl-missing', state: { status: 'ready', report: { ...report, rows: unavailable } }, rows: unavailable,
  }))
  expect(html).not.toContain('известная часть')
  expect(html).toContain('нет данных')
})

it('shows the financial-only expense subtotal without adding it to saved profit', () => {
  const detailed = [{ ...rows[0], financialExpenseSubtotalKopecks: 1234 },
    { ...rows[1], financialExpenseSubtotalKopecks: -234 }]
  const html = renderToStaticMarkup(createElement(PnlLiveWorkbenchIsland, {
    replacementKey: 'pnl-subtotal', state: { status: 'ready', report: { ...report, rows: detailed } }, rows: detailed,
  }))
  expect(html).toContain('Приёмка, реклама, штрафы и удержания WB')
  expect(html).toContain('10 ₽')
  expect(html).toContain('15 ₽') // saved 2000 - 500; subtotal is not another deduction
  const unknown = [{ ...detailed[0], financialExpenseSubtotalKopecks: null }]
  const missing = renderToStaticMarkup(createElement(PnlLiveWorkbenchIsland, {
    replacementKey: 'pnl-subtotal-unknown', state: { status: 'ready', report: { ...report, rows: unknown } }, rows: unknown,
  }))
  expect(missing).toContain('нет данных')
})
