'use strict'
importScripts('contract.js')
importScripts('connection.js')

const C = WbPricesContract
let backendUrl = WbPricesConnection.DEFAULT_SERVER_URL
const API_PATH = '/api/v1/wb/browser-prices'
const PUBLIC_CARD_API = 'https://card.wb.ru/cards/v4/detail'
const PUBLIC_DESTINATION = '-1257786'
const PUBLIC_BATCH_SIZE = 100
const ALARM = 'wb-price-collector'
const MAX_RETRIES = 3
const emptyState = () => ({ status: 'disconnected', reason: null, catalog: null, tabId: null, pageUrl: null,
  seen: {}, visited: [], sellers: [], pendingSellers: [], partialSellers: 0, catalogPage: 0,
  accepted: 0, ignored: 0, unmatched: 0, verifiedAt: null, lastAckAt: null, retryAfterSeconds: null,
  pendingRequest: null, nextRunAt: null, pageDeadline: null, scrollAt: null,
  phase: 'browser', retryAt: null, retryAttempts: 0, suppliers: {}, publicUnavailableUntil: null,
  sellerWaits: 0, sellerRenderedCards: 0,
  nativeCatalogStrategy: 0,
  metrics: { publicRequests: 0, snapshotRequests: 0, navigations: 0, catalogRequests: 0, requestMs: 0, startedAt: null } })
let state = emptyState()
let token = null
let stopped = false
let timeout = null
let scrollTimer = null
let timerGeneration = 0
let controlGeneration = 0
let savedControl = null
let serial = Promise.resolve()

const ready = (async () => {
  await chrome.storage.local.setAccessLevel({ accessLevel: 'TRUSTED_CONTEXTS' })
  await chrome.storage.session.setAccessLevel({ accessLevel: 'TRUSTED_CONTEXTS' })
  const local = await chrome.storage.local.get(['token', 'backendUrl', 'control', 'stopRequested', 'checkpoint'])
  let invalidServer = false
  try {
    // An older installed extension stored only a token. Its credential belongs
    // to the legacy server, even if a new package selects another default.
    backendUrl = WbPricesConnection.normalizeServerUrl(local.backendUrl
      ?? (local.token ? WbPricesConnection.LEGACY_SERVER_URL : backendUrl))
  }
  catch { invalidServer = true; await chrome.storage.local.remove('token') }
  token = invalidServer ? null : local.token
  const stored = await chrome.storage.session.get(['state', 'stopRequested'])
  const saved = stored.state
  if (saved) state = { ...emptyState(), ...saved }
  else if (token && local.control) {
    if (local.checkpoint) Object.assign(state, local.checkpoint)
    state.pendingRequest = local.control.pendingRequest || null
    if (local.control.status === 'waiting' && state.metrics.finishedAt) {
      state.status = 'waiting'; state.reason = local.control.reason; state.nextRunAt = null
    } else if (local.control.enabled) {
      state.recovering = Boolean(local.checkpoint)
      state.status = local.control.status === 'retrying' ? 'retrying' : 'waiting'
      state.retryAt = local.control.retryAt || Date.now()
      state.nextRunAt = local.control.nextRunAt || Date.now()
    }
    else if (local.control.reason) { state.status = 'paused'; state.reason = local.control.reason }
  }
  if (!token) state = emptyState()
  if (invalidServer) state.reason = 'server_invalid'
  // Old builds scheduled an unsolicited second pass after completion.
  if (state.status === 'waiting' && state.metrics.finishedAt) { state.nextRunAt = null; state.recovering = false }
  stopped = Boolean(stored.stopRequested || local.stopRequested)
  if (stopped && token) { state.status = 'paused'; state.reason = 'user_stopped' }
  else if (state.status !== 'paused' && state.pendingRequest?.kind === 'snapshot') {
    // Never replay an unknown POST. Re-observe the remaining offers after a
    // bounded delay; only a new acknowledgement can mark them saved.
    state.status = 'retrying'; state.reason = 'submission_unknown'; state.retryAt = Date.now() + 60000
    state.retryAttempts += 1; state.pendingRequest = null
    if (state.retryAttempts > MAX_RETRIES) { state.status = 'paused'; state.reason = 'retry_exhausted' }
  }
  else if (state.status !== 'paused' && state.pendingRequest?.kind === 'refresh') {
    state.status = 'refreshing'; state.retryReadOnlyRefresh = true
  }
  if (state.status === 'running' && !state.pageDeadline && !state.scrollAt) state.pageDeadline = Date.now()
  await save()
  // Recover the short page deadline as well as the durable alarm after suspension.
  if (['running', 'retrying'].includes(state.status)) {
    const generation = timerGeneration
    timeout = setTimeout(() => enqueue(async () => {
      if (generation === timerGeneration) await runScheduled()
    }).catch(fail), Math.max(0, (state.retryAt || state.scrollAt || state.pageDeadline) - Date.now()))
  }
})()

function enqueue(run) {
  const result = serial.then(() => ready).then(run)
  serial = result.catch(() => {})
  return result
}

