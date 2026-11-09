import { apiRequest } from '@/lib/api'
import { authorizationHeaders } from '@/features/auth/authApi'
import { resetLiveRepricerParityCache, type LiveRepricerSourceRefreshResult } from './liveParityData'
import { createReadCache } from './readCache'

// Membership is current-account data, shared across pages, filters and periods.
const refreshes = createReadCache<boolean>()
const retryAfter = new Map<string, number>()

export async function syncProductsPromotions(token: string, cache: Record<string, unknown> | undefined,
  signal: AbortSignal, isCurrent: () => boolean) {
  const expires = typeof cache?.promotionsRefreshAt === 'string' ? Date.parse(cache.promotionsRefreshAt) : NaN
  const needed = cache?.promotionsRefreshRequired === true
    || (cache?.promotionsRefreshRequired === false && Number.isFinite(expires) && expires <= Date.now())
  if (!needed || signal.aborted || !isCurrent() || (retryAfter.get(token) ?? 0) > Date.now()) return false
  // Yield to the already prepared table. This is a one-shot task, not polling.
  await new Promise<void>(resolve => setTimeout(resolve, 0))
  if (signal.aborted || !isCurrent()) return false
  try {
    return await refreshes.get(token, async requestSignal => {
      const result = await apiRequest<LiveRepricerSourceRefreshResult>(
        '/api/v1/wb-repricer/sku/refresh-promotions?onlyIfStale=true', {
          method: 'POST', headers: authorizationHeaders(token), signal: requestSignal, cache: 'no-store',
        })
      if (!result || result.source !== 'promotions' || result.partial) {
        throw new Error(result?.warning || 'Не удалось подтвердить обновление статусов акций')
      }
      resetLiveRepricerParityCache(token)
      return true
    }, signal)
  } catch (error) {
    if (!signal.aborted && !(error instanceof DOMException && error.name === 'AbortError')) {
      retryAfter.delete(token)
      retryAfter.set(token, Date.now() + 60_000)
      if (retryAfter.size > 32) retryAfter.delete(retryAfter.keys().next().value!)
    }
    throw error
  }
}
