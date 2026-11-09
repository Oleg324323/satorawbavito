import { describe, expect, it } from 'vitest'

import { avitoCooldownRemainingMs, avitoCooldownUntilFromPayload, formatAvitoRefreshCountdown } from './avitoCooldown'

describe('avito cooldown helpers', () => {
  it('extracts the nearest future retryAfterUntil from cache and source errors', () => {
    const now = Date.parse('2026-08-05T10:00:00.000Z')
    const payload = {
      source: {
        cache: { retryAfterUntil: '2026-08-05T10:04:00.000Z' },
        errors: {
          stats: { retryAfterUntil: '2026-08-05T10:02:00.000Z' },
          listings: { retryAfterUntil: '2026-08-05T09:59:00.000Z' },
        },
      },
    }

    expect(avitoCooldownUntilFromPayload(payload, now)).toBe('2026-08-05T10:02:00.000Z')
  })

  it('falls back to retryAfterSeconds when Avito did not return an absolute timestamp', () => {
    const now = Date.parse('2026-08-05T10:00:00.000Z')

    expect(avitoCooldownUntilFromPayload({ source: { errors: { stats: { retryAfterSeconds: 90 } } } }, now)).toBe('2026-08-05T10:01:30.000Z')
    expect(avitoCooldownRemainingMs('2026-08-05T10:01:30.000Z', now)).toBe(90_000)
    expect(formatAvitoRefreshCountdown(90_000)).toBe('1:30')
  })

  it('does not restart an expired absolute cooldown from retryAfterSeconds', () => {
    const now = Date.parse('2026-08-05T10:02:00.000Z')
    const payload = { source: { errors: { stats: {
      retryAfterUntil: '2026-08-05T10:01:00.000Z', retryAfterSeconds: 90,
    } } } }
    expect(avitoCooldownUntilFromPayload(payload, now)).toBeNull()
  })
})
