import { describe, expect, it } from 'vitest'
import { describePnlReportJob, PNL_JOB_STALE_AFTER_MS } from './pnlReportJob'

const now = Date.parse('2026-07-13T12:00:00.000Z')

describe('describePnlReportJob', () => {
  it('treats an expired worker heartbeat as paused, not an idle successful job', () => {
    expect(describePnlReportJob({ state: 'stale', stage: 'pnl', label: 'Нет свежего heartbeat' }, now)).toMatchObject({
      state: 'paused', terminal: true, label: 'Нет свежего heartbeat',
    })
  })

  it('describes a queued job that is still waiting normally', () => {
    expect(describePnlReportJob({ state: 'queued', queuedAt: '2026-07-13T11:59:50.000Z' }, now)).toMatchObject({
      state: 'queued',
      stage: null,
      label: 'В очереди на сборку',
      percent: null,
      displayPercent: 8,
      workerDelayed: false,
      error: null,
      terminal: false,
      activeStepId: 'queue',
    })
  })

  it('marks a job that stayed queued too long as worker-delayed', () => {
    expect(describePnlReportJob({ state: 'queued', queuedAt: new Date(now - PNL_JOB_STALE_AFTER_MS).toISOString() }, now).workerDelayed).toBe(true)
  })

  it('uses backend progress for a running job', () => {
    expect(describePnlReportJob({ state: 'running', stage: 'pnl-ads', label: 'Собираем P&L', percent: 75 }, now)).toMatchObject({
      state: 'running',
      stage: 'pnl-ads',
      label: 'Собираем P&L',
      percent: 75,
      terminal: false,
      activeStepId: 'ads',
    })
  })

  it('builds detailed P&L progress steps from the backend stage', () => {
    const view = describePnlReportJob({ state: 'running', stage: 'pnl-rows', percent: 86 }, now)

    expect(view.steps.map((step) => [step.id, step.status])).toEqual([
      ['queue', 'done'],
      ['cashflow', 'done'],
      ['finance', 'done'],
      ['ads', 'done'],
      ['allocation', 'active'],
      ['summary', 'pending'],
    ])
    expect(view.activeStepId).toBe('allocation')
  })

  it('shows a waiting 1C stage as the active cash-flow step', () => {
    const view = describePnlReportJob({ state: 'waiting_1c', percent: 20 }, now)

    expect(view.activeStepId).toBe('cashflow')
    expect(view.steps.find((step) => step.id === 'cashflow')?.status).toBe('active')
  })

  it('keeps waiting for operating expenses while 1C job is active', () => {
    expect(describePnlReportJob({ state: 'waiting_1c', label: 'Ждём операционные расходы от 1С', percent: 20 }, now)).toMatchObject({
      state: 'waiting_1c',
      stage: null,
      label: 'Ждём операционные расходы от 1С',
      percent: 20,
      displayPercent: 20,
      workerDelayed: false,
      error: null,
      terminal: false,
      activeStepId: 'cashflow',
    })
  })

  it('marks completed and failed jobs as terminal', () => {
    expect(describePnlReportJob({ state: 'completed' }, now)).toMatchObject({ state: 'completed', terminal: true })
    expect(describePnlReportJob({ state: 'failed', error: 'WB timeout' }, now)).toMatchObject({
      state: 'failed',
      label: 'Сборка P&L завершилась с ошибкой',
      error: 'WB timeout',
      terminal: true,
    })
  })
})
