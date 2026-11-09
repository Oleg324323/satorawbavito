const WB_TIME_ZONE = 'Europe/Moscow'

function toIsoDay(date: Date) {
  return date.toISOString().slice(0, 10)
}

function moscowIsoDay(now: Date) {
  const parts = new Intl.DateTimeFormat('en', {
    timeZone: WB_TIME_ZONE,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(now)
  const value = Object.fromEntries(parts.map((part) => [part.type, part.value]))
  return `${value.year}-${value.month}-${value.day}`
}

export function lastClosedWbDay(now: Date = new Date()) {
  const day = new Date(`${moscowIsoDay(now)}T00:00:00Z`)
  day.setUTCDate(day.getUTCDate() - 1)
  return toIsoDay(day)
}

export function resolvePresetPeriodRange(periodDays: number, now: Date = new Date()) {
  const days = Number.isFinite(periodDays) ? Math.max(1, Math.trunc(periodDays)) : 1
  const dateTo = lastClosedWbDay(now)
  const dateFrom = new Date(`${dateTo}T00:00:00Z`)
  dateFrom.setUTCDate(dateFrom.getUTCDate() - (days - 1))
  return { dateFrom: toIsoDay(dateFrom), dateTo }
}

/** Cache coverage must never restrict the reporting calendar. */
export function isSelectableWbDay(iso: string, minIso: string, now: Date = new Date()) {
  return /^\d{4}-\d{2}-\d{2}$/.test(iso)
    && !Number.isNaN(Date.parse(`${iso}T00:00:00Z`))
    && new Date(`${iso}T00:00:00Z`).toISOString().slice(0, 10) === iso
    && iso >= minIso && iso <= lastClosedWbDay(now)
}
