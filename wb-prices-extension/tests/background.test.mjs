import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const TOKEN = `sat_wbp_${'a'.repeat(43)}`
const API = 'https://api.elfprint-system.ru/api/v1/wb/browser-prices'
const clone = (value) => JSON.parse(JSON.stringify(value))
const card = (id) => `https://www.wildberries.ru/catalog/${id}/detail.aspx`
const seller = 'https://www.wildberries.ru/seller/4405572'
const offer = (nmId, sizeId = 123) => ({ nmId, sizeId, sellerPriceKopecks: 170000, sellerPriceObservedAt: new Date().toISOString() })

function harness({ offers = [offer(1025784485)], postStatuses = [], catalogStatus = 200, catalogStatuses = [], refreshStatus = 200, publicStatus = 403, publicStatuses = [], local = {}, session = {}, existingTabs = [], postGate = null, publicGate = null, catalogGate = null, closeGate = null, productionEnvelope = false, baseline = false, clock = null, progress = null, serverUrl = 'https://api.elfprint-system.ru', expectedToken = TOKEN, permission = true } = {}) {
  const selectedApi = `${serverUrl}/api/v1/wb/browser-prices`
  const calls = []; const publicCalls = []; const access = []; const timers = new Map(); let timerId = 0
  const tabs = new Map([[7, { id: 7, url: 'https://example.test', active: true }], ...existingTabs.map((tab) => [tab.id, clone(tab)])])
  const alarms = new Map()
  const navigations = []; const scrolls = []; const removed = []
  let listener; let removedListener; let alarmListener; let startupListener; let nextTab = 100; let revision = 'revision-1'
  const area = (values, name) => ({
    setAccessLevel: async (level) => access.push([name, level.accessLevel]),
    get: async (key) => typeof key === 'string' ? { [key]: clone(values[key] ?? null) } : clone(values),
    set: async (value) => { access.push([name, 'set']); Object.assign(values, clone(value)) },
    remove: async (key) => { delete values[key] },
  })
  const chrome = {
    permissions: { contains: async () => permission },
    storage: { local: area(local, 'local'), session: area(session, 'session') },
    runtime: { id: 'ext', getURL: (path) => `chrome-extension://ext/${path}`, onMessage: { addListener: (fn) => { listener = fn } }, onStartup: { addListener: (fn) => { startupListener = fn } } },
    alarms: {
      create: async (name, options) => alarms.set(name, clone(options)),
      clear: async (name) => alarms.delete(name), get: async (name) => alarms.get(name),
      onAlarm: { addListener: (fn) => { alarmListener = fn } },
    },
    tabs: {
      create: async (data) => { const tab = { id: nextTab++, ...data }; tabs.set(tab.id, tab); navigations.push(clone(tab)); return clone(tab) },
      get: async (id) => { if (id === 100 && closeGate) { const gate = closeGate; closeGate = null; gate.started(); await gate.wait }
        if (!tabs.has(id)) throw new Error('not found'); return clone(tabs.get(id)) },
      update: async (id, data) => { assert.notEqual(id, 7, 'user tab is preserved'); Object.assign(tabs.get(id), data); navigations.push({ id, ...data }); return clone(tabs.get(id)) },
      remove: async (id) => { assert.notEqual(id, 7); removed.push(id); tabs.delete(id) },
      sendMessage: async (id, data) => { scrolls.push({ id, ...data }); return data.type === 'catalog_progress' && progress ? clone(progress) : { ok: true } },
      onRemoved: { addListener: (fn) => { removedListener = fn } },
    },
  }
  const fetch = async (url, options) => {
    if (clock) clock.now += url.includes('/catalog?') ? 5 : url.includes('/snapshots') ? 15 : 20
    if (url.startsWith('https://card.wb.ru/cards/v4/detail?')) {
      publicCalls.push({ url, ...clone({ ...options, signal: undefined }) })
      if (publicGate) { const gate = publicGate; publicGate = null; gate.started(); await gate.wait }
      assert.equal(options.credentials, 'omit')
      assert.equal(options.redirect, 'error')
      assert.equal(options.headers.Authorization, undefined)
      const requested = new URL(url).searchParams.get('nm').split(';').map(Number)
      const products = requested.map((nmId) => {
        const item = offers.find((candidate) => candidate.nmId === nmId)
        return item ? { id: nmId, supplierId: 4405572,
          sizes: [{ optionId: item.sizeId, price: { basic: 190000, product: 130800, wallet: 0 } }] } : null
      }).filter(Boolean)
      const status = publicStatuses.shift() ?? publicStatus
      return { ok: status === 200, status, headers: new Headers({ 'Retry-After': '90' }), json: async () => ({ products }) }
    }
    calls.push({ url, ...clone({ ...options, signal: undefined }) })
    assert.ok(url.startsWith(`${selectedApi}/`), 'requests stay on the explicitly selected CRM API')
    assert.equal(options.credentials, 'omit')
    assert.equal(options.redirect, 'error')
    assert.equal(options.headers.Authorization, `Bearer ${expectedToken}`)
    const errorBody = (code) => productionEnvelope ? { error: { code, message: 'never-render-this', details: {} } }
      : { detail: { code, retryAfterSeconds: 60, unsafe: 'never-render-this' } }
    if (url.endsWith('/catalog/refresh')) {
      assert.equal(options.method, 'POST')
      assert.equal(options.body, undefined)
      if (refreshStatus === 200) revision = 'revision-2'
      return { ok: refreshStatus === 200, status: refreshStatus, headers: new Headers({ 'Retry-After': '60' }), json: async () => refreshStatus === 200
        ? { ok: true, goodsRevision: revision, total: offers.length }
        : errorBody(refreshStatus === 429 ? 'WB_BROWSER_REFRESH_COOLDOWN' : refreshStatus === 409 ? 'WB_BROWSER_REFRESH_BUSY' : 'WB_BROWSER_REFRESH_UNAVAILABLE') }
    }
    if (url.includes('/catalog?')) {
      if (catalogGate) { const gate = catalogGate; catalogGate = null; gate.started(); await gate.wait }
      const page = Number(new URL(url).searchParams.get('page'))
      const status = catalogStatuses.shift() ?? catalogStatus
      return { ok: status === 200, status, json: async () => status === 409
        ? errorBody('WB_BROWSER_GOODS_STALE')
        : ({ marketplaceAccountId: 2, goodsRevision: revision, page, pageSize: 100, total: offers.length, maxAgeSeconds: 1800, items: offers.slice((page - 1) * 100, page * 100) }) }
    }
    assert.equal(url, `${selectedApi}/snapshots`)
    if (postGate) { const gate = postGate; postGate = null; gate.started(); await gate.wait }
    const status = postStatuses.shift() ?? 200
    if (status === 'network') throw new TypeError('synthetic network interruption')
    if (status === 409) revision = 'revision-2'
    const body = JSON.parse(options.body)
    return { ok: status === 200, status, json: async () => ({ ok: true, accepted: body.items.length, ignored: 0, observedAt: new Date().toISOString(), forbiddenEcho: 'must-not-render' }) }
  }
  const TestDate = clock ? class extends Date { static now() { return clock.now } } : Date
  const context = vm.createContext({ chrome, fetch, URL, URLSearchParams, Date: TestDate, Object, Number, Promise, AbortSignal,
    setTimeout: (fn, delay) => { timers.set(++timerId, { fn, delay }); return timerId }, clearTimeout: (id) => timers.delete(id) })
  context.importScripts = (path) => vm.runInContext(readFileSync(new URL(`../src/${path}`, import.meta.url), 'utf8'), context)
  const path = new URL(baseline ? '../dist/src/background.js' : '../src/background.js', import.meta.url)
  assert.ok(existsSync(path), 'background collector is implemented')
  vm.runInContext(readFileSync(path, 'utf8'), context)
  const send = (message, sender) => new Promise((resolve) => { assert.equal(listener(message, sender, resolve), true) })
  const popup = (message) => send(message, { id: 'ext', url: 'chrome-extension://ext/src/popup.html' })
  const page = (message, overrides = {}) => {
    const tab = [...tabs.values()].find((tab) => tab.id !== 7)
    return send({ source: 'satorna-wb-prices-v1', type: 'observations', ...message }, { id: 'ext', frameId: 0, url: tab?.url, tab: clone(tab ?? {}), ...overrides })
  }
  const fire = (delay) => { const entry = [...timers].find(([, timer]) => timer.delay === delay); assert.ok(entry); timers.delete(entry[0]); return entry[1].fn() }
  return { popup, page, calls, publicCalls, local, session, access, tabs, navigations, scrolls, removed, timers, alarms,
    closeOwned: () => removedListener(100),
    fire, tick: async (delay) => { await fire(delay); await popup({ type: 'status' }) },
    alarm: async () => { assert.equal(typeof alarmListener, 'function'); await alarmListener({ name: 'wb-price-collector', scheduledTime: Date.now() }); return popup({ type: 'status' }) },
    startup: async () => { assert.equal(typeof startupListener, 'function'); await startupListener(); return popup({ type: 'status' }) },
  }
}

