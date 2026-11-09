import { readFileSync } from 'node:fs'
import { createContext, runInContext } from 'node:vm'
import ts from 'typescript'
import { afterEach, describe, expect, it, vi } from 'vitest'

// Exercise the actual bridge and loading effects without a browser, credentials,
// a live backend, or the unrelated islands in the legacy page.
const source = readFileSync(new URL('./VellaHtmlParityPage.tsx', import.meta.url), 'utf8')
const ast = ts.createSourceFile('parity.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
function declaration(name: string) {
  const node = ast.statements.find(statement => ts.isFunctionDeclaration(statement) && statement.name?.text === name)
  if (!node || !ts.isFunctionDeclaration(node)) throw new Error(`Missing function ${name}`)
  return node
}
function compile(text: string) {
  return ts.transpileModule(text, { compilerOptions: { target: ts.ScriptTarget.ES2023 } }).outputText
}
const bridge = compile(declaration('installAvitoRepricerReactBridge').getText(ast))
const effects = declaration('AvitoRepricerIsland').body!.statements
  .filter(statement => ts.isExpressionStatement(statement) && ts.isCallExpression(statement.expression)
    && statement.expression.expression.getText(ast) === 'useEffect')
  .map(statement => compile(statement.getText(ast)))

function harness() {
  const load = vi.fn().mockResolvedValue({ rows: [{ itemId: 'synthetic-old' }] })
  const live = vi.fn()
  const windowState: Record<string, any> = {
    dispatchEvent: vi.fn(), setTimeout, clearTimeout, setInterval, clearInterval,
  }
  const previous: Array<{ deps: unknown[]; cleanup?: () => void }> = []
  let effectIndex = 0
  const context = createContext({
    window: windowState, Date, CustomEvent, AbortController, console,
    accessToken: 'synthetic-test-session', isRoute: true,
    defaultAvitoStatsPeriod: () => ({ from: '2026-10-01', to: '2026-10-08' }),
    // Compatibility stubs let the regression exercise the old timer as well.
    readAvitoRepricerLastRefreshAt: () => Date.now(),
    writeAvitoRepricerLastRefreshAt: vi.fn(),
    isAvitoRepricerCacheHit: () => false,
    AVITO_REPRICER_AUTO_REFRESH_MS: 5 * 60 * 1000,
    loadLiveAvitoRepricer: load,
    setAvitoRepricerLiveState: live,
    avitoBackendErrorMessage: (payload: { status?: string }) => payload.status === 'blocked' ? 'Synthetic statistics error' : null,
    showAvitoCooldownToast: vi.fn(),
    useEffect(callback: () => (() => void) | undefined, deps: unknown[]) {
      const index = effectIndex++
      const prior = previous[index]
      if (prior && prior.deps.length === deps.length && deps.every((value, i) => Object.is(value, prior.deps[i]))) return
      prior?.cleanup?.()
      previous[index] = { deps, cleanup: callback() }
    },
  })
  runInContext(`${bridge}\ninstallAvitoRepricerReactBridge()`, context)
  function render() {
    context.state = { ...windowState.__vellaAvitoRepricerState }
    effectIndex = 0
    effects.forEach(effect => runInContext(effect, context))
  }
  return { load, live, windowState, render }
}

afterEach(() => vi.useRealTimers())

describe('Avito repricer manual data refresh', () => {
  it('loads on opening but does not reload after the old five-minute interval', async () => {
    vi.useFakeTimers()
    const test = harness()
    test.render()
    await Promise.resolve()
    test.render()
    expect(test.load).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(16 * 60 * 1000)
    test.render()
    expect(test.load).toHaveBeenCalledTimes(1)
    expect(test.windowState.__vellaAvitoRepricerState.requestSeq).toBe(0)
  })

  it('refreshes the applied period and replaces products once per manual click', async () => {
    vi.useFakeTimers()
    const test = harness()
    test.windowState.__vellaSetAvitoRepricerState({
      dateFrom: '2026-10-07', dateTo: '2026-10-11', dateFromDraft: '2026-09-01', dateToDraft: '2026-09-30',
    })
    test.render()
    await Promise.resolve()
    test.render()
    const next = { rows: [{ itemId: 'synthetic-new' }] }
    test.load.mockResolvedValue(next)
    test.windowState.refreshAvitoRepricer()
    test.render()
    await Promise.resolve()
    test.render() // Clearing forceRefresh must not cause a second GET.
    expect(test.load).toHaveBeenCalledTimes(2)
    expect(test.load.mock.calls[1][1]).toEqual({ dateFrom: '2026-10-07', dateTo: '2026-10-11', forceRefresh: true })
    expect(test.live).toHaveBeenLastCalledWith({ loading: false, error: null, data: next })
    test.windowState.__vellaSetAvitoRepricerState({ query: 'synthetic-new', filter: 'keep' })
    test.render()
    await vi.advanceTimersByTimeAsync(6 * 60 * 1000)
    test.render()
    expect(test.load).toHaveBeenCalledTimes(2)
    test.windowState.refreshAvitoRepricer()
    test.render()
    await Promise.resolve()
    test.render()
    expect(test.load).toHaveBeenCalledTimes(3)
  })

  it('removes the auto-refresh indicator but keeps the manual button and cooldown guard', () => {
    const toolbar = declaration('AvitoRepricerToolbarIsland').getText(ast)
    expect(toolbar).not.toContain('AvitoAutoRefreshTimer')
    expect(toolbar).toContain('<AvitoRefreshButton')
    expect(toolbar).toContain('cooldownUntil={cooldownUntil}')
    expect(toolbar).toContain('window.refreshAvitoRepricer?.()')
  })

  it('exposes a partial statistics error even when inventory rows are available', async () => {
    vi.useFakeTimers()
    const test = harness()
    const data = { status: 'partial', rows: [{ itemId: 'synthetic-item', views: null }],
      source: { error: { code: 'rate_limited' } } }
    test.load.mockResolvedValue(data)
    test.render()
    await Promise.resolve()
    expect(test.live).toHaveBeenLastCalledWith({ loading: false, error: 'Synthetic statistics error', data })
    expect(declaration('AvitoRepricerIsland').getText(ast)).toContain('avito-repricer-source-error')
  })
})
