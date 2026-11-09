import { readFileSync } from 'node:fs'
import { chromium } from 'playwright'
import { expect, it } from 'vitest'

it('keeps Statistics product header and cells fixed during real horizontal scroll', async () => {
  const source = readFileSync(new URL('../vella-parity/VellaHtmlParityPage.tsx', import.meta.url), 'utf8')
  const html = readFileSync(new URL('../../../public/vella-production.html', import.meta.url), 'utf8')
  const baseCss = html.match(/<style>([\s\S]*?)<\/style>/)![1]
  const scopedCss = source.slice(source.lastIndexOf('<style>{`') + '<style>{`'.length, source.lastIndexOf('`}</style>'))
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const page = await browser.newPage({ viewport: { width: 900, height: 500 } })
    const columns = ['product', 'action', 'manager', 'orders', 'clicks', 'baskets', 'drrPct']
    await page.setContent(`<style>${baseCss}\n${scopedCss}</style><div class="vella-html-parity-root"><div id="tab-repricer-stats" class="tab-content active"><div class="report-table-wrap"><table class="report-wide"><thead><tr>${columns.map(column => `<th data-stats-column="${column}">${column}</th>`).join('')}</tr></thead><tbody><tr>${columns.map(column => `<td data-stats-column="${column}"><div class="report-product-cell">${column}</div></td>`).join('')}</tr></tbody></table></div></div></div>`)
    const position = () => page.locator('[data-stats-column="product"]').evaluateAll(cells => cells.map(cell => ({ left: cell.getBoundingClientRect().left, position: getComputedStyle(cell).position })))
    const before = await position()
    await page.locator('#tab-repricer-stats .report-table-wrap').evaluate(wrap => { wrap.scrollLeft = 600 })
    expect(await page.locator('#tab-repricer-stats .report-table-wrap').evaluate(wrap => wrap.scrollLeft)).toBe(600)
    const after = await position()
    expect(after.map(cell => cell.position)).toEqual(['sticky', 'sticky'])
    after.forEach((cell, index) => expect(Math.abs(cell.left - before[index].left)).toBeLessThanOrEqual(1))
  } finally { await browser.close() }
}, 15000)
