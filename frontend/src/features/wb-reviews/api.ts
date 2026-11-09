import { apiData, apiRequest } from '@/lib/api'
import { authorizationHeaders } from '@/features/auth/authApi'
import { canonicalReviewsEnabled } from './canonicalReviewDetail'

function requireLegacyReviewWriter() {
  if (canonicalReviewsEnabled) throw new Error('Используйте локальную каноническую карточку отзыва. Прежняя запись и отправка отключены.')
}

export type WbReviewFeedback = {
  feedbackId: string
  nmId: number
  imtId?: number | null
  brandName: string
  productName: string
  createdDate: string
  text: string
  pros: string
  cons: string
  answerText?: string | null
  isAnswered: boolean
  rating?: number | null
  syncedAt: string
  sourceStatus: 'fresh' | 'partial' | 'blocked'
  latestDraft?: {
    draftId: string
    rating: number
    brandVoiceId: string
    templateId?: string | null
    approvalState: 'not_required' | 'required' | 'approved' | 'rejected' | 'expired'
    sendState: 'draft_only' | 'ready_to_send' | 'sent' | 'blocked'
    generatedText: string
    moderationState: 'clean' | 'manual_review' | 'blocked'
    moderationReasons: string[]
    updatedAt: string
  } | null
}

export type WbReviewDraft = {
  draftId: string
  feedbackId: string
  rating: number
  brandVoiceId: string
  templateId?: string | null
  approvalState: 'not_required' | 'required' | 'approved' | 'rejected' | 'expired'
  approvalActorId?: string | null
  approvedAt?: string | null
  externalSendAllowed: boolean
  sendState: 'draft_only' | 'ready_to_send' | 'sent' | 'blocked'
  generatedText: string
  moderation: {
    state: 'clean' | 'manual_review' | 'blocked'
    reasons: string[]
    matchedStopTopicIds: number[]
    matchedRuleIds: number[]
  }
  promptTraceId?: string | null
  cacheMetrics?: { cachedTokens: number; hitRatePct: number } | null
  createdByActorId: string
  createdAt: string
  updatedAt: string
}

export type WbReviewDraftBatchGenerateItem = {
  feedbackId: string
  brandVoiceId: string
  ratingOverride?: number | null
  promptInstruction?: string
}

export type WbReviewDraftBatchGenerateResponse = {
  requestedCount: number
  generatedCount: number
  failedCount: number
  drafts: WbReviewDraft[]
  errors: Array<{ feedbackId: string; message: string }>
}

export type WbReviewSendJob = {
  sendJobId: string
  draftId: string
  feedbackId: string
  status: 'queued' | 'sent' | 'blocked' | 'failed'
  answerText: string
  externalRequestPath?: string | null
  resultMessage: string
  requestedByActorId: string
  requestedAt: string
  completedAt?: string | null
}

export type WbReviewSyncTokenType = 'personal' | 'service' | 'base'
export type WbReviewAutomationSettings = {
  automationMode: 'draft-first' | 'auto-safe' | 'paused'
  replyDelayMinutes: number
  threeStarAction: 'manual' | 'draft' | 'auto-low'
  lowStarsAction: 'alert-block' | 'manual' | 'draft'
  anomieTone: string
  blessTone: string
  avitoTone: string
  quoteMode: 'approved-search' | 'approved-only' | 'off'
  validateName: boolean
  recommendations30d: boolean
  quotesApproval: boolean
  candidateSearch: boolean
}

export type WbReviewTemplate = {
  templateId: string
  brandVoiceId: string
  title: string
  bodyTemplate: string
  ratingFrom: number
  ratingTo: number
  keywords: string[]
  keywordMode: 'any' | 'all'
  isActive: boolean
  createdByActorId: string
  createdAt: string
  updatedAt: string
}

export type WbReviewStopTopic = {
  stopTopicId: number
  brandVoiceId?: string | null
  phrase: string
  matchType: 'contains' | 'exact'
  action: 'manual_review' | 'block_send'
  isActive: boolean
  createdByActorId: string
  createdAt: string
  updatedAt: string
}

export type WbReviewModerationRule = {
  ruleId: number
  brandVoiceId?: string | null
  name: string
  conditionType: 'low_rating' | 'contains_stop_topic' | 'already_answered' | 'contains_keyword' | 'empty_text'
  action: 'allow' | 'manual_review' | 'block_send' | 'block_draft'
  thresholdInt?: number | null
  keywords: string[]
  isActive: boolean
  createdByActorId: string
  createdAt: string
  updatedAt: string
}

