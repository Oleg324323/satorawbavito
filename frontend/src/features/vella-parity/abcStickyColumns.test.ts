import { afterEach, describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { chromium } from 'playwright'
import { observeAbcStickyColumns } from './abcStickyColumns'

afterEach(() => vi.unstubAllGlobals())

describe('ABC frozen identity columns', () => {
  it('tracks the measured product width, including resizing and hiding, and cleans up', () => {
    let width = 312.5
    let resize: () => void = () => {}
    const observe = vi.fn()
    const disconnect = vi.fn()
    vi.stubGlobal('ResizeObserver', class {
      constructor(callback: () => void) { resize = callback }
      observe = observe
      disconnect = disconnect
    })
    const header = { getBoundingClientRect: () => ({ width }) }
    const values = new Map<string, string>()
    const table = {
      querySelector: vi.fn(() => header),
      style: {
        getPropertyValue: (key: string) => values.get(key) ?? '',
        setProperty: vi.fn((key: string, value: string) => values.set(key, value)),
      },
    }
    const dispose = observeAbcStickyColumns(table as unknown as HTMLTableElement)
    expect(table.querySelector).toHaveBeenCalledWith('th[data-vella-column="abc-position"]')
    expect(observe).toHaveBeenCalledWith(header)
    expect(values.get('--abc-product-width')).toBe('312.5px')
    resize()
    expect(table.style.setProperty).toHaveBeenCalledTimes(1)
    width = 420
    resize()
    expect(values.get('--abc-product-width')).toBe('420px')
    width = 0
    resize()
    expect(values.get('--abc-product-width')).toBe('0px')
    dispose()
    expect(disconnect).toHaveBeenCalledOnce()
  })

  it('does not bind another column when the ABC product header is absent', () => {
    const table = { querySelector: () => null }
    expect(() => observeAbcStickyColumns(table as unknown as HTMLTableElement)()).not.toThrow()
  })

  it('keeps both headers and cells fixed when the actual ABC styles are horizontally scrolled', async () => {
    const html = readFileSync(new URL('../../../public/vella-production.html', import.meta.url), 'utf8')
    const source = readFileSync(new URL('./VellaHtmlParityPage.tsx', import.meta.url), 'utf8')
    const baseCss = html.match(/<style[^>]*>([\s\S]*?)<\/style>/)?.[1]
    const wrapCss = source.slice(
      source.indexOf('.vella-html-parity-root #tab-abc .report-table-wrap,\n'),
      source.indexOf('.vella-html-parity-root #tab-repricer-stats .report-table-wrap.report-scrollable::after'),
    )
    const fixes = source.slice(
      source.indexOf('.vella-html-parity-root #tab-abc .abc-table-workspace table.report-wide { overflow: visible; }'),
      source.indexOf('.vella-html-parity-root #tab-abc .abc-spp-cell {'),
    )
    expect(baseCss).toBeTruthy()
    expect(wrapCss).toContain('contain: inline-size')
    expect(fixes).toContain('left: var(--abc-product-width, 0px)')
    const browser = await chromium.launch({ headless: true, channel: 'chrome' })
    try {
      const page = await browser.newPage()
      const errors: string[] = []
      page.on('pageerror', error => errors.push(error.message))
      await page.route('**/*', route => route.abort())
      await page.setContent(`<style>${baseCss}${wrapCss}${fixes}</style>
        <div class="vella-html-parity-root" style="width:100%;min-width:0;display:block"><div id="tab-abc">
        <div class="report-table-wrap abc-table-workspace">
        <table class="report-wide" style="width:2460px">
        <thead><tr><th style="width:300px" class="report-sticky" data-vella-column="abc-position">Товар</th>
        <th style="width:140px" class="report-sticky abc-sticky-wb">Артикул WB</th><th>Заказы</th><th>Продажи</th></tr></thead>
        <tbody><tr><td class="report-sticky" data-vella-cell="abc-product-cell"><div class="report-product-cell">Товар с длинным названием</div></td>
        <td class="report-sticky abc-sticky-wb"><a href="#product">123456789</a></td><td>10</td><td>5</td></tr></tbody>
        </table></div></div></div>`, { waitUntil: 'domcontentloaded' })
      await page.evaluate(`(${observeAbcStickyColumns.toString()})(document.querySelector('table'))`)
      const positions = () => page.evaluate(() => {
        const rect = (selector: string) => {
          const r = document.querySelector(selector)!.getBoundingClientRect()
          return { left: r.left, right: r.right }
        }
        return { product: rect('[data-vella-cell="abc-product-cell"]'), article: rect('tbody .abc-sticky-wb'),
          productHeader: rect('[data-vella-column="abc-position"]'), articleHeader: rect('thead .abc-sticky-wb'),
          other: rect('tbody td:nth-child(3)') }
      })
      for (const width of [1200, 640]) {
        await page.setViewportSize({ width, height: 500 })
        await page.locator('.abc-table-workspace').evaluate(wrap => { wrap.scrollLeft = 80 })
        await page.waitForTimeout(50)
        const before = await positions()
        await page.locator('.abc-table-workspace').evaluate(wrap => { wrap.scrollLeft = 650 })
        await page.waitForTimeout(50)
        const after = await positions()
        for (const key of ['product', 'article', 'productHeader', 'articleHeader'] as const) {
          expect(after[key].left).toBeCloseTo(before[key].left, 1)
        }
        expect(after.article.left).toBeCloseTo(after.product.right, 1)
        expect(after.articleHeader.left).toBeCloseTo(after.productHeader.right, 1)
        expect(after.other.left).toBeLessThan(before.other.left - 500)
      }
      expect(errors).toEqual([])
    } finally { await browser.close() }
  }, 20_000)
})
