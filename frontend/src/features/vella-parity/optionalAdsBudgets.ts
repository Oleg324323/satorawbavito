type BudgetRow = { campaignId?: string | number | null; budgetCashKopecks?: number | null; budgetNettingKopecks?: number | null; budgetTotalKopecks?: number | null; budgetFetchedAt?: string | null; budgetFresh?: boolean }
type Kpi = { id?: string; value?: string | number | null; hint?: string | null }
export type BudgetPatch = {
  dateFrom: string; dateTo: string; rows: BudgetRow[]
  kpi?: Kpi | null
  balanceKpi?: Kpi | null
  cabinetBalance?: Record<string, unknown> | null
  balanceRefresh?: { state?: string; error?: string; fetchedAt?: string | null; fresh?: boolean }
  budgetRefresh: { state?: string; error?: string; collected?: number; total?: number }
}

export function applyOptionalBudgets<T extends { rows?: BudgetRow[] | null; kpis?: Kpi[] | null }>(report: T, patch: BudgetPatch, from: string, to: string): T {
  if (patch.dateFrom !== from || patch.dateTo !== to) return report
  const byCampaign = new Map(patch.rows.map(row => [String(row.campaignId), row]))
  return {
    ...report,
    budgetRefresh: patch.budgetRefresh,
    ...(patch.balanceRefresh ? { balanceRefresh: patch.balanceRefresh, cabinetBalance: patch.cabinetBalance } : {}),
    rows: report.rows?.map(row => {
      const budget = byCampaign.get(String(row.campaignId))
      return budget ? { ...row, budgetCashKopecks: budget.budgetCashKopecks, budgetNettingKopecks: budget.budgetNettingKopecks, budgetTotalKopecks: budget.budgetTotalKopecks,
        budgetFetchedAt: budget.budgetFetchedAt, budgetFresh: budget.budgetFresh } : row
    }),
    kpis: [...(report.kpis ?? []).filter(kpi => (!patch.kpi || kpi.id !== 'campaign_budget') && (!patch.balanceKpi || kpi.id !== 'cabinet_balance')),
      ...(patch.kpi ? [patch.kpi] : []), ...(patch.balanceKpi ? [patch.balanceKpi] : [])],
  }
}
