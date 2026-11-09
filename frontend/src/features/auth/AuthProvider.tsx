import { useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { ApiError, refreshStoredAccessTokenOnce, shouldTryAccessTokenRefresh } from '@/lib/api'
import { getCurrentUserProfile, getSessions } from '@/features/settings/repository'
import type { SessionInfo } from '@/features/settings/types'
import {
  clearStoredAccessToken,
  fetchCabinetMe,
  fetchSessions,
  AUTH_ACCESS_TOKEN_CLEARED_EVENT,
  AUTH_ACCESS_TOKEN_REFRESHED_EVENT,
  loginWithPassword,
  logoutCurrentSession,
  mapCabinetMeToProfile,
  mapSessionsToSessionInfo,
  readStoredAccessToken,
  registerWithPassword,
  revokeOtherSessions as revokeOtherSessionsRequest,
  revokeSession as revokeSessionRequest,
  storeAccessToken,
  type CabinetMeView,
} from './authApi'
import { AuthContext, type AuthContextValue, type AuthStatus, type SessionsStatus } from './authContext'

const AUTH_BYPASS_ENABLED = import.meta.env.DEV && import.meta.env.VITE_AUTH_BYPASS === 'true'
const BYPASS_TOKEN = 'dev-auth-bypass'
const AUTH_BOOTSTRAP_TIMEOUT_MS = 5000

const AUTH_API_URLS = (() => {
  const raw = import.meta.env.VITE_API_BASE_URL
  if (!raw) return [] as string[]

  try {
    const normalized = raw.endsWith('/') ? raw.slice(0, -1) : raw
    const baseUrl = /^https?:\/\//i.test(normalized) ? normalized : `${window.location.origin}${normalized.startsWith('/') ? '' : '/'}${normalized}`
    return [new URL(baseUrl).origin]
  } catch {
    return []
  }
})()

function isUnauthorized(error: unknown) {
  return error instanceof ApiError && error.status === 401
}

function shouldAttachAuthHeader(url: string) {
  if (url.startsWith('/api/')) return true
  try {
    const parsed = new URL(url, window.location.origin)
    return (
      (parsed.origin === window.location.origin && parsed.pathname.startsWith('/api/'))
      || AUTH_API_URLS.includes(parsed.origin)
    )
  } catch {
    return false
  }
}

function isAuthApiUrl(url: string) {
  try {
    return new URL(url, window.location.origin).pathname.startsWith('/api/v1/auth/')
  } catch {
    return url.includes('/api/v1/auth/')
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const initialStoredToken = AUTH_BYPASS_ENABLED ? BYPASS_TOKEN : readStoredAccessToken()
  const [status, setStatus] = useState<AuthStatus>(() => (AUTH_BYPASS_ENABLED ? 'authenticated' : 'loading'))
  const [bootstrapDone, setBootstrapDone] = useState(() => AUTH_BYPASS_ENABLED)
  const [accessToken, setAccessToken] = useState<string | null>(() => initialStoredToken)
  const [cabinetMe, setCabinetMe] = useState<CabinetMeView | null>(null)
  const [sessions, setSessions] = useState<SessionInfo[]>(() => (AUTH_BYPASS_ENABLED ? getSessions() : []))
  const [sessionsStatus, setSessionsStatus] = useState<SessionsStatus>(() => (AUTH_BYPASS_ENABLED ? 'ready' : 'idle'))

  async function loadSessionsState(token: string, me: CabinetMeView) {
    setSessionsStatus('loading')
    try {
      const sessionViews = await fetchSessions(token)
      setSessions(mapSessionsToSessionInfo(sessionViews, me.activeSession?.sessionId ?? null))
      setSessionsStatus('ready')
    } catch {
      setSessions([])
      setSessionsStatus('error')
    }
  }

  function clearAuthState() {
    clearStoredAccessToken({ notify: false })
    setAccessToken(null)
    setCabinetMe(null)
    setSessions([])
    setSessionsStatus('idle')
    setBootstrapDone(true)
    setStatus('anonymous')
  }

  async function hydrateSession(token: string) {
    const me = await fetchCabinetMe(token)
    setCabinetMe(me)
    void loadSessionsState(token, me)
    return me
  }

  function establishSession(token: string) {
    storeAccessToken(token, { notify: false })
    setAccessToken(token)
    setBootstrapDone(true)
    setStatus('authenticated')
    void hydrateSession(token).catch((error) => {
      console.warn('Failed to hydrate session after token issue', error)
      if (isUnauthorized(error)) {
        clearAuthState()
      }
    })
  }

  useEffect(() => {
    if (AUTH_BYPASS_ENABLED) return
    let cancelled = false

    async function bootstrap() {
      const storedToken = readStoredAccessToken()
      if (storedToken) {
        try {
          const me = await hydrateSession(storedToken)
          if (cancelled) return
          setAccessToken(storedToken)
          setBootstrapDone(true)
          setStatus('authenticated')
          void me
          return
        } catch (error) {
          if (!isUnauthorized(error)) {
            console.warn('Failed to hydrate stored auth session; keeping token for retry', error)
            if (!cancelled) {
              setAccessToken(storedToken)
              setBootstrapDone(true)
              setStatus('authenticated')
              return
            }
          }
        }
      }

      try {
        const refreshed = await refreshStoredAccessTokenOnce()
        if (cancelled) return
        establishSession(refreshed.accessToken)
      } catch {
        if (!cancelled) clearAuthState()
      } finally {
        if (!cancelled) setBootstrapDone(true)
      }
    }

    void bootstrap()

    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    if (AUTH_BYPASS_ENABLED || bootstrapDone) return
    const timer = window.setTimeout(() => {
      console.warn(`Auth bootstrap timed out after ${AUTH_BOOTSTRAP_TIMEOUT_MS}ms`)
      setBootstrapDone(true)
      setStatus((current) => (current === 'loading' ? 'anonymous' : current))
    }, AUTH_BOOTSTRAP_TIMEOUT_MS)

    return () => {
      window.clearTimeout(timer)
    }
  }, [bootstrapDone])

  useEffect(() => {
    if (AUTH_BYPASS_ENABLED) return

    const handleTokenRefreshed = (event: Event) => {
      const nextToken = (event as CustomEvent<{ accessToken?: string }>).detail?.accessToken
      if (!nextToken || nextToken === accessToken) return
      setAccessToken(nextToken)
      setBootstrapDone(true)
      setStatus('authenticated')
      void hydrateSession(nextToken).catch((error) => {
        console.warn('Failed to hydrate session after access token refresh', error)
        if (isUnauthorized(error)) clearAuthState()
      })
    }

    const handleTokenCleared = () => {
      clearAuthState()
    }

    window.addEventListener(AUTH_ACCESS_TOKEN_REFRESHED_EVENT, handleTokenRefreshed as EventListener)
    window.addEventListener(AUTH_ACCESS_TOKEN_CLEARED_EVENT, handleTokenCleared)
    return () => {
      window.removeEventListener(AUTH_ACCESS_TOKEN_REFRESHED_EVENT, handleTokenRefreshed as EventListener)
      window.removeEventListener(AUTH_ACCESS_TOKEN_CLEARED_EVENT, handleTokenCleared)
    }
  }, [accessToken])

  useEffect(() => {
    const originalFetch = window.fetch.bind(window)

    window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
      if (!accessToken || accessToken === BYPASS_TOKEN) return originalFetch(input, init)

      const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
      if (!shouldAttachAuthHeader(url)) return originalFetch(input, init)

      const headers = new Headers(input instanceof Request ? input.headers : init?.headers)
      if (!headers.has('Authorization')) headers.set('Authorization', `Bearer ${accessToken}`)

      const requestInit = { ...init, headers, credentials: init?.credentials ?? 'include' }
      const canRefreshAndRetry = !isAuthApiUrl(url)
      const retryWithFreshToken = async (response: Response) => {
        if (!canRefreshAndRetry || !(await shouldTryAccessTokenRefresh(url, response, headers))) return response
        try {
          const refreshed = await refreshStoredAccessTokenOnce()
          const retryHeaders = new Headers(headers)
          retryHeaders.set('Authorization', `Bearer ${refreshed.accessToken}`)
          if (input instanceof Request) {
            return originalFetch(new Request(input, { ...requestInit, headers: retryHeaders }))
          }
          return originalFetch(input, { ...requestInit, headers: retryHeaders })
        } catch (error) {
          console.warn('Failed to refresh access token after fetch 401', error)
          return response
        }
      }

      if (input instanceof Request) {
        return retryWithFreshToken(await originalFetch(new Request(input, requestInit)))
      }

      return retryWithFreshToken(await originalFetch(input, requestInit))
    }) as typeof window.fetch

    return () => {
      window.fetch = originalFetch
    }
  }, [accessToken])

  const resolvedStatus: AuthStatus = AUTH_BYPASS_ENABLED
    ? 'authenticated'
    : accessToken && cabinetMe
      ? 'authenticated'
      : bootstrapDone
        ? 'anonymous'
        : status

  const value = useMemo<AuthContextValue>(() => ({
    status: resolvedStatus,
    isAuthenticated: Boolean(accessToken),
    accessToken,
    profile: AUTH_BYPASS_ENABLED ? getCurrentUserProfile() : cabinetMe ? mapCabinetMeToProfile(cabinetMe) : null,
    cabinetMe,
    sessions,
    sessionsStatus,
    login: async (payload) => {
      const response = await loginWithPassword(payload)
      await establishSession(response.accessToken)
    },
    register: async (payload) => {
      const response = await registerWithPassword(payload)
      await establishSession(response.accessToken)
    },
    logout: async () => {
      if (AUTH_BYPASS_ENABLED) return
      const token = accessToken
      if (token) await logoutCurrentSession(token)
      clearAuthState()
    },
    refreshProfile: async () => {
      if (!accessToken || AUTH_BYPASS_ENABLED) return
      const me = await fetchCabinetMe(accessToken)
      setCabinetMe(me)
      void loadSessionsState(accessToken, me)
    },
    refreshSessions: async () => {
      if (!accessToken || !cabinetMe || AUTH_BYPASS_ENABLED) return
      await loadSessionsState(accessToken, cabinetMe)
    },
    revokeSession: async (sessionId: string) => {
      if (!accessToken || !cabinetMe || AUTH_BYPASS_ENABLED) return
      await revokeSessionRequest(accessToken, sessionId)
      await loadSessionsState(accessToken, cabinetMe)
    },
    revokeOtherSessions: async () => {
      if (!accessToken || !cabinetMe || AUTH_BYPASS_ENABLED) return 0
      const response = await revokeOtherSessionsRequest(accessToken)
      await loadSessionsState(accessToken, cabinetMe)
      return response.revokedCount
    },
  }), [accessToken, cabinetMe, resolvedStatus, sessions, sessionsStatus])

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}
