import { describe, expect, it } from 'vitest'
import {
  buildCanonicalReviewLocalContextPath, encodeCanonicalReviewLocalCommand,
  parseCanonicalReviewLocalContext, parseCanonicalReviewLocalResult,
  buildCanonicalReviewHistoryPath, parseCanonicalReviewHistory,
  type CanonicalReviewLocalCommand,
} from './canonicalLocalReviews'

const scope = { organizationId: 1, marketplaceAccountId: 2, marketplace: 'wb' as const }
const uid = (n: number) => `10000000-0000-4000-8000-${String(n).padStart(12, '0')}`
const create: CanonicalReviewLocalCommand = {
  schemaVersion: 'review-local-command-v1', ...scope, actorMembershipId: 3,
  localCommandId: uid(1), operationKind: 'review.policy.create.v1', input: { policy: {
    schemaVersion: 'review-policy-v1', ...scope, policyId: uid(2), version: '1208925819614629174706177',
    approvalMode: 'manual', templateVersion: 'v1', modelVersion: 'fake-v1',
  } },
}
const result = { schemaVersion: 'review-local-command-result-v1', localCommandId: uid(1),
  operationKind: 'review.policy.create.v1', auditEventId: uid(3), completedAt: '2026-09-09T12:00:00.123456Z',
  policyId: uid(2), policyVersion: '1208925819614629174706177', draftId: null, draftRevision: null,
  decisionId: null, headId: null, headVersion: null }

describe('canonical local Reviews dormant contract', () => {
  it('retains unbounded versions as strings and validates exact result identity', () => {
    expect(JSON.parse(encodeCanonicalReviewLocalCommand(create, scope))).toEqual(create)
    expect(parseCanonicalReviewLocalResult(result, create)).toEqual(result)
  })

  it.each([
    { ...result, policyVersion: 1 }, { ...result, policyVersion: '012' },
    { ...result, policyId: uid(9) }, { ...result, localCommandId: uid(9) },
    { ...result, headId: uid(9) }, { ...result, credentialRef: 'private' },
  ])('rejects rounded, mismatched, extraneous result fields', value => {
    expect(() => parseCanonicalReviewLocalResult(value, create)).toThrow('CANONICAL_REVIEW_LOCAL_CONTRACT_INVALID')
  })

  it('rejects changed account and policy scope', () => {
    expect(() => encodeCanonicalReviewLocalCommand(create, { ...scope, marketplaceAccountId: 9 })).toThrow()
    const foreign = structuredClone(create)
    foreign.input.policy.marketplaceAccountId = 9
    expect(() => encodeCanonicalReviewLocalCommand(foreign, scope)).toThrow()
  })

  it('does not collapse null text or mistake current context for send permission', () => {
    const review = { reviewId: uid(4), externalReviewId: '0001\0 é' }
    const context = { schemaVersion: 'review-local-context-v1', ...scope, actorMembershipId: 3,
      policy: null, policyHead: null, draft: null, workflowHead: null, decision: null,
      review: { ...review, sourceObservationId: uid(5), sourceChecksum: 'a'.repeat(64), text: null,
        answered: false, canAnswer: null, sourceOrderState: 'current' } }
    expect(parseCanonicalReviewLocalContext(context, scope, review).review?.text).toBeNull()
    expect(() => parseCanonicalReviewLocalContext({ ...context, sendAllowed: true }, scope, review)).toThrow()
    expect(() => parseCanonicalReviewLocalContext(context, scope, { ...review, externalReviewId: '1' })).toThrow()
    expect(buildCanonicalReviewLocalContextPath(scope, review)).toContain('external_review_id=0001%00+e%CC%81')
  })

  it('performs decimal head CAS math without JS number conversion', () => {
    const command: CanonicalReviewLocalCommand = { schemaVersion: 'review-local-command-v1', ...scope,
      actorMembershipId: 3, localCommandId: uid(7), operationKind: 'review.policy.select.v1',
      input: { policyId: uid(2), policyVersion: '1', policyChecksum: 'b'.repeat(64),
        expectedHeadVersion: '9223372036854775808' } }
    const selected = { ...result, localCommandId: uid(7), operationKind: command.operationKind,
      policyVersion: '1', headId: uid(8), headVersion: '9223372036854775809' }
    expect(parseCanonicalReviewLocalResult(selected, command).headVersion).toBe('9223372036854775809')
    expect(() => parseCanonicalReviewLocalResult({ ...selected, headVersion: '9223372036854775810' }, command)).toThrow()
  })
})

describe('immutable local Review history pages', () => {
  const audit = { schemaVersion: 'review-audit-v1', ...scope, eventId: uid(10), aggregateId: uid(11),
    aggregateVersion: '1', eventKind: 'draft.published', occurredAt: '2026-09-09T12:00:00.123456Z',
    actorKind: 'membership', actorMembershipId: 3, commandId: null, attemptId: null, reasonCode: null,
    policyId: uid(2), draftId: uid(12), decisionId: null, beforeState: null, afterState: 'draft_current' }
  const page = { schemaVersion: 'review-local-history-v1', ...scope, reviewId: uid(4), headId: uid(11),
    throughVersion: '2', events: [audit], nextAfterVersion: '1' }
  const query = { reviewId: uid(4), limit: 1 }

  it('checks exact first page and carries frozen head/bound into continuation', () => {
    expect(parseCanonicalReviewHistory(page, scope, query)).toEqual(page)
    const continuation = { ...query, headId: uid(11), throughVersion: '2', afterVersion: '1' }
    const path = buildCanonicalReviewHistoryPath(scope, continuation)
    expect(path).toContain('through_version=2')
    expect(path).toContain('after_version=1')
  })

  it.each([
    { ...page, nextAfterVersion: null }, { ...page, throughVersion: '0' }, { ...page, events: [] },
    { ...page, events: [{ ...audit, marketplaceAccountId: 99 }] },
    { ...page, events: [{ ...audit, aggregateVersion: '2' }] },
    { ...page, events: [{ ...audit, privateText: 'not allowed' }] },
  ])('rejects missing, foreign, noncontiguous or private events', value => {
    expect(() => parseCanonicalReviewHistory(value, scope, query)).toThrow()
  })

  it('rejects upper-bound drift rather than silently using the latest head', () => {
    expect(() => parseCanonicalReviewHistory(page, scope, { ...query, headId: uid(11), throughVersion: '1' })).toThrow()
  })

  it('keeps >BIGINT cursor arithmetic exact', () => {
    const after = '1208925819614629174706176'
    const through = (BigInt(after) + 2n).toString()
    const next = (BigInt(after) + 1n).toString()
    const large = { ...page, throughVersion: through, nextAfterVersion: next,
      events: [{ ...audit, aggregateVersion: next, beforeState: 'draft_current' }] }
    expect(parseCanonicalReviewHistory(large, scope, { ...query, headId: uid(11), throughVersion: through,
      afterVersion: after }).nextAfterVersion).toBe(next)
  })

  it('represents a fact with no draft as empty, not fabricated audit', () => {
    const empty = { ...page, headId: null, throughVersion: '0', events: [], nextAfterVersion: null }
    expect(parseCanonicalReviewHistory(empty, scope, query).events).toEqual([])
    expect(() => buildCanonicalReviewHistoryPath(scope, { ...query, afterVersion: '1' })).toThrow()
  })
})
