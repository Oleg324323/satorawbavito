import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import vm from 'node:vm'
import test from 'node:test'

const source = await readFile(new URL('../src/connection.js', import.meta.url), 'utf8')
const background = await readFile(new URL('../src/background.js', import.meta.url), 'utf8')
const popup = await readFile(new URL('../src/popup.js', import.meta.url), 'utf8')
const manifest = JSON.parse(await readFile(new URL('../manifest.json', import.meta.url), 'utf8'))
function harness() {
  const sync = { backendUrl: 'https://ogni-frontend.vercel.app', accessToken: 'synthetic-legacy' }
  const local = {}
  const store = (data) => ({
    get: async (defaults) => ({ ...defaults, ...data }),
    set: async (values) => Object.assign(data, values),
    remove: async (keys) => keys.forEach(key => delete data[key]),
  })
  const calls = []
  const permissionCalls = []
  const context = vm.createContext({ URL, AbortSignal, atob, TextDecoder, console: { info() {}, error() {}, warn() {} },
    chrome: { storage: { sync: store(sync), local: store(local) },
      permissions: { request: async value => { permissionCalls.push(value); return true } },
      runtime: { getManifest: () => manifest, onMessage: { addListener() {} } } },
    importScripts() {}, fetch: async (url, options) => {
      calls.push({ url, options })
      return { ok: true, status: 200, text: async () => JSON.stringify({ ok: true }), json: async () => ({ orders: [] }) }
    },
  })
  vm.runInContext(source, context)
  vm.runInContext(background, context)
  return { connection: context.SatornaConnection, context, sync, local, calls, permissionCalls }
}

test('accepts loopback HTTP and explicit HTTPS origins', () => {
  const { connection } = harness()
  for (const url of ['http://127.0.0.1:5177', 'http://localhost:58017', 'http://[::1]:5177', 'https://app.comcookie.store']) {
    assert.equal(connection.normalize(` ${url}/ `), url)
  }
})

test('uses canonical production and migrates previous production origins', () => {
  const { connection } = harness()
  assert.equal(connection.defaultUrl, 'https://app.comcookie.store')
  assert.equal(connection.normalize('https://ogni-frontend.vercel.app'), connection.defaultUrl)
  assert.equal(connection.normalize('https://satorna-wb.vercel.app'), connection.defaultUrl)
  assert.equal(connection.normalize('https://comcookie.store'), connection.defaultUrl)
})

test('canonical production page has the collector bridge and receives legacy-token snapshots', async () => {
  const manifest = JSON.parse(await readFile(new URL('../manifest.json', import.meta.url), 'utf8'))
  assert.ok(manifest.content_scripts.some(script => script.matches.includes('https://app.comcookie.store/*')
    && script.js.includes('src/satorna-bridge.js')))
  const { context, calls } = harness()
  await vm.runInContext('postSnapshot({ orders: [] })', context)
  assert.equal(calls.length, 1)
  assert.equal(calls[0].url, 'https://app.comcookie.store/api/v1/avito/orders/browser-snapshot')
  assert.equal(calls[0].options.headers.Authorization, 'Bearer synthetic-legacy')
})

test('rejects unsafe or ambiguous server addresses', () => {
  const { connection } = harness()
  for (const url of ['', 'javascript:alert(1)', 'http://example.com', 'https://user:pass@example.com', 'https://example.com/avito/orders', 'https://example.com/?token=x', 'https://example.com/#x', 'http://localhost.evil.test']) {
    assert.throws(() => connection.normalize(url))
  }
})

test('legacy configuration is preserved until explicit save, then connection stays local', async () => {
  const { connection, sync, local } = harness()
  assert.equal((await connection.read()).accessToken, 'synthetic-legacy')
  await connection.save('http://127.0.0.1:5177/', 'synthetic-local')
  assert.equal((await connection.read()).backendUrl, 'http://127.0.0.1:5177')
  assert.equal(local.connection.accessToken, 'synthetic-local')
  assert.equal(sync.accessToken, undefined)
})

test('snapshot goes only to loopback, keeps photo metadata, forbids redirects and cookies', async () => {
  const { connection, context, calls } = harness()
  await connection.save('http://127.0.0.1:5177', 'synthetic-local')
  const payload = { orders: [{ orderId: 'synthetic', items: [{ imageUrl: 'https://70.img.avito.st/synthetic.jpg', size: 'M', color: 'синий', sellerArticle: 'TEST' }] }] }
  context.payload = payload
  await vm.runInContext('postSnapshot(payload)', context)
  assert.equal(calls.length, 1)
  assert.equal(calls[0].url, 'http://127.0.0.1:5177/api/v1/avito/orders/browser-snapshot')
  assert.equal(calls[0].options.headers.Authorization, 'Bearer synthetic-local')
  assert.equal(calls[0].options.redirect, 'error')
  assert.equal(calls[0].options.credentials, 'omit')
  assert.deepEqual(JSON.parse(calls[0].options.body), payload)
})