export type WbReviewSettingsBundle = {
  templates: WbReviewTemplate[]
  stopTopics: WbReviewStopTopic[]
  moderationRules: WbReviewModerationRule[]
}

export type WbReviewSyncSettings = {
  organizationId: number
  enabled: boolean
  intervalMinutes: number
  tokenType: WbReviewSyncTokenType
  unansweredOnly: boolean
  take: number
  lookbackDays: number
  aiPrompt: string
  reviewSettings: WbReviewAutomationSettings
  minIntervalMinutes: number
  updatedByActorId?: string | null
  createdAt: string
  updatedAt: string
}

export type WbReviewSyncSettingsPayload = Pick<
  WbReviewSyncSettings,
  'enabled' | 'intervalMinutes' | 'tokenType' | 'unansweredOnly' | 'take' | 'lookbackDays' | 'aiPrompt'
> & { reviewSettings?: WbReviewAutomationSettings }

export type WbReviewSyncStatus = {
  organizationId: number
  status: 'idle' | 'running' | 'completed' | 'failed' | 'skipped'
  enabled: boolean
  intervalMinutes: number
  nextRunAt?: string | null
  lastRunAt?: string | null
  lastCompletedAt?: string | null
  lastSyncedCount: number
  lastSourceStatus?: 'fresh' | 'partial' | 'blocked' | null
  lastError?: string | null
}

export type WbReviewPageRisk = 'low' | 'medium' | 'high'
export type WbReviewPageStatus =
  | 'new'
  | 'generating'
  | 'pending_review'
  | 'scheduled'
  | 'sent_pending_verify'
  | 'sent'
  | 'blocked'
  | 'rejected'
  | 'error'

export type WbReviewPageRow = {
  id: string
  draftId?: string
  brand: string
  rating: number
  topic: string
  risk: WbReviewPageRisk
  status: WbReviewPageStatus
  buyer: string
  sku: string
  nm: string
  product: string
  size: string
  age: string
  media: string
  text: string
  reason: string
  draft: string
  audit: string[]
}

type PaginatedEnvelope<T> = {
  items: T[]
  total: number
  limit: number
  offset: number
}

function auth(accessToken: string) {
  return authorizationHeaders(accessToken)
}

export async function listWbReviewFeedbacks(accessToken: string, options: { limit?: number; offset?: number; isAnswered?: boolean } = {}) {
  const limit = options.limit ?? 100
  const offset = options.offset ?? 0
  const params = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
  })
  if (options.isAnswered !== undefined) params.set('isAnswered', String(options.isAnswered))
  return apiRequest<PaginatedEnvelope<WbReviewFeedback>>(`/api/v1/wb-reviews/feedbacks?${params.toString()}`, {
    headers: auth(accessToken),
    cache: 'no-store',
  })
}

export async function syncWbReviewFeedbacks(accessToken: string) {
  return apiData('/api/v1/wb-reviews/sync', {
    method: 'POST',
    headers: auth(accessToken),
    body: JSON.stringify({ scenario: 'complete', take: 500, skip: 0, order: 'dateDesc' }),
  })
}

export async function getWbReviewSyncSettings(accessToken: string) {
  return apiData<WbReviewSyncSettings>('/api/v1/wb-reviews/sync-settings', {
    headers: auth(accessToken),
    cache: 'no-store',
  })
}

export async function updateWbReviewSyncSettings(accessToken: string, payload: WbReviewSyncSettingsPayload) {
  return apiData<WbReviewSyncSettings>('/api/v1/wb-reviews/sync-settings', {
    method: 'PUT',
    headers: auth(accessToken),
    body: JSON.stringify(payload),
  })
}

export async function getWbReviewSettingsBundle(accessToken: string) {
  return apiData<WbReviewSettingsBundle>('/api/v1/wb-reviews/settings', {
    headers: auth(accessToken),
    cache: 'no-store',
  })
}

export async function createWbReviewStopTopic(accessToken: string, payload: Omit<WbReviewStopTopic, 'stopTopicId' | 'createdByActorId' | 'createdAt' | 'updatedAt'>) {
  return apiData<WbReviewStopTopic>('/api/v1/wb-reviews/stop-topics', {
    method: 'POST',
    headers: auth(accessToken),
    body: JSON.stringify(payload),
  })
}

export async function updateWbReviewStopTopic(accessToken: string, stopTopicId: number, payload: Omit<WbReviewStopTopic, 'stopTopicId' | 'createdByActorId' | 'createdAt' | 'updatedAt'>) {
  return apiData<WbReviewStopTopic>(`/api/v1/wb-reviews/stop-topics/${stopTopicId}`, {
    method: 'PUT',
    headers: auth(accessToken),
    body: JSON.stringify(payload),
  })
}

