import { readFileSync } from 'node:fs'
import { chromium } from 'playwright'
import { expect, it } from 'vitest'

it('keeps funnel controls and the brand menu inside the content at wide and narrow widths', async () => {
  const html = readFileSync(new URL('../../../public/vella-production.html', import.meta.url), 'utf8')
  const source = readFileSync(new URL('./VellaHtmlParityPage.tsx', import.meta.url), 'utf8')
  const baseCss = html.match(/<style[^>]*>([\s\S]*?)<\/style>/)?.[1]
  const fixes = source.match(/\.vella-html-parity-root:has\(#tab-digest\.active\) \.subtabs \{[\s\S]*?(?=\.vella-html-parity-root \.subtabs:has)/)?.[0]
  expect(baseCss).toBeTruthy()
  expect(fixes).toBeTruthy()
  const browser = await chromium.launch({ headless: true })
  try {
    const page = await browser.newPage()
    await page.setContent(`<style>${baseCss}${fixes}</style>
      <div class="vella-html-parity-root" style="display:flex;width:100%;height:100vh">
        <aside class="sidebar"></aside><main style="flex:1;min-width:0">
        <div class="subtabs"><div class="subtabs-scroll"></div>
        <div class="subtabs-context active"><div class="brand-filter context-control active open">
          <button class="chip brand-filter-btn active">Бренды: Все</button>
          <div class="brand-menu">Все бренды</div></div>
          <div id="globalPeriod" class="global-period context-control active"><div class="report-period">
          ${['1 день', '7 дней', '14 дней', '30 дней', '2026-09-30', '2026-10-06', 'Календарь данных', 'Применить', 'Применено: с 30.09 по вчера · 7 дн'].map(t => `<button class="period-btn">${t}</button>`).join('')}
          </div></div><button class="btn">Экспорт</button></div></div>
        <div id="tab-digest" class="active"></div></main></div>`)
    for (const width of [2434, 1700, 1440, 1024, 768]) {
      await page.setViewportSize({ width, height: 900 })
      const bounds = await page.evaluate(() => {
        const rect = (selector: string) => {
          const r = document.querySelector(selector)!.getBoundingClientRect()
          return { left: r.left, right: r.right }
        }
        return { main: rect('main'), brand: rect('.brand-filter-btn'), menu: rect('.brand-menu'), controls: Array.from(document.querySelectorAll('.period-btn')).map(el => { const r = el.getBoundingClientRect(); return { left: r.left, right: r.right } }) }
      })
      for (const control of [bounds.brand, bounds.menu, ...bounds.controls]) {
        expect(control.left, `left edge at ${width}`).toBeGreaterThanOrEqual(bounds.main.left)
        expect(control.right, `right edge at ${width}`).toBeLessThanOrEqual(bounds.main.right)
      }
    }
  } finally { await browser.close() }
})
