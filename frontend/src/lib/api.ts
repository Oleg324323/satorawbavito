import { clearStoredAccessToken, readStoredAccessToken, storeAccessToken } from './authTokenStore'
import { wbDemandHeaders } from './wbDemand'

export class ApiError extends Error {
  status: number
  code?: string
  details?: unknown
  headers?: Record<string, string>

  constructor(message: string, status: number, code?: string, details?: unknown, headers?: Record<string, string>) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.details = details
    this.headers = headers
  }
}

type Envelope<T> = {
  data: T
  timestamp: string
}

type RefreshAccessTokenResponse = {
  accessToken: string
  expiresIn: number
  tokenType?: string
}

let accessTokenRefreshInFlight: Promise<RefreshAccessTokenResponse> | null = null

const REFRESHABLE_AUTH_401_CODES = new Set([
  'AUTH_REQUIRED',
  'INVALID_AUTH_SCHEME',
  'INVALID_AUTH_TOKEN',
  'TOKEN_EXPIRED',
  'SESSION_INACTIVE',
])

export function buildApiUrl(path: string) {
  if (/^https?:\/\//.test(path)) return path
  const baseUrl = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')
  const normalizedPath = path.startsWith('/') ? path : `/${path}`
  return `${baseUrl}${normalizedPath}`
}

function buildErrorMessage(payload: unknown, response: Response) {
  if (payload && typeof payload === 'object') {
    const maybeEnvelope = payload as {
      error?: { code?: string; message?: string; details?: unknown }
      detail?: string | Array<{ msg?: string }> | { code?: string; message?: string }
    }
    if (maybeEnvelope.error?.message) return maybeEnvelope.error.message
    if (typeof maybeEnvelope.detail === 'string') return maybeEnvelope.detail
    if (maybeEnvelope.detail && !Array.isArray(maybeEnvelope.detail) && typeof maybeEnvelope.detail === 'object') {
      return maybeEnvelope.detail.message || maybeEnvelope.detail.code || response.statusText || `Request failed: ${response.status}`
    }
    if (Array.isArray(maybeEnvelope.detail)) {
      const joined = maybeEnvelope.detail
        .map((item) => item?.msg)
        .filter(Boolean)
        .join('; ')
      if (joined) return joined
    }
  }
  return response.statusText || `Request failed: ${response.status}`
}

function extractErrorHeaders(response: Response) {
  const headerNames = [
    'retry-after',
    'x-ratelimit-limit',
    'x-ratelimit-remaining',
    'x-ratelimit-retry',
    'x-ratelimit-reset',
    'x-upstream-status',
    'x-wb-request-id',
  ]
  const headers: Record<string, string> = {}
  for (const headerName of headerNames) {
    const value = response.headers.get(headerName)
    if (value) headers[headerName] = value
  }
  return headers
}

async function parseResponse<T>(response: Response) {
  const mediaType = response.headers.get('content-type')?.split(';', 1)[0]?.trim().toLowerCase() ?? ''
  const isJson = mediaType === 'application/json' || mediaType.endsWith('+json')
  if (response.ok && (response.status === 204 || response.status === 205)) return null as T
  const payload = isJson ? ((await response.json()) as T) : null
  if (!response.ok) {
    const errorPayload = payload as {
      error?: { code?: string; details?: unknown }
      detail?: { code?: string; details?: unknown }
    } | null
    throw new ApiError(
      buildErrorMessage(payload, response),
      response.status,
      errorPayload?.error?.code ?? errorPayload?.detail?.code,
      errorPayload?.error?.details ?? errorPayload?.detail?.details,
      extractErrorHeaders(response),
    )
  }
  if (!isJson) {
    throw new ApiError(
      'API returned a successful non-JSON response',
      502,
      'INVALID_API_RESPONSE',
      { contentType: response.headers.get('content-type') },
      extractErrorHeaders(response),
    )
  }
  return payload
}

function skipsAccessTokenRefresh(path: string) {
  const url = buildApiUrl(path)
  try {
    const pathname = new URL(url, 'http://localhost').pathname
    return pathname.startsWith('/api/v1/auth/') && pathname !== '/api/v1/auth/logout'
  } catch {
    return path.includes('/api/v1/auth/')
  }
}

function extractAuthErrorCodes(payload: unknown) {
  const codes: string[] = []
  const pushCode = (value: unknown) => {
    if (typeof value !== 'string' || !value.trim()) return
    const normalized = value.trim()
    codes.push(normalized)
    const prefix = normalized.split(':')[0]?.trim()
    if (prefix && prefix !== normalized) codes.push(prefix)
  }

  if (payload && typeof payload === 'object') {
    const maybeEnvelope = payload as {
      error?: { code?: string; message?: string }
      detail?: string | { code?: string; message?: string } | Array<{ msg?: string }>
    }
    pushCode(maybeEnvelope.error?.code)
    pushCode(maybeEnvelope.error?.message)
    if (typeof maybeEnvelope.detail === 'string') {
      pushCode(maybeEnvelope.detail)
    } else if (maybeEnvelope.detail && !Array.isArray(maybeEnvelope.detail) && typeof maybeEnvelope.detail === 'object') {
      pushCode(maybeEnvelope.detail.code)
      pushCode(maybeEnvelope.detail.message)
    }
  }

  return codes
}

export async function shouldTryAccessTokenRefresh(path: string, response: Response, headers: Headers) {
  if (response.status !== 401) return false
  if (skipsAccessTokenRefresh(path)) return false
  if (!headers.has('Authorization') && !readStoredAccessToken()) return false
  try {
    const isJson = response.headers.get('content-type')?.includes('application/json')
    if (!isJson) return false
    const payload = await response.clone().json()
    return extractAuthErrorCodes(payload).some((code) => REFRESHABLE_AUTH_401_CODES.has(code))
  } catch {
    return false
  }
}

export function refreshStoredAccessTokenOnce() {
  if (!accessTokenRefreshInFlight) {
    accessTokenRefreshInFlight = (async () => {
      const response = await fetch(buildApiUrl('/api/v1/auth/refresh'), {
        method: 'POST',
        headers: { Accept: 'application/json' },
        credentials: 'include',
      })
      const envelope = await parseResponse<Envelope<RefreshAccessTokenResponse>>(response)
      const nextToken = envelope?.data?.accessToken
      if (!nextToken) throw new ApiError('Refresh response did not include access token', 401)
      storeAccessToken(nextToken)
      return envelope.data
    })()
      .catch((error) => {
        if (error instanceof ApiError && error.status === 401) clearStoredAccessToken()
        throw error
      })
      .finally(() => {
        accessTokenRefreshInFlight = null
      })
  }
  return accessTokenRefreshInFlight
}

export async function apiRequest<T>(path: string, init: RequestInit = {}) {
  const headers = new Headers(init.headers)
  Object.entries(wbDemandHeaders(path, init.method)).forEach(([key, value]) => headers.set(key, value))
  if (!headers.has('Accept')) headers.set('Accept', 'application/json')
  if (init.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }

  const response = await fetch(buildApiUrl(path), {
    ...init,
    headers,
    credentials: 'include',
  })

  if (await shouldTryAccessTokenRefresh(path, response, headers)) {
    try {
      const refreshed = await refreshStoredAccessTokenOnce()
      const retryHeaders = new Headers(headers)
      retryHeaders.set('Authorization', `Bearer ${refreshed.accessToken}`)
      const retryResponse = await fetch(buildApiUrl(path), {
        ...init,
        headers: retryHeaders,
        credentials: 'include',
      })
      return parseResponse<T>(retryResponse)
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 401)) {
        console.warn('Failed to refresh access token after API 401', error)
      }
    }
  }

  return parseResponse<T>(response)
}

export async function apiData<T>(path: string, init: RequestInit = {}) {
  const envelope = await apiRequest<Envelope<T>>(path, init)
  if (!envelope) throw new ApiError('Empty API response', 204)
  return envelope.data
}
