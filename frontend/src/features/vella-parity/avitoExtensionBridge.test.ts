import { afterEach, describe, expect, it, vi } from 'vitest'
import { collectAvitoForXlsx } from './avitoExtensionBridge'

class FakeWindow {
  location = { origin: 'https://satorna-wb.vercel.app' }
  listeners = new Set<(event: unknown) => void>()
  requests: Array<{ id: string; type: string }> = []
  addEventListener(_type: string, listener: (event: unknown) => void) { this.listeners.add(listener) }
  removeEventListener(_type: string, listener: (event: unknown) => void) { this.listeners.delete(listener) }
  postMessage(request: { id: string; type: string }) { this.requests.push(request) }
  reply(data: Record<string, unknown>, origin = this.location.origin) {
    for (const listener of [...this.listeners]) listener({ source: this, origin, data })
  }
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('Avito extension XLSX bridge', () => {
  it('waits for a matching extension result and reports partial collection', async () => {
    const fake = new FakeWindow()
    vi.stubGlobal('window', fake)
    vi.useFakeTimers()
    const result = collectAvitoForXlsx(10_000)
    const id = fake.requests[0].id
    fake.reply({ source: 'satorna-extension', type: 'AVITO_COLLECTION_RESULT', id, ok: true, complete: true }, 'https://untrusted.example')
    fake.reply({ source: 'satorna-extension', type: 'AVITO_COLLECTION_ACK', id })
    await vi.advanceTimersByTimeAsync(1501)
    fake.reply({ source: 'satorna-extension', type: 'AVITO_COLLECTION_RESULT', id, ok: true, complete: false, message: 'Часть фото не получена' })
    await expect(result).resolves.toEqual({ state: 'partial', message: 'Часть фото не получена' })
    expect(fake.listeners.size).toBe(0)
  })

  it('falls back quickly when the extension is absent', async () => {
    const fake = new FakeWindow()
    vi.stubGlobal('window', fake)
    vi.useFakeTimers()
    const result = collectAvitoForXlsx()
    await vi.advanceTimersByTimeAsync(1501)
    await expect(result).resolves.toMatchObject({ state: 'unavailable' })
    expect(fake.listeners.size).toBe(0)
  })
})