function clearTimers() {
  timerGeneration += 1
  clearTimeout(timeout); clearTimeout(scrollTimer)
  timeout = scrollTimer = null
  state.pageDeadline = state.scrollAt = null
}

async function save() {
  await chrome.storage.session.set({ state })
  const control = { enabled: !stopped && (['running', 'refreshing', 'retrying'].includes(state.status) || (state.status === 'waiting' && !state.metrics.finishedAt)),
    status: state.status, retryAt: state.retryAt, nextRunAt: state.nextRunAt,
    reason: ['paused', 'waiting'].includes(state.status) ? state.reason : null, pendingRequest: state.pendingRequest }
  const serialized = JSON.stringify(control)
  if (serialized !== savedControl) { await chrome.storage.local.set({ control }); savedControl = serialized }
  // Price observations are non-secret, trusted-context-only. Persist progress
  // without the catalog or credentials, including across browser restarts.
  await chrome.storage.local.set({ checkpoint: {
    binding: state.catalog ? { account: state.catalog.marketplaceAccountId, revision: state.catalog.goodsRevision } : state.binding,
    seen: state.seen, visited: state.visited, suppliers: state.suppliers, sellers: state.sellers,
    pendingSellers: state.pendingSellers, partialSellers: state.partialSellers,
    sellerWaits: state.sellerWaits, sellerRenderedCards: state.sellerRenderedCards,
    nativeCatalogStrategy: state.nativeCatalogStrategy,
    // Persist the route, not a tab ID: browser restart may reuse tab IDs for
    // unrelated user tabs. Recovery creates a new owned collector safely.
    pageUrl: C.pageIdentity(state.pageUrl) ? state.pageUrl : null,
    accepted: state.accepted, ignored: state.ignored, unmatched: state.unmatched, lastAckAt: state.lastAckAt,
    phase: state.phase, retryAttempts: state.retryAttempts, metrics: state.metrics,
    restartCycle: state.restartCycle, retryReadOnlyRefresh: state.retryReadOnlyRefresh,
    publicUnavailableUntil: state.publicUnavailableUntil,
    deferredObservations: state.deferredObservations,
  } })
  const due = state.status === 'retrying' ? state.retryAt : state.status === 'refreshing' ? state.pendingRequest?.startedAt + 125000
    : state.status === 'waiting' ? state.nextRunAt : state.status === 'running' ? state.scrollAt || state.pageDeadline : null
  // One durable wakeup supplements short in-memory timers on Chromium 111+.
  if (!stopped && due) await chrome.alarms.create(ALARM, { when: Math.max(Date.now() + 60000, due) })
  else await chrome.alarms.clear(ALARM)
}

function seenFresh(offer) {
  const observation = state.seen[C.key(offer)]
  return observation && observation.seller === offer.sellerPriceKopecks
    && Date.now() - observation.time <= Math.min(state.catalog.maxAgeSeconds, 1800) * 1000
}

// A completed offer stays completed within this pass even after its price TTL
// expires. Freshness is still enforced for display and every server submission.
function seenThisPass(offer) {
  return state.seen[C.key(offer)]?.seller === offer.sellerPriceKopecks
}

async function continueCurrentPage() {
  const tab = state.tabId !== null ? await chrome.tabs.get(state.tabId).catch(() => null) : null
  if (tab && C.pageIdentity(tab.url) === C.pageIdentity(state.pageUrl)) {
    if (C.sellerId(state.pageUrl)) await advanceSeller()
    else return navigate(state.pageUrl) // Re-observe this exact unfinished card.
    return waitForPage()
  }
  if (C.pageIdentity(state.pageUrl)) return navigate(state.pageUrl)
  return nextPage()
}

function publicState() {
  const items = state.catalog?.items || []
  return { backendUrl, status: state.status, reason: state.reason, totalOffers: items.length,
    observedOffers: items.filter(seenFresh).length, totalProducts: new Set(items.map((item) => item.nmId)).size,
    visitedProducts: new Set([...state.visited, ...items.filter((item) => state.seen[C.key(item)]).map((item) => item.nmId)]).size,
    collectorTabAvailable: state.tabId !== null && C.pageIdentity(state.pageUrl) !== null,
    sellersVisited: state.sellers.length, partialSellers: state.partialSellers,
    accepted: state.accepted, ignored: state.ignored, unmatched: state.unmatched,
    verifiedAt: state.verifiedAt, lastAckAt: state.lastAckAt, retryAfterSeconds: state.retryAfterSeconds, nextRunAt: state.nextRunAt,
    retryAt: state.retryAt, retryAttempts: state.retryAttempts, phase: state.phase, metrics: state.metrics }
}

async function pause(reason) {
  stopped = true
  clearTimers()
  if (state.tabId !== null) void chrome.tabs.sendMessage(state.tabId, { type: 'stop_scroll' }).catch(() => {})
  state.status = token ? 'paused' : 'disconnected'
  state.reason = reason
  state.nextRunAt = null
  state.retryAt = null
  await save()
}

