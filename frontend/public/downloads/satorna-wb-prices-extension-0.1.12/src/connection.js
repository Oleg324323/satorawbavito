'use strict'
globalThis.WbPricesConnection = (() => {
  const DEFAULT_SERVER_URL = "https://app.comcookie.store"
  const LEGACY_SERVER_URL = 'https://api.elfprint-system.ru'
  const ALLOW_LOCAL_SERVER = true
  const LOCAL_ONLY = false
  const CODE_PREFIX = 'satorna-connect-v1.'
  const MAX_CODE_LENGTH = 12288
  function normalizeServerUrl(value) {
    if (typeof value !== 'string' || value.length > 2048 || /[\s\\@\x00-\x1f\x7f]/.test(value.trim())) throw new Error('server_invalid')
    let url
    try { url = new URL(value.trim()) } catch { throw new Error('server_invalid') }
    const authority = value.trim().match(/^https?:\/\/([^/?#]+)\/?$/i)?.[1]
    const local = ALLOW_LOCAL_SERVER && url.protocol === 'http:'
      && /^(localhost|127\.0\.0\.1)(?::\d+)?$/i.test(authority || '')
    if (!authority || (LOCAL_ONLY && !local) || (!local && url.protocol !== 'https:') || url.username || url.password
        || url.pathname !== '/' || url.search || url.hash) throw new Error('server_invalid')
    return url.origin
  }
  function validateToken(value) {
    const token = typeof value === 'string' ? value.trim().replace(/^Bearer\s+/i, '') : ''
    if (!/^sat_wbp_[A-Za-z0-9_-]{43}$/.test(token)) throw new Error('token_invalid')
    return token
  }
  function parseInput(value, savedOrigin) {
    const input = typeof value === 'string' ? value.trim() : ''
    if (!input || input.length > MAX_CODE_LENGTH) throw new Error('token_invalid')
    if (!input.startsWith(CODE_PREFIX)) {
      if (/^satorna-connect-/i.test(input)) throw new Error('connection_code_invalid')
      return { backendUrl: normalizeServerUrl(savedOrigin || DEFAULT_SERVER_URL), token: validateToken(input) }
    }
    let payload
    try {
      const encoded = input.slice(CODE_PREFIX.length)
      if (!/^[A-Za-z0-9_-]+$/.test(encoded) || encoded.length % 4 === 1) throw new Error()
      const bytes = Uint8Array.from(atob(encoded.replace(/-/g, '+').replace(/_/g, '/')), char => char.charCodeAt(0))
      if (bytes.length > 8192) throw new Error()
      payload = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes))
      if (!payload || Array.isArray(payload) || payload.v !== 1) throw new Error()
    } catch { throw new Error('connection_code_invalid') }
    return { backendUrl: normalizeServerUrl(payload.origin), token: validateToken(payload.token) }
  }
  function isTrustedOrigin(origin) {
    const normalized = normalizeServerUrl(origin)
    return [DEFAULT_SERVER_URL, LEGACY_SERVER_URL, 'https://app.comcookie.store'].includes(normalized)
      || ['localhost', '127.0.0.1'].includes(new URL(normalized).hostname)
  }
  return Object.freeze({ DEFAULT_SERVER_URL, LEGACY_SERVER_URL, MAX_CODE_LENGTH, normalizeServerUrl, validateToken, parseInput, isTrustedOrigin,
    permissionOrigin: origin => `${normalizeServerUrl(origin)}/*` })
})()
