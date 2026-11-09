import { buildApiUrl } from '@/lib/api'
import { buildCanonicalOrdersReadPath, buildSavedOrdersSnapshotPath, parseCanonicalOrdersPage, parseSavedOrdersSnapshot,
  type CanonicalOrdersReadRequest, type CanonicalOrdersScope } from './canonicalOrders'

export const canonicalOrdersEnabled = import.meta.env.VITE_CANONICAL_ORDERS_ENABLED === 'true'
export class CanonicalOrdersError extends Error {
  readonly kind: 'scope' | 'snapshot' | 'invalid' | 'unavailable' | 'stale'
  constructor(kind: CanonicalOrdersError['kind']) {
    super(kind === 'scope' ? 'Нет доступа к выбранным аккаунтам. Проверьте сессию и права.'
      : kind === 'snapshot' ? 'Сохранённый снимок отсутствует, устарел или изменился. Повторите поиск снимка.'
        : kind === 'invalid' ? 'Некорректный запрос или ответ Orders. Данные не показаны.'
          : kind === 'stale' ? 'Выбор изменился.' : 'Orders недоступны. Повторите чтение.')
    this.kind = kind
  }
}
/** GET only: no snapshot creation, export, fulfillment or fallback. One client per scope epoch. */
export function createCanonicalOrdersClient(token: string, isCurrent: () => boolean, fetcher: typeof fetch = fetch) {
  let disposed = false, sequence = 0
  const controllers = new Set<AbortController>()
  async function read<T>(path: string, parse: (input: unknown) => T) {
    const request = ++sequence, controller = new AbortController()
    const active = () => !disposed && isCurrent() && request === sequence && !controller.signal.aborted
    if (!active() || !token) throw new CanonicalOrdersError('stale')
    for (const old of controllers) old.abort()
    controllers.add(controller)
    const timer = setTimeout(() => controller.abort(), 20_000)
    try {
      const response = await fetcher(buildApiUrl(path), { cache: 'no-store', credentials: 'omit', redirect: 'error', signal: controller.signal,
        headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' } })
      if (!active()) throw new CanonicalOrdersError('stale')
      if (response.status !== 200) {
        await response.body?.cancel()
        throw new CanonicalOrdersError(response.status === 401 || response.status === 403 ? 'scope' : response.status === 409 ? 'snapshot'
          : response.status === 400 || response.status === 422 ? 'invalid' : 'unavailable')
      }
      if (response.headers.get('content-type')?.split(';')[0].trim() !== 'application/json' || !response.body) throw new CanonicalOrdersError('invalid')
      const reader = response.body.getReader(), decoder = new TextDecoder('utf-8', { fatal: true })
      let bytes = 0, text = ''
      while (true) {
        const chunk = await reader.read()
        if (chunk.done) break
        bytes += chunk.value.byteLength
        if (bytes > 1_048_576) { await reader.cancel(); throw new CanonicalOrdersError('invalid') }
        text += decoder.decode(chunk.value, { stream: true })
      }
      if (!active()) throw new CanonicalOrdersError('stale')
      try { return parse(JSON.parse(text + decoder.decode())) } catch { throw new CanonicalOrdersError('invalid') }
    } catch (failure) {
      if (failure instanceof CanonicalOrdersError) throw failure
      throw new CanonicalOrdersError(active() ? 'unavailable' : 'stale')
    } finally { clearTimeout(timer); controllers.delete(controller) }
  }
  return {
    dispose() { disposed = true; for (const controller of controllers) controller.abort(); controllers.clear() },
    discover(scope: CanonicalOrdersScope) { return read(buildSavedOrdersSnapshotPath(scope), value => parseSavedOrdersSnapshot(value, scope)) },
    page(request: CanonicalOrdersReadRequest, snapshotId: string) {
      return read(buildCanonicalOrdersReadPath(request), value => {
        const page = parseCanonicalOrdersPage(value, { organizationId: request.organizationId, accountIds: request.accountIds, snapshotId })
        if (page.rows.length > (request.limit ?? 100)) throw new CanonicalOrdersError('invalid')
        return page
      })
    },
  }
}
