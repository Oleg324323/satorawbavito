import { buildApiUrl } from '@/lib/api'
import { avitoReviewsPreviewScope, avitoReviewsPreviewRequest, buildAvitoReviewsPreviewPath, parseAvitoReviewsPreview,
  type AvitoReviewsPreviewScope, type AvitoReviewsPreviewRequest } from './canonicalAvitoReviewsPreview'

export class AvitoReviewsPreviewError extends Error {
  readonly kind: 'invalid' | 'scope' | 'unavailable' | 'stale' | 'timeout'
  constructor(kind: AvitoReviewsPreviewError['kind']) {
    super(kind === 'invalid' ? 'Некорректный запрос или ответ предварительного просмотра отзывов Авито.'
      : kind === 'scope' ? 'Нет доступа к метаданным отзывов выбранного аккаунта Авито.'
        : kind === 'stale' ? 'Выбор или сессия изменились; предварительный просмотр отменён.'
          : kind === 'timeout' ? 'Истекло время ожидания метаданных отзывов Авито. Автоматического повтора нет.' : 'Метаданные отзывов Авито недоступны.')
    this.kind = kind
  }
}
/** Dormant, explicit one-page metadata reads. No effect, offset advancement,
 * cache, retries, provider calls, legacy fallback, policy or answer command.
 * canAnswer is source metadata, never a send permission. Capture a new client
 * per auth/org/session/internal+external account epoch; dispose on transition.
 */
export function createAvitoReviewsPreviewClient(token: string, scope: AvitoReviewsPreviewScope, isCurrent: () => boolean, fetcher: typeof fetch = fetch) {
  let captured: AvitoReviewsPreviewScope
  try { captured = avitoReviewsPreviewScope.parse(scope) } catch { throw new AvitoReviewsPreviewError('invalid') }
  let disposed = false, sequence = 0, pending: AbortController | null = null
  function invalidate() { ++sequence; pending?.abort(); pending = null }
  return {
    invalidate,
    dispose() { disposed = true; invalidate() },
    async load(request: AvitoReviewsPreviewRequest, signal?: AbortSignal) {
      let expected: AvitoReviewsPreviewRequest, path: string
      try { expected = avitoReviewsPreviewRequest.parse(request); path = buildAvitoReviewsPreviewPath(captured, expected) } catch { throw new AvitoReviewsPreviewError('invalid') }
      invalidate()
      const id = sequence, controller = new AbortController(), abort = () => controller.abort()
      const active = () => !disposed && id === sequence && Boolean(token) && isCurrent() && !controller.signal.aborted
      if (signal?.aborted || !active()) throw new AvitoReviewsPreviewError('stale')
      pending = controller; signal?.addEventListener('abort', abort, { once: true })
      let timedOut = false, complete = false, response: Response | undefined, reader: ReadableStreamDefaultReader<Uint8Array> | undefined
      const timer = setTimeout(() => { timedOut = true; abort() }, 20_000)
      const cancelled = new Promise<never>((_, reject) => controller.signal.addEventListener('abort', () => reject(new AvitoReviewsPreviewError(timedOut ? 'timeout' : 'stale')), { once: true }))
      void cancelled.catch(() => {})
      try {
        const fetching = fetcher(buildApiUrl(path), { method: 'GET', credentials: 'omit', cache: 'no-store', redirect: 'error', signal: controller.signal,
          headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' } }).then(async result => {
          if (controller.signal.aborted) { try { await result.body?.cancel() } catch { /* preserve cancellation */ } }
          return result
        })
        response = await Promise.race([fetching, cancelled])
        if (!active()) throw new AvitoReviewsPreviewError('stale')
        if (response.status !== 200) throw new AvitoReviewsPreviewError(response.status === 401 || response.status === 403 ? 'scope'
          : response.status === 400 || response.status === 422 ? 'invalid' : 'unavailable')
        if (!response.body || response.headers.get('content-type')?.split(';')[0].trim().toLowerCase() !== 'application/json') throw new AvitoReviewsPreviewError('invalid')
        reader = response.body.getReader()
        const decoder = new TextDecoder('utf-8', { fatal: true }); let text = '', bytes = 0
        while (true) {
          const chunk = await Promise.race([reader.read(), cancelled])
          if (!active()) throw new AvitoReviewsPreviewError('stale')
          if (chunk.done) break
          bytes += chunk.value.byteLength
          if (bytes > 1_048_576) throw new AvitoReviewsPreviewError('invalid')
          text += decoder.decode(chunk.value, { stream: true })
        }
        const result = parseAvitoReviewsPreview(JSON.parse(text + decoder.decode()), captured, expected)
        complete = true; return result
      } catch (error) {
        if (error instanceof AvitoReviewsPreviewError) throw error
        throw new AvitoReviewsPreviewError(timedOut ? 'timeout' : !active() ? 'stale' : response ? 'invalid' : 'unavailable')
      } finally {
        if (!complete) { abort(); try { if (reader) await reader.cancel(); else await response?.body?.cancel() } catch { /* preserve sanitized error */ } }
        try { reader?.releaseLock() } catch { /* preserve result */ }
        clearTimeout(timer); signal?.removeEventListener('abort', abort); if (pending === controller) pending = null
      }
    },
  }
}
