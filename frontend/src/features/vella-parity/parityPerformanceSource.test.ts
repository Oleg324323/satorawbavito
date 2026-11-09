import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const source = readFileSync(new URL('./VellaHtmlParityPage.tsx', import.meta.url), 'utf8')

describe('Vella parity performance guards', () => {
  it('does not run the legacy tab engine or sticky header observer during route changes', () => {
    const routeSync = source.slice(
      source.indexOf('function syncLegacyRuntimeToRoute'),
      source.indexOf('const DEFAULT_PRODUCTS_EMPTY_STATE'),
    )
    const baselineEffect = source.slice(
      source.indexOf('installSecondaryReportHeaderGuard()'),
      source.indexOf('}, [accessToken, runtime])'),
    )

    expect(routeSync).not.toContain('_goSubtabSilent')
    expect(routeSync).not.toContain('__vellaLoadLiveRepricerProducts')
    expect(routeSync).not.toContain('initTooltips')
    expect(baselineEffect).not.toContain('installReportStickyHeaderOverlay(root)')
  })

  it('has a single route-driven product load and does not refetch strategies on every path', () => {
    const strategiesLoad = source.indexOf('void loadLiveRepricerStrategies(accessToken, controller.signal)')
    const strategiesEffect = source.slice(
      source.lastIndexOf('useEffect(() => {', strategiesLoad),
      source.indexOf('\n  useEffect(() => {', strategiesLoad),
    )

    expect(source).not.toContain('productsReloadTick')
    expect(strategiesEffect).not.toContain('location.pathname')
    // The actual React stats page must not issue a strategies request off products.
    // repricerStatsPageBrowser.test.ts also mutation-checks this route scope.
    expect(source).toContain('}, [accessToken, productsTabActive, runtime])')
  })
})
