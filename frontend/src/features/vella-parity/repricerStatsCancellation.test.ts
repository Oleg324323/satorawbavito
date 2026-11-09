import { afterEach, expect, it, vi } from 'vitest'
import { installRepricerStatsLiveBridge } from './VellaHtmlParityPage'
import { resetLiveRepricerParityCache } from '../wb-repricer/liveParityData'

afterEach(() => { resetLiveRepricerParityCache(); vi.unstubAllGlobals() })

it.each(['reload', 'dispose', 'later-page'] as const)('cancels obsolete statistics transport on %s', async action => {
  vi.stubGlobal('window', {
    __vellaReportPeriods: { 'repricer-stats': { days: 7, mode: 'custom', fromIso: '2026-09-01', toIso: '2026-09-07' } },
  })
  vi.stubGlobal('document', { getElementById: () => null, querySelector: () => null })
  const pending: { signal: AbortSignal | null | undefined; finish: () => void }[] = []
  vi.stubGlobal('fetch', vi.fn((_path: string, init?: RequestInit) => new Promise<Response>((resolve, reject) => {
    const index = pending.length
    pending.push({ signal: init?.signal, finish: () => resolve(new Response(JSON.stringify({
      items: [], total: action === 'later-page' && index === 0 ? 501 : 0,
      page: index + 1, pageSize: 500,
    }), { headers: { 'content-type': 'application/json' } })) })
    init?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true })
  })))
  const dispose = installRepricerStatsLiveBridge('synthetic-cancellation')
  const load = window.__vellaLoadLiveRepricerStats!
  const promises = [load()]
  try {
    await vi.waitFor(() => expect(pending).toHaveLength(1))
    if (action === 'later-page') {
      pending[0]!.finish()
      await vi.waitFor(() => expect(pending).toHaveLength(2))
    }
    const obsolete = pending.at(-1)!
    if (action === 'reload') promises.push(load())
    else dispose()
    await Promise.resolve()
    if (action === 'reload') {
      // An identical in-flight read is shared; cancelling its old consumer
      // must not cancel the new consumer or duplicate the provider read.
      expect(pending).toHaveLength(1)
      expect(obsolete.signal?.aborted).toBe(false)
    }
    else {
      expect(obsolete.signal?.aborted).toBe(true)
      expect(await load()).toBeNull()
      expect(window.__vellaLoadLiveRepricerStats).toBeUndefined()
    }
  } finally {
    dispose()
    pending.forEach(request => request.finish())
    await Promise.allSettled(promises)
  }
})
