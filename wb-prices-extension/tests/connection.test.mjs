import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
const source = readFileSync(new URL('../src/connection.js', import.meta.url), 'utf8')
function connection(code = source) {
  const context = vm.createContext({ URL, atob, Uint8Array, TextDecoder })
  vm.runInContext(code, context)
  return context.WbPricesConnection
}

test('custom package defaults never rebind a legacy token to another server', () => {
  const c = connection(source.replace("const DEFAULT_SERVER_URL = 'https://api.elfprint-system.ru'",
    "const DEFAULT_SERVER_URL = 'https://new.example'"))
  assert.equal(c.DEFAULT_SERVER_URL, 'https://new.example')
  assert.equal(c.LEGACY_SERVER_URL, 'https://api.elfprint-system.ru')
})

const TOKEN = `sat_wbp_${'a'.repeat(43)}`
const code = payload => 'satorna-connect-v1.' + Buffer.from(JSON.stringify(payload)).toString('base64url')

test('one input accepts WB tokens and pins encoded keys to the exact issuing origin and local port', () => {
  const c = connection()
  assert.equal(c.parseInput(` Bearer ${TOKEN} `).token, TOKEN)
  assert.equal(c.parseInput(TOKEN, 'https://saved.example').backendUrl, 'https://saved.example')
  for (const origin of ['https://app.comcookie.store', 'http://127.0.0.1:5177', 'http://localhost:58017']) {
    const parsed = c.parseInput(code({ v: 1, origin, token: TOKEN }), 'https://other.example')
    assert.equal(parsed.backendUrl, origin)
    assert.equal(parsed.token, TOKEN)
  }
})

test('wrong marketplace, broken envelopes and unsafe destinations never produce a connection', () => {
  const c = connection()
  for (const input of ['sat_avito_not_a_wb_key', 'wb-api-key', 'satorna-connect-v2.abc', 'satorna-connect-v1.%%%'
    , code({ v: 2, origin: 'https://app.comcookie.store', token: TOKEN }), code({ v: 1, origin: 'https://app.comcookie.store', token: 'sat_avito_bad' })]) {
    assert.throws(() => c.parseInput(input))
  }
  for (const origin of ['http://remote.example', 'https://user:pass@example.test', 'https://example.test/api',
    'https://example.test/?key=secret', 'https://example.test/#secret', 'https://example.test\\@evil.test', 'http://127.1:5177', 'http://2130706433:5177']) {
    assert.throws(() => c.parseInput(code({ v: 1, origin, token: TOKEN })), /server_invalid/)
  }
  assert.equal(c.isTrustedOrigin('https://unknown.example'), false)
  assert.equal(c.isTrustedOrigin('http://127.0.0.1:5177'), true)
})

test('LOCAL artifact cannot connect or request permissions for a remote server', () => {
  const c = connection(source.replace('const ALLOW_LOCAL_SERVER = false', 'const ALLOW_LOCAL_SERVER = true')
    .replace('const LOCAL_ONLY = false', 'const LOCAL_ONLY = true'))
  assert.equal(c.normalizeServerUrl('http://127.0.0.1:58017'), 'http://127.0.0.1:58017')
  for (const url of ['https://api.elfprint-system.ru', 'https://new.example', 'http://remote.example']) {
    assert.throws(() => c.normalizeServerUrl(url), /server_invalid/)
    assert.throws(() => c.permissionOrigin(url), /server_invalid/)
  }
})
