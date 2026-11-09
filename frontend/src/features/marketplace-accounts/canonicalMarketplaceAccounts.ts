import { useEffect, useState } from 'react'
import { z } from 'zod'
import { useAuth } from '@/features/auth/authContext'
import { buildApiUrl } from '@/lib/api'

const accountsSchema = z.array(z.object({ marketplaceAccountId: z.number().int().positive().max(2147483647),
  provider: z.enum(['wb', 'avito']), externalAccountId: z.string().min(1).max(1024), displayName: z.string().max(1024).nullable(), status: z.string().min(1).max(64),
}).strict()).max(1000)
export function parseCanonicalMarketplaceAccounts(value: unknown, provider?: 'wb' | 'avito') {
  const accounts = accountsSchema.parse(value)
  if (new Set(accounts.map(account => account.marketplaceAccountId)).size !== accounts.length
    || provider && accounts.some(account => account.provider !== provider)) throw new Error('ACCOUNT_DISCOVERY_INVALID')
  return accounts
}
export async function readCanonicalMarketplaceAccounts(token: string, signal: AbortSignal, provider?: 'wb' | 'avito', fetcher: typeof fetch = fetch) {
  if (signal.aborted) throw new DOMException('Aborted', 'AbortError')
  const controller = new AbortController(), abort = () => controller.abort()
  signal.addEventListener('abort', abort, { once: true })
  const timeout = setTimeout(abort, 20_000)
  let response: Response | undefined, reader: ReadableStreamDefaultReader<Uint8Array> | undefined, complete = false
  try {
    response = await fetcher(buildApiUrl(`/api/v2/cabinet/marketplace-accounts${provider ? `?provider=${provider}` : ''}`), {
      cache: 'no-store', credentials: 'omit', redirect: 'error', signal: controller.signal,
      headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' },
    })
    if (response.status !== 200) throw new Error('ACCOUNT_DISCOVERY_UNAVAILABLE')
    if (response.headers.get('content-type')?.split(';')[0].trim() !== 'application/json' || !response.body) throw new Error('ACCOUNT_DISCOVERY_INVALID')
    reader = response.body.getReader()
    const decoder = new TextDecoder('utf-8', { fatal: true })
    let bytes = 0, text = ''
    while (true) {
      const chunk = await reader.read()
      if (chunk.done) break
      bytes += chunk.value.byteLength
      if (bytes > 1_048_576) throw new Error('ACCOUNT_DISCOVERY_INVALID')
      text += decoder.decode(chunk.value, { stream: true })
    }
    if (controller.signal.aborted) throw new DOMException('Aborted', 'AbortError')
    const accounts = parseCanonicalMarketplaceAccounts(JSON.parse(text + decoder.decode())?.data, provider)
    complete = true
    return accounts
  } catch (error) {
    if (controller.signal.aborted) throw new DOMException('Aborted', 'AbortError')
    if (error instanceof Error && error.message === 'ACCOUNT_DISCOVERY_UNAVAILABLE') throw error
    throw new Error('ACCOUNT_DISCOVERY_INVALID')
  } finally {
    if (!complete) {
      abort()
      // Cleanup failures must not replace the sanitized response/read error.
      try { if (reader) await reader.cancel(); else await response?.body?.cancel() } catch { /* already failed transport */ }
    }
    try { reader?.releaseLock() } catch { /* preserve the original read result */ } finally {
      clearTimeout(timeout); signal.removeEventListener('abort', abort)
    }
  }
}

/** Metadata discovery only. Consumers derive scope; server capabilities authorize actions. */
export function useCanonicalMarketplaceAccounts() {
  const { accessToken, cabinetMe } = useAuth()
  const session = `${cabinetMe?.activeSession?.sessionId}:${cabinetMe?.organization.organizationId}:${cabinetMe?.user.userId}`
  const [loaded, setLoaded] = useState<{ session: string; token: string; accounts: ReturnType<typeof parseCanonicalMarketplaceAccounts> } | null>(null)
  const [error, setError] = useState<string | null>(null), [loading, setLoading] = useState(true), [revision, setRevision] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    setLoaded(null); setError(null)
    if (!accessToken || !cabinetMe) { setLoading(false); return }
    setLoading(true)
    void readCanonicalMarketplaceAccounts(accessToken, controller.signal)
      .then(accounts => { if (!controller.signal.aborted) setLoaded({ session, token: accessToken, accounts }) })
      .catch(() => { if (!controller.signal.aborted) setError('Аккаунты недоступны. Нужны активная сессия, cabinet:read и включённое каноническое обнаружение аккаунтов.') })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [session, accessToken, revision])
  const accounts = loaded?.session === session && loaded.token === accessToken ? loaded.accounts : []
  return { accessToken, cabinetMe, session, accounts, loading, error, revision, reload: () => setRevision(value => value + 1) }
}
