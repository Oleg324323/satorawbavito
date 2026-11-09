// Transport envelope only. The full code is a secret, just like its token.
export function encodeAvitoExtensionConnectionCode(origin: string, token: string) {
  const bytes = new TextEncoder().encode(JSON.stringify({ v: 1, origin, token }))
  const encoded = btoa(Array.from(bytes, byte => String.fromCharCode(byte)).join(''))
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
  return `satorna-connect-v1.${encoded}`
}