test('failed localhost delivery never falls back to hosted Satorna', async () => {
  const { connection, context, calls } = harness()
  await connection.save('http://127.0.0.1:5177', 'synthetic-local')
  context.fetch = async (url) => { calls.push(url); throw new Error('offline') }
  await assert.rejects(vm.runInContext('postSnapshot({ orders: [] })', context), /offline/)
  assert.deepEqual(calls, ['http://127.0.0.1:5177/api/v1/avito/orders/browser-snapshot'])
})

test('collector shows upload failure or confirmed save, not premature success', async () => {
  const { connection, context } = harness()
  await connection.save('http://127.0.0.1:5177', 'synthetic-local')
  const notifications = []
  context.chrome.tabs = {
    query: (_query, done) => done([{ id: 1 }]),
    sendMessage: (_id, message, _options, done) => { notifications.push(message); done({ ok: true }) },
  }
  await vm.runInContext('postSnapshotWithFeedback({ orders: [] })', context)
  assert.equal(notifications[0].ok, true)
  context.fetch = async () => { throw new Error('offline') }
  await assert.rejects(vm.runInContext('postSnapshotWithFeedback({ orders: [] })', context), /offline/)
  assert.equal(notifications[1].ok, false)
  assert.match(notifications[1].message, /127.0.0.1:5177/)
})

function popupHarness() {
  const state = harness()
  const { context } = state
  const elements = new Map()
  const messages = []
  context.document = { body: { dataset: {} }, getElementById(id) {
    if (!elements.has(id)) elements.set(id, {
      value: '', textContent: '', classList: { toggle() {}, add() {}, remove() {} }, handlers: {},
      addEventListener(type, handler) { this.handlers[type] = handler },
      dispatchEvent(event) { this.handlers[event.type]?.(event) }, focus() {},
    })
    return elements.get(id)
  } }
  context.window = { clearTimeout() {}, setTimeout() {} }
  context.Event = class { constructor(type) { this.type = type } }
  context.chrome.runtime.sendMessage = async (message) => { messages.push(message); return { ok: true, logs: [] } }
  context.chrome.storage.onChanged = { addListener() {} }
  vm.runInContext(popup, context)
  return { ...state, elements, messages }
}

test('popup clears token when switching server, requires explicit connection, and persists locally', async () => {
  const { context, local, elements, messages } = popupHarness()
  await vm.runInContext('loadSettings()', context)
  assert.equal(elements.get('accessToken').value, 'synthetic-legacy')
  elements.get('localServerBtn').handlers.click()
  assert.equal(elements.get('backendUrl').value, 'http://127.0.0.1:5177')
  assert.equal(elements.get('accessToken').value, '')
  elements.get('accessToken').value = 'synthetic-local'
  elements.get('accessToken').handlers.input()
  await assert.rejects(vm.runInContext('collectOrders()', context), /сохраните/)
  assert.equal(messages.some(m => m.type === 'AVITO_ORDERS_OPEN_AND_COLLECT'), false)
  await vm.runInContext('connectManually()', context)
  assert.equal(local.connection.accessToken, 'synthetic-local')
  await vm.runInContext('loadSettings()', context)
  assert.equal(elements.get('backendUrl').value, 'http://127.0.0.1:5177')
  assert.equal(elements.get('accessToken').value, 'synthetic-local')
})

function code(payload) {
  return `satorna-connect-v1.${Buffer.from(JSON.stringify(payload)).toString('base64url')}`
}
const rawToken = 'sat_avito_SYNTHETIC_ONLY_1234567890'

test('decodes the versioned code, trims only its envelope and keeps the local port', () => {
  const { connection } = harness()
  const result = connection.decodeCode(` \n${code({ v: 1, origin: 'http://127.0.0.1:5177', token: rawToken })}\t`)
  assert.equal(result.backendUrl, 'http://127.0.0.1:5177')
  assert.equal(result.accessToken, rawToken)
})

