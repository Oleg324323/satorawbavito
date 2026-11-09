import { afterEach, expect, it, vi } from 'vitest'
import { observeStatsStickyColumns } from './statsStickyColumns'
import { readFileSync } from 'node:fs'

afterEach(() => vi.unstubAllGlobals())

it('pins product after actual preceding widths, including hidden columns and resize', () => {
  let resize = () => {}
  const disconnect = vi.fn(), observe = vi.fn()
  vi.stubGlobal('ResizeObserver', class {
    constructor(callback: () => void) { resize = callback }
    observe = observe
    disconnect = disconnect
  })
  const widths = [40, 230, 100]
  const cells = widths.map((_, index) => ({
    dataset: { statsColumn: ['selection', 'product', 'orders'][index] },
    getBoundingClientRect: () => ({ width: widths[index] }),
  }))
  const setProperty = vi.fn()
  const cleanup = observeStatsStickyColumns({ tHead: { rows: [{ cells }] }, style: { setProperty } } as unknown as HTMLTableElement)
  expect(setProperty).toHaveBeenLastCalledWith('--stats-product-left', '40px')
  widths[0] = 60
  resize()
  expect(setProperty).toHaveBeenLastCalledWith('--stats-product-left', '60px')
  widths[0] = 0
  resize()
  expect(setProperty).toHaveBeenLastCalledWith('--stats-product-left', '0px')
  expect(observe).toHaveBeenCalledTimes(3)
  cleanup()
  expect(disconnect).toHaveBeenCalledOnce()
})

it('removes only the Statistics toolbar shortcuts and preserves its filters and other report pages', () => {
  const html = readFileSync(new URL('../../../public/vella-production.html', import.meta.url), 'utf8')
  const stats = html.slice(html.indexOf('<div class="tab-content" id="tab-repricer-stats">'), html.indexOf('<div class="tab-content" id="tab-expenses">'))
  expect(stats).not.toContain('Планы менеджеров')
  expect(stats).not.toContain("goSubtab(")
  expect(stats).not.toContain('class="help-btn"')
  expect(stats).toContain('id="repricerStatsFiltersTrigger"')
  expect(stats).toContain('id="repricerStatsReset"')
  expect(html).toContain('id="tab-rnp"')
  expect(html).toContain('id="tab-abc"')
  expect(html).toContain('id="tab-pnl"')
})