function safeError(error) {
  const code = error?.message
  return /^(server_invalid|server_permission_denied|catalog_(invalid|changed|stale)|refresh_(cooldown|unavailable|busy)|http_429|observation_stale|ack_invalid|token_invalid|stop_first|page_timeout|tab_closed|tab_changed|crm_http_(401|403|409|429|500|502|503))$/.test(code || '') ? code : 'request_failed'
}

async function fail(error) {
  const code = safeError(error)
  const activeRun = ['running', 'retrying'].includes(state.status)
    || (state.status === 'refreshing' && state.pendingRequest?.resumeStatus === 'running')
  state.retryReadOnlyRefresh ||= state.pendingRequest?.kind === 'refresh' && !error?.httpStatus
  state.pendingRequest = null
  state.retryAfterSeconds = error?.retryAfterSeconds || null
  if (!stopped && token && activeRun
      && ['request_failed', 'observation_stale', 'crm_http_429', 'crm_http_500', 'crm_http_502', 'crm_http_503',
        'refresh_busy', 'refresh_cooldown', 'refresh_unavailable', 'http_429'].includes(code)) {
    state.retryAttempts += 1
    if (state.retryAttempts <= MAX_RETRIES) {
      clearTimers()
      state.status = 'retrying'; state.reason = code
      state.retryAt = Date.now() + Math.max(60000 * 2 ** (state.retryAttempts - 1), (state.retryAfterSeconds || 0) * 1000)
      await save()
      const generation = timerGeneration
      timeout = setTimeout(() => enqueue(async () => {
        if (generation === timerGeneration) await runScheduled()
      }).catch(fail), state.retryAt - Date.now())
      return { ok: false, code, state: publicState() }
    }
    await pause('retry_exhausted')
    return { ok: false, code: 'retry_exhausted', state: publicState() }
  }
  if (code === 'crm_http_401') {
    token = null
    await chrome.storage.local.remove('token')
  }
  await pause(code)
  return { ok: false, code, state: publicState() }
}

async function api(path, { method = 'GET', body, accessToken = token, serverUrl = backendUrl, verification = false } = {}) {
  if (stopped && !verification) throw new Error('stopped')
  if (!await chrome.permissions.contains({ origins: [WbPricesConnection.permissionOrigin(serverUrl)] })) {
    throw new Error('server_permission_denied')
  }
  if (stopped && !verification) throw new Error('stopped')
  const started = Date.now()
  if (!verification && path.startsWith('/catalog?')) state.metrics.catalogRequests += 1
  if (path === '/snapshots') state.metrics.snapshotRequests += 1
  const response = await fetch(`${serverUrl}${API_PATH}${path}`, { method, credentials: 'omit', redirect: 'error',
    headers: { Accept: 'application/json', Authorization: `Bearer ${accessToken}`, ...(body ? { 'Content-Type': 'application/json' } : {}) },
    ...(body ? { body: JSON.stringify(body) } : {}), signal: AbortSignal.timeout(path === '/catalog/refresh' ? 125000 : 20000) })
  if (!verification) state.metrics.requestMs += Date.now() - started
  if (!response.ok) {
    const error = new Error(`crm_http_${response.status}`)
    error.httpStatus = response.status
    const header = response.headers?.get('Retry-After')
    const retry = /^\d+$/.test(header || '') ? Number(header) : Math.ceil((Date.parse(header) - Date.now()) / 1000)
    if (Number.isSafeInteger(retry) && retry > 0 && retry <= 86400) error.retryAfterSeconds = retry
    const allowed = {
      WB_BROWSER_GOODS_STALE: [409, 'catalog_stale'], WB_BROWSER_GOODS_CHANGED: [409, 'catalog_changed'],
      WB_BROWSER_REFRESH_BUSY: [409, 'refresh_busy'], WB_BROWSER_REFRESH_COOLDOWN: [429, 'refresh_cooldown'],
      WB_BROWSER_REFRESH_UNAVAILABLE: [503, 'refresh_unavailable'],
    }
    if ([409, 429, 503].includes(response.status)) {
      try {
        const payload = await response.json()
        const detail = payload?.error || payload?.detail
        if (allowed[detail?.code]?.[0] === response.status) {
          error.serverCode = detail.code
          error.message = allowed[detail.code][1]
          const header = response.headers?.get('Retry-After')
          const retry = detail.details?.retryAfterSeconds ?? detail.retryAfterSeconds ?? (/^\d+$/.test(header || '') ? Number(header) : null)
          if (Number.isSafeInteger(retry) && retry > 0 && retry <= 86400) error.retryAfterSeconds = retry
        }
      } catch { /* Raw error bodies never reach UI or storage. */ }
    }
    throw error
  }
  return response.json()
}

async function readCatalog(accessToken, { serverUrl = backendUrl, verification = false, isCurrent = () => !stopped } = {}) {
  let catalog = null
  const items = []; const keys = new Set()
  for (let page = 1; !catalog || items.length < catalog.total; page++) {
    if (!isCurrent()) throw new Error('stopped')
    const payload = await api(`/catalog?page=${page}&pageSize=100`, { accessToken, serverUrl, verification })
    if (!isCurrent()) throw new Error('stopped')
    const value = C.validateCatalogPage(payload, page, catalog)
    catalog ||= value
    for (const item of value.items) {
      if (keys.has(C.key(item))) throw new Error('catalog_invalid')
      keys.add(C.key(item)); items.push(item)
    }
  }
  return { ...catalog, items }
}

