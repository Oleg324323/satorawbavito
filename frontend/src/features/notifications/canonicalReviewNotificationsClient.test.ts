import { afterEach, describe, expect, it, vi } from 'vitest'
import { createCanonicalReviewNotificationsClient } from './canonicalReviewNotificationsClient'
import { canonicalNotificationItems } from './canonicalNotificationView'
import { fetchNotifications, markAllBackendNotificationsRead, markBackendNotificationRead } from './api'

afterEach(() => { vi.useRealTimers(); vi.unstubAllEnvs(); vi.unstubAllGlobals() })

// Literal0074 golden identities/copy; expected wire versions are not produced by
// the decoder under test. Only HTTP I/O is substituted; real client/parsers run.
const scope = { organizationId: 1, marketplaceAccountId: 11, marketplace: 'wb' as const }
const eventId = '00000000-0000-4000-8000-000000000009'
const entityId = '00000000-0000-4000-8000-000000000001'
const occurredAt = '2026-09-09T12:00:01.123456Z'
const exactVersion = '9223372036854775808'
const receipt = () => ({ value: {
  schemaVersion: 'notification-in-app-receipt-v1', organizationId: 1, marketplaceAccountId: 11,
  eventId, recipientMembershipId: 7, readAt: occurredAt, dismissedAt: null,
}, version: exactVersion })
const visible = () => ({ schemaVersion: 'review-notification-visible-v1', ...scope,
  recipientMembershipId: 7, eventIds: [eventId], items: [{ event: {
    schemaVersion: 'notification-event-v1', eventId, organizationId: 1, marketplaceAccountId: 11,
    scope: 'account', producer: 'reviews', entityId, sourceVersion: exactVersion,
    kind: 'approval_required', occurredAt,
    dedupeKey: 'b6c58324017df7859f2f6779f26f71d19ec304d238a853c508714969e6ddafce',
    title: 'Ответ на отзыв требует подтверждения',
    details: 'Проверьте текущую версию черновика в разделе отзывов.', severity: 'info',
  }, receipt: null as ReturnType<typeof receipt> | null }],
})
const marked = () => ({ schemaVersion: 'review-notification-receipts-v1', ...scope,
  recipientMembershipId: 7, eventIds: [eventId], action: 'read', items: [receipt()],
})
const json = (value: unknown) => new Response(JSON.stringify(value), {
  headers: { 'Content-Type': 'application/json' },
})
function setup(fetcher: typeof fetch, isCurrent = () => true, maxResponseBytes = 8192) {
  return createCanonicalReviewNotificationsClient({ scope, recipientMembershipId: 7,
    accessToken: 'synthetic-test-only', isCurrent, maxVisibleIds: 4, maxResponseBytes, fetch: fetcher })
}

const list = () => ({ ...visible(), schemaVersion: 'review-notification-list-v1', nextCursor: null as string | null,
  eventSetVersion: '1', capabilities: { canRead: true, canMarkRead: true, canDismiss: true } })

