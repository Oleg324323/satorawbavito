import { afterEach, expect, test, vi } from 'vitest'
import { downloadRepricerTable } from './tableExport'
import { loadLiveRepricerParityProducts } from './liveParityData'

vi.mock('./liveParityData', () => ({ loadLiveRepricerParityProducts: vi.fn() }))
afterEach(() => { vi.resetAllMocks(); vi.unstubAllGlobals() })
const query = { dateFrom: '2026-10-01', dateTo: '2026-10-06', periodDays: 6 }
const cache = { dateFrom: query.dateFrom, dateTo: query.dateTo, basketsFetchedAt: 'observation' }
const page = (ids: number[]) => ({ total: 620, cache, products: ids.map(nmId => ({ nmId })) })

test('fetches every page then applies all UI filters and sorting, not selected/visible rows', async () => {
  const load = vi.mocked(loadLiveRepricerParityProducts)
  load.mockResolvedValueOnce(page(Array.from({length:500}, (_, i) => i+1)) as any)
    .mockResolvedValueOnce(page(Array.from({length:120}, (_, i) => i+501)) as any)
  const fetch = vi.fn().mockResolvedValue(new Response('xlsx')); vi.stubGlobal('fetch', fetch)
  const result = await downloadRepricerTable('synthetic', query,
    rows => rows.filter(row => row.nmId % 2 === 0).reverse(), 'active filters + descending sort', new AbortController().signal)
  expect(result.count).toBe(310)
  expect(load.mock.calls.map(call => call[2])).toEqual([1, 2].map(page => ({ ...query, page, pageSize:500, topMode:false })))
  const body = JSON.parse(fetch.mock.calls[0][1].body)
  expect(body.nmIds).toHaveLength(310)
  expect(body.nmIds[0]).toBe(620)
  expect(body.nmIds.at(-1)).toBe(2)
  expect(body).toMatchObject({ dateFrom: query.dateFrom, dateTo: query.dateTo })
  expect(body.filterDescription).toContain('descending sort')
  expect(body).not.toHaveProperty('rows') // Cells are rebuilt from authenticated server sources.
})

test.each(['wrong period', 'incomplete', 'changed revision'])('rejects %s instead of a misleading partial file', async kind => {
  const load = vi.mocked(loadLiveRepricerParityProducts)
  load.mockResolvedValueOnce(page([1]) as any).mockResolvedValueOnce({ ...page([]),
    cache: kind === 'wrong period' ? {...cache, dateFrom:'2026-09-25'}
      : kind === 'changed revision' ? {...cache, basketsFetchedAt:'changed'} : cache } as any)
  const fetch = vi.fn(); vi.stubGlobal('fetch', fetch)
  await expect(downloadRepricerTable('synthetic', query, rows => rows, '{}', new AbortController().signal)).rejects.toThrow()
  expect(fetch).not.toHaveBeenCalled()
})
