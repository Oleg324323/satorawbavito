import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const source = readFileSync(new URL('./VellaHtmlParityPage.tsx', import.meta.url), 'utf8')
const styles = source.slice(
  source.indexOf('.vella-html-parity-root #tab-products #mainTable th:nth-child(3)'),
  source.indexOf('.vella-html-parity-root #tab-products #tableWrap'),
)

describe('Products table density styles', () => {
  it('makes compact mode materially denser than comfortable mode', () => {
    expect(styles).toContain('.vella-html-parity-root.density-comfortable #tab-products #mainTable')
    expect(styles).toContain('body.density-compact .vella-html-parity-root #tab-products #mainTable')
    expect(styles).toContain('min-width: 760px')
    expect(styles).toContain('font-size: 8.5px')
    expect(styles).toContain('height: 16px')
    expect(styles).toContain('font-size: 7.5px')
    expect(styles).toContain('height: 10px')
    expect(styles).toContain('body.density-compact .vella-html-parity-root #tab-products #mainTable .manager-cell')
    expect(styles).toContain('body.density-compact .vella-html-parity-root #tab-products #mainTable .manager-cell-wrap')
    expect(styles).toContain('body.density-compact .vella-html-parity-root #tab-products #mainTable .mgr-avatar')
    expect(styles).toContain('body.density-compact .vella-html-parity-root #tab-products #mainTable .manager-cell-wrap b')
    expect(styles).toContain('font-size: 7.5px !important')
    expect(styles).toContain('body.density-compact .vella-html-parity-root #tab-products #mainTable .tpl-chip')
    expect(styles).toContain('body.density-compact .vella-html-parity-root #tab-products #mainTable .price-edit-i')
    expect(styles).toContain('body.density-compact .vella-html-parity-root #tab-products #mainTable .price')
    expect(styles).toContain('body.density-compact .vella-html-parity-root #tab-products #mainTable .metric-stack')
    expect(styles).toContain('body.density-compact .vella-html-parity-root #tab-products #mainTable .brand-chip')
    expect(styles).toContain('body.density-compact .vella-html-parity-root #tab-products #mainTable .mgr-name')
    expect(styles).toContain('body.density-compact .vella-html-parity-root #tab-products #mainTable .comment-cell')
  })

  it('hides basket and order mini bars in every density, only in the repricer table', () => {
    expect(styles).toMatch(
      /^\s*\.vella-html-parity-root #tab-products #mainTable \.bsk-spark\s*\{\s*display: none;\s*\}/m,
    )
  })
})
