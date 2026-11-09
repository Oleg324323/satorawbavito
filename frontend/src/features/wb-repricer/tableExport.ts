import { authorizationHeaders } from '@/features/auth/authApi'
import { buildApiUrl } from '@/lib/api'
import { loadLiveRepricerParityProducts, type LiveRepricerProductsQuery } from './liveParityData'

type Product = Awaited<ReturnType<typeof loadLiveRepricerParityProducts>>['products'][number]
const sourceKeys = ['periodStatsFetchedAt', 'financeFetchedAt', 'basketsFetchedAt', 'latestFetchedAt', 'adsFetchedAt', 'stocksFetchedAt', 'contentFetchedAt'] as const
export function repricerSourceSignature(cache?: Record<string, unknown>) {
  return sourceKeys.map(key => cache?.[key] ?? null)
}

export async function downloadRepricerTable(token: string, query: LiveRepricerProductsQuery,
  filterAndSort: (rows: Product[]) => Product[], filterDescription: string, signal: AbortSignal) {
  if (!query.dateFrom || !query.dateTo) throw new Error('Для экспорта требуется применённый период.')
  const products: Product[] = []
  let total: number | undefined
  let signature: ReturnType<typeof repricerSourceSignature> | undefined
  const seen = new Set<number>()
  // Fetch the whole catalog before applying the exact UI predicates. Search by
  // identity intentionally bypasses some UI filters, unlike backend filters.
  for (let page = 1; ; page++) {
    signal.throwIfAborted()
    const result = await loadLiveRepricerParityProducts(token, signal, {
      periodDays: query.periodDays, dateFrom: query.dateFrom, dateTo: query.dateTo,
      page, pageSize: 500, topMode: false,
    }, true)
    if ((result.cache?.dateFrom && result.cache.dateFrom !== query.dateFrom)
      || (result.cache?.dateTo && result.cache.dateTo !== query.dateTo)) throw new Error('Сервер вернул другой период.')
    const nextSignature = repricerSourceSignature(result.cache)
    if (total != null && (total !== result.total || JSON.stringify(signature) !== JSON.stringify(nextSignature))) {
      throw new Error('Данные обновились во время экспорта. Повторите выгрузку.')
    }
    total = result.total
    signature = nextSignature
    for (const product of result.products) {
      if (!product.nmId || seen.has(product.nmId)) throw new Error('Каталог изменился: повторяющийся или отсутствующий nmId.')
      seen.add(product.nmId)
      products.push(product)
    }
    if (products.length === total) break
    if (!result.products.length || products.length > total || products.length > 100_000) throw new Error('Не все страницы каталога доступны для экспорта.')
  }
  const ordered = filterAndSort(products)
  const response = await fetch(buildApiUrl('/api/v1/wb-repricer/table.xlsx'), {
    method: 'POST', headers: { ...authorizationHeaders(token), 'Content-Type': 'application/json' },
    credentials: 'include', cache: 'no-store', signal,
    body: JSON.stringify({ dateFrom: query.dateFrom, dateTo: query.dateTo,
      nmIds: ordered.map(product => product.nmId), filterDescription, sourceSignature: signature }),
  })
  if (!response.ok) {
    let message = `Не удалось выгрузить репрайсер (${response.status})`
    try { const error = await response.json(); if (typeof error.detail === 'string') message = error.detail } catch { /* use status */ }
    throw new Error(message)
  }
  return { blob: await response.blob(), count: ordered.length }
}
