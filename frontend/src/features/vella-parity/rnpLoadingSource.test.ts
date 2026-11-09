import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const source = readFileSync(new URL('./VellaHtmlParityPage.tsx', import.meta.url), 'utf8')
const adsIsland = source.slice(source.indexOf('function AdsReportIsland'), source.indexOf('function useStockReportState'))

describe('RNP period loading state', () => {
  // Period-scoped requests and immediate old-row hiding now run against the
  // actual mounted component in rnpPeriodBrowser.test.ts, not source snippets.

  it('renames only the requested RNP headers and preserves their metric bindings', () => {
    expect(source).toContain('<RnpHeaderCell label="Переходы" helpKey="wbOpens" className="num" />')
    expect(source).toContain('<RnpHeaderCell label="CR" helpKey="cartConversion" className="num" />')
    expect(source).not.toContain('<RnpHeaderCell label="Перешли в карточку"')
    expect(source).not.toContain('<RnpHeaderCell label="CR из карточки в корзину, %"')
    expect(source).toContain('<RnpHeaderCell label="CR в заказ, %" helpKey="orderCr" className="num" />')
  })

  it('renders backend fetch diagnostics on the RNP page', () => {
    expect(source).toContain('function RnpDebugPanelIsland')
    expect(source).toContain('data-vella-island="rnp-fetch-debug"')
    expect(source).toContain('ads-only examples')
    expect(source).toContain('funnelRequest')
  })

  it('uses a dedicated scrollable workspace for the wide product table', () => {
    expect(source).toContain('rnp-table-workspace')
    expect(source).toContain('#tab-rnp .rnp-table-workspace')
  })

  it('renders a customer-facing RNP progress panel while backend is building the report', () => {
    expect(source).toContain('RNP_PROGRESS_STEPS')
    expect(source).toContain('data-vella-island="rnp-progress-panel"')
    expect(source).toContain('Загружаем РНП')
    expect(source).toContain('Сверяем продажи, корзины, рекламу и остатки')
  })

  it('adds customer-facing help text to RNP table headers and summary blocks', () => {
    expect(source).toContain('RNP_HELP_TEXT')
    expect(source).toContain('rnpHeaderHelp')
    expect(source).toContain('Переходы в карточку товара за выбранный период')
    expect(source).toContain('Органические заказы считаются как все заказы товара минус заказы, которые WB связал с рекламой')
    expect(source).toContain('ДРР показывает, какую долю от суммы заказов съела реклама')
  })

  it('shows the Ads loader immediately instead of retaining cached rows from the previous range', () => {
    expect(adsIsland).toContain("setState({ status: 'loading' })")
    expect(adsIsland).not.toContain('setState(retainReadyReportWhileRefreshing)')
  })

  it('keeps Ads loading and empty states customer-facing', () => {
    expect(adsIsland).toContain('ReportDataStateIsland')
    expect(adsIsland).toContain('Загружаем рекламу')
    expect(adsIsland).toContain('За выбранный период нет рекламы')
    expect(adsIsland).not.toContain('ReportSourcesRefreshBar')
    expect(adsIsland).not.toContain('WB ads backend')
    expect(adsIsland).not.toContain('SKU fallback')
    expect(adsIsland).not.toContain('Загружаем рекламу с бэкенда')
  })
})
