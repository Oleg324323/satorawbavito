import { afterEach, beforeEach, expect, it, vi } from 'vitest'

vi.mock('./authTokenStore', () => ({ readStoredAccessToken: () => null }))
beforeEach(() => {
  vi.resetModules()
  vi.stubGlobal('window', { addEventListener: vi.fn() })
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response()))
})
afterEach(() => vi.unstubAllGlobals())

it('releases only the old screen and keeps one consumer across navigation', async () => {
  const demand = await import('./wbDemand')
  demand.setWbDemandRoute('first')
  const first = demand.wbDemandHeaders('/api/wb/reports/abc/jobs', 'POST')
  demand.setWbDemandRoute('second')
  const second = demand.wbDemandHeaders('/api/wb/reports/pnl/jobs', 'POST')
  expect(first['X-WB-Consumer']).toBe(second['X-WB-Consumer'])
  expect(first['X-WB-View']).not.toBe(second['X-WB-View'])
  expect(Number(second['X-WB-Sequence'])).toBeGreaterThan(Number(first['X-WB-Sequence']))
  expect(fetch).toHaveBeenCalledWith('/api/wb/reports/work-demand/release', expect.objectContaining({
    method: 'POST', keepalive: true, headers: expect.objectContaining({ 'X-WB-View': first['X-WB-View'] }),
  }))
  demand.setWbDemandRoute('second')
  expect(fetch).toHaveBeenCalledTimes(1)
})

it('does not send demand identifiers to unrelated or external endpoints', async () => {
  const demand = await import('./wbDemand')
  for (const path of ['/api/auth/login', 'https://external.invalid/api/wb/reports/abc/jobs', '/api/wb/reports/work-demand/release']) {
    expect(demand.wbDemandHeaders(path, 'POST')).toEqual({})
  }
})

it('releases the latest view when the document closes', async () => {
  const demand = await import('./wbDemand')
  demand.setWbDemandRoute('first')
  const current = demand.wbDemandHeaders('/api/wb/reports/abc/jobs', 'POST')
  const handler = vi.mocked(window.addEventListener).mock.calls.find(([name]) => name === 'pagehide')?.[1] as () => void
  handler()
  expect(fetch).toHaveBeenCalledWith(expect.any(String), expect.objectContaining({
    headers: expect.objectContaining({ 'X-WB-View': current['X-WB-View'] }),
  }))
})

it('changing dates releases the old collection while same-period reports share the view', async () => {
  const demand = await import('./wbDemand')
  demand.setWbDemandRoute('statistics')
  const first = demand.wbDemandHeaders('/api/wb/reports/stats/jobs?from=2026-10-01&to=2026-10-05', 'POST')
  const related = demand.wbDemandHeaders('/api/wb/reports/abc/jobs?from=2026-10-01&to=2026-10-05', 'POST')
  expect(first['X-WB-View']).toBe(related['X-WB-View'])
  const next = demand.wbDemandHeaders('/api/wb/reports/stats/jobs?from=2026-09-01&to=2026-09-30', 'POST')
  expect(first['X-WB-View']).not.toBe(next['X-WB-View'])
  expect(fetch).toHaveBeenCalledTimes(1)
})

it('statistics comparisons share the selected-period lease without cancelling it or one another', async () => {
  const demand = await import('./wbDemand')
  demand.setWbDemandRoute('statistics')
  const main = demand.wbDemandHeaders('/api/wb/reports/stats/jobs?from=2026-10-01&to=2026-10-09', 'POST')
  for (const dates of ['from=2026-10-07&to=2026-10-09', 'from=2026-10-04&to=2026-10-06']) {
    const comparison = demand.wbDemandHeaders(`/api/wb/reports/stats/jobs?source=statistics-comparison&${dates}`, 'POST')
    expect(comparison['X-WB-View']).toBe(main['X-WB-View'])
  }
  expect(fetch).not.toHaveBeenCalled()
  const next = demand.wbDemandHeaders('/api/wb/reports/stats/jobs?from=2026-09-01&to=2026-09-09', 'POST')
  expect(next['X-WB-View']).not.toBe(main['X-WB-View'])
  expect(fetch).toHaveBeenCalledOnce()
})
