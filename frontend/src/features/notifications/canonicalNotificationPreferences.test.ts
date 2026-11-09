import { afterEach, describe, expect, it, vi } from 'vitest'
import { encodeNotificationPreferences, parseNotificationPreferences, requestNotificationPreferences } from './canonicalNotificationPreferences'

const saved = { schemaVersion: 'notification-preferences-v1', version: '9223372036854775808', email: { enabled: true, dailyDigest: false, criticalAlerts: true }, telegram: { enabled: false } }
const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })
afterEach(() => vi.useRealTimers())

describe('personal versioned notification preferences', () => {
  it('reads only persisted flags and preserves a lossless version', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(json(saved))
    expect(await requestNotificationPreferences('synthetic', new AbortController().signal, undefined, fetcher)).toEqual(saved)
    expect(fetcher.mock.calls[0][0]).toBe('/api/v2/notifications/preferences')
    expect(fetcher.mock.calls[0][1]?.method).toBe('GET')
  })
  it('writes exact CAS version and flags without identity or destination fields', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(json({ ...saved, version: '9223372036854775809' }))
    await requestNotificationPreferences('synthetic', new AbortController().signal, { expectedVersion: saved.version, values: { email: saved.email, telegram: saved.telegram } }, fetcher)
    expect(JSON.parse(String(fetcher.mock.calls[0][1]?.body))).toEqual({ schemaVersion: 'notification-preferences-update-v1', expectedVersion: saved.version, email: saved.email, telegram: saved.telegram })
    expect(fetcher).toHaveBeenCalledOnce()
  })
  it.each([null, { ...saved, version: 1 }, { ...saved, chatId: 'private' }, { ...saved, telegram: { enabled: 'yes' } }, { ...saved, email: {} }])('rejects malformed preferences without creating defaults', value => {
    expect(() => parseNotificationPreferences(value)).toThrow()
  })
  it('rejects extra destination fields in an update', () => {
    expect(() => encodeNotificationPreferences(saved.version, { email: saved.email, telegram: { enabled: true, chatId: 'forbidden' } } as never)).toThrow()
  })
  it('requires explicit readback after a conflict and never retries the PUT', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(new Response('private detail', { status: 409 })).mockResolvedValueOnce(json(saved))
    await expect(requestNotificationPreferences('synthetic', new AbortController().signal, { expectedVersion: saved.version, values: { email: saved.email, telegram: saved.telegram } }, fetcher)).rejects.toMatchObject({ kind: 'conflict', unknownWrite: true })
    expect(fetcher).toHaveBeenCalledOnce()
    await requestNotificationPreferences('synthetic', new AbortController().signal, undefined, fetcher)
    expect(fetcher.mock.calls[1][1]?.method).toBe('GET')
  })
  it('does not replay an unknown write or expose its diagnostic', async () => {
    const fetcher = vi.fn<typeof fetch>().mockRejectedValue(new Error('private token diagnostic'))
    await expect(requestNotificationPreferences('synthetic', new AbortController().signal, { expectedVersion: saved.version, values: { email: saved.email, telegram: saved.telegram } }, fetcher)).rejects.toMatchObject({ kind: 'unavailable', unknownWrite: true })
    expect(fetcher).toHaveBeenCalledOnce()
  })
  it('rejects over-budget responses', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(json({ ...saved, padding: 'x'.repeat(5000) }))
    await expect(requestNotificationPreferences('synthetic', new AbortController().signal, undefined, fetcher)).rejects.toMatchObject({ kind: 'invalid-response' })
  })
  it('rejects a response arriving after disposal even if transport ignored abort', async () => {
    const controller = new AbortController()
    let respond!: (value: Response) => void
    const fetcher = vi.fn<typeof fetch>().mockImplementation(() => new Promise((resolve) => { respond = resolve }))
    const pending = requestNotificationPreferences('synthetic', controller.signal, undefined, fetcher)
    controller.abort(); respond(json(saved))
    await expect(pending).rejects.toMatchObject({ kind: 'unavailable' })
  })
})
