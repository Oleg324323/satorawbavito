import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import ts from 'typescript'
import { describe, expect, it, vi } from 'vitest'

const source = readFileSync(new URL('./VellaHtmlParityPage.tsx', import.meta.url), 'utf8')
const compile = (text: string) => ts.transpileModule(text, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None },
}).outputText

describe('report period selection and refresh', () => {
  it.each(['from', 'to'])('keeps the %s boundary as a draft until the current React calendar applies it', boundary => {
    const calendar = readFileSync(new URL('../wb-repricer/WbPeriodCalendar.tsx', import.meta.url), 'utf8')
    const start = calendar.indexOf('  const allowed = ')
    const end = calendar.indexOf('\n  const toggle = ', start)
    const applied = vi.fn()
    const setFrom = vi.fn(), setTo = vi.fn()
    const context = {
      fromIso: '2026-10-01', toIso: '2026-10-08', boundary,
      minIso: '2026-09-01', maxIso: '2026-10-12',
      isSelectableWbDay: (day: string, min: string) => day >= min,
      setFrom, setTo, setError: vi.fn(), setBoundary: vi.fn(), setOpen: vi.fn(), onApply: applied,
    }
    expect(start).toBeGreaterThan(0)
    const handlers = runInNewContext(`${compile(calendar.slice(start, end))}; ({choose, apply})`, context)
    handlers.choose('2026-10-07')
    expect(boundary === 'from' ? setFrom : setTo).toHaveBeenCalledWith('2026-10-07')
    expect(applied).not.toHaveBeenCalled()
    handlers.apply()
    expect(applied).toHaveBeenCalledOnce()
    expect(applied).toHaveBeenCalledWith('2026-10-01', '2026-10-08')
  })

  it.each([false, undefined, true])('does not pin a partial ABC preview in memory (fresh=%s)', fresh => {
    const start = source.indexOf('function cacheAbcReport(')
    const end = source.indexOf('\nexport function installAbcLiveDataBridge', start)
    const memory = new Map([['period', { cachedAt: Date.now() }]])
    const store = runInNewContext(`${compile(source.slice(start, end))}; cacheAbcReport`, {
      abcReportMemoryCache: memory, ABC_REPORT_MEMORY_TTL_MS: 300_000, Date,
    })
    store('period', { cache: { fresh } }, [])
    expect(memory.has('period')).toBe(fresh === true)
  })

  it('publishes an immediately ready cached refresh and avoids forced recollection', async () => {
    const start = source.indexOf('  async function startRefresh()')
    const end = source.indexOf('\n  return (', start)
    const completed = vi.fn(), setJob = vi.fn(), request = vi.fn(async (_url: string, _options: object) => ({ state: 'completed' }))
    const run = runInNewContext(`${compile(source.slice(start, end))}; startRefresh`, {
      accessToken: 'synthetic', running: false, reportId: 'abc', period: {}, groupBy: 'sku', source: 'operational',
      backgroundReportJobStartPath: () => '/jobs', authorizationHeaders: () => ({}),
      apiRequest: request, describePnlReportJob: (x: unknown) => x, setJob, onCompleted: completed, ApiError: Error,
      refreshRequestRef: { current: null }, AbortController,
    })
    await run()
    expect(request.mock.calls[0][0]).toBe('/jobs')
    expect(completed).toHaveBeenCalledOnce()
    expect(setJob).toHaveBeenCalledWith({ state: 'completed' })
  })

  it('ignores a late refresh response after switching periods', async () => {
    const start = source.indexOf('  async function startRefresh()')
    const end = source.indexOf('\n  return (', start)
    const completed = vi.fn(), setJob = vi.fn()
    let finish!: (value: object) => void
    const response = new Promise(resolve => { finish = resolve })
    const ref = { current: null as AbortController | null }
    const run = runInNewContext(`${compile(source.slice(start, end))}; startRefresh`, {
      accessToken: 'synthetic', running: false, reportId: 'abc', period: {}, groupBy: 'sku', source: 'operational',
      backgroundReportJobStartPath: () => '/jobs', authorizationHeaders: () => ({}),
      apiRequest: () => response, describePnlReportJob: (x: unknown) => x,
      setJob, onCompleted: completed, ApiError: Error, refreshRequestRef: ref, AbortController,
    })
    const pending = run()
    ref.current?.abort()
    ref.current = null
    finish({ state: 'completed' })
    await pending
    expect(setJob).not.toHaveBeenCalled()
    expect(completed).not.toHaveBeenCalled()
  })
})
