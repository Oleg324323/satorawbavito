/** Dormant local authoring contract. No UI switch, auto-approval, send or retry. */
import { z } from 'zod'

const id = z.number().int().positive().max(2147483647)
const uuid = z.string().uuid().refine(value => value === value.toLowerCase()
  && value !== '00000000-0000-0000-0000-000000000000')
const version = z.string().regex(/^[1-9][0-9]*$/)
const expectedVersion = z.string().regex(/^(0|[1-9][0-9]*)$/)
const checksum = z.string().regex(/^[0-9a-f]{64}$/)
const label = z.string().regex(/^[A-Za-z0-9_.:/-]{1,128}$/)
const instant = z.string().regex(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$/)
  .refine(value => Number.isFinite(Date.parse(value)))
const owner = { organizationId: id, marketplaceAccountId: id, marketplace: z.enum(['wb', 'avito']) }
const scopeSchema = z.object(owner).strict()
const policySchema = z.object({ schemaVersion: z.literal('review-policy-v1'), ...owner, policyId: uuid,
  version, approvalMode: z.literal('manual'), templateVersion: label, modelVersion: label }).strict()
const provenance = { schemaVersion: z.literal('review-generation-v1'), generationId: uuid,
  sourceObservationId: uuid, sourceChecksum: checksum, policyId: uuid, policyVersion: version,
  policyChecksum: checksum, templateVersion: label, modelVersion: label, actorMembershipId: id,
  startedAt: instant, completedAt: instant }
const generationSchema = z.discriminatedUnion('mode', [
  z.object({ ...provenance, mode: z.literal('fake') }).strict(),
  z.object({ ...provenance, mode: z.literal('manual') }).strict(),
  z.object({ ...provenance, mode: z.literal('manual_edit'), previousDraftId: uuid }).strict(),
]).refine(value => value.completedAt >= value.startedAt)
const contextSchema = z.object({ schemaVersion: z.literal('review-local-context-v1'), ...owner,
  actorMembershipId: id, policy: policySchema.nullable(),
  policyHead: z.object({ headId: uuid, version, policyChecksum: checksum }).strict().nullable(),
  review: z.object({ reviewId: uuid, externalReviewId: z.string().min(1), sourceObservationId: uuid,
    sourceChecksum: checksum, text: z.string().nullable(), answered: z.boolean(),
    canAnswer: z.boolean().nullable(), sourceOrderState: z.literal('current') }).strict().nullable(),
  draft: z.object({ draftId: uuid, revision: version, text: z.string(), textChecksum: checksum,
    bindingChecksum: checksum, generation: generationSchema, policyHeadId: uuid,
    policyHeadVersion: version }).strict().nullable(),
  workflowHead: z.object({ headId: uuid, version }).strict().nullable(),
  decision: z.object({ decisionId: uuid, draftId: uuid, draftRevision: version,
    decisionKind: z.enum(['approved', 'rejected']), actorMembershipId: id }).strict().nullable(),
}).strict()
const commandBase = { schemaVersion: z.literal('review-local-command-v1'), ...owner,
  actorMembershipId: id, localCommandId: uuid }
const reviewInput = { reviewId: uuid, externalReviewId: z.string().min(1), draftId: uuid,
  expectedPolicyHeadId: uuid, expectedPolicyHeadVersion: version }
const commandSchema = z.discriminatedUnion('operationKind', [
  z.object({ ...commandBase, operationKind: z.literal('review.policy.create.v1'),
    input: z.object({ policy: policySchema }).strict() }).strict(),
  z.object({ ...commandBase, operationKind: z.literal('review.policy.select.v1'),
    input: z.object({ policyId: uuid, policyVersion: version, policyChecksum: checksum,
      expectedHeadVersion: expectedVersion }).strict() }).strict(),
  z.object({ ...commandBase, operationKind: z.literal('review.draft.publish.v1'),
    input: z.object({ ...reviewInput, expectedHeadVersion: expectedVersion,
      expectedDraftRevision: expectedVersion, generation: generationSchema,
      text: z.string().refine(value => value.trim().length > 0
        && !Array.from(value).some(character => { const point = character.codePointAt(0)!; return point >= 0xd800 && point <= 0xdfff })) }).strict() }).strict(),
  z.object({ ...commandBase, operationKind: z.literal('review.decision.record.v1'),
    input: z.object({ ...reviewInput, draftRevision: version, bindingChecksum: checksum,
      sourceObservationId: uuid, expectedHeadVersion: version,
      decisionKind: z.enum(['approved', 'rejected']) }).strict() }).strict(),
])
const resultSchema = z.object({ schemaVersion: z.literal('review-local-command-result-v1'),
  localCommandId: uuid, operationKind: z.enum(['review.policy.create.v1', 'review.policy.select.v1',
    'review.draft.publish.v1', 'review.decision.record.v1']), auditEventId: uuid, completedAt: instant,
  policyId: uuid.nullable(), policyVersion: version.nullable(), draftId: uuid.nullable(),
  draftRevision: version.nullable(), decisionId: uuid.nullable(), headId: uuid.nullable(),
  headVersion: version.nullable() }).strict()

