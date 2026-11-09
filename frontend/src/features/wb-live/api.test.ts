import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '@/lib/api'
import { deleteCurrentUserWbToken, upsertCurrentUserWbToken } from '@/features/settings/backend'
import { formatWbPrice, productsPath, readWbData, saveWbCredential, shouldPollWbSync, startWbSync, wbErrorMessage, type WbProductsPage, type WbSync } from './api'

function response(data: unknown) { return new Response(JSON.stringify({ data }), { headers: { 'Content-Type': 'application/json' } }) }

afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs(); vi.useRealTimers() })

describe('WB account live reads', () => {
  it('preserves unknown barcodes as null for a price-only size', async () => {
    const payload: WbProductsPage = {
      marketplaceAccountId: 91, nextCursor: null, readVersion: 'version', readiness: 'partial', sources: [],
      items: [{
        nmId: '100001', vendorCode: null, title: null, brand: null, subjectId: null, subjectName: null,
        photoUrl: null, contentUpdatedAt: null, pricesUpdatedAt: '2026-09-10T00:00:00Z', sizesTruncated: false,
        sizes: [{ chrtId: '100002', techSize: null, skus: null, skusTruncated: false, priceKopecks: '12345', discountedPriceKopecks: null }],
      }],
    }
    vi.stubGlobal('fetch', vi.fn(async () => response(payload)))
    const page = await readWbData<WbProductsPage>('price-only', productsPath(91, {}), new AbortController().signal)
    expect(page.items[0].sizes[0].skus).toBeNull()
    expect(page.items[0].sizes[0].skusTruncated).toBe(false)
    expect(page.items[0].sizes[0].priceKopecks).toBe('12345')
  })

  it('deduplicates the same authenticated read and isolates cancellation per subscriber', async () => {
    let resolve!: (value: Response) => void
    let transportSignal: AbortSignal | undefined
    const fetchMock = vi.fn((_url, init) => { transportSignal = init.signal; return new Promise<Response>((done) => { resolve = done }) })
    vi.stubGlobal('fetch', fetchMock)
    const first = new AbortController(); const second = new AbortController()
    const firstRead = readWbData('session-a', '/api/account/7', first.signal)
    const secondRead = readWbData('session-a', '/api/account/7', second.signal)
    const rejected = expect(firstRead).rejects.toMatchObject({ name: 'AbortError' })
    first.abort(); await rejected
    expect(transportSignal?.aborted).toBe(false)
    resolve(response({ accountId: 7 }))
    await expect(secondRead).resolves.toEqual({ accountId: 7 })
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('separates sessions and account/query requests without retaining completed responses', async () => {
    const fetchMock = vi.fn(async () => response([]))
    vi.stubGlobal('fetch', fetchMock)
    const signal = new AbortController().signal
    await Promise.all([readWbData('a', '/api/accounts/7?q=x', signal), readWbData('b', '/api/accounts/7?q=x', signal), readWbData('a', '/api/accounts/8?q=x', signal), readWbData('a', '/api/accounts/7?q=y', signal)])
    expect(fetchMock).toHaveBeenCalledTimes(4)
    await readWbData('a', '/api/accounts/7?q=x', signal)
    expect(fetchMock).toHaveBeenCalledTimes(5)
  })

  it('aborts the transport when its last subscriber leaves, and permits a fresh read', async () => {
    const signals: AbortSignal[] = []
    vi.stubGlobal('fetch', vi.fn((_url, init) => new Promise<Response>((_resolve, reject) => {
      signals.push(init.signal)
      init.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))
    })))
    const first = new AbortController()
    const read = readWbData('cancel', '/api/account/7', first.signal)
    const rejected = expect(read).rejects.toMatchObject({ name: 'AbortError' })
    first.abort(); await rejected
    expect(signals[0].aborted).toBe(true)
    const second = new AbortController()
    const next = readWbData('cancel', '/api/account/7', second.signal)
    const nextRejected = expect(next).rejects.toMatchObject({ name: 'AbortError' })
    expect(signals).toHaveLength(2)
    second.abort(); await nextRejected
  })

  it('bounds a hung read to twenty seconds', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('fetch', vi.fn((_url, init) => new Promise<Response>((_resolve, reject) => {
      init.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))
    })))
    const read = readWbData('timeout', '/api/account/7', new AbortController().signal)
    const rejected = expect(read).rejects.toMatchObject({ name: 'AbortError' })
    await vi.advanceTimersByTimeAsync(20_000); await rejected
  })

  it('requests one bounded server page and never adds period or financial assumptions', () => {
    const path = productsPath(91, { cursor: 'opaque+/=', q: ' cotton ', brand: 'WB & Co', sort: 'title', direction: 'desc' })
    const url = new URL(path, 'https://local.invalid')
    expect(url.pathname).toBe('/api/v2/wb/accounts/91/products')
    expect(Object.fromEntries(url.searchParams)).toEqual({ limit: '50', sort: 'title', direction: 'desc', cursor: 'opaque+/=', q: 'cotton', brand: 'WB & Co' })
  })

  it('preserves missing prices and lossless integer money values', () => {
    expect(formatWbPrice(null)).toBe('—')
    expect(formatWbPrice('0')).toBe('0,00 ₽')
    expect(formatWbPrice('12345')).toBe('123,45 ₽')
    expect(formatWbPrice('900719925474099399')).toContain(',99 ₽')
    expect(formatWbPrice(9007199254740994)).toBe('—')
  })

  it('stops polling terminal partial failures and keeps polling queued sources', () => {
    const partial: WbSync = { marketplaceAccountId: 7, jobId: 'job', state: 'partial', updatedAt: null, sources: [{ source: 'prices', state: 'failed', processed: 0, updatedAt: null, errorCode: 'FAILED' }] }
    expect(shouldPollWbSync(partial)).toBe(false)
    expect(shouldPollWbSync({ ...partial, sources: [{ ...partial.sources[0], state: 'queued' }] })).toBe(true)
    expect(shouldPollWbSync({ ...partial, state: 'completed' })).toBe(false)
  })
})

