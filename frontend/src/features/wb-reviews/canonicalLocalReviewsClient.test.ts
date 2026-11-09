import { describe, expect, it, vi } from 'vitest'
import { createCanonicalLocalReviewsClient } from './canonicalLocalReviewsClient'
import type { CanonicalReviewLocalCommand } from './canonicalLocalReviews'

const scope = { organizationId: 1, marketplaceAccountId: 2, marketplace: 'wb' as const }
const uid = (n: number) => `10000000-0000-4000-8000-${String(n).padStart(12, '0')}`
const context = { schemaVersion: 'review-local-context-v1', ...scope, actorMembershipId: 3,
  policy: null, policyHead: null, review: null, draft: null, workflowHead: null, decision: null }
const json = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })
const command = (): CanonicalReviewLocalCommand => ({ schemaVersion: 'review-local-command-v1', ...scope,
  actorMembershipId: 3, localCommandId: uid(1), operationKind: 'review.policy.create.v1', input: { policy: {
    schemaVersion: 'review-policy-v1', ...scope, policyId: uid(2), version: '1208925819614629174706177',
    approvalMode: 'manual', templateVersion: 'v1', modelVersion: 'fake-v1',
  } } })
const result = { schemaVersion: 'review-local-command-result-v1', localCommandId: uid(1),
  operationKind: 'review.policy.create.v1', auditEventId: uid(3), completedAt: '2026-09-09T12:00:00.123456Z',
  policyId: uid(2), policyVersion: '1208925819614629174706177', draftId: null, draftRevision: null,
  decisionId: null, headId: null, headVersion: null }
function setup(fetcher: typeof fetch, isCurrent = () => true) {
  return createCanonicalLocalReviewsClient({ scope, actorMembershipId: 3, accessToken: 'synthetic-test-only',
    isCurrent, fetch: fetcher })
}

describe('dormant canonical local Reviews transport (no real network)', () => {
  it('discovers server membership only from validated GET context; cannot prepare writes without it', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(json(context))
    const client = createCanonicalLocalReviewsClient({ scope, accessToken: 'test-only', isCurrent: () => true, fetch: fetcher })
    expect((await client.context()).state).toBe('ready')
    expect(() => client.prepare(command())).toThrow()
    expect(fetcher).toHaveBeenCalledOnce()
  })

  it('bounds JSON before parsing, without fallback', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(json({ text: 'x'.repeat(1_048_577) }))
    expect((await setup(fetcher).context()).state).toBe('invalid-response')
    expect(fetcher).toHaveBeenCalledOnce()
  })
  it('uses exact authenticated no-store endpoint with no fallback', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(json(context))
    const client = setup(fetcher)
    expect(await client.context()).toEqual({ state: 'ready', data: context })
    expect(fetcher).toHaveBeenCalledOnce()
    expect(fetcher.mock.calls[0][0]).toContain('/api/v2/reviews/local/context?marketplace_account_id=2&marketplace=wb')
    expect(fetcher.mock.calls[0][1]).toMatchObject({ method: 'GET', cache: 'no-store', credentials: 'omit', redirect: 'error' })
  })

  it.each([400, 401, 403, 404, 409, 429, 500, 503])('does not echo error bodies or retry HTTP %s', async status => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response('private provider payload', { status }))
    const response = await setup(fetcher).context()
    expect(response.state).not.toBe('ready')
    expect(JSON.stringify(response)).not.toContain('private')
    expect(fetcher).toHaveBeenCalledOnce()
  })

  it.each([
    () => new Response('<html>private</html>', { headers: { 'Content-Type': 'text/html' } }),
    () => new Response('{', { headers: { 'Content-Type': 'application/json' } }),
    () => json({ ...context, actorMembershipId: 4 }),
    () => json({ ...context, marketplaceAccountId: 4 }),
  ])('rejects malformed/non-JSON and cross-identity success', async response => {
    const client = setup(vi.fn<typeof fetch>().mockResolvedValue(response()))
    expect(await client.context()).toEqual({ state: 'invalid-response', commandOutcome: 'not-submitted' })
  })

  it('rejects a late earlier read even when fetch ignores abort', async () => {
    let resolve!: (value: Response) => void
    const fetcher = vi.fn<typeof fetch>().mockImplementationOnce(() => new Promise(done => { resolve = done }))
      .mockResolvedValueOnce(json(context))
    const client = setup(fetcher)
    const earlier = client.context()
    expect((await client.context()).state).toBe('ready')
    resolve(json(context))
    expect((await earlier).state).toBe('stale')
  })

  it('invalidates a disposed session epoch even after selecting the same account again', async () => {
    let resolve!: (value: Response) => void
    const fetcher = vi.fn<typeof fetch>().mockImplementation(() => new Promise(done => { resolve = done }))
    const client = setup(fetcher)
    const earlier = client.context()
    client.dispose()
    expect(fetcher.mock.calls[0][1]?.signal?.aborted).toBe(true)
    resolve(json(context))
    expect((await earlier).state).toBe('stale')
    expect((await client.context()).state).toBe('stale')
    expect(fetcher).toHaveBeenCalledOnce()
  })

  it('freezes intent and reuses identical bytes only on an explicit retry', async () => {
    const fetcher = vi.fn<typeof fetch>().mockRejectedValueOnce(new Error('private'))
      .mockResolvedValueOnce(json(result))
    const client = setup(fetcher)
    const mutable = command(), handle = client.prepare(mutable)
    mutable.localCommandId = uid(8)
    expect(await client.submit(handle)).toEqual({ state: 'unavailable', commandOutcome: 'unknown' })
    expect(fetcher).toHaveBeenCalledOnce()
    expect(await client.submit(handle)).toEqual({ state: 'ready', data: result })
    expect(fetcher.mock.calls[0][1]?.body).toBe(fetcher.mock.calls[1][1]?.body)
    expect(fetcher.mock.calls[0][1]?.method).toBe('POST')
    expect(Object.keys(handle)).toEqual(['localCommandId'])
  })

  it('does not manufacture retry or approval after 409; handles are client-owned', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 409 }))
    const client = setup(fetcher), other = setup(fetcher)
    const handle = client.prepare(command())
    expect(await other.submit(handle)).toEqual({ state: 'invalid-request', commandOutcome: 'not-submitted' })
    expect(await client.submit(handle)).toEqual({ state: 'conflict', commandOutcome: 'unknown' })
    expect(fetcher).toHaveBeenCalledOnce()
  })

  it('does not submit for a revoked UI epoch or a different actor', async () => {
    let active = true
    const fetcher = vi.fn<typeof fetch>()
    const client = setup(fetcher, () => active), handle = client.prepare(command())
    expect(() => client.prepare({ ...command(), actorMembershipId: 4 })).toThrow()
    active = false
    expect(await client.submit(handle)).toEqual({ state: 'stale', commandOutcome: 'not-submitted' })
    expect(fetcher).not.toHaveBeenCalled()
  })

  it('validates a history page without writing receipts', async () => {
    const page = { schemaVersion: 'review-local-history-v1', ...scope, reviewId: uid(4), headId: null,
      throughVersion: '0', events: [], nextAfterVersion: null }
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(json(page))
    expect(await setup(fetcher).history({ reviewId: uid(4) })).toEqual({ state: 'ready', data: page })
    expect(fetcher.mock.calls[0][1]?.method).toBe('GET')
    expect(fetcher).toHaveBeenCalledOnce()
  })
})