function gate() {
  let release; let started
  const wait = new Promise((resolve) => { release = resolve })
  const entered = new Promise((resolve) => { started = resolve })
  return { wait, release, started, entered }
}

test('explicit server switch forgets old token/progress and all calls use the new connection', async () => {
  const newToken = `sat_wbp_${'b'.repeat(43)}`
  const h = harness({ serverUrl: 'https://new-satorna.example', expectedToken: newToken,
    local: { token: TOKEN, checkpoint: { accepted: 99, seen: { old: {} } } } })
  const result = await h.popup({ type: 'connect', backendUrl: 'https://new-satorna.example/', token: newToken })
  assert.equal(result.ok, true)
  assert.equal(result.state.backendUrl, 'https://new-satorna.example')
  assert.equal(h.local.token, newToken)
  assert.equal(h.local.backendUrl, 'https://new-satorna.example')
  assert.equal(h.local.checkpoint.accepted, 0)
  assert.deepEqual(h.local.checkpoint.seen, {})
  assert.equal(JSON.stringify(result).includes(newToken), false)
  assert.equal(h.calls.length, 1)
  const restarted = harness({ local: clone(h.local), serverUrl: 'https://new-satorna.example', expectedToken: newToken })
  assert.equal((await restarted.popup({ type: 'status' })).state.backendUrl, 'https://new-satorna.example')
  assert.equal((await restarted.popup({ type: 'connect' })).ok, true)
})

test('server switch never reuses the old token and a rejected candidate preserves the old binding and checkpoint', async () => {
  const h = harness({ serverUrl: 'https://new-satorna.example', catalogStatus: 401,
    local: { token: TOKEN, backendUrl: 'https://api.elfprint-system.ru', control: { enabled: false, reason: 'user_stopped' },
      checkpoint: { accepted: 99, seen: { old: {} } } } })
  assert.equal((await h.popup({ type: 'status' })).state.accepted, 99)
  assert.equal((await h.popup({ type: 'connect', backendUrl: 'https://new-satorna.example' })).code, 'token_invalid')
  assert.equal(h.calls.length, 0)
  const before = JSON.stringify(h.local)
  const failed = await h.popup({ type: 'connect', backendUrl: 'https://new-satorna.example', token: TOKEN })
  assert.equal(failed.code, 'connection_rejected')
  assert.equal(JSON.stringify(h.local), before)
  assert.equal(failed.state.backendUrl, 'https://api.elfprint-system.ru')
})

test('failed replacement on the same server never removes the verified key, checkpoint or account', async () => {
  const candidate = `sat_wbp_${'b'.repeat(43)}`
  for (const status of [401, 403, 503]) {
    const h = harness({ catalogStatus: status, expectedToken: candidate,
      local: { token: TOKEN, backendUrl: 'https://api.elfprint-system.ru', control: { enabled: false, reason: 'user_stopped' },
        checkpoint: { accepted: 75, seen: { old: { time: 1 } }, binding: { account: 2, revision: 'old' } } } })
    await h.popup({ type: 'status' })
    const before = JSON.stringify(h.local), beforeState = JSON.stringify(h.session.state)
    const result = await h.popup({ type: 'connect', token: candidate })
    assert.equal(result.ok, false)
    assert.equal(JSON.stringify(h.local), before)
    assert.equal(JSON.stringify(h.session.state), beforeState)
    assert.equal(h.calls.length, 1)
    assert.equal(h.calls[0].method, 'GET')
    assert.equal(JSON.stringify(result).includes(candidate), false)
  }
})

test('Stop during connection cleanup cannot replace the token or resume collection', async () => {
  const cleanup = gate(), candidate = `sat_wbp_${'b'.repeat(43)}`
  const h = harness({ closeGate: cleanup, expectedToken: candidate, local: { token: TOKEN },
    existingTabs: [{ id: 100, url: card(1025784485) }],
    session: { state: { status: 'ready', tabId: 100, pageUrl: card(1025784485), accepted: 99 } } })
  const connect = h.popup({ type: 'connect', token: candidate })
  await cleanup.entered
  const stop = h.popup({ type: 'stop' })
  cleanup.release()
  assert.equal((await connect).code, 'stopped')
  await stop
  assert.equal(h.local.token, TOKEN)
  const state = (await h.popup({ type: 'status' })).state
  assert.equal(state.reason, 'user_stopped')
  assert.equal(state.accepted, 99)
  assert.equal(h.calls.some(call => call.method === 'POST'), false)
})

test('malformed server and missing origin permission send no credential or request', async () => {
  for (const url of ['http://remote.example', 'https://user:password@example.org', 'https://example.org/api',
    'https://example.org/?q=1', 'https://example.org/#section', 'file:///tmp/test']) {
    const h = harness()
    assert.equal((await h.popup({ type: 'connect', backendUrl: url, token: TOKEN })).code, 'server_invalid')
    assert.equal(h.calls.length, 0)
  }
  const h = harness({ permission: false })
  assert.equal((await h.popup({ type: 'connect', token: TOKEN })).code, 'server_permission_denied')
  assert.equal(h.calls.length, 0)
  const badStored = harness({ local: { token: TOKEN, backendUrl: 'https://user:password@example.org' } })
  assert.equal((await badStored.popup({ type: 'status' })).state.reason, 'server_invalid')
  assert.equal(badStored.local.token, undefined)
  assert.equal(badStored.calls.length, 0)
})