describe('WB credential and sync writes', () => {
  it('disables both legacy credential mutations in the enabled live scope', async () => {
    vi.stubEnv('VITE_WB_LIVE_ENABLED', 'true')
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    await expect(upsertCurrentUserWbToken('session', 'test-only')).rejects.toThrow('выбранного аккаунта')
    await expect(deleteCurrentUserWbToken('session')).rejects.toThrow('выбранного аккаунта')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('preserves the legacy writer when the live rollout is disabled', async () => {
    vi.stubEnv('VITE_WB_LIVE_ENABLED', 'false')
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => response({ hasToken: true }))
    vi.stubGlobal('fetch', fetchMock)
    await upsertCurrentUserWbToken('session', 'test-only')
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/cabinet/wb-token')
  })

  it('writes only the selected account credential route', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => response({ marketplaceAccountId: 17, status: 'active', updatedAt: null }))
    vi.stubGlobal('fetch', fetchMock)
    await saveWbCredential('session', 17, 'test-only-credential')
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/cabinet/marketplace-accounts/17/credentials/wb/wb_api')
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({ wbToken: 'test-only-credential' })
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('does not repeat an uncertain enqueue and sends the caller idempotency key', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => { throw new TypeError('network lost') })
    vi.stubGlobal('fetch', fetchMock)
    await expect(startWbSync('session', 17, 'same-intent')).rejects.toThrow('network lost')
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v2/wb/accounts/17/sync')
    expect(new Headers(fetchMock.mock.calls[0][1]?.headers).get('Idempotency-Key')).toBe('same-intent')
  })

  it('does not expose backend messages or credential values in feedback', () => {
    const error = new ApiError('provider echoed a credential', 503, 'PRIVATE')
    expect(wbErrorMessage(error, true)).not.toContain('credential')
    expect(wbErrorMessage(new TypeError('secret'), true)).toContain('неизвестен')
    expect(wbErrorMessage(new ApiError('secret', 403))).toContain('Нет доступа')
  })
})
