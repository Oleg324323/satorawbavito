import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it } from 'vitest'
import { FunnelStages, funnelPeriodMatches, funnelNeedsRefresh, type FunnelPage, type FunnelTotalsComparison } from './WbSalesFunnel'

const data = (totals = { visits: 1000, carts: 100, orders: 20, buyouts: 10, ctr: null, cartConversion: 10 }, complete = true) => ({
  totals, complete, unavailable: { ctr: 'API не передаёт CTR' }, dateFrom: '2026-10-01', dateTo: '2026-10-08',
}) as unknown as FunnelPage
const render = (page: FunnelPage | null) => renderToStaticMarkup(createElement(FunnelStages, { data: page }))

it('shows period summary CR without CTR, impressions or extra table columns', () => {
  const html = render(data())
  expect(html).not.toContain('CTR')
  expect(html).toContain('CR 10%')
  expect(html).toContain('cartCount и openCount WB')
  expect(html.match(/class="sf-stage sf-stage-/g)).toHaveLength(4)
  expect(html).not.toContain('Показы')
})

it('keeps an observed zero distinct from unavailable percentages', () => {
  const zero = data(); zero.totals.cartConversion = 0
  expect(render(zero)).toContain('CR 0%')
  zero.totals.cartConversion = null
  expect(render(zero)).toContain('CR —')
  expect(render(null)).not.toContain('CTR')
})

it('keeps partial coverage in the tooltip without a visible suffix beside CR', () => {
  const html = render(data(undefined, false))
  expect(html).not.toContain('частично')
  expect(html).toContain('CR 10%')
  expect(html).toContain('не подтверждённый итог всего периода')
})

it('does not mistake an omitted catalog card for an incomplete seller summary', () => {
  const page = data(undefined, false)
  page.summaryComplete = true; page.summaryProducts = 3063
  expect(render(page)).toContain('CR 10%')
  expect(render(page)).not.toContain('не подтверждённый итог всего периода')
  expect(funnelNeedsRefresh({ total: 0, complete: false, refreshRequired: true })).toBe(true)
})

it('rejects previously selected or wider reporting periods', () => {
  expect(funnelPeriodMatches(data(), '2026-10-01', '2026-10-08')).toBe(true)
  expect(funnelPeriodMatches(data(), '2026-10-07', '2026-10-11')).toBe(false)
  expect(funnelPeriodMatches(data(), '2026-10-01', '2026-10-04')).toBe(false)
})

it('does not repeatedly collect an exhausted WB listing, but refreshes actual gaps', () => {
  const page = data(undefined, false); page.total = 3522; page.refreshRequired = false
  expect(funnelNeedsRefresh(page)).toBe(false)
  page.refreshRequired = true
  expect(funnelNeedsRefresh(page)).toBe(true)
  delete page.refreshRequired
  expect(funnelNeedsRefresh(page)).toBe(true)
})

it('renders calculated aggregate changes independently without changing absolute values', () => {
  const page = data()
  const comparison = {
    source: 'calculated_wb_sales_funnel_totals_v1',
    totals: { visits: 100 / 3, carts: -100, orders: 0, buyouts: null, cartConversion: 20 },
    selectedTotals: page.totals, previousTotals: { visits: 750, carts: 200, orders: 20, buyouts: null, cartConversion: 10 / 1.2 },
    ready: { visits: true, carts: true, orders: true, buyouts: false, cartConversion: true },
    unavailable: { buyouts: 'Нет данных прошлого периода.' }, coverage: { visits: { products: 2, matchedProducts: 2 } },
  } as unknown as FunnelTotalsComparison
  const html = renderToStaticMarkup(createElement(FunnelStages, { data: page, comparison }))
  expect(html).toContain('↑ +33,33%')
  expect(html).toContain('↓ −100%')
  expect(html).toContain('0%')
  expect(html).toContain('CR 10%')
  expect(html).toContain('↑ +20%')
  expect(html).toContain('Динамика рассчитана из полных итогов WB обоих периодов, без ограничения локальным каталогом (товаров в выбранном отчёте: 2)')
  expect(html).not.toContain('Динамика передана API WB')
  expect(html).toContain('Нет данных прошлого периода.')
  comparison.selectedTotals = { ...page.totals, visits: 999 }
  expect(renderToStaticMarkup(createElement(FunnelStages, { data: page, comparison }))).not.toContain('↑ +33,33%')
})
