import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const empty = { status: 'disconnected', totalOffers: 0, observedOffers: 0, accepted: 0, ignored: 0, unmatched: 0, partialSellers: 0 }
async function load(initial = empty, failCommand = false, permission = true) {
  let state = initial
  const elements = new Map(); const sent = []; const permissionRequests = []
  const element = (id) => {
    if (!elements.has(id)) elements.set(id, { value: '', textContent: '', disabled: false, hidden: false, handlers: {}, addEventListener(name, fn) { this.handlers[name] = fn } })
    return elements.get(id)
  }
  const context = vm.createContext({ Date, Number, URL, atob, TextDecoder, Uint8Array, document: { getElementById: element }, setInterval: () => 1,
    chrome: { permissions: { request: async value => { permissionRequests.push(value); return permission } }, runtime: { sendMessage: async (message) => {
      sent.push(message)
      if (failCommand && message.type !== 'status') throw new Error('private transport detail')
      if (message.type === 'connect') state = { ...empty, status: 'ready', totalOffers: 4 }
      return { ok: true, state, forbiddenEcho: '<script>secret</script>' }
    } } } })
  const path = new URL('../src/popup.js', import.meta.url)
  assert.ok(existsSync(path), 'popup is implemented')
  vm.runInContext(readFileSync(new URL('../src/connection.js', import.meta.url), 'utf8'), context)
  vm.runInContext(readFileSync(path, 'utf8'), context)
  await new Promise((resolve) => setImmediate(resolve))
  return { element, sent, permissionRequests }
}

test('a pasted token alone never presents connected state, and verified connect clears its input', async () => {
  const h = await load()
  h.element('token').value = `sat_wbp_${'a'.repeat(43)}`
  assert.equal(h.element('start').disabled, true)
  assert.match(h.element('status').textContent, /Не подключено/)
  await h.element('connect-form').handlers.submit({ preventDefault() {} })
  assert.match(h.element('status').textContent, /Каталог проверен/)
  assert.equal(h.element('start').disabled, false)
  assert.equal(h.element('token').value, '')
})

test('partial coverage and server acknowledgements remain distinct and unknown error text is never rendered', async () => {
  const h = await load({ ...empty, status: 'complete', totalOffers: 5, observedOffers: 2, accepted: 1, ignored: 1,
    reason: '<script>secret</script>', lastAckAt: '2026-09-16T09:00:00Z' })
  assert.equal(h.element('coverage').textContent, '2 / 5')
  assert.equal(h.element('missing').textContent, '3')
  assert.equal(h.element('accepted').textContent, '1')
  assert.equal(h.element('ignored').textContent, '1')
  assert.equal(h.element('reason').textContent.includes('secret'), false)
})

test('completed partial pass has no automatic repeat and preserves honest coverage', async () => {
  const h = await load({ ...empty, status: 'waiting', totalOffers: 5, observedOffers: 2, reason: 'partial_coverage', nextRunAt: null })
  assert.equal(h.element('stop').disabled, true)
  assert.match(h.element('status').textContent, /завершён частично/)
  assert.match(h.element('next-run').textContent, /второго круга нет/)
  assert.equal(h.element('coverage').textContent, '2 / 5')
})

test('command transport failure remains visible after busy state clears, without raw details', async () => {
  const h = await load(empty, true)
  h.element('token').value = `sat_wbp_${'a'.repeat(43)}`
  await h.element('connect-form').handlers.submit({ preventDefault() {} })
  assert.match(h.element('reason').textContent, /Не удалось проверить/)
  assert.equal(h.element('reason').textContent.includes('private'), false)
})

test('paused collection exposes its WB tab through an explicit button', async () => {
  const h = await load({ ...empty, status: 'paused', reason: 'challenge', collectorTabAvailable: true })
  assert.equal(h.element('show-tab').disabled, false)
  await h.element('show-tab').handlers.click()
  assert.equal(h.sent.at(-1).type, 'show_tab')
})

test('encoded custom destination requires confirmation, then exactly its permission and raw bearer', async () => {
  const h = await load({ ...empty, backendUrl: 'https://saved.example' })
  const token = `sat_wbp_${'a'.repeat(43)}`
  h.element('token').value = 'satorna-connect-v1.' + Buffer.from(JSON.stringify({ v: 1, origin: 'https://new.example', token })).toString('base64url')
  await h.element('connect-form').handlers.submit({ preventDefault() {} })
  assert.equal(h.permissionRequests.length, 0)
  assert.equal(h.sent.filter(item => item.type === 'connect').length, 0)
  assert.equal(h.element('destination-confirmation').hidden, false)
  assert.equal(h.element('destination-origin').textContent, 'https://new.example')
  await h.element('confirm-destination').handlers.click()
  assert.equal(h.permissionRequests[0].origins[0], 'https://new.example/*')
  assert.equal(h.sent.at(-1).backendUrl, 'https://new.example')
  assert.equal(h.sent.at(-1).token, token)
  assert.equal(h.element('token').value, '')
})

test('denied permission never sends the token and leaves a clear error', async () => {
  const h = await load(empty, false, false)
  h.element('token').value = `sat_wbp_${'a'.repeat(43)}`
  await h.element('connect-form').handlers.submit({ preventDefault() {} })
  assert.equal(h.sent.filter(message => message.type === 'connect').length, 0)
  assert.match(h.element('reason').textContent, /Разрешите/)
  assert.equal(h.element('connect').disabled, false)
})

test('only one credential input exists, and editing is disabled during active collection', async () => {
  const h = await load({ ...empty, status: 'running', backendUrl: 'https://saved.example' })
  assert.equal(h.element('token').disabled, true)
  const html = readFileSync(new URL('../src/popup.html', import.meta.url), 'utf8')
  assert.equal((html.match(/<input\b/g) || []).length, 1)
  assert.equal(html.includes('id="backend-url"'), false)
  assert.equal(html.includes('Адрес сервера Satorna'), false)
})

test('a local issued key preserves its port and never falls back to production', async () => {
  const h = await load()
  const token = `sat_wbp_${'a'.repeat(43)}`
  h.element('token').value = 'satorna-connect-v1.' + Buffer.from(JSON.stringify({ v: 1, origin: 'http://127.0.0.1:5177', token })).toString('base64url')
  await h.element('connect-form').handlers.submit({ preventDefault() {} })
  assert.equal(h.sent.at(-1).backendUrl, 'http://127.0.0.1:5177')
  assert.equal(h.permissionRequests[0].origins[0], 'http://127.0.0.1:5177/*')
})

test('cancelling an unknown destination never sends the key or requests access', async () => {
  const h = await load()
  h.element('token').value = 'satorna-connect-v1.' + Buffer.from(JSON.stringify({ v: 1, origin: 'https://unknown.example', token: `sat_wbp_${'a'.repeat(43)}` })).toString('base64url')
  await h.element('connect-form').handlers.submit({ preventDefault() {} })
  await h.element('cancel-destination').handlers.click()
  await h.element('confirm-destination').handlers.click()
  assert.equal(h.permissionRequests.length, 0)
  assert.equal(h.sent.filter(item => item.type === 'connect').length, 0)
})