export async function createWbReviewModerationRule(accessToken: string, payload: Omit<WbReviewModerationRule, 'ruleId' | 'createdByActorId' | 'createdAt' | 'updatedAt'>) {
  return apiData<WbReviewModerationRule>('/api/v1/wb-reviews/moderation-rules', {
    method: 'POST',
    headers: auth(accessToken),
    body: JSON.stringify(payload),
  })
}

export async function updateWbReviewModerationRule(accessToken: string, ruleId: number, payload: Omit<WbReviewModerationRule, 'ruleId' | 'createdByActorId' | 'createdAt' | 'updatedAt'>) {
  return apiData<WbReviewModerationRule>(`/api/v1/wb-reviews/moderation-rules/${ruleId}`, {
    method: 'PUT',
    headers: auth(accessToken),
    body: JSON.stringify(payload),
  })
}

export async function createWbReviewTemplate(accessToken: string, payload: Omit<WbReviewTemplate, 'templateId' | 'createdByActorId' | 'createdAt' | 'updatedAt'>) {
  return apiData<WbReviewTemplate>('/api/v1/wb-reviews/templates', {
    method: 'POST',
    headers: auth(accessToken),
    body: JSON.stringify(payload),
  })
}

export async function updateWbReviewTemplate(accessToken: string, templateId: string, payload: Omit<WbReviewTemplate, 'templateId' | 'createdByActorId' | 'createdAt' | 'updatedAt'>) {
  return apiData<WbReviewTemplate>(`/api/v1/wb-reviews/templates/${encodeURIComponent(templateId)}`, {
    method: 'PUT',
    headers: auth(accessToken),
    body: JSON.stringify(payload),
  })
}

export async function getWbReviewSyncStatus(accessToken: string) {
  return apiData<WbReviewSyncStatus>('/api/v1/wb-reviews/sync-status', {
    headers: auth(accessToken),
    cache: 'no-store',
  })
}

export async function generateWbReviewDraft(accessToken: string, feedbackId: string, regenerate = true, brandVoiceId = 'wb-default', promptInstruction?: string) {
  requireLegacyReviewWriter()
  return apiData<WbReviewDraft>('/api/v1/wb-reviews/drafts/generate', {
    method: 'POST',
    headers: auth(accessToken),
    body: JSON.stringify({ feedbackId, brandVoiceId, regenerate, ...(promptInstruction ? { promptInstruction } : {}) }),
  })
}

export async function generateWbReviewDraftsBatch(accessToken: string, items: WbReviewDraftBatchGenerateItem[], regenerate = true) {
  requireLegacyReviewWriter()
  return apiData<WbReviewDraftBatchGenerateResponse>('/api/v1/wb-reviews/drafts/generate-batch', {
    method: 'POST',
    headers: auth(accessToken),
    body: JSON.stringify({ items, regenerate }),
  })
}

export async function approveWbReviewDraft(accessToken: string, draftId: string, reason = 'approved from reviews page') {
  requireLegacyReviewWriter()
  return apiData<WbReviewDraft>(`/api/v1/wb-reviews/drafts/${encodeURIComponent(draftId)}/approve`, {
    method: 'POST',
    headers: auth(accessToken),
    body: JSON.stringify({ approvalRef: 'frontend-approval', reason }),
  })
}

export async function rejectWbReviewDraft(accessToken: string, draftId: string, reason = 'rejected from reviews page') {
  requireLegacyReviewWriter()
  return apiData<WbReviewDraft>(`/api/v1/wb-reviews/drafts/${encodeURIComponent(draftId)}/reject`, {
    method: 'POST',
    headers: auth(accessToken),
    body: JSON.stringify({ reason }),
  })
}

export async function requestWbReviewSend(accessToken: string, draftId: string) {
  requireLegacyReviewWriter()
  return apiData<WbReviewSendJob>(`/api/v1/wb-reviews/drafts/${encodeURIComponent(draftId)}/send`, {
    method: 'POST',
    headers: auth(accessToken),
    body: JSON.stringify({ dryRun: true, reason: 'frontend draft-only smoke' }),
  })
}

