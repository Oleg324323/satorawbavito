import { afterEach, describe, expect, it, vi } from 'vitest'

import { downloadAvitoOrdersPickingXlsx, downloadFreshAvitoPickingXlsx } from './avitoOrdersXlsx'
import { collectAvitoForXlsx } from './avitoExtensionBridge'
vi.mock('./avitoExtensionBridge', () => ({ collectAvitoForXlsx: vi.fn() }))

describe('downloadAvitoOrdersPickingXlsx', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.clearAllMocks()
  })

  it('downloads the Avito picking list for the selected account', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(new Blob(['xlsx']), {
        status: 200,
        headers: {
          'content-type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
          'content-disposition': 'attachment; filename="avito-picking-list-2026-07-01.xlsx"',
        },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const link = { href: '', download: '', click: vi.fn(), remove: vi.fn() }
    const createObjectUrl = vi.fn(() => 'blob:avito-orders')
    const revokeObjectUrl = vi.fn()

    const filename = await downloadAvitoOrdersPickingXlsx('access-token', { accountId: 'account-1' }, {
      createObjectUrl,
      revokeObjectUrl,
      createLink: () => link,
      scheduleCleanup: callback => callback(),
    })

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/avito/orders/picking-list.xlsx?accountId=account-1'),
      expect.objectContaining({
        headers: { Authorization: 'Bearer access-token' },
        credentials: 'include',
      }),
    )
    expect(filename).toBe('avito-picking-list-2026-07-01.xlsx')
    expect(link.href).toBe('blob:avito-orders')
    expect(link.download).toBe('avito-picking-list-2026-07-01.xlsx')
    expect(link.click).toHaveBeenCalledTimes(1)
    expect(link.remove).toHaveBeenCalledTimes(1)
    expect(createObjectUrl).toHaveBeenCalledTimes(1)
    expect(revokeObjectUrl).toHaveBeenCalledWith('blob:avito-orders')
  })

  it('times out a hanging request instead of leaving the button busy forever', async () => {
    vi.stubGlobal('fetch', vi.fn((_url, options) => new Promise((_resolve, reject) => {
      options.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
    })))
    await expect(downloadAvitoOrdersPickingXlsx('test-token', {}, { timeoutMs: 5 })).rejects.toThrow('слишком много времени')
  })

  it('reports expired sessions, missing data and server errors without creating a download', async () => {
    for (const [status, message] of [[401, 'Сессия истекла'], [404, 'Сохранённых заказов пока нет'], [503, 'Сервер временно недоступен']] as const) {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', { status })))
      await expect(downloadAvitoOrdersPickingXlsx('test-token', {})).rejects.toThrow(message)
    }
  })

  it('does not download an HTML error page as an Excel file', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('<html/>', { headers: { 'content-type': 'text/html' } })))
    await expect(downloadAvitoOrdersPickingXlsx('test-token', {})).rejects.toThrow('не Excel-файл')
  })

  it.each([true, false])('checks DB freshness and refreshes only when stale: fresh=%s', async fresh => {
    const sequence: string[] = []
    let checks = 0
    vi.mocked(collectAvitoForXlsx).mockImplementation(async () => {
      sequence.push('collect')
      return { state: 'partial', message: 'Размер одного товара требует проверки' }
    })
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url.includes('/freshness')) {
        sequence.push('check')
        return Response.json({ fresh: fresh || checks++ > 0 })
      }
      sequence.push('xlsx')
      expect(url).toContain('accountId=a')
      expect(url).toContain('requireFresh=true')
      return new Response('xlsx', { headers: { 'content-type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' } })
    }))
    const click = vi.fn()
    await downloadFreshAvitoPickingXlsx('synthetic', { accountId: 'a' }, {
      createObjectUrl: () => 'blob:synthetic', revokeObjectUrl: () => {},
      createLink: () => ({ href: '', download: '', click }), scheduleCleanup: callback => callback(),
    })
    expect(sequence).toEqual(fresh ? ['check', 'xlsx'] : ['check', 'collect', 'check', 'xlsx'])
    expect(click).toHaveBeenCalledOnce()
  })

  it.each(['unavailable', 'error', 'partial'] as const)('never exports stale data after collection outcome %s', async state => {
    const fetchMock = vi.fn(async (_url: string) => Response.json({ fresh: false }))
    vi.stubGlobal('fetch', fetchMock)
    vi.mocked(collectAvitoForXlsx).mockResolvedValue({ state, message: 'Не удалось обновить' })
    await expect(downloadFreshAvitoPickingXlsx('synthetic', {})).rejects.toThrow()
    expect(fetchMock.mock.calls.every(args => String(args[0]).includes('/freshness'))).toBe(true)
  })
})