test('3522 products: one start batches whole catalog; comparable fresh repeat avoids 36 requests/posts', async (t) => {
  const offers = Array.from({ length: 3522 }, (_, i) => offer(1000 + i, 5000 + i))
  const measure = async baseline => {
    const clock = { now: Date.now() }
    const h = harness({ offers, publicStatus: 200, baseline, clock })
    await h.popup({ type: 'connect', token: TOKEN })
    const began = clock.now
    const result = await h.popup({ type: 'start' })
    assert.equal(result.state.observedOffers, 3522)
    assert.equal(result.state.accepted, 3522)
    assert.equal(result.state.status, 'waiting')
    assert.equal(h.navigations.length, 0)
    const firstMs = clock.now - began
    await h.popup({ type: 'stop' })
    const repeatsBefore = h.publicCalls.length
    const repeatBegan = clock.now
    await h.popup({ type: 'start' })
    return { firstMs, firstRequests: repeatsBefore, repeatMs: clock.now - repeatBegan,
      repeatRequests: h.publicCalls.length - repeatsBefore }
  }
  const historicalPath = new URL('../dist/src/background.js', import.meta.url)
  const historicalAvailable = existsSync(historicalPath)
    && readFileSync(historicalPath, 'utf8').includes('state.seen = {}; state.visited = []; state.sellers = []; state.pendingSellers = []')
  const before = historicalAvailable ? await measure(true) : null
  const after = await measure(false)
  if (before) assert.equal(before.firstRequests, 36)
  assert.equal(after.firstRequests, 36)
  if (before) assert.equal(before.repeatRequests, 36)
  assert.equal(after.repeatRequests, 0)
  t.diagnostic(JSON.stringify({ controlledLatencyModelMs: { public: 20, snapshot: 15, catalog: 5 }, before, after,
    limitation: 'synthetic transport, not live WB throughput; initial full pass unchanged' }))
})

test('3522 products: interrupted second snapshot re-observes unsaved batch automatically and finishes', async () => {
  const offers = Array.from({ length: 3522 }, (_, i) => offer(1000 + i, 5000 + i))
  const clock = { now: Date.now() }
  const h = harness({ offers, publicStatus: 200, postStatuses: [200, 'network'], clock })
  await h.popup({ type: 'connect', token: TOKEN })
  const failed = await h.popup({ type: 'start' })
  assert.equal(failed.state.status, 'retrying')
  assert.equal(failed.state.accepted, 100)
  assert.equal(failed.state.observedOffers, 100)
  clock.now = failed.state.retryAt + 1
  await h.alarm()
  const finished = (await h.popup({ type: 'status' })).state
  assert.equal(finished.status, 'waiting')
  assert.equal(finished.accepted, 3522)
  assert.equal(finished.observedOffers, 3522)
  assert.equal(h.navigations.length, 0)
  const publicBatches = h.publicCalls.map(call => new URL(call.url).searchParams.get('nm').split(';'))
  assert.equal(publicBatches[2][0], publicBatches[1][0], 'new observation, not blind POST replay')
})

test('public 429 honors retry header, stops after finite retries, manual Stop never resumes', async () => {
  const clock = { now: Date.now() }
  const h = harness({ publicStatus: 429, clock })
  await h.popup({ type: 'connect', token: TOKEN })
  let current = (await h.popup({ type: 'start' })).state
  assert.equal(current.status, 'retrying')
  assert.ok(current.retryAt - clock.now >= 90000)
  for (let i = 0; i < 3; i++) {
    clock.now = current.retryAt + 1
    await h.alarm()
    current = (await h.popup({ type: 'status' })).state
  }
  assert.equal(current.status, 'paused')
  assert.equal(current.reason, 'retry_exhausted')
  const count = h.publicCalls.length
  clock.now += 86400000
  await h.alarm()
  assert.equal(h.publicCalls.length, count)
  const stopped = harness({ publicStatus: 429, clock: { now: Date.now() } })
  await stopped.popup({ type: 'connect', token: TOKEN }); await stopped.popup({ type: 'start' })
  await stopped.popup({ type: 'stop' }); await stopped.alarm()
  assert.equal(stopped.publicCalls.length, 1)
  assert.equal((await stopped.popup({ type: 'status' })).state.reason, 'user_stopped')
})

test('suspension during public batching resumes only remaining offers, no duplicate run', async () => {
  const offers = Array.from({ length: 201 }, (_, i) => offer(1000 + i, 5000 + i))
  const h = harness({ offers, publicStatus: 200 })
  await h.popup({ type: 'connect', token: TOKEN })
  const checkpoint = clone(h.session)
  checkpoint.state.status = 'running'; checkpoint.state.phase = 'public'
  checkpoint.state.seen = Object.fromEntries(offers.slice(0, 100).map(item =>
    [`${item.nmId}:${item.sizeId}`, { seller: item.sellerPriceKopecks, buyer: 130800, time: Date.now() }]))
  checkpoint.state.accepted = 100
  const restored = harness({ offers, publicStatus: 200, local: clone(h.local), session: checkpoint })
  const duplicate = await restored.popup({ type: 'start' })
  assert.equal(duplicate.code, 'stop_first')
  assert.equal(duplicate.state.status, 'running')
  await restored.alarm()
  assert.equal(restored.publicCalls.length, 2)
  assert.equal(new URL(restored.publicCalls[0].url).searchParams.get('nm').split(';')[0], '1100')
  assert.equal((await restored.popup({ type: 'status' })).state.accepted, 201)
})

test('browser restart retains acknowledged progress and retry delay without catalog/secrets in checkpoint', async () => {
  const offers = Array.from({ length: 201 }, (_, i) => offer(1000 + i, 5000 + i))
  const clock = { now: Date.now() }
  const h = harness({ offers, publicStatus: 200, postStatuses: [200, 503], clock })
  await h.popup({ type: 'connect', token: TOKEN }); await h.popup({ type: 'start' })
  assert.equal(JSON.stringify(h.local.checkpoint).includes(TOKEN), false)
  assert.equal(h.local.checkpoint.catalog, undefined)
  const reopened = harness({ offers, publicStatus: 200, local: clone(h.local), clock })
  await reopened.startup()
  assert.equal(reopened.publicCalls.length, 0, 'backoff survives browser restart')
  clock.now = h.session.state.retryAt + 1
  await reopened.alarm()
  assert.equal((await reopened.popup({ type: 'status' })).state.accepted, 201)
  assert.equal(new URL(reopened.publicCalls[0].url).searchParams.get('nm').split(';')[0], '1100')
})

test('completed pass ignores an old scheduled repeat even after browser restart', async () => {
  const clock = { now: Date.now() }
  const h = harness({ publicStatus: 200, clock })
  await h.popup({ type: 'connect', token: TOKEN }); await h.popup({ type: 'start' })
  const saved = clone(h.session)
  saved.state.nextRunAt = clock.now - 1
  const next = harness({ publicStatus: 200, catalogStatuses: [503, 200], local: clone(h.local), session: saved, clock })
  await next.alarm()
  const state = (await next.popup({ type: 'status' })).state
  assert.equal(state.status, 'waiting')
  assert.equal(state.accepted, 1)
  assert.equal(next.publicCalls.length, 0)
  assert.equal(next.calls.length, 0)
  assert.equal(state.nextRunAt, null)
  const reopened = harness({ local: clone(next.local), session: {}, clock })
  await reopened.alarm()
  assert.equal(reopened.calls.length, 0)
  assert.equal((await reopened.popup({ type: 'status' })).state.status, 'waiting')
})