export type CanonicalReviewScope = z.infer<typeof scopeSchema>
export type CanonicalReviewLocalContext = z.infer<typeof contextSchema>
export type CanonicalReviewLocalCommand = z.infer<typeof commandSchema>
export type CanonicalReviewLocalResult = z.infer<typeof resultSchema>

const sameScope = (a: CanonicalReviewScope, b: CanonicalReviewScope) => a.organizationId === b.organizationId
  && a.marketplaceAccountId === b.marketplaceAccountId && a.marketplace === b.marketplace
function invalid(): never { throw new Error('CANONICAL_REVIEW_LOCAL_CONTRACT_INVALID') }

export function parseCanonicalReviewLocalContext(payload: unknown, scope: CanonicalReviewScope,
  expectedReview?: { reviewId: string; externalReviewId: string }): CanonicalReviewLocalContext {
  const parsed = contextSchema.safeParse(payload), checked = scopeSchema.safeParse(scope)
  if (!parsed.success || !checked.success || !sameScope(parsed.data, checked.data)) return invalid()
  const value = parsed.data
  if ((value.policy === null) !== (value.policyHead === null)
    || value.policy && !sameScope(value.policy, checked.data)
    || (value.draft === null) !== (value.workflowHead === null)
    || value.draft && !value.review
    || value.draft?.generation.mode === 'manual' && value.draft.revision !== '1'
    || value.decision && (!value.draft || value.decision.draftId !== value.draft.draftId
      || value.decision.draftRevision !== value.draft.revision)
    || expectedReview && (!value.review || value.review.reviewId !== expectedReview.reviewId
      || value.review.externalReviewId !== expectedReview.externalReviewId)
    || !expectedReview && value.review !== null) return invalid()
  return value
}

/** Retain this exact string for retries; do not recreate UUIDs or generation. */
export function encodeCanonicalReviewLocalCommand(input: CanonicalReviewLocalCommand, scope: CanonicalReviewScope): string {
  const parsed = commandSchema.safeParse(input), checked = scopeSchema.safeParse(scope)
  if (!parsed.success || !checked.success || !sameScope(parsed.data, checked.data)) return invalid()
  const value = parsed.data
  if (value.operationKind === 'review.policy.create.v1' && !sameScope(value.input.policy, checked.data)) return invalid()
  if (value.operationKind === 'review.draft.publish.v1') {
    if (value.input.generation.actorMembershipId !== value.actorMembershipId
      || (value.input.expectedHeadVersion === '0') !== (value.input.expectedDraftRevision === '0')
      || value.input.generation.mode === 'manual' && value.input.expectedHeadVersion !== '0'
      || value.input.generation.mode === 'manual_edit' && value.input.expectedHeadVersion === '0') return invalid()
  }
  return JSON.stringify(value)
}

export function parseCanonicalReviewLocalResult(payload: unknown,
  command: CanonicalReviewLocalCommand): CanonicalReviewLocalResult {
  // Validate command as well: response expectations must not be an unchecked cast.
  encodeCanonicalReviewLocalCommand(command, { organizationId: command.organizationId,
    marketplaceAccountId: command.marketplaceAccountId, marketplace: command.marketplace })
  const parsed = resultSchema.safeParse(payload)
  if (!parsed.success) return invalid()
  const r = parsed.data
  if (r.localCommandId !== command.localCommandId || r.operationKind !== command.operationKind) return invalid()
  const refs = ['policyId', 'policyVersion', 'draftId', 'draftRevision', 'decisionId', 'headId', 'headVersion'] as const
  const used = {
    'review.policy.create.v1': ['policyId', 'policyVersion'],
    'review.policy.select.v1': ['policyId', 'policyVersion', 'headId', 'headVersion'],
    'review.draft.publish.v1': ['policyId', 'policyVersion', 'draftId', 'draftRevision', 'headId', 'headVersion'],
    'review.decision.record.v1': ['draftId', 'draftRevision', 'decisionId', 'headId', 'headVersion'],
  }[r.operationKind]
  if (refs.some(key => used.includes(key) === (r[key] === null))) return invalid()
  if (command.operationKind === 'review.policy.create.v1') {
    if (r.policyId !== command.input.policy.policyId || r.policyVersion !== command.input.policy.version) return invalid()
  } else {
    if (r.headVersion !== (BigInt(command.input.expectedHeadVersion) + 1n).toString()) return invalid()
    if (command.operationKind === 'review.policy.select.v1'
      && (r.policyId !== command.input.policyId || r.policyVersion !== command.input.policyVersion)) return invalid()
    if (command.operationKind === 'review.draft.publish.v1'
      && (r.draftId !== command.input.draftId || r.draftRevision !== (BigInt(command.input.expectedDraftRevision) + 1n).toString()
        || r.policyId !== command.input.generation.policyId || r.policyVersion !== command.input.generation.policyVersion)) return invalid()
    if (command.operationKind === 'review.decision.record.v1'
      && (r.draftId !== command.input.draftId || r.draftRevision !== command.input.draftRevision)) return invalid()
  }
  return r
}