function applyCatalog(catalog) {
  const items = catalog.items
  if (state.binding && state.binding.account !== catalog.marketplaceAccountId) {
    state.seen = {}; state.visited = []; state.suppliers = {}; state.sellers = []; state.pendingSellers = []
    state.accepted = state.ignored = state.unmatched = 0
    state.deferredObservations = null
  }
  // New/changed exact offers may need work, but a new revision must not rewind
  // the current seller. Account changes above still discard all bound progress.
  if (state.binding && state.binding.revision !== catalog.goodsRevision) {
    const before = new Map((state.catalog?.items || []).map(item => [C.key(item), item.sellerPriceKopecks]))
    const changed = new Set(items.filter(item => before.has(C.key(item)) && before.get(C.key(item)) !== item.sellerPriceKopecks).map(item => item.nmId))
    state.visited = state.visited.filter(id => !changed.has(id))
  }
  state.catalog = catalog
  state.binding = { account: catalog.marketplaceAccountId, revision: catalog.goodsRevision }
  state.verifiedAt = new Date().toISOString()
  state.retryReadOnlyRefresh = false
}

async function loadCatalog(accessToken = token, refreshed = false, initialError = null) {
  try {
    if (initialError) throw initialError
    applyCatalog(await readCatalog(accessToken))
  } catch (error) {
    if (error.serverCode !== 'WB_BROWSER_GOODS_STALE') throw error
    // This specific response establishes that the extension bearer was verified.
    error.catalogAuthenticated = true
    if (refreshed) throw error
    try {
      token = accessToken
      await chrome.storage.local.set({ token })
      state.pendingRequest = { kind: 'refresh', startedAt: Date.now(), resumeStatus: state.status }
      state.status = 'refreshing'
      await save()
      const ack = await api('/catalog/refresh', { method: 'POST', accessToken })
      if (ack?.ok !== true || typeof ack.goodsRevision !== 'string' || !ack.goodsRevision || !Number.isSafeInteger(ack.total) || ack.total < 0) throw new Error('ack_invalid')
      await loadCatalog(accessToken, true)
      state.status = state.pendingRequest.resumeStatus
      state.pendingRequest = null
    } catch (refreshError) { refreshError.catalogAuthenticated = true; throw refreshError }
  }
}

async function closeOwnedTab() {
  const tabId = state.tabId
  state.tabId = null
  if (tabId === null) return
  try {
    const tab = await chrome.tabs.get(tabId)
    // A tab navigated by the user is no longer ours to close or redirect.
    if (C.pageIdentity(tab.url) === C.pageIdentity(state.pageUrl)) await chrome.tabs.remove(tabId)
  } catch { /* The user may already have closed the collector. */ }
}

async function navigate(url) {
  if (stopped || state.status !== 'running') return
  clearTimers()
  let owned = null
  if (state.tabId !== null) {
    try { owned = await chrome.tabs.get(state.tabId) } catch { /* Create a new owned tab below. */ }
    if (owned && C.pageIdentity(owned.url) !== C.pageIdentity(state.pageUrl)) throw new Error('tab_changed')
  }
  state.pageUrl = url
  state.phase = 'browser'
  state.metrics.navigations += 1
  state.catalogPage = 0
  state.sellerWaits = 0; state.sellerRenderedCards = 0
  // Persist the expected URL before navigation can deliver document_start messages.
  await save()
  if (stopped) return
  if (owned) await chrome.tabs.update(state.tabId, { url, active: Boolean(C.sellerId(url)) })
  else {
    state.tabId = (await chrome.tabs.create({ url: 'about:blank', active: false })).id
    await save()
    if (stopped) return
    await chrome.tabs.update(state.tabId, { url, active: Boolean(C.sellerId(url)) })
  }
  await save()
  await waitForPage()
}

async function sellerCommand(type, extra = {}) {
  const expectedPageUrl = state.pageUrl
  const tab = await chrome.tabs.get(state.tabId).catch(() => { throw new Error('tab_closed') })
  if (C.pageIdentity(tab.url) !== C.pageIdentity(expectedPageUrl)) throw new Error('tab_changed')
  if (stopped || state.status !== 'running' || state.pageUrl !== expectedPageUrl) return null
  return chrome.tabs.sendMessage(state.tabId, { ...extra, type, expectedPageUrl }).catch(error => {
    if (type === 'catalog_progress') return null
    throw error
  })
}

async function advanceSeller() {
  await sellerCommand('scroll')
}

