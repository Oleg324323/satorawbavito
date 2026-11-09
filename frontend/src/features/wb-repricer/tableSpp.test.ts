import { readFileSync } from 'node:fs'
import { expect, it } from 'vitest'
import { mapLiveRepricerRowToParityProduct } from './liveParityData'

function product(analytics: Record<string, unknown>) {
  return mapLiveRepricerRowToParityProduct({
    meta: { articleId: 'TEST', nmId: 1025784485, name: 'Test', status: 'auto', currentPriceKopecks: 206400, basketsLast7d: 0, basketNorm: 1 },
    settings: { cogsKopecks: 80000, logisticsKopecks: 0, minMarginPct: 15, wbCommissionPct: 15 },
    analytics,
  } as any, 0)
}

it('uses base price for the reporting SPP without changing current price or strategy SPP', () => {
  const actual = product({ basePriceKopecks: 430000, buyerPriceNoWalletKopecks: 142600, sppPct: 30.91 })
  expect(actual.tableSppPct).toBe(66.8)
  expect(actual.price).toBe(2064)
  expect(actual.tableBuyerPrice).toBe(1426)
  expect(actual.prevPrice).toBe(4300)
  expect(actual.spp).toBe(30.91)
})

it('rounds once and distinguishes zero from missing or invalid prices', () => {
  expect(product({ basePriceKopecks: 100000, buyerPriceNoWalletKopecks: 33151 }).tableSppPct).toBe(66.8)
  expect(product({ basePriceKopecks: 100000, buyerPriceNoWalletKopecks: 100000 }).tableSppPct).toBe(0)
  for (const analytics of [
    { basePriceKopecks: 430000, buyerPriceWithWalletKopecks: 142600, avgPriceWithSppKopecks: 142600 },
    { buyerPriceNoWalletKopecks: 142600 },
    { basePriceKopecks: 0, buyerPriceNoWalletKopecks: 0 },
    { basePriceKopecks: 100000, buyerPriceNoWalletKopecks: 100001 },
  ]) expect(product(analytics).tableSppPct).toBeNull()
  expect(product({ basePriceKopecks: 430000, buyerPriceWithWalletKopecks: 139700 }).tableBuyerPrice).toBeNull()
})

it('removes only the two price columns and renders the new percentage', () => {
  const source = readFileSync(new URL('../../../public/vella-production.html', import.meta.url), 'utf8')
  const table = source.match(/<table[^>]*id="mainTable"[\s\S]*?<\/thead>/)![0]
  expect(table).not.toMatch(/>С СПП<|>С кошельком</)
  expect(table).toContain('Цена до СПП')
  expect(table).toContain('% СПП')
  expect(table.match(/<th[\s>]/g)).toHaveLength(22)
  expect(table.match(/<col[\s>]/g)).toHaveLength(22)
  const fn = source.match(/function sppHTML\(p\)\{[\s\S]*?\n\}/)![0]
  const render = new Function('p', `${fn}; return sppHTML(p)`)
  expect(render({ tableSppPct: 66.8 })).toContain('66.8%')
  expect(render({ tableSppPct: 0 })).toContain('0.0%')
  expect(render({ tableSppPct: null, spp: 30 })).toContain('нет данных')
  const priceFn = source.match(/function priceCellHTML\(p\)\{[\s\S]*?\n\}/)![0]
  const renderPrice = new Function('p', `const fmt = value => String(value); ${priceFn}; return priceCellHTML(p)`)
  const prices = { price: 2064, prevPrice: 4300, tableBuyerPrice: 1426, sku: 'TEST' }
  expect(renderPrice(prices)).toContain('1426 ₽')
  expect(renderPrice(prices)).toContain('c-dn">↓ с 4300 ₽')
  expect(renderPrice(prices)).not.toContain('2064 ₽')
  const missing = renderPrice({ ...prices, tableBuyerPrice: null })
  expect(missing).toContain('нет данных')
  expect(missing).not.toMatch(/price-hint|↓|↑/)
  expect(renderPrice({ ...prices, prevPrice: null })).not.toMatch(/↓|↑/)
})

it('the runtime price-cell override displays the same buyer price and preserves the red base hint', () => {
  const source = readFileSync(new URL('../vella-parity/VellaHtmlParityPage.tsx', import.meta.url), 'utf8')
  const fn = source.match(/window.priceCellHTML = function priceCellHTML\(p\) \{[\s\S]*?\n      \};/)![0]
  const render = new Function('p', `const window = {}; const hasNumber = value => value != null && Number.isFinite(Number(value)); const htmlEscape = String; const rub = String; const updatePriceHeaders = () => {}; ${fn}; return window.priceCellHTML(p)`)
  const prices = { price: 2064, prevPrice: 4300, tableBuyerPrice: 1426, sku: 'TEST' }
  expect(render(prices)).toContain('1426 ₽')
  expect(render(prices)).toContain('c-dn">↓ с 4300 ₽')
  expect(render(prices)).not.toContain('2064 ₽')
  const missing = render({ ...prices, tableBuyerPrice: null })
  expect(missing).toContain('нет данных')
  expect(missing).not.toMatch(/price-hint|↓|↑/)
  expect(render({ ...prices, prevPrice: null })).not.toMatch(/↓|↑/)
})

it('shows a labelled product subtotal without promoting it to final margin', () => {
  const analytics = { financeState: 'ok', salesUnits: 3, returnsUnits: 1, revenueKopecks: 100000,
    settlementFormulaVersion: 'wb-final-payout-cogs-tax-v1', settlementProfitKopecks: null,
    settlementBlockers: ['settlement_account_expenses_unreconciled'],
    settlementAttributedProfitKopecks: -10001, settlementCogsState: 'assumed' }
  const mapped = product(analytics)
  expect(mapped.mg).toBeNull()
  expect(mapped.mgRub).toBeNull()
  expect(mapped.marginBeforeAllocationRub).toBe(-50.005)
  expect(mapped.netSku).toBeNull()
  const source = readFileSync(new URL('../../../public/vella-production.html', import.meta.url), 'utf8')
  const fn = source.match(/function marginHTML\(p\)\{[\s\S]*?\n\}/)![0]
  const render = new Function('p', `const escapeHtml=String, fmt=String; ${fn}; return marginHTML(p)`)
  const text = render(mapped)
  expect(text).toContain('до общих расходов')
  expect(text).toContain('-50,01')
  expect(text).toContain('примерная с/с')
  expect(text).not.toContain('%')
  expect(product({ ...analytics, settlementBlockers: ['settlement_tax_missing'] }).marginBeforeAllocationRub).toBeNull()
  expect(product({ ...analytics, settlementAttributedProfitKopecks: null }).marginBeforeAllocationRub).toBeNull()
  expect(product({ ...analytics, salesUnits: 1, returnsUnits: 1 }).marginBeforeAllocationRub).toBeNull()
})
