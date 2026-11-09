import { afterEach, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { observeProductsStickyColumns } from './productsStickyColumns'

afterEach(() => vi.unstubAllGlobals())

it('places WB article after the actual frozen widths and updates on resize or hiding', () => {
  let resize = () => {}
  const disconnect = vi.fn(), observe = vi.fn()
  vi.stubGlobal('ResizeObserver', class {
    constructor(callback: () => void) { resize = callback }
    observe = observe
    disconnect = disconnect
  })
  const widths = [36, 218]
  const cells = widths.map((_, index) => ({ getBoundingClientRect: () => ({ width: widths[index] }) }))
  const setProperty = vi.fn()
  const table = { tHead: { rows: [{ cells }] }, style: { setProperty } } as unknown as HTMLTableElement
  const cleanup = observeProductsStickyColumns(table)
  expect(setProperty).toHaveBeenLastCalledWith('--wb-article-sticky-left', '254px')
  expect(observe).toHaveBeenCalledTimes(2)
  widths[1] = 120
  resize()
  expect(setProperty).toHaveBeenLastCalledWith('--wb-article-sticky-left', '156px')
  widths[1] = 0
  resize()
  expect(setProperty).toHaveBeenLastCalledWith('--wb-article-sticky-left', '36px')
  cleanup()
  expect(disconnect).toHaveBeenCalledOnce()
})

it('pins both WB article header and cells only in the WB products table', () => {
  const source = readFileSync(new URL('../vella-parity/VellaHtmlParityPage.tsx', import.meta.url), 'utf8')
  expect(source).toContain('return observeProductsStickyColumns(tableRef.current)')
  expect(source).toContain(`#mainTable td:nth-child(3) {
          min-width: 82px;
          position: sticky;
          left: var(--wb-article-sticky-left, 204px);`)
  expect(source).toContain('#tab-products #mainTable thead th:nth-child(3)')
  expect(source).toContain('#tab-products #mainTable tbody tr.sel td:nth-child(3)')
})