describe('canonical notification discovery and UI cutover', () => {
  it('uses Avito scope for list and personal receipt while preserving review routing', async () => {
    const avitoScope = { ...scope, marketplace: 'avito' as const }
    const payload = { ...list(), marketplace: 'avito' }
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(json(payload)).mockResolvedValueOnce(json({ ...marked(), marketplace: 'avito' }))
    const bound = createCanonicalReviewNotificationsClient({ scope: avitoScope, recipientMembershipId: 7, accessToken: 'synthetic-only', isCurrent: () => true, maxVisibleIds: 50, maxResponseBytes: 8192, fetch: fetcher })
    const result = await bound.list()
    expect(result.state).toBe('ready')
    if (result.state !== 'ready') throw new Error('Expected Avito list')
    expect(canonicalNotificationItems(result.data)[0]).toMatchObject({ source: 'Отзывы Авито', route: '/avito/reviews' })
    expect((await bound.mark([eventId], 'read')).state).toBe('ready')
    expect(String(fetcher.mock.calls[0][0])).toContain('marketplace=avito')
    expect(JSON.parse(fetcher.mock.calls[1][1]?.body as string)).toMatchObject({ marketplace: 'avito', marketplaceAccountId: 11, eventIds: [eventId] })
  })
  it('rejects a WB payload after authoritative selection changes to Avito', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(json(list()))
    const client = createCanonicalReviewNotificationsClient({ scope: { ...scope, marketplace: 'avito' }, accessToken: 'synthetic-only', isCurrent: () => true, maxVisibleIds: 50, maxResponseBytes: 8192, fetch: fetcher })
    expect((await client.list()).state).toBe('invalid-response')
  })
  it('discovers a bounded account page and the server-derived membership without an input member or event IDs', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(json(list()))
    const client = createCanonicalReviewNotificationsClient({ scope, accessToken: 'synthetic-test-only', isCurrent: () => true, maxVisibleIds: 50, maxResponseBytes: 8192, fetch: fetcher })
    const result = await client.list('opaque+/=')
    expect(result.state).toBe('ready')
    if (result.state !== 'ready') throw new Error('Expected discovery')
    expect(result.data.recipientMembershipId).toBe(7)
    expect(result.data.eventSetVersion).toBe('1')
    expect(canonicalNotificationItems(result.data)[0]).toMatchObject({ id: eventId, readAt: null, source: 'Отзывы WB', route: '/wb/reviews' })
    const url = new URL(String(fetcher.mock.calls[0][0]), 'https://local.invalid')
    expect(url.pathname).toBe('/api/v2/reviews/notifications')
    expect(Object.fromEntries(url.searchParams)).toEqual({ marketplace_account_id: '11', marketplace: 'wb', limit: '50', cursor: 'opaque+/=' })
    expect(fetcher).toHaveBeenCalledOnce()
  })

  it('distinguishes a validated empty inbox from unavailability', async () => {
    const result = await setup(vi.fn<typeof fetch>().mockResolvedValue(json({ ...list(), eventIds: [], items: [], eventSetVersion: '0' }))).list()
    expect(result.state).toBe('ready')
    if (result.state === 'ready') expect(canonicalNotificationItems(result.data)).toEqual([])
    expect(await setup(vi.fn<typeof fetch>().mockResolvedValue(new Response('', { status: 503 }))).list()).toEqual({ state: 'unavailable', outcome: 'not-submitted' })
  })

  it.each(['account', 'nested-account', 'member-receipt', 'missing-items', 'numeric-version', 'unbounded'] as const)('rejects malformed discovery: %s', async issue => {
    const payload = list()
    if (issue === 'account') payload.marketplaceAccountId = 12
    if (issue === 'nested-account') payload.items[0].event.marketplaceAccountId = 12
    if (issue === 'member-receipt') { payload.items[0].receipt = receipt(); payload.items[0].receipt.value.recipientMembershipId = 8 }
    const wire: Record<string, unknown> = payload
    if (issue === 'missing-items') delete wire.items
    if (issue === 'numeric-version') wire.eventSetVersion = 1
    if (issue === 'unbounded') wire.items = Array(101).fill(payload.items[0])
    expect(await setup(vi.fn<typeof fetch>().mockResolvedValue(json(wire))).list()).toEqual({ state: 'invalid-response', outcome: 'not-submitted' })
  })

  it('uses only persisted personal receipts and excludes dismissed events from the display', async () => {
    const payload = list(); payload.items[0].receipt = receipt()
    const result = await setup(vi.fn<typeof fetch>().mockResolvedValue(json(payload))).list()
    if (result.state !== 'ready') throw new Error('Expected validated list')
    expect(canonicalNotificationItems(result.data)[0].readAt).toBe(occurredAt)
    result.data.items[0].receipt!.value.dismissedAt = occurredAt
    expect(canonicalNotificationItems(result.data)).toEqual([])
  })

  it('bounds a stalled read without replay', async () => {
    vi.useFakeTimers()
    const fetcher = vi.fn<typeof fetch>().mockImplementation((_url, init) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))
    }))
    const pending = setup(fetcher).list()
    await vi.advanceTimersByTimeAsync(20_000)
    expect(await pending).toEqual({ state: 'unavailable', outcome: 'not-submitted' })
    expect(fetcher).toHaveBeenCalledOnce()
  })

  it('blocks the organization-global inbox and both old receipt writers when canonical UI is enabled', async () => {
    vi.stubEnv('VITE_CANONICAL_NOTIFICATIONS_ENABLED', 'true')
    const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher)
    await expect(fetchNotifications('synthetic')).rejects.toThrow('выбранного аккаунта')
    await expect(markBackendNotificationRead('synthetic', eventId)).rejects.toThrow('выбранного аккаунта')
    await expect(markAllBackendNotificationsRead('synthetic')).rejects.toThrow('выбранного аккаунта')
    expect(fetcher).not.toHaveBeenCalled()
  })
})