test('unknown refresh POST remains read-only across recovery GET failures', async () => {
  const clock = { now: Date.now() }
  const seed = harness()
  await seed.popup({ type: 'connect', token: TOKEN })
  const saved = clone(seed.session)
  saved.state.status = 'running'
  saved.state.pendingRequest = { kind: 'refresh', startedAt: clock.now - 130000, resumeStatus: 'running' }
  const h = harness({ local: clone(seed.local), session: saved, catalogStatuses: [503, 409], clock })
  await h.alarm()
  let state = (await h.popup({ type: 'status' })).state
  assert.equal(state.status, 'retrying')
  assert.equal(h.session.state.retryReadOnlyRefresh, true)
  clock.now = state.retryAt + 1
  await h.alarm()
  state = (await h.popup({ type: 'status' })).state
  assert.equal(state.status, 'paused')
  assert.equal(state.reason, 'catalog_stale')
  assert.equal(h.calls.filter(call => call.method === 'POST').length, 0)
})

test('one-click resume includes automatic recovery of its initial catalog GET', async () => {
  const clock = { now: Date.now() }
  const seed = harness()
  await seed.popup({ type: 'connect', token: TOKEN })
  const saved = clone(seed.session)
  saved.state.status = 'paused'; saved.state.reason = 'user_stopped'
  const h = harness({ local: clone(seed.local), session: saved, catalogStatuses: [503, 200], clock })
  const failed = await h.popup({ type: 'resume' })
  assert.equal(failed.state.status, 'retrying')
  clock.now = failed.state.retryAt + 1
  await h.alarm()
  assert.equal((await h.popup({ type: 'status' })).state.status, 'running')
  assert.equal(h.navigations.at(-1).url, card(1025784485))
})

test('saved supplier grouping starts directly at seller, avoiding rediscovery cards', async () => {
  const offers = [offer(1000, 1), offer(2000, 2)]
  const seed = harness({ offers })
  await seed.popup({ type: 'connect', token: TOKEN })
  const session = clone(seed.session)
  session.state.suppliers = { 1000: 4405572, 2000: 4405572 }
  const h = harness({ offers, local: clone(seed.local), session })
  await h.popup({ type: 'start' })
  assert.equal(h.navigations.at(-1).url, seller)
  assert.equal(h.navigations.some(tab => tab.url.includes('/catalog/')), false)
  await h.page({ items: offers.map(item => ({ nmId: item.nmId, sizeId: item.sizeId,
    buyerPriceNoWalletKopecks: 130800, supplierId: 4405572 })), catalogPage: 1, productCount: 2 })
  assert.equal((await h.popup({ type: 'status' })).state.status, 'waiting')
  assert.equal(h.navigations.filter(tab => tab.url.startsWith('https://')).length, 1)
})

test('restored progress never crosses account binding and page fallback remains partial without prices', async () => {
  const base = harness()
  await base.popup({ type: 'connect', token: TOKEN })
  const local = clone(base.local)
  local.control = { enabled: true }
  local.checkpoint.binding = { account: 999, revision: 'revision-1' }
  local.checkpoint.seen = { '1025784485:123': { buyer: 130800, seller: 170000, time: Date.now() } }
  local.checkpoint.accepted = 99
  const restored = harness({ local })
  await restored.startup()
  const state = (await restored.popup({ type: 'status' })).state
  assert.equal(state.observedOffers, 0)
  assert.equal(state.accepted, 0)
  await restored.page({ items: [], loadedNmId: 1025784485 })
  const partial = (await restored.popup({ type: 'status' })).state
  assert.equal(partial.status, 'waiting')
  assert.equal(partial.reason, 'partial_coverage')
  assert.equal(partial.accepted, 0)
})

test('3522 offers stay on one visible seller catalog through native acknowledged pages', async () => {
  const offers = Array.from({ length: 3522 }, (_, i) => offer(1000 + i, 5000 + i))
  const h = harness({ offers })
  await h.popup({ type: 'connect', token: TOKEN })
  await h.popup({ type: 'start' })
  const observation = item => ({ nmId: item.nmId, sizeId: item.sizeId, supplierId: 4405572, buyerPriceNoWalletKopecks: 130800 })
  await h.page({ items: [observation(offers[0])], loadedNmId: offers[0].nmId })
  assert.equal(h.navigations.at(-1).url, seller)
  assert.equal(h.navigations.at(-1).active, true)
  for (let offset = 0; offset < offers.length; offset += 200) {
    const chunk = offers.slice(offset, offset + 200)
    await h.page({ items: chunk.map(observation), catalogPage: offset / 200 + 1, productCount: chunk.length })
    if (offset + 200 < offers.length) await h.tick(1500)
  }
  const state = (await h.popup({ type: 'status' })).state
  assert.equal(state.accepted, 3522)
  assert.equal(state.status, 'waiting')
  assert.equal(state.partialSellers, 0)
  assert.equal(h.navigations.filter(item => item.url?.includes('/catalog/')).length, 1, 'only the discovery card; no per-product fallback')
  assert.equal(h.navigations.filter(item => item.url === seller).length, 1)
  assert.equal(h.scrolls.filter(item => item.type === 'scroll').length, 17)
})

test('hidden native catalog pauses without per-product fallback; visibility resumes, manual Stop never resumes', async () => {
  const progress = { visible: false, cards: 80 }
  const h = harness({ offers: [offer(1000), offer(1001), offer(1002)], progress })
  await h.popup({ type: 'connect', token: TOKEN }); await h.popup({ type: 'start' })
  await h.page({ items: [{ nmId: 1000, sizeId: 123, supplierId: 4405572, buyerPriceNoWalletKopecks: 130800 }] })
  await h.page({ items: [], catalogPage: 1, productCount: 200 })
  await h.tick(1500)
  await h.tick(25000)
  assert.equal(h.navigations.at(-1).url, seller)
  assert.equal((await h.popup({ type: 'status' })).state.reason, 'collector_hidden')
  await h.page({ type: 'collector_visible' })
  assert.equal((await h.popup({ type: 'status' })).state.status, 'paused', 'hidden cannot resume itself')
  progress.visible = true
  await h.page({ type: 'collector_visible' }, { id: 'foreign' })
  assert.equal((await h.popup({ type: 'status' })).state.status, 'paused', 'foreign sender cannot resume')
  await h.page({ type: 'collector_visible' })
  assert.equal((await h.popup({ type: 'status' })).state.status, 'running')
  const before = h.navigations.length
  await h.popup({ type: 'stop' }); await h.page({ type: 'collector_visible' }); await h.alarm()
  assert.equal(h.navigations.length, before)
  assert.equal((await h.popup({ type: 'status' })).state.reason, 'user_stopped')
})

test('visible native catalog gets only three extra windows before honest partial fallback', async () => {
  const h = harness({ offers: [offer(1000), offer(1001)], progress: { visible: true, cards: 80 } })
  await h.popup({ type: 'connect', token: TOKEN }); await h.popup({ type: 'start' })
  await h.page({ items: [{ nmId: 1000, sizeId: 123, supplierId: 4405572, buyerPriceNoWalletKopecks: 130800 }] })
  await h.page({ items: [], catalogPage: 1, productCount: 200 }); await h.tick(1500)
  for (let i = 0; i < 3; i++) { await h.tick(25000); assert.equal(h.navigations.at(-1).url, seller) }
  await h.tick(25000)
  assert.equal(h.navigations.at(-1).url, card(1001))
  const before = h.navigations.length
  await h.popup({ type: 'stop' }); await h.alarm()
  assert.equal(h.navigations.length, before)
  assert.equal((await h.popup({ type: 'status' })).state.status, 'paused')
})

