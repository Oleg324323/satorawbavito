// Large historical ranges may need hundreds of rate-limited campaign reads.
// Keep polling bounded, but do not stop before a normal collection can finish.
export const BACKGROUND_REPORT_MAX_POLL_ATTEMPTS = 600

type BackgroundReportJob = {
  state?: string | null
  stage?: string | null
  retryAfterSeconds?: number | null
}

export const REPORT_LOADING_BUDGET_MS = 20 * 60_000

// These are status-read intervals, not marketplace request limits. Keep early
// results responsive, then reduce polling while the existing worker continues.
export function reportStatusPollWaitMs(job: BackgroundReportJob, attempts: number) {
  if (job.stage === 'rate_limit' && typeof job.retryAfterSeconds === 'number' && Number.isFinite(job.retryAfterSeconds)) {
    return Math.max(2000, Math.min(30_000, job.retryAfterSeconds * 1000))
  }
  return Math.min(10_000, 2000 + Math.floor(attempts / 5) * 1000)
}

export function shouldPollBackgroundReportJob(
  job: BackgroundReportJob | null | undefined,
  attempts: number,
) {
  if (attempts >= BACKGROUND_REPORT_MAX_POLL_ATTEMPTS) return false
  return job?.state === 'queued' || job?.state === 'running'
}