async function pageTimeout() {
  if (C.sellerId(state.pageUrl)) {
    const progress = await sellerCommand('catalog_progress')
    if (stopped || state.status !== 'running') return
    if (progress?.visible === false) return pause('collector_hidden')
  }
  if (C.sellerId(state.pageUrl) && state.catalogPage && state.sellerWaits < 3) {
    const progress = await sellerCommand('catalog_progress')
    if (stopped || state.status !== 'running') return
    if (progress && typeof progress.visible === 'boolean' && Number.isSafeInteger(progress.cards)
      && progress.cards >= 0 && progress.cards <= 100000
      ) {
      // Background throttling / lazy rendering is not an exhausted catalog.
      // Give native scrolling a bounded extra window, never synthesize API URLs.
      state.sellerWaits += 1; state.sellerRenderedCards = progress.cards
      await advanceSeller()
      return waitForPage()
    }
  }
  if (C.sellerId(state.pageUrl)) state.partialSellers += 1
  const nmId = C.cardNmId(state.pageUrl)
  if (nmId && !state.visited.includes(nmId)) state.visited.push(nmId)
  // Sold-out cards can have no price. Keep them unknown and continue this pass.
  await nextPage()
}

async function waitForPage() {
  clearTimeout(timeout)
  const generation = ++timerGeneration
  state.scrollAt = null
  state.pageDeadline = Date.now() + 25000
  await save()
  if (stopped || state.status !== 'running' || generation !== timerGeneration) return
  timeout = setTimeout(() => enqueue(async () => {
    if (generation !== timerGeneration || stopped || state.status !== 'running') return
    await pageTimeout()
  }).catch(fail), 25000)
}

async function nextPage() {
  if (stopped || state.status !== 'running') return
  const remaining = state.catalog.items.filter((item) => !seenThisPass(item))
  if (remaining.length && state.pendingSellers.length) {
    const supplier = state.pendingSellers.shift()
    state.sellers.push(supplier)
    return navigate(`${C.WB_ORIGIN}/seller/${supplier}`)
  }
  const item = remaining.find((item) => !state.visited.includes(item.nmId))
  if (item) return navigate(`${C.WB_ORIGIN}/catalog/${item.nmId}/detail.aspx`)
  clearTimers()
  state.status = 'waiting'
  state.metrics.finishedAt = Date.now()
  state.nextRunAt = null
  state.reason = remaining.length ? 'partial_coverage' : null
  await closeOwnedTab()
  await save()
}

async function submit(observations, receivedAt) {
  if (Date.now() - receivedAt > 300000) throw new Error('observation_stale')
  if (state.catalog.items.some((item) => Date.now() - Date.parse(item.sellerPriceObservedAt) > state.catalog.maxAgeSeconds * 1000)) await loadCatalog()
  const observedAt = new Date(receivedAt).toISOString()
  const current = C.snapshotItems(observations, state.catalog.items, observedAt)
  state.unmatched += current.ignored
  const pending = current.items.filter((item) => {
    const seen = state.seen[C.key(item)]
    return !seen || seen.buyer !== item.buyerPriceNoWalletKopecks || seen.seller !== item.sellerPriceKopecks || !seenFresh(item)
      || (item.originalPriceKopecks != null && seen.original !== item.originalPriceKopecks)
      || (item.buyerPriceWithWalletKopecks != null && seen.wallet !== item.buyerPriceWithWalletKopecks)
  })
  for (let start = 0; start < pending.length && !stopped; start += 100) {
    if (Date.now() - receivedAt > 300000) throw new Error('observation_stale')
    let items = C.snapshotItems(pending.slice(start, start + 100), state.catalog.items, observedAt).items
    if (!items.length) continue
    let ack
    state.pendingRequest = { kind: 'snapshot', startedAt: Date.now() }
    // Only the not-yet-submitted tail is replayable. Never persist the batch
    // whose POST result may be unknown after worker suspension.
    state.deferredObservations = { items: pending.slice(start + 100), receivedAt }
    await save()
    try { ack = await api('/snapshots', { method: 'POST', body: { goodsRevision: state.catalog.goodsRevision, items } }) }
    catch (error) {
      if (error.httpStatus !== 409 || stopped) throw error
      await loadCatalog()
      items = C.snapshotItems(items, state.catalog.items, observedAt).items
      if (!items.length || stopped) { state.pendingRequest = null; continue }
      state.pendingRequest = { kind: 'snapshot', startedAt: Date.now() }
      await save()
      ack = await api('/snapshots', { method: 'POST', body: { goodsRevision: state.catalog.goodsRevision, items } })
    }
    if (ack?.ok !== true || !Number.isSafeInteger(ack.accepted) || ack.accepted < 0
      || !Number.isSafeInteger(ack.ignored) || ack.ignored < 0 || ack.accepted + ack.ignored !== items.length
      || !Number.isFinite(Date.parse(ack.observedAt))) throw new Error('ack_invalid')
    state.accepted += ack.accepted
    state.ignored += ack.ignored
    state.lastAckAt = ack.observedAt
    for (const item of items) state.seen[C.key(item)] = { buyer: item.buyerPriceNoWalletKopecks, wallet: item.buyerPriceWithWalletKopecks, original: item.originalPriceKopecks, seller: item.sellerPriceKopecks, time: receivedAt, parserVersion: 2 }
    state.pendingRequest = null
    state.retryAttempts = 0
    await save()
  }
}

