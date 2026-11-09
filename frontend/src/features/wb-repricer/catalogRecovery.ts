import { ApiError, apiRequest } from '@/lib/api'
import { authorizationHeaders } from '@/features/auth/authApi'
import { syncStatsSources } from './statsSourceSync'

// Reads remain side-effect free. Only an explicit missing-catalog response
// starts the existing read-only collection queue, then retries the same URL.
export async function readWithCatalogRecovery<T>(url: string, token: string,
  from: string, to: string, signal?: AbortSignal): Promise<T | null> {
  const read = () => apiRequest<T>(url, { headers: authorizationHeaders(token), cache: 'no-store', signal })
  try {
    return await read()
  } catch (error) {
    if (!(error instanceof ApiError) || error.code !== 'WB_CATALOG_CACHE_REQUIRED') throw error
    await syncStatsSources(token, from.slice(0, 10), to.slice(0, 10),
      signal ?? new AbortController().signal, () => {}, () => !signal?.aborted, 'catalog')
    return read()
  }
}