test('upgrade keeps current card and progress without requeueing the old seller', async () => {
  const local = {}; const session = {}
  const offers = [offer(1000), offer(1001)]
  const old = harness({ offers, local, session })
  await old.popup({ type: 'connect', token: TOKEN }); await old.popup({ type: 'start' })
  await old.page({ items: [{ nmId: 1000, sizeId: 123, supplierId: 4405572, buyerPriceNoWalletKopecks: 130800 }] })
  await old.tick(25000)
  await old.popup({ type: 'stop' })
  delete session.state.nativeCatalogStrategy; delete local.checkpoint.nativeCatalogStrategy
  const updated = harness({ offers, local, session, existingTabs: [...old.tabs.values()].filter(item => item.id !== 7) })
  const result = await updated.popup({ type: 'resume' })
  assert.equal(result.state.accepted, 1)
  assert.equal(result.state.observedOffers, 1)
  assert.equal(updated.navigations.length, 1)
  assert.equal(updated.navigations.at(-1).url, card(1001))
  assert.deepEqual(session.state.pendingSellers, [])
  assert.equal(session.state.nativeCatalogStrategy, 3)
})

test('browser restart resumes the exact unfinished card without revisiting stores', async () => {
  const local = {}; const session = {}; const offers = [offer(1000), offer(1001)]
  const old = harness({ offers, local, session })
  await old.popup({ type: 'connect', token: TOKEN }); await old.popup({ type: 'start' })
  await old.page({ items: [{ nmId: 1000, sizeId: 123, supplierId: 4405572, buyerPriceNoWalletKopecks: 130800 }] })
  await old.tick(25000); await old.popup({ type: 'stop' })
  assert.equal(local.checkpoint.pageUrl, card(1001))
  assert.equal(local.checkpoint.tabId, undefined)
  const updated = harness({ offers, local, session: {} })
  await updated.popup({ type: 'resume' })
  assert.equal(updated.navigations.at(-1).url, card(1001))
  assert.equal((await updated.popup({ type: 'status' })).state.accepted, 1)
})

test('catalog grace never scrolls a different seller opened by the user', async () => {
  const h = harness({ offers: [offer(1000), offer(1001)], progress: { visible: false, cards: 80 } })
  await h.popup({ type: 'connect', token: TOKEN }); await h.popup({ type: 'start' })
  await h.page({ items: [{ nmId: 1000, sizeId: 123, supplierId: 4405572, buyerPriceNoWalletKopecks: 130800 }] })
  await h.page({ items: [], catalogPage: 1, productCount: 200 })
  await h.tick(1500)
  h.tabs.get(100).url = 'https://www.wildberries.ru/seller/999'
  const count = h.scrolls.length
  await h.tick(25000)
  assert.equal(h.scrolls.slice(count).some(item => ['scroll', 'catalog_progress'].includes(item.type)), false)
  const state = (await h.popup({ type: 'status' })).state
  assert.equal(state.status, 'paused'); assert.equal(state.reason, 'tab_changed')
  assert.equal(h.tabs.get(100).url, 'https://www.wildberries.ru/seller/999')
})

test('closing the collector pauses; explicit Resume creates a new owned tab', async () => {
  const h = harness({ offers: [offer(1000), offer(1001)] })
  await h.popup({ type: 'connect', token: TOKEN }); await h.popup({ type: 'start' })
  await h.page({ items: [{ nmId: 1000, sizeId: 123, supplierId: 4405572, buyerPriceNoWalletKopecks: 130800 }] })
  assert.equal(h.tabs.has(100), true)
  h.tabs.delete(100); h.closeOwned()
  await new Promise(resolve => setImmediate(resolve)) // onRemoved persists its queued pause.
  assert.equal((await h.popup({ type: 'status' })).state.status, 'paused')
  await h.alarm()
  assert.equal(h.tabs.has(101), false)
  await h.popup({ type: 'resume' })
  assert.equal(h.tabs.has(101), true)
  assert.equal(h.tabs.get(7).url, 'https://example.test')
})

test('public card batches submit exact buyer prices without opening product pages', async () => {
  const offers = Array.from({ length: 101 }, (_, index) => offer(1000 + index, 5000 + index))
  const h = harness({ offers, publicStatus: 200 })
  await h.popup({ type: 'connect', token: TOKEN })
  const result = await h.popup({ type: 'start' })
  assert.equal(result.state.status, 'waiting')
  assert.equal(result.state.observedOffers, 101)
  assert.equal(h.navigations.length, 0)
  assert.equal(h.publicCalls.length, 2)
  assert.deepEqual(h.publicCalls.map(({ url }) => {
    const parsed = new URL(url)
    return {
      origin: parsed.origin, path: parsed.pathname, appType: parsed.searchParams.get('appType'),
      currency: parsed.searchParams.get('curr'), destination: parsed.searchParams.get('dest'),
      language: parsed.searchParams.get('lang'), count: parsed.searchParams.get('nm').split(';').length,
    }
  }), [
    { origin: 'https://card.wb.ru', path: '/cards/v4/detail', appType: '1', currency: 'rub', destination: '-1257786', language: 'ru', count: 100 },
    { origin: 'https://card.wb.ru', path: '/cards/v4/detail', appType: '1', currency: 'rub', destination: '-1257786', language: 'ru', count: 1 },
  ])
  const posted = h.calls.filter((call) => call.url.endsWith('/snapshots')).flatMap((call) => JSON.parse(call.body).items)
  assert.equal(posted.length, 101)
  assert.deepEqual(posted.at(-1), {
    nmId: 1100, sizeId: 5100, sellerPriceKopecks: 170000,
    originalPriceKopecks: 190000,
    buyerPriceNoWalletKopecks: 130800, buyerPriceWithWalletKopecks: null,
    observedAt: posted.at(-1).observedAt,
  })
})

test('explicit Start re-observes a legacy parser checkpoint once, without automatic repeats', async () => {
  const h = harness({ publicStatus: 200 })
  await h.popup({ type: 'connect', token: TOKEN })
  await h.popup({ type: 'start' })
  const session = clone(h.session)
  for (const item of Object.values(session.state.seen)) { delete item.parserVersion; item.buyer = 170000 }
  const upgraded = harness({ publicStatus: 200, local: clone(h.local), session })
  await upgraded.popup({ type: 'start' })
  assert.equal(upgraded.publicCalls.length, 1)
  assert.equal(upgraded.calls.filter(call => call.url.endsWith('/snapshots')).length, 1)
  assert.equal(upgraded.alarms.size, 0)
  await upgraded.popup({ type: 'start' })
  assert.equal(upgraded.publicCalls.length, 1, 'fresh observations from the new parser are still deduplicated')
})

test('new wallet observation is acknowledged even when regular price is already fresh; duplicate wallet is not posted again', async () => {
  const h = harness({ offers: [offer(1025784485), offer(200)] })
  await h.popup({ type: 'connect', token: TOKEN }); await h.popup({ type: 'start' })
  const item = { nmId: 1025784485, sizeId: 123, buyerPriceNoWalletKopecks: 130800, supplierId: 4405572 }
  await h.page({ items: [item] })
  assert.equal(h.navigations.at(-1).url, seller)
  const before = h.calls.filter(call => call.url.endsWith('/snapshots')).length
  const wallet = { ...item, buyerPriceWithWalletKopecks: 128000 }
  await h.page({ items: [wallet] })
  const posts = h.calls.filter(call => call.url.endsWith('/snapshots'))
  assert.equal(posts.length, before + 1)
  assert.equal(JSON.parse(posts.at(-1).body).items[0].buyerPriceWithWalletKopecks, 128000)
  await h.page({ items: [wallet] }); await h.page({ items: [item] })
  assert.equal(h.calls.filter(call => call.url.endsWith('/snapshots')).length, before + 1)
  assert.equal(h.session.state.seen['1025784485:123'].wallet, 128000)
})

