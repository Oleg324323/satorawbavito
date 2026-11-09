import { ApiError, buildApiUrl } from '@/lib/api'
import { parseWbSync } from './validation'

export const wbHistorySource = 'wb-statistics-supplier-orders'
/** UI deliberately accepts the date-only subset of validate_date_from; sends native text unchanged. */
export function validWbHistoryDate(value: string) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || value.startsWith('0000')) return false
  const time = Date.parse(`${value}T00:00:00Z`)
  return Number.isFinite(time) && new Date(time).toISOString().slice(0, 10) === value
}
export function prepareWbHistory(dateFrom: string) {
  if (!validWbHistoryDate(dateFrom)) throw new Error('Выберите корректную дату начала истории.')
  return Object.freeze({ idempotencyKey: crypto.randomUUID(), body: JSON.stringify({ dateFrom }), dateFrom })
}
export async function startWbHistory(token: string, accountId: number, intent: ReturnType<typeof prepareWbHistory>, signal: AbortSignal, fetcher: typeof fetch = fetch) {
  if (signal.aborted) throw new DOMException('Aborted', 'AbortError')
  if (!Number.isSafeInteger(accountId) || accountId <= 0 || !validWbHistoryDate(intent.dateFrom)
    || intent.body !== JSON.stringify({ dateFrom: intent.dateFrom }) || !/^[A-Za-z0-9._:-]{8,128}$/.test(intent.idempotencyKey)) throw new ApiError('Некорректный запрос истории', 400)
  const controller = new AbortController(), abort = () => controller.abort()
  if (signal.aborted) abort()
  signal.addEventListener('abort', abort, { once: true })
  const timeout = setTimeout(abort, 20_000)
  try {
    // Deliberately bypass auth refresh/replay: a single explicit POST only.
    const response = await fetcher(buildApiUrl(`/api/v2/wb/accounts/${accountId}/history`), {
      method: 'POST', cache: 'no-store', credentials: 'omit', redirect: 'error', signal: controller.signal,
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json', Accept: 'application/json', 'Idempotency-Key': intent.idempotencyKey }, body: intent.body,
    })
    if (response.status !== 200) { await response.body?.cancel(); throw new ApiError('Запрос истории не подтверждён', response.status) }
    if (response.headers.get('content-type')?.split(';')[0].trim() !== 'application/json' || !response.body) throw new ApiError('Некорректный ответ истории', 502)
    const reader = response.body.getReader(), decoder = new TextDecoder('utf-8', { fatal: true })
    let bytes = 0, text = ''
    while (true) {
      const chunk = await reader.read()
      if (chunk.done) break
      bytes += chunk.value.byteLength
      if (bytes > 131_072) { await reader.cancel(); throw new ApiError('Ответ истории превышает лимит', 502) }
      text += decoder.decode(chunk.value, { stream: true })
    }
    if (controller.signal.aborted) throw new DOMException('Aborted', 'AbortError')
    const envelope = JSON.parse(text + decoder.decode())
    const result = parseWbSync(envelope?.data, accountId)
    if (!result.sources.some(source => source.source === wbHistorySource)) throw new ApiError('Источник истории не подтверждён', 502)
    return result
  } finally { clearTimeout(timeout); signal.removeEventListener('abort', abort) }
}
