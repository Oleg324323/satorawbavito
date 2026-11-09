import { describe, expect, it } from 'vitest'

import { REPORT_LOADING_BUDGET_MS, reportStatusPollWaitMs, shouldPollBackgroundReportJob } from './backgroundReportPolling'

describe('background report polling', () => {
  it('polls only active jobs up to the bounded twenty minute allowance', () => {
    expect(shouldPollBackgroundReportJob({ state: 'queued' }, 0)).toBe(true)
    expect(shouldPollBackgroundReportJob({ state: 'running' }, 150)).toBe(true)
    expect(shouldPollBackgroundReportJob({ state: 'running' }, 599)).toBe(true)
    expect(shouldPollBackgroundReportJob({ state: 'completed' }, 0)).toBe(false)
    expect(shouldPollBackgroundReportJob({ state: 'waiting_1c' }, 0)).toBe(false)
    expect(shouldPollBackgroundReportJob({ state: 'running' }, 600)).toBe(false)
  })

  it('backs off status reads without extending the total loading budget', () => {
    expect(REPORT_LOADING_BUDGET_MS).toBe(1_200_000)
    expect(reportStatusPollWaitMs({ state: 'running' }, 0)).toBe(2000)
    expect(reportStatusPollWaitMs({ state: 'running' }, 10)).toBe(4000)
    expect(reportStatusPollWaitMs({ state: 'running' }, 100)).toBe(10_000)
    expect(reportStatusPollWaitMs({ stage: 'rate_limit', retryAfterSeconds: 126 }, 0)).toBe(30_000)
    expect(reportStatusPollWaitMs({ stage: 'rate_limit', retryAfterSeconds: Number.NaN }, 0)).toBe(2000)
  })
})
