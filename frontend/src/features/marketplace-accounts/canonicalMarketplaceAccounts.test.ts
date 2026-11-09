import { describe, expect, it, vi } from 'vitest'
import { parseCanonicalMarketplaceAccounts, readCanonicalMarketplaceAccounts } from './canonicalMarketplaceAccounts'
const accounts = [{ marketplaceAccountId: 11, provider: 'wb', externalAccountId: '00011', displayName: null, status: 'disconnected' },
  { marketplaceAccountId: 12, provider: 'avito', externalAccountId: 'actual-avito', displayName: 'Synthetic Avito', status: 'active' }]
const json = (data: unknown) => new Response(JSON.stringify({ data }), { headers: { 'Content-Type': 'application/json' } })
describe('shared canonical marketplace metadata boundary', () => {
  it('discovers both providers through shared API, preserving status but granting no actions', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(json(accounts))
    expect(await readCanonicalMarketplaceAccounts('synthetic', new AbortController().signal, undefined, fetcher)).toEqual(accounts)
    expect(fetcher.mock.calls[0][0]).toBe('/api/v2/cabinet/marketplace-accounts')
    expect(fetcher.mock.calls[0][1]).toMatchObject({ cache: 'no-store', credentials: 'omit', redirect: 'error' })
  })
  it('validates optional provider filter against response and does not invent or alias provider', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(json([accounts[0]]))
    await expect(readCanonicalMarketplaceAccounts('synthetic', new AbortController().signal, 'avito', fetcher)).rejects.toThrow()
    expect(fetcher.mock.calls[0][0]).toBe('/api/v2/cabinet/marketplace-accounts?provider=avito')
    expect(() => parseCanonicalMarketplaceAccounts([{ ...accounts[0], provider: 'Avito' }])).toThrow()
  })
  it.each([[accounts[0], accounts[0]], [{ ...accounts[0], marketplaceAccountId: '11' }], [{ ...accounts[0], credentialRef: 'forbidden' }], { data: [] }])('rejects malformed, duplicate or secret-bearing account metadata', value => {
    expect(() => parseCanonicalMarketplaceAccounts(value)).toThrow()
  })
  it('cancels old identity reads even if transport returns late; a fresh provider list remains separate', async () => {
    let finish!: (response: Response) => void
    const fetcher = vi.fn<typeof fetch>().mockImplementationOnce(() => new Promise(resolve => { finish = resolve })).mockResolvedValueOnce(json([accounts[1]]))
    const controller = new AbortController(), old = readCanonicalMarketplaceAccounts('old-synthetic', controller.signal, undefined, fetcher)
    controller.abort()
    expect(await readCanonicalMarketplaceAccounts('new-synthetic', new AbortController().signal, undefined, fetcher)).toEqual([accounts[1]])
    finish(json(accounts)); await expect(old).rejects.toMatchObject({ name: 'AbortError' })
  })
  it('accepts an authoritative changed-provider response without carrying a client-invented provider forward', () => {
    expect(parseCanonicalMarketplaceAccounts([{ ...accounts[0], provider: 'avito' }])[0].provider).toBe('avito')
  })
  it('bounds response and never falls back after unavailable discovery', async () => {
    for (const response of [json({ oversized: 'x'.repeat(1_048_577) }), new Response('', { status: 503 })]) {
      const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response)
      await expect(readCanonicalMarketplaceAccounts('synthetic', new AbortController().signal, undefined, fetcher)).rejects.toThrow()
      expect(fetcher).toHaveBeenCalledOnce()
    }
  })
  it.each([
    ['wrong MIME', 'text/html', new TextEncoder().encode('<html>')],
    ['malformed UTF-8', 'application/json', new Uint8Array([0xff])],
    ['oversize', 'application/json', new Uint8Array(1_048_577)],
  ])('cancels and unlocks %s responses without leaking cleanup errors', async (_name, mime, chunk) => {
    const cancel = vi.fn(() => Promise.reject(new Error('private cleanup detail')))
    const body = new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(chunk as Uint8Array) }, cancel })
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(body, { headers: { 'Content-Type': mime as string } }))
    await expect(readCanonicalMarketplaceAccounts('synthetic', new AbortController().signal, undefined, fetcher)).rejects.toThrow('ACCOUNT_DISCOVERY_INVALID')
    expect(cancel).toHaveBeenCalledOnce()
    expect(body.locked).toBe(false)
    expect(fetcher.mock.calls[0][1]?.signal?.aborted).toBe(true)
  })
  it('unlocks failed reads and cancels the failed reader while retaining a safe error', async () => {
    const body = new ReadableStream<Uint8Array>({ start(controller) { controller.error(new Error('private transport detail')) } })
    const response = new Response(body, { headers: { 'Content-Type': 'application/json' } })
    const reader = body.getReader(), cancel = vi.spyOn(reader, 'cancel'), release = vi.spyOn(reader, 'releaseLock')
    vi.spyOn(body, 'getReader').mockReturnValue(reader)
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response)
    await expect(readCanonicalMarketplaceAccounts('synthetic', new AbortController().signal, undefined, fetcher)).rejects.toThrow('ACCOUNT_DISCOVERY_INVALID')
    expect(cancel).toHaveBeenCalledOnce(); expect(release).toHaveBeenCalledOnce()
    expect(body.locked).toBe(false)
    expect(fetcher.mock.calls[0][1]?.signal?.aborted).toBe(true)
  })
  it.each(['{"data":', '{"data":[{"private":"invalid metadata"}]}'])('cancels and unlocks a fully read invalid payload', async payload => {
    const response = new Response(payload, { headers: { 'Content-Type': 'application/json' } })
    const reader = response.body!.getReader(), cancel = vi.spyOn(reader, 'cancel')
    vi.spyOn(response.body!, 'getReader').mockReturnValue(reader)
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response)
    await expect(readCanonicalMarketplaceAccounts('synthetic', new AbortController().signal, undefined, fetcher)).rejects.toThrow('ACCOUNT_DISCOVERY_INVALID')
    expect(cancel).toHaveBeenCalledOnce(); expect(response.body!.locked).toBe(false)
  })
  it('releases a successfully read body without aborting successful transport', async () => {
    const response = json(accounts), fetcher = vi.fn<typeof fetch>().mockResolvedValue(response)
    await expect(readCanonicalMarketplaceAccounts('synthetic', new AbortController().signal, undefined, fetcher)).resolves.toEqual(accounts)
    expect(response.body!.locked).toBe(false)
    expect(fetcher.mock.calls[0][1]?.signal?.aborted).toBe(false)
  })
  it('preserves the unavailable error when HTTP rejection cleanup also fails', async () => {
    const cancel = vi.fn(() => Promise.reject(new Error('private cleanup detail')))
    const body = new ReadableStream<Uint8Array>({ cancel })
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(body, { status: 503 }))
    await expect(readCanonicalMarketplaceAccounts('synthetic', new AbortController().signal, undefined, fetcher)).rejects.toThrow('ACCOUNT_DISCOVERY_UNAVAILABLE')
    expect(cancel).toHaveBeenCalledOnce(); expect(body.locked).toBe(false)
    expect(fetcher.mock.calls[0][1]?.signal?.aborted).toBe(true)
  })
})
