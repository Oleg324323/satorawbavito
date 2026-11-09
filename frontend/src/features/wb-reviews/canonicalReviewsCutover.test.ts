import { afterEach, expect, it, vi } from 'vitest'

afterEach(() => { vi.unstubAllEnvs(); vi.resetModules() })
it('blocks all former draft/send writers without network or fallback after explicit cutover', async () => {
  vi.stubEnv('VITE_CANONICAL_REVIEWS_ENABLED', 'true')
  vi.resetModules()
  const api = await import('./api')
  const fetcher = vi.spyOn(globalThis, 'fetch')
  try {
    await expect(api.generateWbReviewDraft('test-only', 'review')).rejects.toThrow('каноническую')
    await expect(api.generateWbReviewDraftsBatch('test-only', [])).rejects.toThrow('каноническую')
    await expect(api.approveWbReviewDraft('test-only', 'draft')).rejects.toThrow('каноническую')
    await expect(api.rejectWbReviewDraft('test-only', 'draft')).rejects.toThrow('каноническую')
    await expect(api.requestWbReviewSend('test-only', 'draft')).rejects.toThrow('каноническую')
    expect(fetcher).not.toHaveBeenCalled()
  } finally { fetcher.mockRestore() }
})