test('stop during a public card request submits nothing and stays user-stopped', async () => {
  const publicGate = gate()
  const h = harness({ publicStatus: 200, publicGate })
  await h.popup({ type: 'connect', token: TOKEN })
  const started = h.popup({ type: 'start' })
  await publicGate.entered
  const stopped = h.popup({ type: 'stop' })
  publicGate.release()
  await started; await stopped
  const state = (await h.popup({ type: 'status' })).state
  assert.equal(state.status, 'paused')
  assert.equal(state.reason, 'user_stopped')
  assert.equal(h.calls.filter((call) => call.url.endsWith('/snapshots')).length, 0)
  assert.equal(h.navigations.length, 0)
})

test('production error envelope invokes one refresh and respects HTTP Retry-After', async () => {
  const h = harness({ catalogStatuses: [409, 200], productionEnvelope: true })
  assert.equal((await h.popup({ type: 'connect', token: TOKEN })).state.status, 'ready')
  assert.equal(h.calls.filter((call) => call.url.endsWith('/catalog/refresh')).length, 1)
  for (const status of [409, 429, 503]) {
    const limited = harness({ catalogStatuses: [409], refreshStatus: status, productionEnvelope: true })
    const result = await limited.popup({ type: 'connect', token: TOKEN })
    assert.equal(result.state.status, 'paused')
    assert.equal(result.state.retryAfterSeconds, 60)
    assert.equal(limited.calls.length, 2)
    assert.equal(JSON.stringify(result).includes('never-render-this'), false)
  }
})

test('the popup can reveal only the owned collector tab after a WB challenge', async () => {
  const h = harness()
  await h.popup({ type: 'connect', token: TOKEN })
  await h.popup({ type: 'start' })
  await h.page({ type: 'blocked', code: 'challenge' })
  const result = await h.popup({ type: 'show_tab' })
  assert.equal(result.ok, true)
  assert.equal(result.state.status, 'paused')
  assert.deepEqual(h.navigations.at(-1), { id: 100, active: true })
  h.tabs.get(100).url = 'https://example.test'
  assert.equal((await h.popup({ type: 'show_tab' })).code, 'tab_changed')
})

test('queued timeout from the previous card cannot interrupt the new seller page', async () => {
  const postGate = gate()
  const h = harness({ offers: [offer(1025784485), offer(200)], postGate })
  await h.popup({ type: 'connect', token: TOKEN }); await h.popup({ type: 'start' })
  const observation = h.page({ items: [{ nmId: 1025784485, sizeId: 123, buyerPriceNoWalletKopecks: 130800, supplierId: 4405572 }] })
  await postGate.entered
  const expired = h.fire(25000)
  postGate.release()
  await observation; await expired
  const state = (await h.popup({ type: 'status' })).state
  assert.equal(state.status, 'running')
  assert.equal(state.partialSellers, 0)
  assert.equal(h.navigations.at(-1).url, seller)
})

test('a card without a price advances; WB rate limiting waits automatically', async () => {
  const h = harness({ offers: [offer(100), offer(200), offer(300)] })
  await h.popup({ type: 'connect', token: TOKEN }); await h.popup({ type: 'start' })
  await h.tick(25000)
  assert.equal(h.navigations.at(-1).url, card(200))
  assert.equal((await h.popup({ type: 'status' })).state.observedOffers, 0)
  await h.page({ items: [{ nmId: 200, sizeId: 123, buyerPriceNoWalletKopecks: 130800 }] })
  assert.equal(h.navigations.at(-1).url, card(300))
  await h.page({ type: 'blocked', code: 'http_429' })
  const state = (await h.popup({ type: 'status' })).state
  assert.equal(state.status, 'retrying')
  assert.equal(state.observedOffers, 1)
  assert.equal(state.reason, 'http_429')
})

test('stop during an in-flight chunk preserves its acknowledgement and sends no second chunk', async () => {
  const postGate = gate()
  const offers = Array.from({ length: 101 }, (_, index) => offer(1000 + index))
  const h = harness({ offers, postGate })
  await h.popup({ type: 'connect', token: TOKEN }); await h.popup({ type: 'start' })
  const submitted = h.page({ items: offers.map((item) => ({ nmId: item.nmId, sizeId: item.sizeId, buyerPriceNoWalletKopecks: 130800 })) })
  await postGate.entered
  const stopped = h.popup({ type: 'stop' })
  const reopened = harness({ local: clone(h.local) })
  await reopened.startup()
  assert.equal((await reopened.popup({ type: 'status' })).state.status, 'paused', 'browser restart must retain a stop issued before the POST returned')
  assert.equal(reopened.calls.length, 0)
  postGate.release()
  await submitted; await stopped
  const state = (await h.popup({ type: 'status' })).state
  assert.equal(state.status, 'paused')
  assert.equal(state.accepted, 100)
  assert.equal(h.calls.filter((call) => call.url.endsWith('/snapshots')).length, 1)
  assert.equal(h.local.checkpoint.deferredObservations.items.length, 1)
  await h.popup({ type: 'resume' })
  assert.equal((await h.popup({ type: 'status' })).state.accepted, 101, 'unsent tail survives Stop and is ACKed on Resume')
  assert.equal(h.calls.filter((call) => call.url.endsWith('/snapshots')).length, 2)
})

test('stop during catalog GET prevents another page or a stale-cache refresh POST', async () => {
  for (const catalogStatus of [200, 409]) {
    const catalogGate = gate()
    const h = harness({ offers: Array.from({ length: 101 }, (_, index) => offer(1000 + index)), catalogStatus, catalogGate })
    const connected = h.popup({ type: 'connect', token: TOKEN })
    await catalogGate.entered
    const stopped = h.popup({ type: 'stop' })
    catalogGate.release()
    await connected; await stopped
    assert.equal(h.calls.length, 1)
    assert.equal(h.navigations.length, 0)
    assert.equal((await h.popup({ type: 'status' })).state.reason, 'user_stopped')
  }
})

test('stop during a recurring catalog read remains paused after closing the browser', async () => {
  const catalogGate = gate()
  const h = harness({ local: { token: TOKEN, control: { enabled: true, nextRunAt: Date.now() - 1 } }, catalogGate })
  const running = h.startup()
  await catalogGate.entered
  const stopping = h.popup({ type: 'stop' })
  const reopened = harness({ local: clone(h.local) })
  await reopened.startup()
  catalogGate.release()
  await running; await stopping
  assert.equal((await reopened.popup({ type: 'status' })).state.status, 'paused')
  assert.equal(reopened.calls.length, 0)
})

test('manual start on a paused pass continues the same catalog page without losing counters', async () => {
  const h = harness({ offers: [offer(1025784485), offer(200)] })
  await h.popup({ type: 'connect', token: TOKEN }); await h.popup({ type: 'start' })
  await h.page({ items: [{ nmId: 1025784485, sizeId: 123, buyerPriceNoWalletKopecks: 130800, supplierId: 4405572 }] })
  await h.page({ items: [], catalogPage: 5, productCount: 100 })
  await h.popup({ type: 'stop' })
  const navigations = h.navigations.length
  const requests = h.publicCalls.length
  await h.popup({ type: 'start' })
  assert.equal(h.navigations.length, navigations)
  assert.equal(h.publicCalls.length, requests)
  assert.equal(h.session.state.catalogPage, 5)
  assert.equal((await h.popup({ type: 'status' })).state.accepted, 1)
})

