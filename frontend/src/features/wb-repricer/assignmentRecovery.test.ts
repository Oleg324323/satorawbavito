import { afterEach, expect, it, vi } from 'vitest'
import { applyLiveRepricerStrategy, strategyAssignmentExecutionWarning } from './liveParityData'
import { rememberAssignmentRevisions } from './strategyAssignmentOrdering'

afterEach(() => vi.unstubAllGlobals())
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

it('reads the actual committed revision after a lost acknowledgement and clears with CAS, without replay', async () => {
  const token = 'synthetic-lost-ack', article = 'synthetic-product'
  rememberAssignmentRevisions(token, { [article]: 'before' })
  const calls: Array<{ url: string; body?: Record<string, unknown> }> = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit) => {
    const body = typeof init.body === 'string' ? JSON.parse(init.body) : undefined
    calls.push({ url, body })
    if (url.endsWith('/configuration-state')) return json({ articleId: article, assignmentRevision: 'committed' })
    if (body?.strategyName === 'night_price_mode') return json({ detail: 'synthetic execution failure' }, 500)
    expect(body.expectedRevisions).toEqual({ [article]: 'committed' })
    expect(body.executeAfterAssign).toBe(false)
    return json({ unassignedCount: 1, assignmentRevisions: { [article]: 'none' } })
  }))
  await expect(applyLiveRepricerStrategy(token, [article], 'night_price_mode')).rejects.toThrow()
  const result = await applyLiveRepricerStrategy(token, [article], 'none', undefined, false)
  expect(result.unassignedCount).toBe(1)
  expect(calls.map(call => call.url.split('/').at(-1))).toEqual(['bulk', 'configuration-state', 'bulk'])
})

it('preserves the saved revision and explicit execution error acknowledgement', async () => {
  const token = 'synthetic-execution-error', article = 'synthetic-product'
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ assignedCount: 1, assignmentRevisions: { [article]: 'saved' },
    executionError: { code: 'STRATEGY_EXECUTION_FAILED', message: 'Saved; execution failed' } })))
  const result = await applyLiveRepricerStrategy(token, [article], 'night_price_mode')
  expect(strategyAssignmentExecutionWarning(result)).toBe('Saved; execution failed')
  expect(result.assignmentRevisions).toEqual({ [article]: 'saved' })
})