describe('essential canonical notification acceptance (no network)', () => {
  it('preserves exact UUIDs and >BIGINT versions across HTTP without marking a read', async () => {
    const payload = visible()
    payload.items[0].receipt = receipt()
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(json(payload))
    const result = await setup(fetcher).visible([eventId])
    expect(result.state).toBe('ready')
    if (result.state !== 'ready') throw new Error('Expected validated notification')
    expect(result.data.items[0].event.eventId).toBe('00000000-0000-4000-8000-000000000009')
    expect(result.data.items[0].event.sourceVersion).toBe('9223372036854775808')
    expect(result.data.items[0].receipt?.version).toBe('9223372036854775808')
    expect(fetcher).toHaveBeenCalledOnce()
    const [url, options] = fetcher.mock.calls[0]
    expect(String(url)).toContain('/api/v2/reviews/notifications/visible?marketplace_account_id=11&marketplace=wb&event_id=')
    expect(options).toMatchObject({ method: 'GET', cache: 'no-store', credentials: 'omit', redirect: 'error' })
    expect(options?.body).toBeUndefined()
  })

  it.each(['organization', 'account', 'recipient', 'event', 'nested-receipt'] as const)(
    'rejects a successful response for the wrong %s instead of caching it', async changed => {
      const payload = visible()
      if (changed === 'organization') payload.organizationId = 2
      if (changed === 'account') payload.items[0].event.marketplaceAccountId = 12
      if (changed === 'recipient') payload.recipientMembershipId = 8
      if (changed === 'event') payload.items[0].event.eventId = entityId
      if (changed === 'nested-receipt') {
        payload.items[0].receipt = receipt()
        payload.items[0].receipt.value.recipientMembershipId = 8
      }
      const fetcher = vi.fn<typeof fetch>().mockResolvedValue(json(payload))
      expect(await setup(fetcher).visible([eventId])).toEqual({ state: 'invalid-response', outcome: 'not-submitted' })
      expect(fetcher).toHaveBeenCalledOnce()
    },
  )

  it('refuses numeric versions even when they arrive in otherwise successful JSON', async () => {
    const payload = visible()
    const corrupted = { ...payload, items: [{ ...payload.items[0], event: {
      ...payload.items[0].event, sourceVersion: 9007199254740992,
    } }] }
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(json(corrupted))
    expect(await setup(fetcher).visible([eventId])).toEqual({ state: 'invalid-response', outcome: 'not-submitted' })
  })

  it('discards an old account/session epoch after A→B→A even if fetch ignores abort', async () => {
    let epoch = 1
    let finishOld!: (value: Response) => void
    const oldFetch = vi.fn<typeof fetch>().mockImplementation(() => new Promise(resolve => { finishOld = resolve }))
    const old = setup(oldFetch, () => epoch === 1)
    const pending = old.visible([eventId])
    epoch = 2
    old.dispose()
    epoch = 3
    const fresh = setup(vi.fn<typeof fetch>().mockResolvedValue(json(visible())), () => epoch === 3)
    expect((await fresh.visible([eventId])).state).toBe('ready')
    finishOld(json(visible()))
    expect(await pending).toEqual({ state: 'stale', outcome: 'not-submitted' })
    expect(await old.mark([eventId], 'read')).toEqual({ state: 'stale', outcome: 'not-submitted' })
    expect(oldFetch).toHaveBeenCalledOnce()
  })

  it('never lets a delayed visible GET overwrite a later acknowledged receipt', async () => {
    let finishRead!: (value: Response) => void
    const fetcher = vi.fn<typeof fetch>()
      .mockImplementationOnce(() => new Promise(resolve => { finishRead = resolve }))
      .mockResolvedValueOnce(json(marked()))
    const client = setup(fetcher)
    const earlier = client.visible([eventId])
    const result = await client.mark([eventId], 'read')
    expect(result.state).toBe('ready')
    finishRead(json(visible()))
    expect(await earlier).toEqual({ state: 'stale', outcome: 'not-submitted' })
    expect(fetcher).toHaveBeenCalledTimes(2)
  })

  it('keeps a lost receipt response unknown and allows only explicit readback, never automatic resend', async () => {
    let committed = false
    const fetcher = vi.fn<typeof fetch>()
      .mockImplementationOnce(async (_url, options) => {
        expect(options?.method).toBe('POST')
        expect(JSON.parse(String(options?.body))).toEqual({
          schemaVersion: 'review-notification-action-v1', organizationId: 1,
          marketplaceAccountId: 11, marketplace: 'wb', eventIds: [eventId], action: 'read',
        })
        committed = true // Remote commit, response lost: a deliberately external fault.
        throw new Error('synthetic-private-provider-diagnostic')
      })
      .mockImplementationOnce(async (_url, options) => {
        expect(options?.method).toBe('GET')
        const payload = visible()
        if (committed) payload.items[0].receipt = receipt()
        return json(payload)
      })
    const client = setup(fetcher)
    expect(await client.mark([eventId], 'read')).toEqual({ state: 'unavailable', outcome: 'unknown' })
    expect(fetcher).toHaveBeenCalledOnce()
    const reconciled = await client.visible([eventId])
    expect(reconciled.state).toBe('ready')
    if (reconciled.state !== 'ready') throw new Error('Expected explicit readback')
    expect(reconciled.data.items[0].receipt?.value.readAt).toBe('2026-09-09T12:00:01.123456Z')
    expect(fetcher).toHaveBeenCalledTimes(2)
  })

  it.each([403, 409, 503])('HTTP %s after POST neither echoes private error text nor retries', async status => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response('synthetic-private-error', { status }))
    const result = await setup(fetcher).mark([eventId], 'read')
    expect(result).toEqual({ state: status === 403 ? 'no-access' : status === 409 ? 'conflict' : 'unavailable', outcome: 'unknown' })
    expect(fetcher).toHaveBeenCalledOnce()
  })

  it('copies selected IDs before I/O and rejects an over-budget mutation response as unknown', async () => {
    const selected = [eventId]
    let finish!: (value: Response) => void
    const fetcher = vi.fn<typeof fetch>().mockImplementation(() => new Promise(resolve => { finish = resolve }))
    const pending = setup(fetcher, () => true, 128).mark(selected, 'read')
    selected[0] = entityId
    expect(JSON.parse(String(fetcher.mock.calls[0][1]?.body)).eventIds).toEqual([eventId])
    finish(json(marked()))
    expect(await pending).toEqual({ state: 'invalid-response', outcome: 'unknown' })
    expect(fetcher).toHaveBeenCalledOnce()
  })
})
