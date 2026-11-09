import { afterEach, expect, it, vi } from 'vitest'
import { installRepricerStatsLiveBridge, renderLiveRepricerStatsRow, statsAggregate, statsMetricTotals } from './VellaHtmlParityPage'
import { loadLiveRepricerStats, resetLiveRepricerParityCache, type LiveRepricerStatsItem } from '../wb-repricer/liveParityData'

afterEach(() => { resetLiveRepricerParityCache(); vi.unstubAllGlobals() })
const item = (metrics: LiveRepricerStatsItem['metrics']) => ({ articleId: 'fixture', nmId: 101, metrics }) as LiveRepricerStatsItem
const json = (payload: unknown) => new Response(JSON.stringify(payload), { headers: { 'content-type': 'application/json' } })

it('keeps total and advertising impressions separate, renders DRR to two decimals and removes only requested columns', () => {
  const html = renderLiveRepricerStatsRow(item({ totalImpressions: 113741, impressions: 15, drrPct: .02 }))
  expect(html).toMatch(/data-stats-column="totalImpressions">113\s*741<\/td>/)
  expect(html).toContain('data-stats-column="impressions">15</td>')
  expect(html).toContain('data-stats-column="drrPct">0,02%')
  for (const column of ['stockUnits', 'protection', 'sources']) expect(html).not.toContain(`data-stats-column="${column}"`)
  expect((html.match(/<td\b/g) || []).length).toBe(15)
})

it('uses weighted ratios and distinguishes missing observations, zero spend and undefined denominators', () => {
  const rows = [item({ adSpendKopecks: 100, adRevenueKopecks: 1000, clicks: 1, impressions: 10, orders: 2, baskets: 4 }),
    item({ adSpendKopecks: 900, adRevenueKopecks: 3000, clicks: 9, impressions: 90, orders: 3, baskets: 6 })]
  const totals = statsMetricTotals(rows)
  expect(totals.drrPct.value).toBe(25)
  expect(totals.ctrPct.value).toBe(10)
  expect(totals.cartToOrderCrPct.value).toBe(50)
  expect(statsAggregate(rows, 'drrPct')).toBe(25)
  expect(statsAggregate([item({ adSpendKopecks: 0, adRevenueKopecks: 100 })], 'drrPct')).toBe(0)
  expect(statsAggregate([item({ adSpendKopecks: 0, adRevenueKopecks: 0 })], 'drrPct')).toBeNull()
  expect(statsMetricTotals([item({ adSpendKopecks: null })]).adSpendKopecks.value).toBeNull()
  expect(statsAggregate([...rows, item({ adSpendKopecks: null })], 'adSpendKopecks')).toBeNull()
})

it('rejects another reporting period rather than displaying it as the selected dates', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => json({ items: [], dateFrom: '2026-10-01', dateTo: '2026-10-06' })))
  await expect(loadLiveRepricerStats('fixture', undefined, { dateFrom: '2026-10-01', dateTo: '2026-10-09' })).rejects.toThrow('другой период')
})

it('starts collection after page one without waiting for the remaining catalog pages', async () => {
  vi.stubGlobal('window', { addEventListener: vi.fn(), __vellaReportPeriods: { 'repricer-stats': { days: 9, mode: 'custom', fromIso: '2026-10-01', toIso: '2026-10-09' } } })
  vi.stubGlobal('document', { getElementById: () => null, querySelector: () => null })
  let finishPage: (() => void) | undefined
  const jobs: URL[] = []
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    const url = new URL(path, 'http://fixture')
    if (init?.method === 'POST') {
      jobs.push(url)
      return json({ state: 'failed', error: 'synthetic unavailable' })
    }
    const response = () => json({ items: [], total: 501, dateFrom: '2026-10-01', dateTo: '2026-10-09' })
    if (url.searchParams.get('page') === '2') return new Promise<Response>(resolve => { finishPage = () => resolve(response()) })
    return response()
  }))
  const dispose = installRepricerStatsLiveBridge('fixture')
  const loading = window.__vellaLoadLiveRepricerStats!()
  try {
    await vi.waitFor(() => expect(finishPage).toBeDefined())
    expect(jobs).toHaveLength(1)
    expect(jobs[0].searchParams.get('from')).toBe('2026-10-01')
    expect(jobs[0].searchParams.get('to')).toBe('2026-10-09')
  } finally { finishPage?.(); await loading; dispose() }
})

it('collects the existing three-day comparison windows independently from the selected range', async () => {
  vi.stubGlobal('window', { addEventListener: vi.fn(), __vellaReportPeriods: { 'repricer-stats': { days: 9, mode: 'custom', fromIso: '2026-10-01', toIso: '2026-10-09' } } })
  vi.stubGlobal('document', { getElementById: () => null,
    querySelector: (selector: string) => selector.includes('th[data-stats-trend]') ? {} : null, querySelectorAll: () => [] })
  const jobs: URL[] = []
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    const url = new URL(path, 'http://fixture')
    if (init?.method === 'POST') {
      jobs.push(url)
      return json({ state: 'completed' })
    }
    return json({ items: [], total: 0,
      dateFrom: url.searchParams.get('dateFrom'), dateTo: url.searchParams.get('dateTo') })
  }))
  const dispose = installRepricerStatsLiveBridge('fixture')
  try {
    await window.__vellaLoadLiveRepricerStats!()
    await vi.waitFor(() => expect(jobs.filter(url => url.searchParams.get('source') === 'statistics-comparison')).toHaveLength(2))
    expect(jobs.map(url => [url.searchParams.get('from'), url.searchParams.get('to')])).toEqual([
      ['2026-10-01', '2026-10-09'], ['2026-10-07', '2026-10-09'], ['2026-10-04', '2026-10-06'],
    ])
  } finally { dispose() }
})
