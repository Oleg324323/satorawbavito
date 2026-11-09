// Session-scoped, bounded reads. Cancellation belongs to each consumer; a shared
// request is aborted only when no screen still needs it. Never retry provider errors.
export function createReadCache<T>(ttlMs = 30_000, timeoutMs = 30_000) {
  const values = new Map<string, { at: number; value: T }>()
  const pending = new Map<string, { controller: AbortController; promise: Promise<T>; users: number }>()
  const abortError = () => new DOMException('Загрузка отменена', 'AbortError')
  function clear() {
    values.clear()
    for (const entry of pending.values()) entry.controller.abort()
    pending.clear()
  }
  function get(key: string, read: (signal: AbortSignal) => Promise<T>, signal?: AbortSignal): Promise<T> {
    if (signal?.aborted) return Promise.reject(abortError())
    const saved = values.get(key)
    if (saved && Date.now() - saved.at < ttlMs) return Promise.resolve(saved.value)
    let entry = pending.get(key)
    if (!entry || entry.controller.signal.aborted) {
      const controller = new AbortController()
      const created = { controller, users: 0, promise: null as unknown as Promise<T> }
      const timer = setTimeout(() => controller.abort(new Error('Сервер не ответил за 30 секунд. Повторите загрузку.')), timeoutMs)
      created.promise = Promise.resolve().then(() => read(controller.signal)).then(value => {
        if (!controller.signal.aborted && pending.get(key) === created) {
          values.delete(key)
          values.set(key, { at: Date.now(), value })
          if (values.size > 32) values.delete(values.keys().next().value!)
        }
        return value
      }).catch(error => {
        if (controller.signal.aborted && controller.signal.reason instanceof Error && controller.signal.reason.name !== 'AbortError') throw controller.signal.reason
        throw error
      }).finally(() => {
        clearTimeout(timer)
        if (pending.get(key) === created) pending.delete(key)
      })
      pending.set(key, created)
      entry = created
    }
    const shared = entry
    shared.users++
    return new Promise<T>((resolve, reject) => {
      let finished = false
      const finish = (callback: () => void) => {
        if (finished) return
        finished = true
        signal?.removeEventListener('abort', cancel)
        shared.users--
        callback()
      }
      const cancel = () => {
        finish(() => reject(abortError()))
        queueMicrotask(() => { if (!shared.users && pending.get(key) === shared) shared.controller.abort() })
      }
      signal?.addEventListener('abort', cancel, { once: true })
      shared.promise.then(value => finish(() => resolve(value)), error => finish(() => reject(error)))
    })
  }
  return { get, clear }
}
