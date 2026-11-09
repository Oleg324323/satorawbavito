import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { requiresHumanApproval } from './contracts/avitoReviews'
import { validateExpenseRowContract } from './contracts/finance'
import { avitoReviewBrandSettings, avitoReviewRows } from './data/demoAvitoReviews'
import { expenseRows, expenseSummaryMetrics } from './data/demoFinance'
import { sourceRows } from './data/demoSources'

const root = process.cwd()
const read = (path: string) => readFileSync(join(root, path), 'utf8')

describe('React Vella final contracts', () => {
  it('keeps hard ДДС article invariants explicit', () => {
    expect(expenseRows.every(validateExpenseRowContract)).toBe(true)
    expect(expenseSummaryMetrics.totalExpensesKopecks).toBeGreaterThan(0)
    expect(expenseSummaryMetrics.pnlOnlyKopecks).toBeGreaterThan(0)
    expect(expenseSummaryMetrics.draftNetProfitKopecks).toBeGreaterThan(0)
    expect(expenseSummaryMetrics.reconciliationRequiredKopecks).toBeGreaterThan(0)
    expect(expenseRows.find((row) => row.scope === 'cashflow_reconciliation')).toMatchObject({
      entersSkuPnl: false,
      entersCompanyPnl: false,
    })
    expect(expenseRows.find((row) => row.scope === 'pnl_only_admin')).toMatchObject({
      entersSkuPnl: false,
      entersCompanyPnl: true,
      skuCoveragePct: null,
      planFactBase: 'monthly_plan_completion',
    })
  })

  it('keeps React-final as a candidate contract while public routes stay on canonical Vella parity', () => {
    const app = read('src/App.tsx')
    const shell = read('src/features/vella-react-final/shell/VellaProductionShell.tsx')
    expect(app).toContain('<Route path="/wb/repricer/stats" element={<VellaHtmlParityPage />} />')
    expect(app).toContain('<Route path="/wb/reports/expenses" element={<Navigate to="/wb/reports/pnl" replace />} />')
    expect(app).toContain('<Route path="/wb/sources" element={<VellaHtmlParityPage />} />')
    expect(app).not.toContain('WbExpensesPage as VellaReactFinalExpensesPage')
    expect(app).not.toContain('WbRepricerStatsPage as VellaReactFinalRepricerStatsPage')
    expect(app).not.toContain('WbSourcesPage as VellaReactFinalSourcesPage')
    expect(shell).toContain("currentPath.startsWith('/wb/reports')")
    expect(shell).toContain("activeModule === 'reports' ? reportTabs : []")
    expect(shell).toContain('{tabs || activeTabs.length ? (')
    expect(shell).not.toContain("currentPath === '/wb/sources'")
    expect(shell).not.toContain("{ label: 'Источники и загрузки', path: '/wb/sources', icon: <Database size={14} />, count: 'WB' }")
  })

  it('keeps expenses KPI and add-expense drawer copy in React-final', () => {
    const page = read('src/features/vella-react-final/pages/WbExpensesPage.tsx')
    expect(page).toContain('Все расходы')
    expect(page).toContain('P&L only')
    expect(page).toContain('Чистая прибыль')
    expect(page).toContain('Требует сверки')
    expect(page).toContain('Добавить расход')
    expect(page).toContain('Сохранить черновик')
    expect(page).toContain('Черновой расчёт по учтённым строкам, не закрытый P&L.')
    expect(page).not.toContain('финальный P&L')
  })

  it('models sources as readiness controls without forbidden source wording', () => {
    expect(sourceRows.some((row) => row.name === '1С · Движение денежных средств')).toBe(true)
    expect(sourceRows.some((row) => row.name.toLowerCase().includes('parser') || row.name.toLowerCase().includes('парсер'))).toBe(false)
    expect(sourceRows.find((row) => row.id === 'dds-rules')?.dependentSurfaces).toContain('expenses')
  })

  it('keeps Jason Statham meme tone Avito-only and guards low rating reviews', () => {
    expect(avitoReviewBrandSettings.some((settings) => settings.voicePreset === 'jason_statham_meme')).toBe(true)
    expect(avitoReviewBrandSettings.find((settings) => settings.voicePreset === 'jason_statham_meme')?.customInstructions).toContain('только Авито')

    const stopTopics = avitoReviewBrandSettings.flatMap((settings) => settings.stopTopics)
    expect(avitoReviewRows.filter((row) => row.rating <= 3).every((row) => requiresHumanApproval(row, stopTopics))).toBe(true)
  })
})
