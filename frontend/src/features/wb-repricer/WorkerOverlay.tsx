import { useCallback, useEffect, useMemo, useState } from 'react'
import { AlertTriangle, CheckCircle2, X } from 'lucide-react'
import { authorizationHeaders } from '@/features/auth/authApi'
import { useAuth } from '@/features/auth/authContext'
import { apiRequest } from '@/lib/api'
import { resetLiveRepricerParityCache } from './liveParityData'
import { PRICE_PENDING_MESSAGE, priceApplyOutcome, priceWasApplied } from './priceApplyOutcome'
import { announceConfirmedPrice } from './priceRefresh'
import './WorkerOverlay.css'

type PendingPriceApproval = {
  approvalId: string
  draftId?: string | null
  jobId?: string | null
  runId?: string | null
  trigger?: string | null
  articleId: string
  nmId?: number | null
  name?: string | null
  skuName?: string | null
  productName?: string | null
  status: string
  applyState?: string | null
  sourceStatus?: string | null
  wbMutationSent?: boolean | null
  priceConfirmed?: boolean | null
  wbUploadId?: number | null
  wbStatus?: number | null
  statusLabel?: string | null
  blockedReasons?: string[]
  notes?: string[]
  rowErrors?: Array<{ errorText?: string | null }>
  error?: string | null
  source?: string | null
  scenario?: string | null
  frontendStrategyId?: string | null
  strategyId?: string | null
  strategyName?: string | null
  oldPriceKopecks?: number | null
  recommendedPriceKopecks?: number | null
  deltaKopecks?: number | null
  explanation?: string | null
  createdAt?: string | null
  updatedAt?: string | null
}

type PriceApprovalDecisionResponse = {
  approval?: PendingPriceApproval | null
  job?: {
    lastApplyResult?: { priceConfirmed?: boolean; wbMutationSent?: boolean; statusLabel?: string; rowErrors?: unknown[] } | null
    state?: string | null
    wbUploadId?: number | null
    wbStatus?: number | null
    blockerIds?: string[]
    notes?: string[]
    rowErrors?: Array<{ errorText?: string | null }>
  } | null
}

type PriceApprovalBulkDecisionResponse = {
  results: Array<PriceApprovalDecisionResponse & {
    approvalId: string
    ok: boolean
    error?: string | null
  }>
}

const STATUS_REFRESH_MS = 15_000

const WORKER_REASON_LABELS: Record<string, string> = {
  auth_required: 'WB токен некорректный или истёк. Обновите токен в кабинете.',
  forbidden_scope: 'В WB токене нет роли записи цен. Нужен доступ Prices and discounts с правом записи.',
  WB_TOKEN_REQUIRED: 'WB токен не сохранён в кабинете организации.',
  'NO_ACCESS:price:send': 'У пользователя нет права price:send для отправки цен.',
  'WB-23': 'Не хватает свежих price/SPP/stock данных для безопасной отправки цены.',
  discovery_not_ready: 'Готовность WB price apply/SPP ещё не подтверждена.',
  margin_preview_blocked: 'Не удалось посчитать маржу: источник маржи заблокирован.',
  pmin_pmax_block: 'Цена не прошла проверку P_MIN/P_MAX.',
  source_stale_or_blocked: 'Критичные источники цены устарели или заблокированы.',
  source_stale_or_blocked_for_validation: 'Нельзя проверить цену: источник СПП/цены устарел или заблокирован.',
  spp_missing: 'Нет СПП по SKU, поэтому нельзя проверить цену покупателя и безопасно отправить цену в WB.',
  stock_oos: 'Остаток WB равен нулю, автоматическое изменение цены заблокировано.',
  cogs_missing: 'Не указана себестоимость SKU.',
  commission_missing: 'Не указана комиссия WB.',
  logistics_missing: 'Не указана логистика.',
  buyout_missing: 'Нет процента выкупа.',
  stock_missing: 'Нет актуального остатка WB.',
  wb_rate_limited: 'WB ограничил частоту запросов на изменение цен. Worker повторит позже.',
  wb_status_rate_limited: 'WB принял заявку на изменение цены, но временно ограничил проверку статуса. Цена может примениться после обработки WB.',
  rate_limited: 'WB ограничил частоту запросов. Нужно дождаться следующего окна.',
  manual_mode: 'Автоисполнение выключено: control plane стоит в ручном режиме.',
  warmup: 'SKU в прогреве, worker не меняет цену.',
  automation_disabled: 'Автоматизация выключена для этого SKU.',
  no_nm_id: 'У SKU нет nmId, цену нельзя отправить в WB.',
  no_strategy: 'Для SKU не назначена стратегия.',
  no_price_change: 'Стратегия не нашла изменения цены.',
  liquidation_not_due: 'Шаг ликвидации ещё не наступил.',
  night_median_collecting: 'Ночная медиана собирает корзины; цены ночью не меняются.',
  night_mode_disabled: 'Ночной режим выключен.',
  sku_not_found: 'SKU не найден в текущей WB-выборке.',
  wb_request_failed: 'WB отклонил запрос изменения цены.',
  wb_server_error: 'WB вернул серверную ошибку, retry возможен позже.',
  wb_sync_running: 'Идёт синхронизация WB-данных, расчёт цен пропущен до следующего тика.',
  execute_interval_not_due: 'Интервал стратегии ещё не наступил.',
  scheduler_disabled: 'Планировщик репрайсера выключен.',
  no_strategy_assignments: 'Нет SKU с назначенной стратегией.',
  no_cabinet_wb_token: 'В кабинете организации не найден WB token.',
}