test('rejects malformed, oversized, unsupported and secret-bearing invalid values safely', () => {
  const { connection } = harness()
  for (const value of ['', 'sat_avito_raw_token', 'satorna-connect-v2.e30', 'satorna-connect-v1.=',
    'satorna-connect-v1.a', 'satorna-connect-v1.' + 'x'.repeat(13000),
    'satorna-connect-v1.' + Buffer.from('not JSON: ' + rawToken).toString('base64url'),
    'satorna-connect-v1._w', code(null), code([]), code({ v: 2 }),
    code({ v: 1, origin: 'https://app.comcookie.store', token: 42 }),
    code({ v: 1, origin: 'https://app.comcookie.store', token: 'Bearer ' + rawToken }),
    code({ v: 1, origin: 'https://app.comcookie.store', token: 'sat_avito_' + 'x'.repeat(4096) }),
  ]) {
    assert.throws(() => connection.decodeCode(value), error => !error.message.includes(rawToken))
  }
})

test('new codes reject all non-origin URL components and HTTP aliases', () => {
  const { connection } = harness()
  for (const origin of ['https://x.test/a/..', 'https://x.test//', 'https://x.test/?', 'https://x.test/#',
    'https://@x.test', 'https://user:password@x.test', 'https://x.test\\@evil.test',
    'http://127.1:5177', 'http://2130706433:5177', 'http://localhost.evil.test', 'http://remote.test',
    'javascript:alert(1)', 'https://x.test/?token=' + rawToken,
  ]) assert.throws(() => connection.decodeCode(code({ v: 1, origin, token: rawToken })))
})

test('code-pinned retired frontend origin never migrates or runs backend discovery', async () => {
  const { connection, context, calls } = harness()
  const decoded = connection.decodeCode(code({ v: 1, origin: 'https://satorna-wb.vercel.app', token: rawToken }))
  await connection.save(decoded.backendUrl, decoded.accessToken, { pinOrigin: true })
  await vm.runInContext('postSnapshot({ orders: [] })', context)
  assert.equal(calls.length, 1)
  assert.equal(calls[0].url, 'https://satorna-wb.vercel.app/api/v1/avito/orders/browser-snapshot')
  assert.equal(calls[0].options.headers.Authorization, `Bearer ${rawToken}`)
})

test('unknown destination gets no permission request or token until separate confirmation', async () => {
  const { context, local, elements, calls, permissionCalls } = popupHarness()
  await vm.runInContext('loadSettings()', context)
  elements.get('connectionCode').value = code({ v: 1, origin: 'https://unknown.test:8443', token: rawToken })
  await vm.runInContext('connectFromCode()', context)
  assert.equal(calls.length, 0)
  assert.equal(permissionCalls.length, 0)
  assert.equal(local.connection, undefined)
  assert.equal(elements.get('confirmationDestination').textContent, 'https://unknown.test:8443')
  assert.equal(elements.get('destinationConfirmation').hidden, false)
  await vm.runInContext('finishConnection(pendingConnection)', context)
  assert.equal(calls.length, 1)
  assert.equal(calls[0].url, 'https://unknown.test:8443/api/v1/avito/orders/browser-collection-context')
  assert.equal(calls[0].options.headers.Authorization, `Bearer ${rawToken}`)
  assert.equal(calls[0].options.redirect, 'error')
  assert.equal(calls[0].options.credentials, 'omit')
  assert.equal(permissionCalls[0].origins[0], 'https://unknown.test:8443/*')
  assert.equal(local.connection.accessToken, rawToken)
  assert.equal(local.connection.originPinned, true)
  assert.equal(elements.get('connectionCode').value, '')
  assert.equal(elements.get('connectionStatus').textContent, 'Подключение проверено')
})

test('invalid code and failed verification never overwrite an existing connection or expose secrets', async () => {
  const { connection, context, local, elements, calls } = popupHarness()
  await connection.save('http://localhost:5177', 'synthetic-working', { pinOrigin: true })
  await vm.runInContext('loadSettings()', context)
  const before = JSON.stringify(local.connection)
  elements.get('connectionCode').value = 'invalid-' + rawToken
  assert.throws(() => vm.runInContext('connectFromCode()', context))
  assert.equal(calls.length, 0)
  for (const status of [401, 403, 500, 302]) {
    context.fetch = async () => ({ ok: false, status, text: async () => rawToken })
    elements.get('connectionCode').value = code({ v: 1, origin: 'http://127.0.0.1:5177', token: rawToken })
    await assert.rejects(vm.runInContext('connectFromCode()', context), error => !error.message.includes(rawToken))
    assert.equal(JSON.stringify(local.connection), before)
  }
  context.fetch = async () => { throw new Error('network ' + rawToken) }
  await assert.rejects(vm.runInContext('connectFromCode()', context), error => !error.message.includes(rawToken))
  assert.equal(JSON.stringify(local.connection), before)
})

