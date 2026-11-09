import { expect, test } from 'vitest'
import { applyOptionalBudgets, type BudgetPatch } from './optionalAdsBudgets'

const patch: BudgetPatch = { dateFrom: '2026-09-01', dateTo: '2026-09-07', rows: [{ campaignId: '11', budgetTotalKopecks: 12000 }], kpi: { id: 'campaign_budget', value: '12000' }, budgetRefresh: { state: 'completed' } }
test('updates only budgets, preserving historical values and row ordering', () => {
  const report = { rows: [{ campaignId: '11', adSpendKopecks: 16128796 }, { campaignId: '12', adSpendKopecks: 0 }], kpis: [{ id: 'ad_spend', value: '16128796' }, { id: 'campaign_budget', value: '—' }] }
  const result = applyOptionalBudgets(report, patch, patch.dateFrom, patch.dateTo)
  expect(result.rows[0]).toEqual({ campaignId: '11', adSpendKopecks: 16128796, budgetTotalKopecks: 12000 })
  expect(result.rows[1]).toEqual(report.rows[1])
  expect(result.kpis[0]).toEqual(report.kpis[0])
  expect(report.kpis[1].value).toBe('—')
})
test('ignores budget responses for an earlier period', () => {
  const report = { rows: [{ campaignId: '11', budgetTotalKopecks: null }] }
  expect(applyOptionalBudgets(report, patch, '2026-09-09', '2026-09-16')).toBe(report)
})

test('attaches a current balance separately from historical metrics and preserves a genuine zero', () => {
  const money: BudgetPatch = { ...patch, balanceKpi: { id: 'cabinet_balance', value: '0', hint: 'Текущий снимок на сегодня' },
    cabinetBalance: { balanceKopecks: 0, netKopecks: 10000, bonusKopecks: 2000 },
    balanceRefresh: { state: 'completed', fresh: true, fetchedAt: '2026-10-12T10:00:00Z' } }
  const report = { rows: [{ campaignId: '11', impressions: 200, orders: { kopecks: 3920000 } }],
    kpis: [{ id: 'ad_spend', value: '300576' }] }
  const result = applyOptionalBudgets(report, money, patch.dateFrom, patch.dateTo)
  expect(result.kpis).toContainEqual(money.balanceKpi)
  expect(result.kpis).toContainEqual(money.kpi)
  expect(result.rows[0].orders.kopecks).toBe(3920000)
  expect(result).toMatchObject({ cabinetBalance: money.cabinetBalance, balanceRefresh: money.balanceRefresh })
  expect(applyOptionalBudgets(report, money, '2026-10-01', '2026-10-08')).toBe(report)
  expect(applyOptionalBudgets(result, { ...money, kpi: null }, patch.dateFrom, patch.dateTo).kpis)
    .toContainEqual(money.kpi)
})