const WORKER_STRATEGY_LABELS: Record<string, string> = {
  night_price_mode: 'ночная медиана',
  illiquid: 'ликвидация',
  baskets_orders: 'корзины+заказы',
  baskets_orders_4599: 'корзины+заказы',
  metric_dynamics: 'динамика выручки',
  revenue_dynamics_4600: 'динамика выручки',
}

function workerStrategyLabel(item: Pick<PendingPriceApproval, 'frontendStrategyId' | 'strategyId'>) {
  const id = item.frontendStrategyId || item.strategyId || ''
  return WORKER_STRATEGY_LABELS[id] ?? (id || 'strategy')
}

function approvalProductName(item: PendingPriceApproval) {
  return item.productName || item.skuName || item.name || item.articleId || 'Товар'
}

function approvalProductMeta(item: PendingPriceApproval) {
  return [
    item.articleId ? `Артикул ${item.articleId}` : null,
    item.nmId ? `WB ${item.nmId}` : null,
    item.strategyName || workerStrategyLabel(item),
  ].filter(Boolean).join(' · ')
}

function approvalExplanation(item: PendingPriceApproval) {
  const explanation = item.explanation?.trim()
  if (explanation && !/approval_required|draft|worker|applyState/i.test(explanation)) {
    return explanation
  }
  const strategy = item.strategyName || workerStrategyLabel(item)
  return `Стратегия «${strategy}» пересчитала цену по текущим данным WB. Автоматическая отправка выключена, поэтому нужно решение менеджера.`
}

function describeWorkerReason(code: string | null | undefined) {
  if (!code) return ''
  return WORKER_REASON_LABELS[code] ?? code
}

function formatKopecks(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return '—'
  return `${Math.round(value / 100).toLocaleString('ru-RU')} ₽`
}

function describePriceApprovalFailure(response: PriceApprovalDecisionResponse) {
  const approval = response.approval ?? null
  const job = response.job ?? null
  const state = job?.state || approval?.applyState || approval?.status || 'unknown'
  if (priceApplyOutcome(response) === 'pending') return PRICE_PENDING_MESSAGE
  const blockers = job?.blockerIds?.length ? job.blockerIds : approval?.blockedReasons
  const notes = job?.notes?.length ? job.notes : approval?.notes
  const rowErrors = job?.rowErrors?.length ? job.rowErrors : approval?.rowErrors
  const details = [
    blockers?.filter(Boolean).map(describeWorkerReason).join(', '),
    notes?.filter(Boolean).join('; '),
    rowErrors?.map((row) => row.errorText).filter(Boolean).join('; '),
    approval?.error,
    job?.wbStatus || approval?.wbStatus ? `WB status ${job?.wbStatus ?? approval?.wbStatus}` : null,
    job?.wbUploadId || approval?.wbUploadId ? `uploadID ${job?.wbUploadId ?? approval?.wbUploadId}` : null,
  ].filter(Boolean)
  return `WB не принял изменение цены: ${state}${details.length ? ` · ${details.join(' · ')}` : ''}`
}