async function submitDeferred() {
  const deferred = state.deferredObservations
  state.deferredObservations = null
  if (deferred?.items?.length && Date.now() - deferred.receivedAt <= 300000) await submit(deferred.items, deferred.receivedAt)
}

async function collectPublicPrices() {
  state.phase = 'public'
  state.pageDeadline = Date.now()
  await save()
  if (state.publicUnavailableUntil > Date.now()) return false
  const nmIds = [...new Set(state.catalog.items.filter((item) => !seenThisPass(item)).map((item) => item.nmId))]
  for (let start = 0; start < nmIds.length && !stopped; start += PUBLIC_BATCH_SIZE) {
    const batch = nmIds.slice(start, start + PUBLIC_BATCH_SIZE)
    const query = new URLSearchParams({ appType: '1', curr: 'rub', dest: PUBLIC_DESTINATION, lang: 'ru', nm: batch.join(';') })
    let response
    const started = Date.now()
    state.metrics.publicRequests += 1
    try {
      response = await fetch(`${PUBLIC_CARD_API}?${query}`, { credentials: 'omit', redirect: 'error',
        headers: { Accept: 'application/json' }, signal: AbortSignal.timeout(20000) })
    } catch { throw new Error('request_failed') }
    state.metrics.requestMs += Date.now() - started
    if (response.status === 403) {
      // Do not retry or disguise this access denial. Existing ordinary-page
      // observation remains the fallback; remember the unavailable transport.
      state.publicUnavailableUntil = Date.now() + 30 * 60 * 1000
      await save(); return false
    }
    if (response.status === 429) {
      const error = new Error('http_429')
      const header = response.headers?.get('Retry-After')
      const retry = /^\d+$/.test(header || '') ? Number(header) : Math.ceil((Date.parse(header) - Date.now()) / 1000)
      if (Number.isSafeInteger(retry) && retry > 0 && retry <= 86400) error.retryAfterSeconds = retry
      throw error
    }
    if (!response.ok) throw new Error('request_failed')
    let payload
    try { payload = await response.json() } catch { return false }
    if (!Array.isArray(payload?.products) || payload.products.length > 1000) return false
    const items = C.parsePublicDetail(payload, batch)
    rememberSuppliers(items)
    if (items.length) await submit(items, Date.now())
    state.pageDeadline = Date.now()
    await save()
  }
  return true
}

function rememberSuppliers(items) {
  const ids = new Set(state.catalog.items.map(item => item.nmId))
  for (const item of items) if (ids.has(item.nmId) && C.integer(item.supplierId)) state.suppliers[item.nmId] = item.supplierId
}

async function observe(message, sender, receivedAt) {
  if (state.status !== 'running' || !C.isTrustedPageSender(sender, chrome.runtime.id, state.tabId, state.pageUrl)) return { ok: false, code: 'sender_not_allowed' }
  const observation = C.sanitizePageMessage(message, state.pageUrl, true)
  if (!observation) return { ok: false, code: 'observation_invalid' }
  if (observation.type === 'blocked') {
    if (observation.code === 'http_429') {
      const error = new Error('http_429'); error.retryAfterSeconds = observation.retryAfterSeconds
      return fail(error)
    }
    await pause(observation.code); return { ok: true }
  }
  if (stopped) return { ok: false, code: 'stopped' }
  const pageUrl = state.pageUrl
  await submit(observation.items, receivedAt)
  rememberSuppliers(observation.items)
  if (stopped) return { ok: true }
  const nmIds = new Set(state.catalog.items.map((item) => item.nmId))
  for (const item of observation.items) {
    if (nmIds.has(item.nmId) && C.integer(item.supplierId) && !state.sellers.includes(item.supplierId) && !state.pendingSellers.includes(item.supplierId)) state.pendingSellers.push(item.supplierId)
  }
  const nmId = C.cardNmId(pageUrl)
  if (nmId && (observation.loadedNmId === nmId || observation.items.some((item) => item.nmId === nmId))) {
    if (!state.visited.includes(nmId)) state.visited.push(nmId)
    await nextPage()
  } else if (C.sellerId(pageUrl) && observation.catalogPage > state.catalogPage) {
    state.catalogPage = observation.catalogPage
    state.metrics.sellerPages = (state.metrics.sellerPages || 0) + 1
    state.sellerWaits = 0
    clearTimers()
    const supplier = C.sellerId(pageUrl)
    const remaining = state.catalog.items.filter(item => !seenThisPass(item))
    const sellerFinished = remaining.length && remaining.every(item => state.suppliers[item.nmId]
      && state.suppliers[item.nmId] !== supplier)
    if (observation.productCount === 0 || !remaining.length || sellerFinished) await nextPage()
    else {
      state.scrollAt = Date.now() + 1500
      const generation = timerGeneration
      await save()
      scrollTimer = setTimeout(() => enqueue(async () => {
        if (generation !== timerGeneration || stopped || state.status !== 'running' || state.pageUrl !== pageUrl) return
        await advanceSeller()
        await waitForPage()
      }).catch(fail), 1500)
    }
  }
  await save()
  return { ok: true }
}

