import { resetLiveRepricerParityCache } from './liveParityData'

export function invalidateCostViews() {
  resetLiveRepricerParityCache()
  if (typeof window !== 'undefined') window.dispatchEvent(new Event('satorna:costs-saved'))
}