export function buildCanonicalReviewLocalContextPath(scope: CanonicalReviewScope,
  review?: { reviewId: string; externalReviewId: string }): string {
  const value = scopeSchema.safeParse(scope)
  if (!value.success || review && (!uuid.safeParse(review.reviewId).success || !review.externalReviewId)) return invalid()
  const query = new URLSearchParams({ marketplace_account_id: String(value.data.marketplaceAccountId), marketplace: value.data.marketplace })
  if (review) { query.set('review_id', review.reviewId); query.set('external_review_id', review.externalReviewId) }
  return `/api/v2/reviews/local/context?${query}`
}

export const canonicalReviewLocalCommandPath = '/api/v2/reviews/local/commands'

const historyAudit = z.object({ schemaVersion: z.literal('review-audit-v1'), ...owner,
  eventId: uuid, aggregateId: uuid, aggregateVersion: version,
  eventKind: z.enum(['draft.published', 'decision.approved', 'decision.rejected']), occurredAt: instant,
  actorKind: z.literal('membership'), actorMembershipId: id, commandId: z.null(), attemptId: z.null(),
  reasonCode: z.null(), policyId: uuid.nullable(), draftId: uuid, decisionId: uuid.nullable(),
  beforeState: z.enum(['draft_current', 'decision_current']).nullable(),
  afterState: z.enum(['draft_current', 'decision_current']),
}).strict().refine(value => value.eventKind === 'draft.published'
  ? value.policyId !== null && value.decisionId === null && value.afterState === 'draft_current'
    && (value.aggregateVersion === '1') === (value.beforeState === null)
  : value.policyId === null && value.decisionId !== null && value.afterState === 'decision_current'
    && value.beforeState !== null && BigInt(value.aggregateVersion) >= 2n)
  .refine(value => value.beforeState !== 'decision_current' || BigInt(value.aggregateVersion) >= 3n)
const historyRequest = z.object({ reviewId: uuid, headId: uuid.optional(), throughVersion: version.optional(),
  afterVersion: expectedVersion.default('0'), limit: z.number().int().min(1).max(200).default(50),
}).strict().refine(value => (value.headId === undefined) === (value.throughVersion === undefined)
  && (value.afterVersion === '0' || value.throughVersion !== undefined))
const historyPage = z.object({ schemaVersion: z.literal('review-local-history-v1'), ...owner,
  reviewId: uuid, headId: uuid.nullable(), throughVersion: expectedVersion,
  events: z.array(historyAudit).max(200), nextAfterVersion: version.nullable(),
}).strict()
export type CanonicalReviewHistoryRequest = z.input<typeof historyRequest>
export type CanonicalReviewHistoryPage = z.infer<typeof historyPage>

export function buildCanonicalReviewHistoryPath(scope: CanonicalReviewScope, request: CanonicalReviewHistoryRequest): string {
  const s = scopeSchema.safeParse(scope), q = historyRequest.safeParse(request)
  if (!s.success || !q.success) return invalid()
  const query = new URLSearchParams({ marketplace_account_id: String(s.data.marketplaceAccountId),
    marketplace: s.data.marketplace, review_id: q.data.reviewId, limit: String(q.data.limit), after_version: q.data.afterVersion })
  if (q.data.headId !== undefined && q.data.throughVersion !== undefined) {
    query.set('head_id', q.data.headId); query.set('through_version', q.data.throughVersion)
  }
  return `/api/v2/reviews/local/history?${query}`
}

export function parseCanonicalReviewHistory(payload: unknown, scope: CanonicalReviewScope,
  request: CanonicalReviewHistoryRequest): CanonicalReviewHistoryPage {
  const s = scopeSchema.safeParse(scope), q = historyRequest.safeParse(request), p = historyPage.safeParse(payload)
  if (!s.success || !q.success || !p.success || !sameScope(p.data, s.data) || p.data.reviewId !== q.data.reviewId) return invalid()
  const page = p.data, expected = q.data
  if ((page.headId === null) !== (page.throughVersion === '0')
    || expected.headId !== undefined && (page.headId !== expected.headId || page.throughVersion !== expected.throughVersion)) return invalid()
  const after = BigInt(expected.afterVersion), through = BigInt(page.throughVersion), limit = BigInt(expected.limit)
  if (through < after) return invalid()
  const count = through - after > limit ? limit : through - after
  if (BigInt(page.events.length) !== count
    || page.nextAfterVersion !== (through - after > limit ? (after + limit).toString() : null)) return invalid()
  const seen = new Set<string>()
  for (const [index, item] of page.events.entries()) {
    if (!sameScope(item, s.data) || item.aggregateId !== page.headId
      || BigInt(item.aggregateVersion) !== after + BigInt(index) + 1n || seen.has(item.eventId)) return invalid()
    seen.add(item.eventId)
  }
  return page
}
