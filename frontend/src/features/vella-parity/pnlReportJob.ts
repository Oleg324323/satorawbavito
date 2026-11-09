export const PNL_JOB_POLL_INTERVAL_MS = 2500
export const PNL_JOB_STALE_AFTER_MS = 30_000

export type PnlReportJobPayload = {
  state?: string | null
  stage?: string | null
  label?: string | null
  percent?: number | null
  queuedAt?: string | null
  error?: string | null
  retryAfterSeconds?: number | null
}

export type PnlReportJobStep = {
  id: 'queue' | 'cashflow' | 'finance' | 'ads' | 'allocation' | 'summary'
  label: string
  detail: string
  status: 'pending' | 'active' | 'done' | 'failed'
}

export type PnlReportJobView = {
  state: 'idle' | 'queued' | 'running' | 'waiting_1c' | 'completed' | 'failed' | 'paused'
  stage: string | null
  label: string
  percent: number | null
  displayPercent: number
  activeStepId: PnlReportJobStep['id']
  steps: PnlReportJobStep[]
  workerDelayed: boolean
  error: string | null
  terminal: boolean
}

const PNL_PROGRESS_STEPS: Array<Omit<PnlReportJobStep, 'status'>> = [
  { id: 'queue', label: 'Очередь', detail: 'Передаём задачу worker' },
  { id: 'cashflow', label: '1С ДДС', detail: 'Ждём операционные расходы' },
  { id: 'finance', label: 'WB финансы', detail: 'Загружаем продажи и выручку' },
  { id: 'ads', label: 'Реклама', detail: 'Подтягиваем WB Ads' },
  { id: 'allocation', label: 'Расчёт SKU', detail: 'Распределяем расходы и маржу' },
  { id: 'summary', label: 'Витрина', detail: 'Собираем таблицу и KPI' },
]

function activePnlStepId(state: PnlReportJobView['state'], stage: string | null): PnlReportJobStep['id'] {
  if (state === 'completed') return 'summary'
  if (state === 'failed') return stage ? activePnlStepId('running', stage) : 'summary'
  if (state === 'queued' || state === 'idle') return 'queue'
  if (state === 'waiting_1c' || stage === 'waiting_1c') return 'cashflow'
  if (!stage) return 'queue'
  if (stage.includes('ads')) return 'ads'
  if (stage.includes('rows') || stage.includes('allocation')) return 'allocation'
  if (stage.includes('summary') || stage.includes('map') || stage.includes('completed')) return 'summary'
  if (stage.includes('source') || stage === 'pnl' || stage.includes('finance') || stage.includes('sales')) return 'finance'
  return 'finance'
}

function buildPnlSteps(
  state: PnlReportJobView['state'],
  activeStepId: PnlReportJobStep['id'],
): PnlReportJobStep[] {
  const activeIndex = PNL_PROGRESS_STEPS.findIndex((step) => step.id === activeStepId)
  const safeActiveIndex = activeIndex >= 0 ? activeIndex : 0
  return PNL_PROGRESS_STEPS.map((step, index) => {
    const status = state === 'completed'
      ? 'done'
      : state === 'failed' && index === safeActiveIndex
        ? 'failed'
        : index < safeActiveIndex
          ? 'done'
          : index === safeActiveIndex
            ? 'active'
            : 'pending'
    return { ...step, status }
  })
}

function fallbackPnlPercent(state: PnlReportJobView['state'], activeStepId: PnlReportJobStep['id']) {
  if (state === 'completed') return 100
  if (state === 'failed') return 100
  return {
    queue: 8,
    cashflow: 20,
    finance: 45,
    ads: 75,
    allocation: 86,
    summary: 97,
  }[activeStepId]
}

export function describePnlReportJob(job: PnlReportJobPayload | null | undefined, nowMs = Date.now()): PnlReportJobView {
  const state = job?.state === 'queued' || job?.state === 'running' || job?.state === 'waiting_1c' || job?.state === 'completed' || job?.state === 'failed' || job?.state === 'paused'
    ? job.state
    : job?.state === 'stale' ? 'paused' : 'idle'
  const stage = job?.stage?.trim() || null
  const queuedAtMs = job?.queuedAt ? Date.parse(job.queuedAt) : Number.NaN
  const workerDelayed = state === 'queued'
    && Number.isFinite(queuedAtMs)
    && nowMs - queuedAtMs >= PNL_JOB_STALE_AFTER_MS
  const percent = typeof job?.percent === 'number' && Number.isFinite(job.percent)
    ? Math.max(0, Math.min(100, Math.round(job.percent)))
    : null
  const fallbackLabel = {
    idle: 'Ожидаем запуск сборки',
    queued: 'В очереди на сборку',
    running: 'Собираем P&L',
    waiting_1c: 'Ждём операционные расходы от 1С',
    completed: 'P&L готов',
    failed: 'Сборка P&L завершилась с ошибкой',
    paused: 'Сбор остановлен; полученная часть сохранена',
  }[state]
  const activeStepId = activePnlStepId(state, stage)
  const steps = buildPnlSteps(state, activeStepId)

  return {
    state,
    stage,
    label: job?.label?.trim() || fallbackLabel,
    percent,
    displayPercent: percent ?? fallbackPnlPercent(state, activeStepId),
    activeStepId,
    steps,
    workerDelayed,
    error: job?.error?.trim() || null,
    terminal: state === 'completed' || state === 'failed' || state === 'paused',
  }
}
