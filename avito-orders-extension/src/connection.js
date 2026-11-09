// Shared by the popup and service worker; never infer a server from an Avito page.
globalThis.SatornaConnection = {
  defaultUrl: 'https://app.comcookie.store',
  localUrl: 'http://127.0.0.1:5177',
  codePrefix: 'satorna-connect-v1.',
  maxCodeLength: 12288,
  normalizeOrigin(value) {
    const text = typeof value === 'string' ? value.trim() : ''
    let url
    try { url = new URL(text) } catch {
      throw new Error('Укажите полный адрес Satorna: HTTPS или локальный HTTP с портом.')
    }
    const authority = text.match(/^https?:\/\/([^/?#]+)\/?$/i)?.[1] || ''
    const loopback = /^(localhost|127\.0\.0\.1|\[::1\])(?::\d+)?$/i.test(authority)
    if (text.length > 2048 || !authority || /[\s\\@\x00-\x1f\x7f]/.test(text)
      || url.username || url.password || url.search || url.hash || url.pathname !== '/'
      || !(url.protocol === 'https:' || (url.protocol === 'http:' && loopback))) {
      throw new Error('Нужен адрес без пути и пароля: HTTPS либо HTTP на localhost/127.0.0.1')
    }
    return url.origin
  },
  normalize(value) {
    const origin = this.normalizeOrigin(value)
    // Retain the existing migration only for legacy, unpinned connections.
    return ['https://ogni-frontend.vercel.app', 'https://satorna-wb.vercel.app', 'https://comcookie.store'].includes(origin) ? this.defaultUrl : origin
  },
  destination(connection) {
    return connection.originPinned ? this.normalizeOrigin(connection.backendUrl) : this.normalize(connection.backendUrl)
  },
  validateToken(value, legacy = false) {
    if (typeof value !== 'string' || !value || value.length > 4096
      || (legacy ? !/^(?:Bearer )?[\x21-\x7e]+$/i.test(value) || /^satorna-connect-/i.test(value)
        : !/^sat_avito_[A-Za-z0-9_-]+$/.test(value))) {
      throw new Error('Некорректный токен. Создайте новый код подключения на сайте Satorna.')
    }
    return value
  },
  decodeCode(value) {
    const code = typeof value === 'string' ? value.trim() : ''
    if (!code || code.length > this.maxCodeLength) throw new Error('Код подключения пустой или слишком длинный.')
    if (/^satorna-connect-v(?!1\.)/.test(code)) throw new Error('Эта версия кода не поддерживается. Обновите расширение.')
    if (!code.startsWith(this.codePrefix)) throw new Error('Некорректный код подключения. Скопируйте его с сайта Satorna.')
    const encoded = code.slice(this.codePrefix.length)
    let payload
    try {
      if (!/^[A-Za-z0-9_-]+$/.test(encoded) || encoded.length % 4 === 1) throw new Error()
      const bytes = Uint8Array.from(atob(encoded.replace(/-/g, '+').replace(/_/g, '/')), char => char.charCodeAt(0))
      if (bytes.length > 8192) throw new Error()
      payload = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes))
    } catch {
      throw new Error('Код подключения повреждён. Скопируйте его целиком с сайта Satorna.')
    }
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) throw new Error('Некорректный код подключения.')
    if (payload.v !== 1) throw new Error('Эта версия кода не поддерживается. Обновите расширение.')
    return { backendUrl: this.normalizeOrigin(payload.origin), accessToken: this.validateToken(payload.token) }
  },
  isTrustedOrigin(value) {
    const origin = this.normalizeOrigin(value)
    const hostname = new URL(origin).hostname
    const bridge = chrome.runtime.getManifest?.().content_scripts?.find(item => item.js?.includes('src/satorna-bridge.js'))
    return origin === this.defaultUrl || ['localhost', '127.0.0.1', '[::1]'].includes(hostname)
      || Boolean(bridge?.matches?.includes(`${origin}/*`))
  },
  async verify(connection) {
    const origin = this.normalizeOrigin(connection.backendUrl)
    let response
    try {
      // Existing authenticated, DB-only read. No collection or seller action.
      response = await fetch(`${origin}/api/v1/avito/orders/browser-collection-context`, {
        method: 'GET', headers: { Authorization: `Bearer ${connection.accessToken.replace(/^Bearer\s+/i, '')}` },
        redirect: 'error', credentials: 'omit', cache: 'no-store', signal: AbortSignal.timeout(15000),
      })
    } catch {
      throw new Error('Satorna недоступна. Проверьте адрес и подключение к сети.')
    }
    if ([401, 403].includes(response.status)) throw new Error('Satorna отклонила токен. Создайте новый код подключения на этом сайте.')
    if (response.status === 404) return false // Older server: configuration saved, not authentication verified.
    if (!response.ok) throw new Error('Не удалось проверить подключение к Satorna. Повторите позже.')
    try {
      const payload = await response.json()
      if (!Array.isArray(payload?.orders)) throw new Error()
    } catch {
      throw new Error('Адрес не вернул ожидаемый ответ Satorna. Проверьте адрес сайта.')
    }
    return true
  },
  async read() {
    const legacy = await chrome.storage.sync.get({ backendUrl: '', accessToken: '' })
    const local = await chrome.storage.local.get({ connection: null })
    return local.connection || legacy
  },
  async save(backendUrl, accessToken, { pinOrigin = false } = {}) {
    const connection = { backendUrl: pinOrigin ? this.normalizeOrigin(backendUrl) : this.normalize(backendUrl), accessToken: String(accessToken || '').trim(),
      ...(pinOrigin ? { originPinned: true } : {}) }
    // Connection credentials belong to this computer, not Chrome cloud sync.
    await chrome.storage.local.set({ connection })
    await chrome.storage.sync.remove(['backendUrl', 'accessToken'])
    return connection
  },
}