async function command(message, generation = controlGeneration) {
  if (message.type === 'start' && state.status === 'paused' && state.metrics.startedAt && !state.metrics.finishedAt) message = { ...message, type: 'resume' }
  if (message.type === 'status') return { ok: true, state: publicState() }
  if (message.type === 'show_tab') {
    if (state.tabId === null) return { ok: false, code: 'tab_closed', state: publicState() }
    let tab
    try { tab = await chrome.tabs.get(state.tabId) } catch { return { ok: false, code: 'tab_closed', state: publicState() } }
    if (!C.pageIdentity(state.pageUrl) || C.pageIdentity(tab.url) !== C.pageIdentity(state.pageUrl)) return { ok: false, code: 'tab_changed', state: publicState() }
    await chrome.tabs.update(state.tabId, { active: true })
    return { ok: true, state: publicState() }
  }
  if (message.type === 'stop') { await pause('user_stopped'); return { ok: true, state: publicState() } }
  if (message.type === 'disconnect') {
    stopped = true; clearTimers()
    await closeOwnedTab()
    token = null; await chrome.storage.local.remove('token')
    state = emptyState(); await save()
    return { ok: true, state: publicState() }
  }
  if (!['connect', 'start', 'resume'].includes(message.type)) return { ok: false, code: 'command_invalid' }
  if (generation !== controlGeneration) return { ok: false, code: 'stopped', state: publicState() }
  if (['running', 'refreshing', 'retrying'].includes(state.status)
      || (state.status === 'waiting' && !state.metrics.finishedAt)) return { ok: false, code: 'stop_first', state: publicState() }
  if (message.type === 'connect') {
    let nextUrl, candidate, catalog, catalogError
    try {
      nextUrl = WbPricesConnection.normalizeServerUrl(message.backendUrl ?? backendUrl)
      candidate = WbPricesConnection.validateToken(message.token?.trim() || (nextUrl === backendUrl ? token : null))
      // Verify against exactly one selected origin before replacing credentials
      // or clearing the old account's checkpoint. This GET cannot collect prices.
      catalog = await readCatalog(candidate, { serverUrl: nextUrl, verification: true,
        isCurrent: () => generation === controlGeneration })
    } catch (error) {
      if (generation !== controlGeneration) return { ok: false, code: 'stopped', state: publicState() }
      if (error.serverCode === 'WB_BROWSER_GOODS_STALE') catalogError = error // Authenticated, existing refresh workflow below.
      else return { ok: false, code: error.httpStatus === 401 ? 'connection_rejected' : safeError(error), state: publicState() }
    }
    if (generation !== controlGeneration) return { ok: false, code: 'stopped', state: publicState() }
    await closeOwnedTab()
    if (generation !== controlGeneration) return { ok: false, code: 'stopped', state: publicState() }
    // Never leave an old session's in-flight request bound to a new token after
    // a worker restart. Local credential/binding replacement is a single write.
    await chrome.storage.session.remove('state')
    if (generation !== controlGeneration) return { ok: false, code: 'stopped', state: publicState() }
    state = emptyState()
    backendUrl = nextUrl; token = candidate; stopped = false
    await chrome.storage.local.set({ token, backendUrl, control: null, checkpoint: null })
    await chrome.storage.session.remove('stopRequested')
    await chrome.storage.local.remove('stopRequested')
    if (generation !== controlGeneration) {
      await chrome.storage.session.set({ stopRequested: true })
      await chrome.storage.local.set({ stopRequested: true })
      await pause('user_stopped')
      return { ok: false, code: 'stopped', state: publicState() }
    }
    if (catalogError) await loadCatalog(candidate, false, catalogError)
    else applyCatalog(catalog)
    state.status = stopped ? 'paused' : 'ready'
  } else {
    if (!token) throw new Error('token_invalid')
    stopped = false
    state.retryReadOnlyRefresh = false // explicit user start/resume only
    await chrome.storage.session.remove('stopRequested')
    await chrome.storage.local.remove('stopRequested')
    // A requested start/resume includes catalog loading, not just navigation.
    // Transient refresh failures here must recover without a second user click.
    state.status = 'running'; state.reason = null; state.nextRunAt = null
    if (message.type === 'resume' || !state.catalog) await loadCatalog(token, state.pendingRequest?.kind === 'refresh')
    if (stopped) return { ok: false, code: 'stopped', state: publicState() }
    // Upgrades must not enqueue already visited stores or rewind a live pass.
    state.nativeCatalogStrategy = 3
    if (message.type === 'start') {
      // Re-observe prices from older parsers once on explicit Start. Resume
      // retains progress; subsequent fresh passes keep the batching/dedup guard.
      state.seen = Object.fromEntries(Object.entries(state.seen).filter(([, item]) => item.parserVersion === 2))
      state.visited = []; state.sellers = []
      state.pendingSellers = [...new Set(state.catalog.items.filter(item => !seenFresh(item)).map(item => state.suppliers[item.nmId]).filter(C.integer))]
      state.accepted = state.ignored = state.unmatched = state.partialSellers = 0
      state.metrics = { publicRequests: 0, snapshotRequests: 0, navigations: 0, catalogRequests: 0, requestMs: 0, startedAt: Date.now() }
    }
    state.status = 'running'; state.reason = null; state.retryAttempts = 0
    state.metrics.startedAt ||= Date.now()
    state.metrics.finishedAt = null
    state.retryAt = state.retryAfterSeconds = state.nextRunAt = state.pendingRequest = null
    await submitDeferred()
    if (stopped) return { ok: false, code: 'stopped', state: publicState() }
    if (message.type !== 'resume' || state.phase === 'public') await collectPublicPrices()
    state.phase = 'browser'
    const remaining = state.catalog.items.some((item) => !seenThisPass(item))
    if (remaining && message.type === 'resume' && C.pageIdentity(state.pageUrl)) await continueCurrentPage()
    else await nextPage()
  }
  await save()
  return { ok: true, state: publicState() }
}

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  const popup = C.isTrustedPopupSender(sender, chrome.runtime.id)
  if (popup && message?.type === 'status') {
    void ready.then(() => respond({ ok: true, state: publicState() })).catch(() => respond({ ok: false, code: 'request_failed' }))
    return true
  }
  const receivedAt = Date.now()
  if (popup && ['stop', 'disconnect'].includes(message?.type)) {
    stopped = true; controlGeneration += 1; clearTimers()
    void chrome.storage.session.set({ stopRequested: true })
    void chrome.storage.local.set({ stopRequested: true })
  }
  if (!popup && C.isTrustedPageSender(sender, chrome.runtime.id, state.tabId, state.pageUrl)
    && C.sanitizePageMessage(message, state.pageUrl)?.type === 'blocked'
    && message.code !== 'http_429') { stopped = true; clearTimers() }
  const generation = controlGeneration
  void enqueue(async () => {
    if (!popup && message?.source === C.SOURCE && message.type === 'collector_visible'
      && C.isTrustedPageSender(sender, chrome.runtime.id, state.tabId, state.pageUrl)
      && C.sellerId(state.pageUrl) && state.status === 'paused' && state.reason === 'collector_hidden'
      && generation === controlGeneration) {
      const progress = await chrome.tabs.sendMessage(state.tabId, { type: 'catalog_progress', expectedPageUrl: state.pageUrl }).catch(() => null)
      if (progress?.visible !== true || generation !== controlGeneration) return { ok: false, code: 'stopped' }
      stopped = false; state.status = 'running'; state.reason = null
      await advanceSeller(); await waitForPage(); return { ok: true }
    }
    return popup ? command(message || {}, generation) : observe(message, sender, receivedAt)
  }).catch(fail).then(respond)
  return true
})