test('expired freshness does not put acknowledged products back into this pass', async () => {
  const clock = { now: Date.now() }
  const offers = [offer(1000), offer(1001)]
  const h = harness({ offers, clock })
  await h.popup({ type: 'connect', token: TOKEN }); await h.popup({ type: 'start' })
  await h.page({ items: [{ nmId: 1000, sizeId: 123, supplierId: 4405572, buyerPriceNoWalletKopecks: 130800 }] })
  clock.now += 31 * 60 * 1000
  for (const item of offers) item.sellerPriceObservedAt = new Date(clock.now).toISOString()
  assert.equal((await h.popup({ type: 'status' })).state.observedOffers, 0, 'TTL is not extended')
  await h.page({ items: [], catalogPage: 1, productCount: 0 })
  assert.equal(h.navigations.at(-1).url, card(1001), 'ACKed 1000 is not revisited')
  await h.page({ items: [], loadedNmId: 1001 })
  const state = (await h.popup({ type: 'status' })).state
  assert.equal(state.status, 'waiting')
  assert.equal(state.nextRunAt, null)
  assert.equal(state.accepted, 1)
})

test('MV3 suspension preserves running progress and completion never schedules another pass', async () => {
  const h = harness()
  await h.popup({ type: 'connect', token: TOKEN }); await h.popup({ type: 'start' })
  const restored = harness({ local: clone(h.local), session: clone(h.session), existingTabs: [...h.tabs.values()].filter((tab) => tab.id !== 7) })
  assert.equal((await restored.popup({ type: 'status' })).state.status, 'running')
  await restored.page({ items: [{ nmId: 1025784485, sizeId: 123, buyerPriceNoWalletKopecks: 130800 }] })
  assert.equal((await restored.popup({ type: 'status' })).state.status, 'waiting')
  assert.equal(restored.alarms.size, 0)
  const next = clone(restored.session)
  next.state.nextRunAt = Date.now() - 1
  const nextCycle = harness({ local: clone(restored.local), session: next })
  await nextCycle.alarm()
  assert.equal((await nextCycle.popup({ type: 'status' })).state.status, 'waiting')
  assert.equal(nextCycle.navigations.length, 0)
  await nextCycle.popup({ type: 'stop' })
  await nextCycle.alarm()
  assert.equal((await nextCycle.popup({ type: 'status' })).state.status, 'paused')
  assert.equal(nextCycle.alarms.size, 0)
})

test('interrupted POSTs recover safely: refresh reads only, unknown snapshot re-observes after delay', async () => {
  const h = harness(); await h.popup({ type: 'connect', token: TOKEN })
  const base = { ...clone(h.session.state), pendingRequest: { kind: 'refresh', startedAt: Date.now() - 130000, resumeStatus: 'ready' } }
  const refresh = harness({ local: { token: TOKEN }, session: { state: base } })
  await refresh.alarm()
  assert.equal((await refresh.popup({ type: 'status' })).state.status, 'ready')
  assert.equal(refresh.calls.length, 1)
  assert.equal(refresh.calls[0].method, 'GET')
  const snapshot = harness({ local: { token: TOKEN }, session: { state: { ...base, status: 'running', pendingRequest: { kind: 'snapshot' } } } })
  const state = (await snapshot.popup({ type: 'status' })).state
  assert.equal(state.status, 'retrying')
  assert.equal(state.reason, 'submission_unknown')
  assert.equal(snapshot.calls.length, 0)
})

test('browser restart restores enabled collection, but never revives an explicit pause', async () => {
  const h = harness()
  await h.popup({ type: 'connect', token: TOKEN }); await h.popup({ type: 'start' })
  const local = clone(h.local)
  assert.equal(local.control.enabled, true)
  assert.equal(JSON.stringify(local.control).includes('sellerPrice'), false)
  const reopened = harness({ local })
  await reopened.startup()
  assert.equal((await reopened.popup({ type: 'status' })).state.status, 'running')
  assert.equal(reopened.navigations.filter((tab) => tab.url.startsWith('https://')).length, 1)
  await reopened.popup({ type: 'stop' })
  const paused = harness({ local: clone(reopened.local) })
  await paused.startup(); await paused.alarm()
  assert.equal((await paused.popup({ type: 'status' })).state.status, 'paused')
  assert.equal(paused.calls.length, 0)
  assert.equal(paused.navigations.length, 0)
})

test('known refresh rejection remains paused after a worker restart', async () => {
  const h = harness({ catalogStatuses: [409], refreshStatus: 429, productionEnvelope: true })
  await h.popup({ type: 'connect', token: TOKEN })
  const restored = harness({ local: clone(h.local), session: clone(h.session) })
  await restored.alarm()
  assert.equal((await restored.popup({ type: 'status' })).state.status, 'paused')
  assert.equal(restored.calls.length, 0)
})

test('connection is verified by CRM, token is trusted-local only, and page messages cannot configure it', async () => {
  const h = harness()
  assert.equal((await h.popup({ type: 'status' })).state.status, 'disconnected')
  const connected = await h.popup({ type: 'connect', token: TOKEN, apiUrl: 'https://evil.test' })
  assert.equal(connected.state.status, 'ready')
  assert.equal(h.local.token, TOKEN)
  assert.equal(JSON.stringify(h.session).includes(TOKEN), false)
  assert.equal(JSON.stringify(connected).includes(TOKEN), false)
  assert.deepEqual(h.access[0], ['local', 'TRUSTED_CONTEXTS'])
  assert.equal(h.calls.length, 1)
  const rejected = await h.page({ type: 'connect', token: TOKEN }, { tab: { id: 7, url: card(1025784485) }, url: card(1025784485) })
  assert.equal(rejected.ok, false)
  assert.equal(h.calls.length, 1)
})

test('stale seller cache refreshes once, restarts pagination, and cooldown does not loop', async () => {
  const h = harness({ offers: Array.from({ length: 101 }, (_, index) => offer(1000 + index)), catalogStatuses: [200, 409, 200, 200] })
  assert.equal((await h.popup({ type: 'connect', token: TOKEN })).state.status, 'ready')
  assert.deepEqual(h.calls.map((call) => call.url.slice(API.length)), [
    '/catalog?page=1&pageSize=100', '/catalog?page=2&pageSize=100', '/catalog/refresh', '/catalog?page=1&pageSize=100', '/catalog?page=2&pageSize=100',
  ])
  for (const status of [429, 503]) {
    const limited = harness({ catalogStatuses: [409], refreshStatus: status })
    const result = await limited.popup({ type: 'connect', token: TOKEN })
    assert.equal(result.ok, false)
    assert.equal(result.state.status, 'paused')
    assert.equal(result.state.retryAfterSeconds, 60)
    assert.equal(limited.calls.length, 2)
    assert.equal(limited.timers.size, 0)
    assert.equal(JSON.stringify(result).includes('never-render-this'), false)
  }
  const stillStale = harness({ catalogStatuses: [409, 409] })
  assert.equal((await stillStale.popup({ type: 'connect', token: TOKEN })).ok, false)
  assert.equal(stillStale.calls.filter((call) => call.url.endsWith('/catalog/refresh')).length, 1)
})

