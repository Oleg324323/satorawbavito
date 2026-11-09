type JsonRecord = Record<string, unknown>

function asRecord(value: unknown): JsonRecord | null {
  return value && typeof value === 'object' ? value as JsonRecord : null
}

function pushRetryAfterCandidate(absolute: number[], relative: number[], value: unknown, nowMs: number) {
  if (typeof value === 'string') {
    const timestamp = Date.parse(value)
    if (Number.isFinite(timestamp)) absolute.push(timestamp)
    return
  }
  if (typeof value === 'number' && Number.isFinite(value) && value > 0) {
    relative.push(nowMs + value * 1000)
  }
}

function collectRetryAfterCandidates(absolute: number[], relative: number[], source: JsonRecord | null, nowMs: number) {
  if (!source) return
  pushRetryAfterCandidate(absolute, relative, source.retryAfterUntil, nowMs)
  pushRetryAfterCandidate(absolute, relative, source.retryAfterSeconds, nowMs)
  for (const value of Object.values(source)) {
    if (value && typeof value === 'object') collectRetryAfterCandidates(absolute, relative, value as JsonRecord, nowMs)
  }
}

export function avitoCooldownUntilFromPayload(payload: unknown, nowMs = Date.now()) {
  const root = asRecord(payload)
  const source = asRecord(root?.source)
  const absolute: number[] = [], relative: number[] = []
  collectRetryAfterCandidates(absolute, relative, source, nowMs)
  const candidates = absolute.length ? absolute.filter(timestamp => timestamp > nowMs) : relative
  return candidates.length ? new Date(Math.min(...candidates)).toISOString() : null
}

export function avitoCooldownRemainingMs(cooldownUntil: string | null | undefined, nowMs = Date.now()) {
  if (!cooldownUntil) return 0
  const target = Date.parse(cooldownUntil)
  if (!Number.isFinite(target)) return 0
  return Math.max(0, target - nowMs)
}

export function formatAvitoRefreshCountdown(ms: number) {
  const safeMs = Math.max(0, ms)
  const totalSeconds = Math.ceil(safeMs / 1000)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return `${minutes}:${String(seconds).padStart(2, '0')}`
}