function ageLabel(iso: string) {
  const created = new Date(iso).getTime()
  const diffMs = Date.now() - created
  if (!Number.isFinite(created) || diffMs < 0) return 'только что'
  const minutes = Math.max(1, Math.round(diffMs / 60_000))
  if (minutes < 60) return `${minutes} мин`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours} ч`
  return `${Math.round(hours / 24)} дн`
}

function reviewTopic(feedback: WbReviewFeedback) {
  const text = `${feedback.text} ${feedback.pros} ${feedback.cons}`.toLowerCase()
  if (!text.trim()) return 'без текста'
  if (text.includes('цен') || text.includes('дорог')) return 'цена'
  if (text.includes('достав') || text.includes('упаков')) return 'доставка'
  if (text.includes('размер') || text.includes('посад')) return 'размер'
  if (text.includes('печать')) return 'печать'
  if (text.includes('принт')) return 'принт'
  if (text.includes('качест') || text.includes('ткан')) return 'качество'
  return 'отзыв'
}

function reviewRisk(feedback: WbReviewFeedback): WbReviewPageRisk {
  const topic = reviewTopic(feedback)
  const rating = feedback.rating ?? 5
  if (rating <= 2 || ['цена', 'печать', 'доставка'].includes(topic)) return 'high'
  if (rating <= 4 || ['размер', 'качество'].includes(topic)) return 'medium'
  return 'low'
}

export function mapWbFeedbackToVellaReview(feedback: WbReviewFeedback): WbReviewPageRow {
  const rating = feedback.rating ?? 5
  const topic = reviewTopic(feedback)
  const risk = reviewRisk(feedback)
  const draftStatus: WbReviewPageStatus | null = feedback.latestDraft
    ? feedback.latestDraft.approvalState === 'rejected'
      ? 'rejected'
      : feedback.latestDraft.sendState === 'blocked'
        ? 'blocked'
        : feedback.latestDraft.sendState === 'sent'
          ? 'sent'
          : feedback.latestDraft.sendState === 'ready_to_send'
            ? 'scheduled'
            : 'pending_review'
    : null
  const status: WbReviewPageStatus = draftStatus ?? (feedback.isAnswered ? 'sent' : 'new')
  const text = [feedback.text, feedback.pros && `Плюсы: ${feedback.pros}`, feedback.cons && `Минусы: ${feedback.cons}`]
    .filter(Boolean)
    .join('\n')

  return {
    id: feedback.feedbackId,
    brand: feedback.brandName,
    draftId: feedback.latestDraft?.draftId,
    rating: feedback.latestDraft?.rating ?? rating,
    topic,
    risk: feedback.latestDraft?.moderationState === 'blocked' ? 'high' : feedback.latestDraft?.moderationState === 'manual_review' ? 'medium' : risk,
    status,
    buyer: 'Покупатель WB',
    sku: String(feedback.nmId),
    nm: String(feedback.nmId),
    product: feedback.productName,
    size: 'WB',
    age: ageLabel(feedback.createdDate),
    media: text.trim() ? 'без фото' : 'без текста',
    text,
    reason: feedback.latestDraft
      ? feedback.latestDraft.sendState === 'draft_only'
        ? 'Черновик готов; отправка в WB отключена'
        : feedback.latestDraft.moderationReasons.join(' · ') || 'Черновик загружен из backend'
      : feedback.isAnswered ? 'Ответ уже есть в WB' : risk === 'low' ? 'Свежий отзыв без ответа' : 'Нужен черновик и проверка правил',
    draft: feedback.latestDraft?.generatedText || feedback.answerText || '',
    audit: [
      ...(feedback.latestDraft ? [`AI черновик загружен из backend · ${new Date(feedback.latestDraft.updatedAt).toLocaleString('ru-RU')}`] : []),
      `Отзыв загружен из WB API · ${new Date(feedback.syncedAt).toLocaleString('ru-RU')}`,
    ],
  }
}

export function mergeWbDraftIntoVellaReview(review: WbReviewPageRow, draft: WbReviewDraft): WbReviewPageRow {
  const status: WbReviewPageStatus = draft.approvalState === 'rejected'
    ? 'rejected'
    : draft.sendState === 'blocked'
      ? 'blocked'
      : draft.sendState === 'sent'
        ? 'sent'
        : draft.sendState === 'ready_to_send'
          ? 'scheduled'
          : 'pending_review'
  const reason = draft.sendState === 'draft_only'
    ? 'Черновик готов; отправка в WB отключена'
    : draft.moderation.reasons.join(' · ') || review.reason

  return {
    ...review,
    draftId: draft.draftId,
    rating: draft.rating,
    risk: draft.moderation.state === 'blocked' ? 'high' : draft.moderation.state === 'manual_review' ? 'medium' : review.risk,
    status,
    draft: draft.generatedText,
    reason,
    audit: [`AI подготовил черновик · ${new Date(draft.updatedAt).toLocaleString('ru-RU')}`, ...review.audit],
  }
}