export function WorkerOverlay() {
  const { accessToken } = useAuth()
  const [status, setStatus] = useState<{ pendingApprovals: PendingPriceApproval[] } | null>(null)
  const [approvalBusy, setApprovalBusy] = useState<string | null>(null)
  const [approvalError, setApprovalError] = useState<string | null>(null)
  const [failedApproval, setFailedApproval] = useState<PendingPriceApproval | null>(null)
  const headers = useMemo(() => authorizationHeaders(accessToken ?? ''), [accessToken])

  const loadStatus = useCallback(() => apiRequest<{ pendingApprovals: PendingPriceApproval[] }>(
    '/api/v1/wb-repricer/worker/status?limit=8',
    { headers, cache: 'no-store' },
  )
    .then((payload) => {
      if (payload) setStatus(payload)
    })
    .catch(() => {
      // The next poll retries; existing approvals remain available.
    }), [headers])

  const closePendingApproval = useCallback((approvalId: string) => {
    setStatus((current) => current
      ? {
          ...current,
          pendingApprovals: (current.pendingApprovals ?? []).filter((item) => item.approvalId !== approvalId),
        }
      : current)
    setApprovalError(null)
    setFailedApproval(null)
  }, [])

  const decidePendingApproval = useCallback(async (approvalId: string, decision: 'approve' | 'reject') => {
    setApprovalBusy(`${decision}:${approvalId}`)
    setApprovalError(null)
    setFailedApproval(null)
    try {
      const response = await apiRequest<PriceApprovalDecisionResponse>(`/api/v1/wb-repricer/price-approvals/${encodeURIComponent(approvalId)}/${decision}`, {
        method: 'POST',
        headers,
        body: JSON.stringify({
          reason: decision === 'approve' ? 'manager confirmed worker price change' : 'manager rejected worker price change',
        }),
        cache: 'no-store',
      })
      if (decision === 'approve') {
        announceConfirmedPrice(response ?? {})
        if (priceApplyOutcome(response ?? {}) === 'pending') {
          // WB is still processing the request. Do not reopen a blocking
          // receipt dialog, resubmit, or publish an unconfirmed price.
          closePendingApproval(approvalId)
          void loadStatus()
          return
        }
        if (!priceWasApplied(response ?? {})) {
          setFailedApproval(response?.approval ?? null)
          setApprovalError(describePriceApprovalFailure(response ?? {}))
          void loadStatus()
          return
        }
      }
      closePendingApproval(approvalId)
      resetLiveRepricerParityCache(accessToken)
      window.dispatchEvent(new CustomEvent('vella:products-rows-updated'))
      window.dispatchEvent(new CustomEvent('vella:products-kpi-updated'))
      if (decision === 'approve') void loadStatus()
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : 'не удалось обработать подтверждение цены'
      if (message.includes('PENDING_PRICE_APPROVAL_ALREADY_CLOSED')) {
        closePendingApproval(approvalId)
        void loadStatus()
        return
      }
      setApprovalError(message)
    } finally {
      setApprovalBusy(null)
    }
  }, [accessToken, closePendingApproval, headers, loadStatus])

  useEffect(() => {
    void loadStatus()
    const intervalId = window.setInterval(() => void loadStatus(), STATUS_REFRESH_MS)
    return () => window.clearInterval(intervalId)
  }, [loadStatus])

  const pendingApprovals = status?.pendingApprovals ?? []
  // Only an unsent recommendation needs a manager's decision. In-flight
  // receipts stay in worker/history status, never in an automatic popup.
  const approvalsNeedingDecision = pendingApprovals.filter((item) => item.status === 'pending'
    && priceApplyOutcome({ approval: item }) !== 'pending')
  const currentApproval = approvalsNeedingDecision[0] ?? null
  const approvalModal = currentApproval ?? (approvalError && failedApproval
    && priceApplyOutcome({ approval: failedApproval }) !== 'pending' ? failedApproval : null)
  const approvalCanDecide = approvalModal?.status === 'pending'
  const approvalIsPending = approvalModal ? priceApplyOutcome({ approval: approvalModal }) === 'pending' : false
  const pendingApprovalCount = approvalsNeedingDecision.length
  const decideAllPendingApprovals = async (decision: 'approve' | 'reject') => {
    const targets = approvalsNeedingDecision
    if (!targets.length) return
    setApprovalBusy(`${decision}:all`)
    setApprovalError(null)
    setFailedApproval(null)
    try {
      const response = await apiRequest<PriceApprovalBulkDecisionResponse>('/api/v1/wb-repricer/price-approvals/bulk', {
        method: 'POST',
        headers,
        body: JSON.stringify({
          approvalIds: targets.map((item) => item.approvalId),
          decision,
          reason: decision === 'approve' ? 'manager confirmed all worker price changes' : 'manager rejected all worker price changes',
        }),
        cache: 'no-store',
      })
      const results = response?.results ?? []
      if (decision === 'approve') results.forEach(announceConfirmedPrice)
      if (!results.length) throw new Error('сервер не вернул результат обработки цен')
      const succeededIds = new Set(results.filter((item) => decision === 'reject' ? item.ok : priceWasApplied(item)).map((item) => item.approvalId))
      const inflightIds = new Set(results.filter((item) => decision === 'approve'
        && priceApplyOutcome(item) === 'pending').map((item) => item.approvalId))
      setStatus((current) => current
        ? {
            ...current,
            pendingApprovals: (current.pendingApprovals ?? []).filter((item) => !succeededIds.has(item.approvalId) && !inflightIds.has(item.approvalId)),
          }
        : current)
      if (succeededIds.size) {
        resetLiveRepricerParityCache(accessToken)
        window.dispatchEvent(new CustomEvent('vella:products-rows-updated'))
        window.dispatchEvent(new CustomEvent('vella:products-kpi-updated'))
      }
      const failed = results.find((item) => !item.ok)
      if (failed) {
        setFailedApproval(failed.approval ?? targets.find((item) => item.approvalId === failed.approvalId) ?? null)
        setApprovalError(failed.approval || failed.job ? describePriceApprovalFailure(failed) : failed.error || 'не удалось обработать часть цен')
      }
      if (decision === 'approve' || failed) void loadStatus()
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : 'не удалось обработать подтверждения цен'
      setApprovalError(message.includes('PENDING_PRICE_APPROVAL_ALREADY_CLOSED') ? 'Часть цен уже обработана. Обновляем список.' : message)
      void loadStatus()
    } finally {
      setApprovalBusy(null)
    }
  }
  return approvalModal ? (
    <div className="worker-approval-backdrop" role="presentation">
      <section className="worker-approval-modal" role="dialog" aria-modal="true" aria-labelledby="worker-approval-title">
        <div className="worker-approval-head">
          <div className="worker-approval-mark"><AlertTriangle size={20} /></div>
          <div>
            <h2 id="worker-approval-title">{approvalCanDecide ? 'Нужно подтвердить изменение цены' : approvalIsPending ? 'Проверяется применение цены' : 'Цена не подтверждена'}</h2>
            <p>
              {approvalCanDecide
                ? 'Репрайсер предлагает новую цену, но автоматическая отправка в WB выключена.'
                : approvalIsPending ? PRICE_PENDING_MESSAGE : 'Проверьте причину ниже. Последняя подтверждённая цена сохранена.'}
            </p>
          </div>
        </div>
        <div className="worker-approval-product">
          <b>{approvalProductName(approvalModal)}</b>
          <span>{approvalProductMeta(approvalModal)}</span>
        </div>
        <div className="worker-approval-price">
          <span><small>Сейчас</small>{formatKopecks(approvalModal.oldPriceKopecks)}</span>
          <b>→</b>
          <strong><small>Предлагаем</small>{formatKopecks(approvalModal.recommendedPriceKopecks)}</strong>
          {approvalModal.deltaKopecks ? <em>{approvalModal.deltaKopecks > 0 ? '+' : ''}{formatKopecks(approvalModal.deltaKopecks)}</em> : null}
        </div>
        <div className="worker-approval-reason">
          <b>Что происходит</b>
          <span>{approvalExplanation(approvalModal)}</span>
          {approvalCanDecide ? (
            <small>
              Нажмите «Отправить цену в WB», чтобы применить рекомендацию. Нажмите «Не менять цену», если цену нужно оставить как есть.
            </small>
          ) : null}
        </div>
        {approvalError ? <div className="worker-approval-error">{approvalError}</div> : null}
        <div className="worker-approval-actions">
          {approvalIsPending && approvalModal.jobId ? <button type="button" className="worker-approval-btn ghost"
            disabled={approvalBusy !== null} onClick={() => {
              setApprovalBusy('reconcile')
              void apiRequest<{ data: NonNullable<PriceApprovalDecisionResponse['job']> }>(`/api/v1/wb-repricer/jobs/${encodeURIComponent(approvalModal.jobId!)}/retry`, {
                method: 'POST', headers, body: JSON.stringify({ pollMaxAttempts: 1 }), cache: 'no-store',
              }).then((response) => {
                announceConfirmedPrice({ job: response?.data })
                if (priceWasApplied({ job: response?.data })) {
                  resetLiveRepricerParityCache(accessToken)
                  window.dispatchEvent(new CustomEvent('vella:products-rows-updated'))
                  window.dispatchEvent(new CustomEvent('vella:products-kpi-updated'))
                }
                return loadStatus()
              }).catch((error: unknown) => setApprovalError(error instanceof Error ? error.message : 'Проверка статуса недоступна'))
                .finally(() => setApprovalBusy(null))
            }}>Проверить применение</button> : null}
          {approvalCanDecide ? (
            <>
              {pendingApprovalCount > 1 ? (
                <>
                  <button
                    type="button"
                    className="worker-approval-btn ghost"
                    onClick={() => void decideAllPendingApprovals('reject')}
                    disabled={approvalBusy !== null}
                  >
                    <X size={15} /> Отклонить все цены {pendingApprovalCount}
                  </button>
                  <button
                    type="button"
                    className="worker-approval-btn primary"
                    onClick={() => void decideAllPendingApprovals('approve')}
                    disabled={approvalBusy !== null}
                  >
                    <CheckCircle2 size={15} /> {approvalBusy === 'approve:all' ? 'Принимаем все…' : `Принять все цены ${pendingApprovalCount}`}
                  </button>
                </>
              ) : null}
              <button
                type="button"
                className="worker-approval-btn ghost"
                onClick={() => void decidePendingApproval(approvalModal.approvalId, 'reject')}
                disabled={approvalBusy !== null}
              >
                <X size={15} /> Не менять цену
              </button>
              <button
                type="button"
                className="worker-approval-btn primary"
                onClick={() => void decidePendingApproval(approvalModal.approvalId, 'approve')}
                disabled={approvalBusy !== null}
              >
                <CheckCircle2 size={15} /> {approvalBusy?.startsWith('approve:') ? 'Отправляем…' : 'Отправить цену в WB'}
              </button>
            </>
          ) : (
            <button
              type="button"
              className="worker-approval-btn ghost"
              onClick={() => {
                setApprovalError(null)
                setFailedApproval(null)
              }}
            >
              <X size={15} /> Закрыть
            </button>
          )}
        </div>
      </section>
    </div>
  ) : null
}
