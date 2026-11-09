import { afterEach, describe, expect, it, vi } from 'vitest'

import { downloadReportTableXlsx } from './tableXlsx'

describe('downloadReportTableXlsx', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('posts the report rows and downloads the returned workbook', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(new Blob(['xlsx']), {
      status: 200,
      headers: {
        'content-disposition': 'attachment; filename="wb-abc-2026-10-01.xlsx"',
      },
    }))
    vi.stubGlobal('fetch', fetchMock)
    const link = { href: '', download: '', click: vi.fn(), remove: vi.fn() }
    const createObjectUrl = vi.fn(() => 'blob:wb-report')
    const revokeObjectUrl = vi.fn()

    const filename = await downloadReportTableXlsx('token', 'abc', ['Артикул', 'Продажи, шт'], [['SKU-1', '3']], {
      createObjectUrl,
      revokeObjectUrl,
      createLink: () => link,
    })

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/api/wb/reports/table.xlsx'),
      expect.objectContaining({
        method: 'POST',
        credentials: 'include',
        headers: expect.objectContaining({ Authorization: 'Bearer token', 'Content-Type': 'application/json' }),
        body: JSON.stringify({ reportKind: 'abc', headers: ['Артикул', 'Продажи, шт'], rows: [['SKU-1', '3']] }),
      }),
    )
    expect(filename).toBe('wb-abc-2026-10-01.xlsx')
    expect(link).toMatchObject({ href: 'blob:wb-report', download: filename })
    expect(link.click).toHaveBeenCalledOnce()
    expect(revokeObjectUrl).toHaveBeenCalledWith('blob:wb-report')
  })
})
