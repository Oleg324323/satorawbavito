import { z } from 'zod'
import type { CanonicalReviewLocalCommand, CanonicalReviewLocalContext, CanonicalReviewScope } from './canonicalLocalReviews'

export const canonicalReviewsEnabled = import.meta.env.VITE_CANONICAL_REVIEWS_ENABLED === 'true'
const uuid = z.string().uuid()
const factSchema = z.object({
  schema_version: z.literal('canonical-review-fact-v1'), organization_id: z.number().int().positive(),
  marketplace_account_id: z.number().int().positive(), marketplace: z.literal('wb'),
  external_review_id: z.string().min(1).max(512), review_id: uuid, current_observation_id: uuid,
  version: z.string().regex(/^[1-9][0-9]*$/).max(128), revision: z.string().regex(/^[1-9][0-9]*$/).max(128),
  text: z.string().max(200_000).nullable(), answered: z.boolean(), can_answer: z.boolean().nullable(),
  source_order_state: z.enum(['current', 'ambiguous']), content_checksum: z.string().regex(/^[0-9a-f]{64}$/),
  external_product_id: z.string().max(512).nullable(), source_created_at: z.string().datetime({ offset: true }),
  source_updated_at: z.string().datetime({ offset: true }).nullable(),
  source_schema_version: z.string().min(1).max(128), normalization_version: z.string().min(1).max(128),
}).strict()
export function parseCanonicalReviewFact(value: unknown, scope: CanonicalReviewScope, externalId: string) {
  const fact = factSchema.parse(value)
  if (fact.organization_id !== scope.organizationId || fact.marketplace_account_id !== scope.marketplaceAccountId
    || scope.marketplace !== 'wb' || fact.external_review_id !== externalId) throw new Error('REVIEW_FACT_SCOPE')
  return fact
}
export function localReviewReady(c: CanonicalReviewLocalContext) {
  return Boolean(c.policy && c.policyHead && c.review && c.draft && c.workflowHead
    && !c.review.answered && c.review.canAnswer === true && c.review.sourceOrderState === 'current'
    && c.draft.policyHeadId === c.policyHead.headId && c.draft.policyHeadVersion === c.policyHead.version
    && c.draft.generation.policyId === c.policy.policyId && c.draft.generation.policyVersion === c.policy.version
    && c.draft.generation.policyChecksum === c.policyHead.policyChecksum
    && c.draft.generation.sourceObservationId === c.review.sourceObservationId
    && c.draft.generation.sourceChecksum === c.review.sourceChecksum)
}
export function firstManualReviewReady(c: CanonicalReviewLocalContext) {
  return Boolean(c.policy && c.policyHead && c.review && !c.draft && !c.workflowHead && !c.decision
    && !c.review.answered && c.review.canAnswer === true && c.review.sourceOrderState === 'current')
}
/** Explicit human operation only; never creates a fake first draft or sends externally. */
export function prepareReviewDetailCommand(c: CanonicalReviewLocalContext, action: 'create' | 'edit' | 'approved' | 'rejected', text: string): CanonicalReviewLocalCommand {
  if (!(action === 'create' ? firstManualReviewReady(c) : localReviewReady(c)) || !c.review || !c.policyHead || !c.policy) throw new Error('REVIEW_NOT_READY')
  const base = { schemaVersion: 'review-local-command-v1' as const, organizationId: c.organizationId,
    marketplaceAccountId: c.marketplaceAccountId, marketplace: c.marketplace,
    actorMembershipId: c.actorMembershipId, localCommandId: crypto.randomUUID() }
  const input = { reviewId: c.review.reviewId, externalReviewId: c.review.externalReviewId, draftId: c.draft?.draftId ?? crypto.randomUUID(),
    expectedPolicyHeadId: c.policyHead.headId, expectedPolicyHeadVersion: c.policyHead.version,
    expectedHeadVersion: c.workflowHead?.version ?? '0' }
  if (action !== 'edit' && action !== 'create') {
    if (!c.draft) throw new Error('REVIEW_NOT_READY')
    return { ...base, operationKind: 'review.decision.record.v1', input: { ...input,
    draftRevision: c.draft.revision, bindingChecksum: c.draft.bindingChecksum,
    sourceObservationId: c.review.sourceObservationId, decisionKind: action } }
  }
  const at = new Date().toISOString().replace(/Z$/, '000Z')
  return { ...base, operationKind: 'review.draft.publish.v1', input: { ...input, draftId: crypto.randomUUID(),
    expectedDraftRevision: c.draft?.revision ?? '0', text, generation: { schemaVersion: 'review-generation-v1',
      generationId: crypto.randomUUID(), ...(action === 'create' ? { mode: 'manual' as const }
        : { mode: 'manual_edit' as const, previousDraftId: c.draft!.draftId }),
      sourceObservationId: c.review.sourceObservationId, sourceChecksum: c.review.sourceChecksum,
      policyId: c.policy.policyId, policyVersion: c.policy.version, policyChecksum: c.policyHead.policyChecksum,
      templateVersion: c.policy.templateVersion, modelVersion: c.policy.modelVersion,
      actorMembershipId: c.actorMembershipId, startedAt: at, completedAt: at } } }
}