test('owned top-frame observations produce an exact snapshot once; all user tabs survive', async () => {
  const h = harness()
  await h.popup({ type: 'connect', token: TOKEN })
  await h.popup({ type: 'start' })
  assert.equal(h.navigations[0].active, false)
  assert.equal(h.navigations[0].url, 'about:blank', 'claim the owned tab before starting a WB document')
  assert.equal(h.navigations[1].url, card(1025784485))
  const observation = { items: [{ nmId: 1025784485, sizeId: 123, buyerPriceNoWalletKopecks: 130800, buyerPriceWithWalletKopecks: 128000 }] }
  await h.page(observation, { frameId: 1 })
  assert.equal(h.calls.filter((call) => call.method === 'POST').length, 0)
  await h.page(observation)
  const posts = h.calls.filter((call) => call.method === 'POST')
  assert.equal(posts.length, 1)
  const body = JSON.parse(posts[0].body)
  assert.equal(body.goodsRevision, 'revision-1')
  assert.deepEqual({ ...body.items[0], observedAt: 'time' }, { nmId: 1025784485, sizeId: 123, sellerPriceKopecks: 170000, buyerPriceNoWalletKopecks: 130800, buyerPriceWithWalletKopecks: 128000, observedAt: 'time' })
  const state = (await h.popup({ type: 'status' })).state
  assert.equal(state.status, 'waiting')
  assert.equal(state.accepted, 1)
  assert.equal(state.observedOffers, 1)
  await h.page(observation)
  assert.equal(h.calls.filter((call) => call.method === 'POST').length, 1)
  assert.equal(h.tabs.get(7).url, 'https://example.test')
})

test('discovered seller is visited normally; bulk prices are filtered then remaining products continue', async () => {
  const h = harness({ offers: [offer(1025784485), offer(200), offer(300)] })
  await h.popup({ type: 'connect', token: TOKEN })
  await h.popup({ type: 'start' })
  await h.page({ items: [{ nmId: 1025784485, sizeId: 123, buyerPriceNoWalletKopecks: 130800, supplierId: 4405572 }] })
  assert.equal(h.navigations.at(-1).url, seller)
  await h.page({ items: [
    { nmId: 200, sizeId: 123, buyerPriceNoWalletKopecks: 120000, supplierId: 4405572 },
    { nmId: 999, sizeId: 123, buyerPriceNoWalletKopecks: 120000, supplierId: 4405572 },
  ], catalogPage: 1, productCount: 2 })
  const posts = h.calls.filter((call) => call.method === 'POST')
  assert.equal(JSON.parse(posts.at(-1).body).items.length, 1)
  await h.tick(1500)
  assert.equal(h.scrolls.at(-1).type, 'scroll')
  await h.page({ items: [], catalogPage: 2, productCount: 0 })
  assert.equal(h.navigations.at(-1).url, card(300))
})

test('page 403/challenge pauses the run without automatic retries', async () => {
  for (const code of ['http_403', 'challenge']) {
    const h = harness()
    await h.popup({ type: 'connect', token: TOKEN })
    await h.popup({ type: 'start' })
    await h.page({ type: 'blocked', code })
    const state = (await h.popup({ type: 'status' })).state
    assert.equal(state.status, 'paused')
    assert.equal(state.reason, code)
    assert.equal(h.navigations.filter((tab) => tab.url.startsWith('https://')).length, 1)
    assert.equal(h.timers.size, 0)
    assert.equal(h.calls.filter((call) => call.method === 'POST').length, 0)
  }
})

test('409 refreshes catalog once and revalidates before one retry', async () => {
  const h = harness({ postStatuses: [409, 200] })
  await h.popup({ type: 'connect', token: TOKEN })
  await h.popup({ type: 'start' })
  await h.page({ items: [{ nmId: 1025784485, sizeId: 123, buyerPriceNoWalletKopecks: 130800 }] })
  const posts = h.calls.filter((call) => call.method === 'POST')
  assert.deepEqual(posts.map((call) => JSON.parse(call.body).goodsRevision), ['revision-1', 'revision-2'])
  assert.equal((await h.popup({ type: 'status' })).state.accepted, 1)
  const failed = harness({ postStatuses: [409, 409] })
  await failed.popup({ type: 'connect', token: TOKEN }); await failed.popup({ type: 'start' })
  await failed.page({ items: [{ nmId: 1025784485, sizeId: 123, buyerPriceNoWalletKopecks: 130800 }] })
  assert.equal(failed.calls.filter((call) => call.method === 'POST').length, 2)
  assert.equal((await failed.popup({ type: 'status' })).state.status, 'paused')
})

test('401 removes the token and stale seller catalog cannot establish a connection', async () => {
  const h = harness({ postStatuses: [401] })
  await h.popup({ type: 'connect', token: TOKEN }); await h.popup({ type: 'start' })
  await h.page({ items: [{ nmId: 1025784485, sizeId: 123, buyerPriceNoWalletKopecks: 130800 }] })
  assert.equal(h.local.token, undefined)
  assert.equal((await h.popup({ type: 'status' })).state.status, 'disconnected')
  const stale = harness({ offers: [{ ...offer(1025784485), sellerPriceObservedAt: '2020-01-01T00:00:00Z' }] })
  assert.equal((await stale.popup({ type: 'connect', token: TOKEN })).ok, false)
  assert.equal(stale.local.token, undefined)
  assert.notEqual((await stale.popup({ type: 'status' })).state.status, 'ready')
})

test('bulk submissions are bounded to 100 and serialized', async () => {
  const offers = Array.from({ length: 101 }, (_, index) => offer(1000 + index))
  const h = harness({ offers })
  await h.popup({ type: 'connect', token: TOKEN }); await h.popup({ type: 'start' })
  const message = { items: offers.map((item) => ({ nmId: item.nmId, sizeId: item.sizeId, buyerPriceNoWalletKopecks: 130800 })) }
  await Promise.all([h.page(message), h.page(message)])
  assert.deepEqual(h.calls.filter((call) => call.method === 'POST').map((call) => JSON.parse(call.body).items.length), [100, 1])
  assert.equal((await h.popup({ type: 'status' })).state.accepted, 101)
})

test('stop, disconnect and a worker restart never take control of an existing user tab', async () => {
  const h = harness()
  await h.popup({ type: 'connect', token: TOKEN }); await h.popup({ type: 'start' })
  const saved = clone(h.session)
  await h.popup({ type: 'stop' })
  assert.equal((await h.popup({ type: 'status' })).state.status, 'paused')
  await h.popup({ type: 'disconnect' })
  assert.equal(h.local.token, undefined)
  assert.equal(h.tabs.has(7), true)
  const restarted = harness({ local: { token: TOKEN }, session: saved })
  assert.equal((await restarted.popup({ type: 'status' })).state.status, 'running')
  assert.equal(restarted.navigations.length, 0)
})

 test('a loaded sold-out card advances without waiting or inventing a price', async () => {
  const h = harness({ offers: [offer(100), offer(200)] })
  await h.popup({ type: 'connect', token: TOKEN }); await h.popup({ type: 'start' })
  await h.page({ items: [], loadedNmId: 100 })
  const state = (await h.popup({ type: 'status' })).state
  assert.equal(state.status, 'running')
  assert.equal(state.observedOffers, 0)
  assert.equal(state.visitedProducts, 1)
  assert.equal(h.navigations.at(-1).url, card(200))
  assert.equal(h.calls.filter((call) => call.url.endsWith('/snapshots')).length, 0)
})
