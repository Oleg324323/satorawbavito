import { ApiError, apiRequest } from '@/lib/api'

// Report-local deadline: do not change authentication or other marketplaces.
// Promise.race also bounds a shared auth-refresh wait that fetch abort alone
// cannot cancel. The original request is aborted and never publishes late data.
export async function reportRequest<T>(path: string, init: RequestInit = {}, timeoutMs = 30_000) {
  const controller = new AbortController()
  let timer: ReturnType<typeof setTimeout> | undefined
  let cancel: (() => void) | undefined
  const deadline = new Promise<never>((_, reject) => {
    cancel = () => {
      reject(new DOMException('Report loading aborted', 'AbortError'))
      controller.abort()
    }
    if (init.signal?.aborted) { cancel(); return }
    init.signal?.addEventListener('abort', cancel, { once: true })
    timer = setTimeout(() => {
      reject(new ApiError('Сервер не ответил вовремя. Сохранённые данные не удалены; повторите загрузку.', 504))
      controller.abort()
    }, timeoutMs)
  })
  try {
    if (init.signal?.aborted) return await deadline
    return await Promise.race([apiRequest<T>(path, { ...init, signal: controller.signal }), deadline])
  } finally {
    clearTimeout(timer)
    if (cancel) init.signal?.removeEventListener('abort', cancel)
  }
}
