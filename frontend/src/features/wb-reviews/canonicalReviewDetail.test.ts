import { describe, expect, it } from 'vitest'
import { firstManualReviewReady, localReviewReady, parseCanonicalReviewFact, prepareReviewDetailCommand } from './canonicalReviewDetail'
import { encodeCanonicalReviewLocalCommand, parseCanonicalReviewLocalContext, type CanonicalReviewLocalContext } from './canonicalLocalReviews'

const uid = (n: number) => `10000000-0000-4000-8000-${String(n).padStart(12, '0')}`
const scope = { organizationId: 1, marketplaceAccountId: 2, marketplace: 'wb' as const }
const fact = { schema_version: 'canonical-review-fact-v1', organization_id: 1, marketplace_account_id: 2,
  marketplace: 'wb', external_review_id: 'actual-wb-id', review_id: uid(1), current_observation_id: uid(2),
  version: '9223372036854775808', revision: '1', text: null, answered: false, can_answer: null,
  source_order_state: 'ambiguous', content_checksum: 'a'.repeat(64), external_product_id: null,
  source_created_at: '2026-09-10T12:00:00Z', source_updated_at: null, source_schema_version: 'v1', normalization_version: 'v1' }
function context(): CanonicalReviewLocalContext {
  return { schemaVersion: 'review-local-context-v1', ...scope, actorMembershipId: 3,
    policy: { schemaVersion: 'review-policy-v1', ...scope, policyId: uid(3), version: '1', approvalMode: 'manual', templateVersion: 'v1', modelVersion: 'manual' },
    policyHead: { headId: uid(4), version: '9007199254740993', policyChecksum: 'b'.repeat(64) },
    review: { reviewId: uid(1), externalReviewId: 'actual-wb-id', sourceObservationId: uid(2), sourceChecksum: 'a'.repeat(64), text: null, answered: false, canAnswer: true, sourceOrderState: 'current' },
    draft: { draftId: uid(5), revision: '9007199254740993', text: 'Existing', textChecksum: 'c'.repeat(64), bindingChecksum: 'd'.repeat(64), policyHeadId: uid(4), policyHeadVersion: '9007199254740993',
      generation: { schemaVersion: 'review-generation-v1', generationId: uid(6), sourceObservationId: uid(2), sourceChecksum: 'a'.repeat(64), policyId: uid(3), policyVersion: '1', policyChecksum: 'b'.repeat(64), templateVersion: 'v1', modelVersion: 'manual', actorMembershipId: 3, mode: 'manual_edit', previousDraftId: uid(7), startedAt: '2026-09-10T12:00:00.000000Z', completedAt: '2026-09-10T12:00:00.000000Z' } },
    workflowHead: { headId: uid(8), version: '9007199254740994' }, decision: null }
}
describe('canonical Reviews selected record adapter', () => {
  it('creates first human draft with zero CAS and captured policy, without predecessor or AI metadata fabrication', () => {
    const c = context(); c.draft = null; c.workflowHead = null; c.policy!.modelVersion = 'existing-policy-label'
    expect(firstManualReviewReady(c)).toBe(true)
    const command = prepareReviewDetailCommand(c, 'create', 'Human first reply')
    const encoded = JSON.parse(encodeCanonicalReviewLocalCommand(command, scope))
    expect(encoded.input).toMatchObject({ expectedHeadVersion: '0', expectedDraftRevision: '0', text: 'Human first reply',
      generation: { mode: 'manual', modelVersion: 'existing-policy-label', policyId: c.policy!.policyId,
        policyVersion: c.policy!.version, policyChecksum: c.policyHead!.policyChecksum, sourceObservationId: c.review!.sourceObservationId } })
    expect(encoded.input.generation).not.toHaveProperty('previousDraftId')
    expect(encoded).not.toHaveProperty('send')
  })
  it('closes creation without actual policy/source or when a predecessor exists', () => {
    const existing = context(), missingPolicy = context(), missingSource = context()
    missingPolicy.draft = null; missingPolicy.workflowHead = null; missingPolicy.policy = null; missingPolicy.policyHead = null
    missingSource.draft = null; missingSource.workflowHead = null; missingSource.review = null
    for (const c of [existing, missingPolicy, missingSource]) {
      expect(firstManualReviewReady(c)).toBe(false)
      expect(() => prepareReviewDetailCommand(c, 'create', 'Human')).toThrow()
    }
  })
  it.each(['', '  ', '\ud800'])('rejects invalid initial human text', text => {
    const c = context(); c.draft = null; c.workflowHead = null
    expect(() => encodeCanonicalReviewLocalCommand(prepareReviewDetailCommand(c, 'create', text), scope)).toThrow()
  })
  it('rejects initial manual command with predecessor or nonzero CAS', () => {
    const c = context(); c.draft = null; c.workflowHead = null
    const command = prepareReviewDetailCommand(c, 'create', 'Human')
    if (command.operationKind !== 'review.draft.publish.v1') throw new Error('unexpected operation')
    for (const versions of [['1', '1'], ['4', '2'], ['0', '1'], ['1', '0']]) {
      expect(() => encodeCanonicalReviewLocalCommand({ ...command, input: { ...command.input, expectedHeadVersion: versions[0], expectedDraftRevision: versions[1] } }, scope)).toThrow()
    }
    const payload = JSON.parse(JSON.stringify(command)); payload.input.generation.previousDraftId = uid(8)
    expect(() => encodeCanonicalReviewLocalCommand(payload, scope)).toThrow()
  })
  it('reads persisted first manual draft and supports subsequent manual edit, rejecting impossible manual revision', () => {
    const c = context(), generation = c.draft!.generation
    const { previousDraftId: ignored, ...base } = generation.mode === 'manual_edit' ? generation : { ...generation, previousDraftId: '' }
    void ignored
    c.draft!.generation = { ...base, mode: 'manual' }; c.draft!.revision = '1'; c.workflowHead!.version = '1'
    expect(parseCanonicalReviewLocalContext(c, scope, c.review!)).toEqual(c)
    const next = prepareReviewDetailCommand(c, 'edit', 'Edited')
    expect(JSON.parse(encodeCanonicalReviewLocalCommand(next, scope)).input.generation).toMatchObject({ mode: 'manual_edit', previousDraftId: c.draft!.draftId })
    c.draft!.revision = '2'
    expect(() => parseCanonicalReviewLocalContext(c, scope, c.review!)).toThrow()
  })
  it('retains exact versions and unknown/ambiguous fact state', () => {
    expect(parseCanonicalReviewFact(fact, scope, 'actual-wb-id')).toEqual(fact)
  })
  it.each([{ marketplace_account_id: 3 }, { organization_id: 3 }, { external_review_id: 'other' }, { can_answer: 'true' }, { revision: 2 }, { text: {} }])('rejects malformed or cross-owner fact', change => {
    expect(() => parseCanonicalReviewFact({ ...fact, ...change }, scope, 'actual-wb-id')).toThrow()
  })
  it('prepares explicit manual edit with predecessor and exact CAS, never fake or send', () => {
    const c = context(), command = prepareReviewDetailCommand(c, 'edit', 'Human text')
    expect(command.operationKind).toBe('review.draft.publish.v1')
    expect(JSON.parse(encodeCanonicalReviewLocalCommand(command, scope)).input).toMatchObject({
      expectedHeadVersion: '9007199254740994', expectedDraftRevision: '9007199254740993',
      generation: { mode: 'manual_edit', previousDraftId: uid(5), actorMembershipId: 3 }, text: 'Human text' })
    expect(command.localCommandId).not.toBe(prepareReviewDetailCommand(c, 'edit', 'Human text').localCommandId)
  })
  it('binds decision to the stored draft, source and head, not unsaved text', () => {
    const command = prepareReviewDetailCommand(context(), 'approved', 'ignored unsaved text')
    expect(JSON.parse(encodeCanonicalReviewLocalCommand(command, scope)).input).toMatchObject({ draftId: uid(5), draftRevision: '9007199254740993', sourceObservationId: uid(2), decisionKind: 'approved' })
  })
  it('closes first draft, unknown answerability, source and policy drift', () => {
    const cases = [context(), context(), context(), context()]
    cases[0].draft = null; cases[0].workflowHead = null
    cases[1].review!.canAnswer = null
    cases[2].draft!.generation.sourceObservationId = uid(99)
    cases[3].policyHead!.version = '9007199254740995'
    for (const c of cases) { expect(localReviewReady(c)).toBe(false); expect(() => prepareReviewDetailCommand(c, 'approved', '')).toThrow() }
  })
})
