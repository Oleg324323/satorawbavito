import { z } from 'zod'

const accountId = z.number().int().positive().max(2147483647)
const count = z.string().regex(/^(0|[1-9][0-9]{0,18})$/).refine(value => value.length < 19 || value.length === 19 && value <= '9223372036854775807')
const edgeWhitespace = /^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]|[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]$/
const exactText = z.string().min(1).max(1024).refine(value => {
  const characters = Array.from(value)
  return characters.length <= 512 && !edgeWhitespace.test(value) && !characters.some(character => {
    const point = character.codePointAt(0)!
    return point < 32 || point === 127 || point >= 0xd800 && point <= 0xdfff
  })
})
const identity = exactText.refine(value => !/^[+-]?(?:\p{Nd}+(?:\.\p{Nd}*)?|\.\p{Nd}+)(?:[eE][+-]?\p{Nd}+)?$/u.test(value) || /^\p{Nd}+$/u.test(value))
// Actual rating scalar: mathematical 0..5, fixed-point scale <=20, no rounding
// or Number conversion. Review row scores have a different domain: 1..5.
const ratingScore = z.string().regex(/^(?:[0-4](?:\.[0-9]{1,20})?|5(?:\.0{1,20})?)$/).nullable()
const sourceTime = z.string().regex(/^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$/).refine(value => {
  const parsed = new Date(value)
  return !value.startsWith('0000-') && Number.isFinite(parsed.getTime()) && parsed.toISOString() === value.replace('Z', '.000Z')
}).nullable()
export const avitoReviewsPreviewScope = z.object({ organizationId: accountId, marketplaceAccountId: accountId, provider: z.literal('avito'),
  externalAccountId: z.string().regex(/^[1-9][0-9]{0,127}$/), sessionKey: z.string().min(1).max(1024) }).strict()
export const avitoReviewsPreviewRequest = z.object({ offset: count }).strict()
const row = z.object({ reviewId: identity, score: z.number().int().min(1).max(5).nullable(), stage: exactText.nullable(),
  usedInScore: z.boolean().nullable(), canAnswer: z.boolean().nullable(), createdAt: sourceTime,
  itemId: identity.nullable(), answerId: identity.nullable(), answerStatus: exactText.nullable(), accountEvidence: z.literal('credential_scope') }).strict()
const response = z.object({ data: z.object({ marketplaceAccountId: accountId, provider: z.literal('avito'), externalAccountId: z.string(), offset: count,
  limit: z.literal(50), coverageState: z.literal('partial'), total: count.nullable(), rating: z.object({ isEnabled: z.boolean().nullable(),
    score: ratingScore, reviewsCount: count.nullable(), reviewsWithScoreCount: count.nullable() }).strict(), rows: z.array(row).max(50) }).strict() }).strict()
export type AvitoReviewsPreviewScope = z.infer<typeof avitoReviewsPreviewScope>
export type AvitoReviewsPreviewRequest = z.infer<typeof avitoReviewsPreviewRequest>
export type AvitoReviewsPreview = z.infer<typeof response>['data']

export function buildAvitoReviewsPreviewPath(scope: AvitoReviewsPreviewScope, request: AvitoReviewsPreviewRequest) {
  const captured = avitoReviewsPreviewScope.parse(scope), query = avitoReviewsPreviewRequest.parse(request)
  return `/api/v2/avito/accounts/${captured.marketplaceAccountId}/reviews/preview?${new URLSearchParams(query)}`
}
export function parseAvitoReviewsPreview(input: unknown, scope: AvitoReviewsPreviewScope, request: AvitoReviewsPreviewRequest): AvitoReviewsPreview {
  avitoReviewsPreviewScope.parse(scope); avitoReviewsPreviewRequest.parse(request)
  const value = response.parse(input).data
  if (value.marketplaceAccountId !== scope.marketplaceAccountId || value.externalAccountId !== scope.externalAccountId
    || value.offset !== request.offset || new Set(value.rows.map(row => row.reviewId)).size !== value.rows.length) throw new Error('AVITO_REVIEWS_PREVIEW_INVALID')
  // Keep unknown/null and exact decimal scale. Never infer totals from page
  // length, default score/flags, add private review text, or grant send policy.
  return value
}
