// A transport envelope, not encryption. Keep the entire value ephemeral/secret.
export function encodeWbExtensionConnectionCode(origin: string, token: string) {
  const url = new URL(origin)
  const local = url.protocol === 'http:' && ['127.0.0.1', 'localhost'].includes(url.hostname)
  if ((!local && url.protocol !== 'https:') || url.username || url.password || url.pathname !== '/' || url.search || url.hash
      || !/^sat_wbp_[A-Za-z0-9_-]{43}$/.test(token)) throw new Error('Не удалось создать токен подключения WB')
  const bytes = new TextEncoder().encode(JSON.stringify({ v: 1, origin: url.origin, token }))
  return 'satorna-connect-v1.' + btoa(Array.from(bytes, byte => String.fromCharCode(byte)).join(''))
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}
