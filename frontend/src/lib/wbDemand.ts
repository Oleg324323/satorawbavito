import { readStoredAccessToken } from './authTokenStore'

let consumer = ''
let view = ''
let route = ''
let sequence = 0
let period = ''

function identity() {
  if (!consumer && typeof window !== 'undefined') {
    consumer = crypto.randomUUID()
    view = crypto.randomUUID()
    window.addEventListener('pagehide', () => release(view))
  }
}

function release(previousView: string) {
  if (!consumer || !previousView) return
  const token = readStoredAccessToken()
  const base = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')
  void fetch(`${base}/api/wb/reports/work-demand/release`, {
    method: 'POST', credentials: 'include', keepalive: true,
    headers: { 'X-WB-Consumer': consumer, 'X-WB-View': previousView,
      ...(token ? { Authorization: `Bearer ${token}` } : {}) },
  }).catch(() => { /* The server lease expires if unload cannot send. */ })
}

export function setWbDemandRoute(nextRoute: string) {
  identity()
  if (route === nextRoute) return
  const previous = route ? view : ''
  route = nextRoute
  period = ''
  view = typeof window === 'undefined' ? '' : crypto.randomUUID()
  sequence++
  release(previous)
}

export function wbDemandHeaders(path: string, method = 'GET'): Record<string, string> {
  if (!path.startsWith('/api/wb/reports/') || path.includes('/work-demand/')) return {}
  identity()
  if (!consumer) return {}
  if (method.toUpperCase() === 'POST') {
    const params = new URLSearchParams(path.split('?')[1] ?? '')
    // Statistics comparison windows belong to the same selected-period view.
    // Rotating here would cancel the main load or the other comparison job.
    const comparison = path.startsWith('/api/wb/reports/stats/jobs?') && params.get('source') === 'statistics-comparison'
    const nextPeriod = !comparison && params.has('from') && params.has('to') ? `${params.get('from')}:${params.get('to')}` : ''
    if (nextPeriod && period && nextPeriod !== period) {
      const previous = view
      view = crypto.randomUUID()
      release(previous)
    }
    if (nextPeriod) period = nextPeriod
    sequence++
  }
  return { 'X-WB-Consumer': consumer, 'X-WB-View': view, 'X-WB-Sequence': String(sequence) }
}
