import { apiData, ApiError } from '@/lib/api'
import { authorizationHeaders } from '@/features/auth/authApi'
import { parseWbCredential, parseWbSync } from './validation'

export type WbAccount = { marketplaceAccountId: number; provider: string; externalAccountId: string; displayName: string | null; status: string }
export type WbCredential = { marketplaceAccountId: number; status: 'missing' | 'active' | 'expired' | 'revoked'; updatedAt: string | null }
export type WbSync = {
  marketplaceAccountId: number; jobId: string | null
  state: 'idle' | 'queued' | 'running' | 'partial' | 'completed' | 'failed'
  updatedAt: string | null
  sources: { source: string; state: string; processed: number; updatedAt: string | null; errorCode: string | null }[]
}
export type WbProduct = {
  nmId: string; vendorCode: string | null; title: string | null; brand: string | null; subjectId: string | null; subjectName: string | null
  photoUrl: string | null; contentUpdatedAt: string | null; pricesUpdatedAt: string | null
  sizesTruncated: boolean
  truncatedFields?: string[]
  sizes: { chrtId: string; techSize: string | null; skus: string[] | null; skusTruncated: boolean; priceKopecks: string | null; discountedPriceKopecks: string | null; truncatedFields?: string[] }[]
}
export type WbProductsPage = {
  marketplaceAccountId: number; items: WbProduct[]; nextCursor: string | null; readVersion: string
  readiness: 'empty' | 'partial' | 'ready' | 'error'; sources: WbSync['sources']
}
export type WbProductSort = 'nmId' | 'vendorCode' | 'title' | 'brand'
export function productsPath(accountId: number, query: { cursor?: string | null; q?: string; brand?: string; sort?: WbProductSort; direction?: 'asc' | 'desc' }) {
  const params = new URLSearchParams({ limit: '50', sort: query.sort ?? 'nmId', direction: query.direction ?? 'asc' })
  if (query.cursor) params.set('cursor', query.cursor)
  if (query.q?.trim()) params.set('q', query.q.trim())
  if (query.brand?.trim()) params.set('brand', query.brand.trim())
  return `/api/v2/wb/accounts/${accountId}/products?${params}`
}

export function formatWbPrice(value: string | number | null) {
  if (value === null || (typeof value === 'number' && !Number.isSafeInteger(value))) return '—'
  const digits = String(value)
  if (!/^\d+$/.test(digits)) return '—'
  const kopecks = BigInt(digits)
  return `${(kopecks / 100n).toLocaleString('ru-RU')},${String(kopecks % 100n).padStart(2, '0')} ₽`
}

type PendingRead = { controller: AbortController; promise: Promise<unknown>; consumers: number }
const pendingReads = new Map<string, PendingRead>()

// No response cache: completed data belongs to the mounted, authenticated consumer.
// Each subscriber can cancel without cancelling another subscriber's request.
export function readWbData<T>(accessToken: string, path: string, signal: AbortSignal, parse?: (value: unknown) => T): Promise<T> {
  if (signal.aborted) return Promise.reject(new DOMException('Aborted', 'AbortError'))
  const key = `${accessToken}\n${path}`
  let entry = pendingReads.get(key)
  if (!entry) {
    const controller = new AbortController()
    const timeout = setTimeout(() => controller.abort(), 20_000)
    const promise = apiData<unknown>(path, { headers: authorizationHeaders(accessToken), signal: controller.signal, cache: 'no-store' })
      .finally(() => {
        clearTimeout(timeout)
        if (pendingReads.get(key)?.promise === promise) pendingReads.delete(key)
      })
    entry = { controller, promise, consumers: 0 }
    pendingReads.set(key, entry)
  }
  const read = entry
  read.consumers += 1
  return new Promise<T>((resolve, reject) => {
    let settled = false
    const finish = () => {
      if (settled) return false
      settled = true
      signal.removeEventListener('abort', abort)
      read.consumers -= 1
      if (read.consumers === 0) {
        read.controller.abort()
        if (pendingReads.get(key) === read) pendingReads.delete(key)
      }
      return true
    }
    const abort = () => { if (finish()) reject(new DOMException('Aborted', 'AbortError')) }
    signal.addEventListener('abort', abort, { once: true })
    read.promise.then((value) => {
      if (!finish()) return
      try { resolve(parse ? parse(value) : value as T) } catch (error) { reject(error) }
    }, (error) => { if (finish()) reject(error) })
  })
}

export const credentialPath = (accountId: number) => `/api/v1/cabinet/marketplace-accounts/${accountId}/credentials/wb/wb_api`
export const syncPath = (accountId: number) => `/api/v2/wb/accounts/${accountId}/sync`
export function shouldPollWbSync(data: WbSync) {
  return data.state === 'queued' || data.state === 'running'
    || (data.state === 'partial' && data.sources.some((source) => source.state === 'queued' || source.state === 'running'))
}

// Explicit writes only. A lost response never triggers an automatic write retry.
export function saveWbCredential(accessToken: string, accountId: number, wbToken: string) {
  return apiData<unknown>(credentialPath(accountId), {
    method: 'PUT', headers: authorizationHeaders(accessToken), body: JSON.stringify({ wbToken }),
  }).then((value) => parseWbCredential(value, accountId))
}
export function revokeWbCredential(accessToken: string, accountId: number) {
  return apiData<unknown>(credentialPath(accountId), { method: 'DELETE', headers: authorizationHeaders(accessToken) }).then((value) => parseWbCredential(value, accountId))
}
export function startWbSync(accessToken: string, accountId: number, idempotencyKey: string) {
  return apiData<unknown>(syncPath(accountId), {
    method: 'POST', headers: { ...authorizationHeaders(accessToken), 'Idempotency-Key': idempotencyKey }, body: '{}',
  }).then((value) => parseWbSync(value, accountId))
}

export function wbErrorMessage(error: unknown, write = false) {
  if (error instanceof ApiError) {
    if (error.status === 401) return 'Сессия завершена. Войдите снова.'
    if (error.status === 403) return 'Нет доступа к выбранному аккаунту или этому действию.'
    if (error.status === 409) return 'Действие конфликтует с текущим состоянием. Обновите состояние подключения.'
    if (error.status === 422 || error.status === 400) return 'Не удалось принять данные. Проверьте введённое значение.'
    if (error.status === 503) return 'Сервис временно недоступен. Проверьте состояние перед повторным действием.'
  }
  return write
    ? 'Ответ не получен: результат действия неизвестен. Обновите состояние перед повтором.'
    : 'Не удалось загрузить данные. Проверьте соединение и повторите чтение.'
}
