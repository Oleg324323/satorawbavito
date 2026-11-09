import { authorizationHeaders } from '@/features/auth/authApi'
import { buildApiUrl } from '@/lib/api'

export async function downloadWeekTable(accessToken: string, payload: {
  dateFrom: string; dateTo: string; sourceState: string; warning: string; headers: string[]; rows: string[][]
}) {
  const response = await fetch(buildApiUrl('/api/wb/reports/week-over-week/table.xlsx'), {
    method: 'POST', credentials: 'include',
    headers: { ...authorizationHeaders(accessToken), 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!response.ok) throw new Error(`Не удалось выгрузить XLSX: ${response.status}`)
  if (!response.headers.get('content-type')?.includes('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')) {
    throw new Error('Сервер вернул не XLSX-файл')
  }
  const blob = await response.blob()
  if (!blob.size) throw new Error('Сервер вернул пустой файл')
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  const filename = `wb-week-${payload.dateFrom}-${payload.dateTo}.xlsx`
  link.href = url; link.download = filename
  document.body.appendChild(link)
  link.click(); link.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
  return filename
}
