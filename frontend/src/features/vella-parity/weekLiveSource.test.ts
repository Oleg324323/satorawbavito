import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const source = readFileSync(new URL('./VellaHtmlParityPage.tsx', import.meta.url), 'utf8')
const stockIsland = source.slice(source.indexOf('function StockReportIsland'), source.indexOf('function WeekReportIsland'))
const weekIsland = source.slice(source.indexOf('function WeekReportIsland'), source.indexOf('function AbcHelpRowIsland'))

describe('stock and week report user-facing states', () => {
  it('keeps live stock and week tables while removing legacy fallback tables', () => {
    const fallbackRemoval = source.slice(
      source.indexOf('function removeLegacyReportTableFallbacks'),
      source.indexOf('function getStockRows'),
    )

    expect(source).toContain('data-vella-runtime-binding="backend-stock"')
    expect(source).toContain('data-vella-runtime-binding="backend-week-over-week"')
    expect(fallbackRemoval).toContain("stock: 'backend-stock'")
    expect(fallbackRemoval).toContain("'week': 'backend-week-over-week'")
  })

  it('shows clean stock loading and empty states instead of technical source panels', () => {
    expect(stockIsland).toContain('ReportDataStateIsland')
    expect(stockIsland).toContain('Загружаем остатки')
    expect(stockIsland).toContain('За выбранный период нет остатков')
    expect(stockIsland).not.toContain('ReportSourcesRefreshBar')
    // Loading/empty/error rendering is exercised against the actual React page
    // in reportLoadingBrowser.test.ts; a progress component is not itself a technical panel.
    expect(stockIsland).not.toContain('OOS риск')
    expect(stockIsland).not.toContain('КТР высокий')
  })

  it('renders stock product cells with photos and readable product metadata', () => {
    expect(source).toContain('function stockProductPhoto')
    expect(source).toContain('function stockProductTitle')
    expect(source).toContain('stockProductMeta(row)')
    expect(source).toContain('photoUrl={stockProductPhoto(row)}')
    expect(source).toContain('title={stockProductTitle(row)}')
  })

  it('shows clean week loading and empty states instead of technical source panels', () => {
    expect(weekIsland).toContain('ReportDataStateIsland')
    expect(weekIsland).toContain('Загружаем сравнение недель')
    expect(weekIsland).toContain('За выбранный период нет сравнения')
    expect(weekIsland).not.toContain('ReportSourcesRefreshBar')
    // Actual progress-to-empty/error behavior is covered by reportLoadingBrowser.test.ts.
    expect(weekIsland).not.toContain('live WoW')
    expect(weekIsland).not.toContain('Week over week')
  })

  it('renders week product cells with photos and Russian table copy', () => {
    expect(source).toContain('function weekProductPhoto')
    expect(source).toContain('function weekProductTitle')
    expect(source).toContain('weekProductMeta(row)')
    expect(source).toContain('photoUrl={weekProductPhoto(row)}')
    expect(source).toContain('label="Был без остатка"')
  })
})
