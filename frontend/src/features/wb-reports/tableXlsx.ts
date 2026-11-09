import { authorizationHeaders } from '@/features/auth/authApi'
import { buildApiUrl } from '@/lib/api'

export type WbReportTableKind = 'abc' | 'ads' | 'week'

type DownloadLink = {
  href: string
  download: string
  click: () => void
  remove?: () => void
}

type DownloadDeps = {
  createObjectUrl?: (blob: Blob) => string
  revokeObjectUrl?: (url: string) => void
  createLink?: () => DownloadLink
}

function filenameFromContentDisposition(value: string | null) {
  const utfMatch = value?.match(/filename\*=UTF-8''([^;]+)/i)
  if (utfMatch?.[1]) return decodeURIComponent(utfMatch[1].replace(/"/g, ''))
  return value?.match(/filename="?([^";]+)"?/i)?.[1] ?? null
}

export async function downloadReportTableXlsx(
  accessToken: string,
  reportKind: WbReportTableKind,
  headers: string[],
  rows: string[][],
  deps: DownloadDeps = {},
) {
  const response = await fetch(buildApiUrl('/api/wb/reports/table.xlsx'), {
    method: 'POST',
    headers: { ...authorizationHeaders(accessToken), 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ reportKind, headers, rows }),
  })
  if (!response.ok) throw new Error(`Не удалось выгрузить XLSX: ${response.status}`)

  const blob = await response.blob()
  const filename = filenameFromContentDisposition(response.headers.get('content-disposition'))
    ?? `wb-${reportKind}-${new Date().toISOString().slice(0, 10)}.xlsx`
  const createObjectUrl = deps.createObjectUrl ?? URL.createObjectURL.bind(URL)
  const revokeObjectUrl = deps.revokeObjectUrl ?? URL.revokeObjectURL.bind(URL)
  const createLink = deps.createLink ?? (() => document.createElement('a'))
  const url = createObjectUrl(blob)
  const link = createLink()
  link.href = url
  link.download = filename
  link.click()
  link.remove?.()
  revokeObjectUrl(url)
  return filename
}
