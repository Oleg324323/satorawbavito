const DAY = 86_400_000

// Date-only Moscow business boundaries, not elapsed local hours (DST-safe).
export function previousFunnelPeriod(from: string, to: string) {
  const start = Date.parse(`${from}T00:00:00Z`), end = Date.parse(`${to}T00:00:00Z`)
  if (!Number.isFinite(start) || !Number.isFinite(end) || start > end) return null
  const days = (end - start) / DAY + 1
  return { dateFrom: new Date(start - days * DAY).toISOString().slice(0, 10), dateTo: new Date(start - DAY).toISOString().slice(0, 10) }
}

export function formatNativeFunnelDynamic(value: unknown, ready = true) {
  if (!ready || typeof value !== 'number' || !Number.isFinite(value)) return null
  if (value === 0) return { direction: 'flat', text: '0%' }
  // Formatting only. Preserve WB's percentage, sign and supplied precision.
  const text = Math.abs(value).toLocaleString('ru-RU', { maximumSignificantDigits: 21 })
  return { direction: value > 0 ? 'up' : 'down', text: `${value > 0 ? '↑ +' : '↓ −'}${text}%` }
}

export function formatAggregateFunnelDynamic(value: unknown, ready = true) {
  if (!ready || typeof value !== 'number' || !Number.isFinite(value)) return null
  // Only calculated summary percentages are rounded for display. Native
  // product percentages continue through their unchanged formatter above.
  const rounded = Number(value.toFixed(2))
  if (rounded === 0) return { direction: 'flat', text: '0%' }
  const text = Math.abs(rounded).toLocaleString('ru-RU', { maximumFractionDigits: 2 })
  return { direction: rounded > 0 ? 'up' : 'down', text: `${rounded > 0 ? '↑ +' : '↓ −'}${text}%` }
}