async function runScheduled() {
    if (stopped) return
    if (state.status === 'retrying' && Date.now() >= state.retryAt) {
      state.status = 'running'; state.retryAt = null; state.reason = null
      await loadCatalog(token, Boolean(state.retryReadOnlyRefresh))
      if (state.restartCycle) {
        state.restartCycle = false; state.seen = {}; state.status = 'ready'
        await command({ type: 'start' })
      } else if (state.phase === 'public') {
        await collectPublicPrices(); state.phase = 'browser'; await nextPage()
      } else if (C.pageIdentity(state.pageUrl)) await continueCurrentPage()
      else await nextPage()
    } else if (state.status === 'running' && state.phase === 'public') {
      await collectPublicPrices(); state.phase = 'browser'; await nextPage()
    } else if (state.status === 'refreshing' && Date.now() >= state.pendingRequest.startedAt + 125000) {
      const previous = state.pendingRequest.resumeStatus
      await loadCatalog(token, true) // The prior POST may have completed; never repeat it on recovery.
      state.pendingRequest = null
      state.status = previous === 'running' ? 'running' : 'ready'
      if (state.status === 'running') {
        if (C.pageIdentity(state.pageUrl)) await continueCurrentPage()
        else await nextPage()
      }
    } else if (state.status === 'waiting' && !state.metrics.finishedAt && state.nextRunAt && Date.now() >= state.nextRunAt) {
      // The scheduled catalog read belongs to the active run too. A transient
      // error must not turn an unattended recurring/recovered run into a pause.
      state.restartCycle = !state.recovering
      state.status = 'running'; state.phase = 'public'
      await loadCatalog()
      state.status = 'ready'
      if (state.recovering) { state.recovering = false; await command({ type: 'resume' }) }
      else { state.restartCycle = false; state.seen = {}; await command({ type: 'start' }) }
    } else if (state.status === 'running') {
      if (state.scrollAt && Date.now() >= state.scrollAt) {
        await advanceSeller()
        await waitForPage()
      } else if (state.pageDeadline && Date.now() >= state.pageDeadline) await pageTimeout()
    }
    await save()
}

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM) return enqueue(runScheduled).catch(fail)
})

chrome.runtime.onStartup.addListener(() => enqueue(runScheduled).catch(fail))

chrome.tabs.onRemoved.addListener((tabId) => {
  if (tabId === state.tabId) {
    stopped = true; clearTimers()
    void enqueue(async () => { state.tabId = null; await pause('tab_closed') }).catch(fail)
  }
})
