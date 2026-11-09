import { authorizationHeaders } from '@/features/auth/authApi'
import { buildApiUrl } from '@/lib/api'
import { collectAvitoForXlsx } from './avitoExtensionBridge'

export type AvitoOrdersPickingXlsxOptions = {
  accountId?: string
  kind?: 'picking' | 'returns'
  search?: string
  readyOnly?: boolean
  requireFresh?: boolean
}

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
  timeoutMs?: number
  scheduleCleanup?: (callback: () => void) => void
}

function filenameFromContentDisposition(value: string | null) {
  if (!value) return null
  const utfMatch = value.match(/filename\*=UTF-8''([^;]+)/i)
  if (utfMatch?.[1]) return decodeURIComponent(utfMatch[1].replace(/"/g, ''))
  const asciiMatch = value.match(/filename="?([^";]+)"?/i)
  return asciiMatch?.[1] ?? null
}

function buildPickingListPath(options: AvitoOrdersPickingXlsxOptions) {
  const path = options.kind === 'returns' ? 'returns-list.xlsx' : 'picking-list.xlsx'
  const params = new URLSearchParams()
  if (options.accountId) params.set('accountId', options.accountId)
  if (options.search) params.set('search', options.search)
  if (options.readyOnly) params.set('readyOnly', 'true')
  if (options.requireFresh && options.kind !== 'returns') params.set('requireFresh', 'true')
  return `/api/v1/avito/orders/${path}${params.size ? `?${params.toString()}` : ''}`
}

export async function downloadAvitoOrdersPickingXlsx(
  accessToken: string,
  options: AvitoOrdersPickingXlsxOptions,
  deps: DownloadDeps = {},
) {
  const path = buildPickingListPath(options)
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), deps.timeoutMs ?? 25000)
  let blob: Blob, disposition: string | null
  try {
    const response = await fetch(buildApiUrl(path), {
      headers: authorizationHeaders(accessToken),
      credentials: 'include',
      signal: controller.signal,
    })
    if (!response.ok) {
      if (response.status === 401) throw new Error('Сессия истекла. Войдите снова и скачайте лист подбора.')
      if (response.status === 404) throw new Error('Сохранённых заказов пока нет. Нажмите «Обновить данные», затем скачайте лист подбора.')
      if (response.status === 503) throw new Error('Сервер временно недоступен. Повторите скачивание.')
      if (response.status === 409) throw new Error('Актуальность заказов не подтверждена. Нажмите «Собрать всё» в расширении и повторите экспорт.')
      throw new Error(`Не удалось скачать лист подбора Авито: ${response.status}`)
    }
    if (!response.headers.get('content-type')?.includes('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')) {
      throw new Error('Сервер вернул не Excel-файл. Обновите страницу и повторите скачивание.')
    }
    blob = await response.blob()
    if (!blob.size) throw new Error('Сервер вернул пустой файл. Повторите скачивание.')
    disposition = response.headers.get('content-disposition')
  } catch (error) {
    if (controller.signal.aborted) throw new Error('Выгрузка заняла слишком много времени. Повторите скачивание.')
    throw error
  } finally { clearTimeout(timeout) }
  const filename = filenameFromContentDisposition(disposition)
    ?? `avito-${options.kind === 'returns' ? 'returns' : 'picking'}-list.xlsx`
  const createObjectUrl = deps.createObjectUrl ?? URL.createObjectURL.bind(URL)
  const revokeObjectUrl = deps.revokeObjectUrl ?? URL.revokeObjectURL.bind(URL)
  const createLink = deps.createLink ?? (() => {
    const anchor = document.createElement('a')
    anchor.style.display = 'none'
    document.body.appendChild(anchor)
    return anchor
  })
  const url = createObjectUrl(blob)
  const link = createLink()
  link.href = url
  link.download = filename
  try { link.click() } finally {
    link.remove?.()
    // Let the browser start consuming the blob before releasing it.
    const schedule = deps.scheduleCleanup ?? ((callback: () => void) => setTimeout(callback, 30000))
    schedule(() => revokeObjectUrl(url))
  }
  return filename
}

export async function checkAvitoPickingFreshness(accessToken: string, accountId?: string): Promise<boolean> {
  const query = accountId ? `?accountId=${encodeURIComponent(accountId)}` : ''
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), 15000)
  try {
    const response = await fetch(buildApiUrl(`/api/v1/avito/orders/picking-list/freshness${query}`), {
      headers: authorizationHeaders(accessToken), credentials: 'include', signal: controller.signal,
    })
    if (!response.ok) throw new Error(`Не удалось проверить актуальность заказов: ${response.status}`)
    const result = await response.json()
    if (typeof result?.fresh !== 'boolean') throw new Error('Сервер не подтвердил актуальность заказов.')
    return result.fresh
  } finally { clearTimeout(timer) }
}

export async function downloadFreshAvitoPickingXlsx(
  accessToken: string,
  options: AvitoOrdersPickingXlsxOptions,
  deps: DownloadDeps & { onStatus?: (status: string) => void } = {},
) {
  deps.onStatus?.('Проверяем актуальность заказов…')
  if (!await checkAvitoPickingFreshness(accessToken, options.accountId)) {
    deps.onStatus?.('Обновляем заказы через расширение Авито…')
    const result = await collectAvitoForXlsx()
    if (result.state === 'error' || result.state === 'unavailable') throw new Error(result.message)
    if (!await checkAvitoPickingFreshness(accessToken, options.accountId)) throw new Error('Сбор не подтвердил актуальность всей очереди. Завершите сбор в расширении и повторите экспорт.')
  }
  deps.onStatus?.('Готовим Excel из актуальных данных…')
  return downloadAvitoOrdersPickingXlsx(accessToken, { ...options, kind: 'picking', requireFresh: true }, deps)
}
