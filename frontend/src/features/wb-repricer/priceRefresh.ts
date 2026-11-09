import { priceApplyOutcome } from './priceApplyOutcome'

export const CONFIRMED_PRICE_EVENT = 'vella:wb-price-confirmed'

export function announceConfirmedPrice(receipt: Parameters<typeof priceApplyOutcome>[0]) {
  if (priceApplyOutcome(receipt) === 'confirmed') window.dispatchEvent(new Event(CONFIRMED_PRICE_EVENT))
}

// Background DB reads only. Do not restart collection or replace an in-flight
// period/filter change merely because the extension has uploaded another batch.
export function watchPriceRefresh(read: () => Promise<void>, canRead: () => boolean, onError: (error: unknown) => void, intervalMs = 60_000) {
  let stopped = false, pending = false, lastRead = Date.now()
  let confirmed = false
  const refresh = async () => {
    if (stopped || pending || document.visibilityState === 'hidden' || (!confirmed && Date.now() - lastRead < intervalMs) || !canRead()) return
    pending = true
    confirmed = false
    lastRead = Date.now()
    try { await read() } catch (error) { if (!stopped) onError(error) }
    finally { pending = false; if (!stopped && confirmed) void refresh() }
  }
  const onConfirmed = () => { confirmed = true; void refresh() }
  const timer = window.setInterval(() => { void refresh() }, intervalMs)
  window.addEventListener('focus', refresh)
  window.addEventListener(CONFIRMED_PRICE_EVENT, onConfirmed)
  window.addEventListener('satorna:costs-saved', onConfirmed)
  document.addEventListener('visibilitychange', refresh)
  return () => {
    stopped = true
    window.clearInterval(timer)
    window.removeEventListener('focus', refresh)
    window.removeEventListener(CONFIRMED_PRICE_EVENT, onConfirmed)
    window.removeEventListener('satorna:costs-saved', onConfirmed)
    document.removeEventListener('visibilitychange', refresh)
  }
}