test('older-server 404 saves explicitly unverified configuration; no alternate request is made', async () => {
  const { context, local, elements } = popupHarness()
  await vm.runInContext('loadSettings()', context)
  context.fetch = async () => ({ ok: false, status: 404 })
  elements.get('connectionCode').value = code({ v: 1, origin: 'http://127.0.0.1:5177', token: rawToken })
  await vm.runInContext('connectFromCode()', context)
  assert.equal(local.connection.accessToken, rawToken)
  assert.match(elements.get('connectionStatus').textContent, /Настройки сохранены/)
  assert.doesNotMatch(elements.get('connectionStatus').textContent, /Подключение проверено/)
})

test('raw token without an explicit address cannot select a default destination', async () => {
  const { connection, context, sync, calls } = harness()
  delete sync.backendUrl
  assert.equal((await connection.read()).backendUrl, '')
  await assert.rejects(vm.runInContext('postSnapshot({ orders: [] })', context), /явный адрес/)
  assert.equal(calls.length, 0)
})

test('saving collection options does not discard a pinned connection or persist a code', async () => {
  const { connection, context, local, sync, elements } = popupHarness()
  await connection.save('https://satorna-wb.vercel.app', rawToken, { pinOrigin: true })
  await vm.runInContext('loadSettings()', context)
  const before = JSON.stringify(local.connection)
  elements.get('connectionCode').value = code({ v: 1, origin: 'https://other.test', token: rawToken })
  await vm.runInContext('saveSettings()', context)
  assert.equal(JSON.stringify(local.connection), before)
  assert.ok(sync.collectOptions)
  assert.equal(JSON.stringify(sync).includes('satorna-connect-v1.'), false)
})

test('denied host access does not transmit a token or replace saved credentials', async () => {
  const { connection, context, local, elements, calls } = popupHarness()
  await connection.save('http://localhost:5177', 'synthetic-working', { pinOrigin: true })
  await vm.runInContext('loadSettings()', context)
  const before = JSON.stringify(local.connection)
  context.chrome.permissions.request = async () => false
  elements.get('connectionCode').value = code({ v: 1, origin: 'http://127.0.0.1:5177', token: rawToken })
  await assert.rejects(vm.runInContext('connectFromCode()', context), /не разрешён/)
  assert.equal(calls.length, 0)
  assert.equal(JSON.stringify(local.connection), before)
})

test('editing a pending unknown destination invalidates its confirmation before any request', async () => {
  const { context, elements, calls, permissionCalls } = popupHarness()
  await vm.runInContext('loadSettings()', context)
  elements.get('connectionCode').value = code({ v: 1, origin: 'https://unknown.test', token: rawToken })
  await vm.runInContext('connectFromCode()', context)
  elements.get('connectionCode').value = 'invalid replacement'
  elements.get('connectionCode').handlers.input()
  elements.get('confirmDestinationBtn').handlers.click()
  assert.equal(elements.get('destinationConfirmation').hidden, true)
  assert.equal(calls.length, 0)
  assert.equal(permissionCalls.length, 0)
})

test('unexpected successful response does not confirm authentication or replace credentials', async () => {
  const { connection, context, local, elements } = popupHarness()
  await connection.save('http://localhost:5177', 'synthetic-working', { pinOrigin: true })
  await vm.runInContext('loadSettings()', context)
  const before = JSON.stringify(local.connection)
  elements.get('connectionCode').value = code({ v: 1, origin: 'http://127.0.0.1:5177', token: rawToken })
  for (const json of [async () => ({ ok: true }), async () => { throw new Error(rawToken) }]) {
    context.fetch = async () => ({ ok: true, status: 200, json })
    await assert.rejects(vm.runInContext('connectFromCode()', context), error => !error.message.includes(rawToken))
    assert.equal(JSON.stringify(local.connection), before)
  }
})

test('editing during verification cannot apply an obsolete connection attempt', async () => {
  const { connection, context, local, elements } = popupHarness()
  await connection.save('http://localhost:5177', 'synthetic-working', { pinOrigin: true })
  await vm.runInContext('loadSettings()', context)
  const before = JSON.stringify(local.connection)
  let respond
  context.fetch = () => new Promise(resolve => { respond = resolve })
  elements.get('connectionCode').value = code({ v: 1, origin: 'http://127.0.0.1:5177', token: rawToken })
  const attempt = vm.runInContext('connectFromCode()', context)
  await new Promise(resolve => setImmediate(resolve))
  elements.get('connectionCode').handlers.input()
  respond({ ok: true, status: 200, json: async () => ({ orders: [] }) })
  await attempt
  assert.equal(JSON.stringify(local.connection), before)
})
