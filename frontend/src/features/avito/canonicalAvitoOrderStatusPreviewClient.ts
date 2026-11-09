import { buildApiUrl } from '@/lib/api'
import { avitoOrderPreviewRequest, avitoOrderPreviewScope, buildAvitoOrderPreviewPath, parseAvitoOrderStatusPreview,
  type AvitoOrderPreviewRequest, type AvitoOrderPreviewScope } from './canonicalAvitoOrderStatusPreview'

export class AvitoOrderPreviewError extends Error {
  readonly kind: 'invalid' | 'scope' | 'unavailable' | 'stale' | 'timeout'
  constructor(kind: AvitoOrderPreviewError['kind']) {
    super(kind === 'invalid' ? 'Некорректный запрос или ответ предварительного просмотра статусов Авито.'
      : kind === 'scope' ? 'Нет доступа к статусам выбранного аккаунта Авито.'
        : kind === 'stale' ? 'Выбор или сессия изменились; предварительный просмотр отменён.'
          : kind === 'timeout' ? 'Истекло время ожидания статусов Авито. Автоматического повтора нет.' : 'Предварительный просмотр статусов Авито недоступен.')
    this.kind = kind
  }
}
/** Explicit one-page reads only. This partial preview is not the canonical queue,
 * sync, pagination automation or status mutation. Metadata grants no authority.
 * Capture a new client per token/org/session/account epoch; dispose on transition.
 * Editing criteria must invalidate even A→B→A. No cache/localStorage/fallback.
 */
export function createAvitoOrderStatusPreviewClient(token: string, scope: AvitoOrderPreviewScope, isCurrent: () => boolean, fetcher: typeof fetch = fetch) {
  let captured: AvitoOrderPreviewScope
  try { captured = avitoOrderPreviewScope.parse(scope) } catch { throw new AvitoOrderPreviewError('invalid') }
  let disposed = false, sequence = 0, pending: AbortController | null = null
  function invalidate() { ++sequence; pending?.abort(); pending = null }
  return {
    invalidate,
    dispose() { disposed = true; invalidate() },
    async load(request: AvitoOrderPreviewRequest, signal?: AbortSignal) {
      let expected: AvitoOrderPreviewRequest, path: string
      try { expected = avitoOrderPreviewRequest.parse(request); path = buildAvitoOrderPreviewPath(captured, expected) } catch { throw new AvitoOrderPreviewError('invalid') }
      invalidate()
      const id = sequence, controller = new AbortController(), abort = () => controller.abort()
      const active = () => !disposed && id === sequence && Boolean(token) && isCurrent() && !controller.signal.aborted
      if (signal?.aborted || !active()) throw new AvitoOrderPreviewError('stale')
      pending = controller; signal?.addEventListener('abort', abort, { once: true })
      let timedOut = false, complete = false, response: Response | undefined, reader: ReadableStreamDefaultReader<Uint8Array> | undefined
      const timer = setTimeout(() => { timedOut = true; abort() }, 20_000)
      const cancelled = new Promise<never>((_, reject) => controller.signal.addEventListener('abort', () => reject(new AvitoOrderPreviewError(timedOut ? 'timeout' : 'stale')), { once: true }))
      void cancelled.catch(() => {})
      try {
        const fetching = fetcher(buildApiUrl(path), { method: 'GET', credentials: 'omit', cache: 'no-store', redirect: 'error', signal: controller.signal,
          headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' } }).then(async result => {
          if (controller.signal.aborted) { try { await result.body?.cancel() } catch { /* preserve cancellation */ } }
          return result
        })
        response = await Promise.race([fetching, cancelled])
        if (!active()) throw new AvitoOrderPreviewError('stale')
        if (response.status !== 200) throw new AvitoOrderPreviewError(response.status === 401 || response.status === 403 ? 'scope'
          : response.status === 400 || response.status === 422 ? 'invalid' : 'unavailable')
        if (!response.body || response.headers.get('content-type')?.split(';')[0].trim().toLowerCase() !== 'application/json') throw new AvitoOrderPreviewError('invalid')
        reader = response.body.getReader()
        const decoder = new TextDecoder('utf-8', { fatal: true }); let text = '', bytes = 0
        while (true) {
          const chunk = await Promise.race([reader.read(), cancelled])
          if (!active()) throw new AvitoOrderPreviewError('stale')
          if (chunk.done) break
          bytes += chunk.value.byteLength
          if (bytes > 1_048_576) throw new AvitoOrderPreviewError('invalid')
          text += decoder.decode(chunk.value, { stream: true })
        }
        const result = parseAvitoOrderStatusPreview(JSON.parse(text + decoder.decode()), captured, expected)
        complete = true; return result
      } catch (error) {
        if (error instanceof AvitoOrderPreviewError) throw error
        throw new AvitoOrderPreviewError(timedOut ? 'timeout' : !active() ? 'stale' : response ? 'invalid' : 'unavailable')
      } finally {
        if (!complete) { abort(); try { if (reader) await reader.cancel(); else await response?.body?.cancel() } catch { /* preserve sanitized error */ } }
        try { reader?.releaseLock() } catch { /* preserve result */ }
        clearTimeout(timer); signal?.removeEventListener('abort', abort); if (pending === controller) pending = null
      }
    },
  }
}
