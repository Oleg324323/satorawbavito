importScripts('runtime-retry.js')
importScripts('connection.js')
importScripts('shipment-number.js')
importScripts('labels.js')
importScripts('listing-photos.js')

const DEFAULT_BACKEND_URL = SatornaConnection.defaultUrl
const SNAPSHOT_PATH = '/api/v1/avito/orders/browser-snapshot'
const AVITO_ORDERS_URL = 'https://www.avito.ru/orders'
const AVITO_RETURNS_URL = 'https://www.avito.ru/orders?status%5B%5D=on_return'
const LOG_KEY = 'satornaAvitoLogs'
let detailCollectionBlocked = false

async function writeLog(level, message, data = {}) {
  const entry = {
    at: new Date().toISOString(),
    level,
    message,
    data,
  }
  const method = level === 'error' ? 'error' : level === 'warn' ? 'warn' : 'info'
  console[method]('[Satorna Avito]', message, data)
  try {
    const stored = await chrome.storage.local.get({ [LOG_KEY]: [] })
    const logs = Array.isArray(stored[LOG_KEY]) ? stored[LOG_KEY] : []
    await chrome.storage.local.set({ [LOG_KEY]: [...logs, entry].slice(-200) })
  } catch (error) {
    console.warn('[Satorna Avito] log storage failed', error)
  }
}

function normalizeBackendUrl(value) {
  return SatornaConnection.normalize(value || DEFAULT_BACKEND_URL)
}

function looksLikeFrontendUrl(value) {
  try {
    const url = new URL(value)
    return url.hostname.includes('vercel.app') || url.hostname.includes('frontend') || url.pathname.includes('/wb/') || url.pathname.includes('/avito/')
  } catch (_error) {
    return false
  }
}

async function resolveBackendUrl(value) {
  const normalized = normalizeBackendUrl(value)
  if (!looksLikeFrontendUrl(normalized)) return normalized
  try {
    const response = await fetch(`${normalized}/api/config.js`, { method: 'GET', cache: 'no-store', redirect: 'error', credentials: 'omit' })
    const text = await response.text()
    const match = text.match(/VITE_API_BASE_URL["']?\s*[:=]\s*["']([^"']+)["']/)
      || text.match(/apiBaseUrl["']?\s*[:=]\s*["']([^"']+)["']/i)
    if (match?.[1]) return normalizeBackendUrl(match[1])
  } catch (_error) {
    // Keep the explicit frontend-url error below.
  }
  return normalized
}

function authorizationValue(value) {
  const token = String(value || '').trim()
  if (!token) return ''
  return /^bearer\s+/i.test(token) ? token : `Bearer ${token}`
}

async function readSettings() {
  const settings = await chrome.storage.sync.get({
    backendUrl: DEFAULT_BACKEND_URL,
    accessToken: '',
    collectOptions: {
      photoMode: 'one',
      colorFromDescription: true,
      sizeMode: 'chat_ai',
      articleFromDescription: true,
    },
    lastStatus: '',
    lastSnapshotAt: '',
  })
  const migration = await chrome.storage.sync.get({ chatSizeDefaultVersion: 0 })
  if (migration.chatSizeDefaultVersion < 1) {
    // One-time migration of the old implicit listing-size default.
    settings.collectOptions = { ...settings.collectOptions, sizeMode: settings.collectOptions?.sizeMode === 'none' ? 'none' : 'chat_ai' }
    await chrome.storage.sync.set({ collectOptions: settings.collectOptions, chatSizeDefaultVersion: 1 })
  }
  return { ...settings, ...await SatornaConnection.read() }
}

async function apiUrlFromSettings(settings) {
  if (!settings?.backendUrl) throw new Error('Сохраните явный адрес Satorna вместе с токеном.')
  // A new connection code pins same-origin /api; never discover a replacement
  // backend or apply a retired-domain migration to its credential.
  if (settings.originPinned) return SatornaConnection.destination(settings)
  return resolveBackendUrl(settings.backendUrl)
}

async function readApiJson(response, { backendUrl, action }) {
  const text = await response.text()
  if (!response.ok) {
    const message = text ? `Backend вернул ${response.status}: ${text.slice(0, 240)}` : `Backend вернул ${response.status}`
    await writeLog('error', 'backend request failed', { action, status: response.status, backendUrl, message })
    throw new Error(message)
  }
  try {
    return text ? JSON.parse(text) : {}
  } catch (_error) {
    const preview = text.slice(0, 80).replace(/\s+/g, ' ')
    await writeLog('error', 'backend returned non-json response', {
      action,
      backendUrl,
      status: response.status,
      preview,
    })
    throw new Error('Satorna вернула страницу вместо API-ответа. Проверьте адрес backend в настройках расширения или обновите деплой Satorna.')
  }
}

async function saveStatus(status) {
  await chrome.storage.sync.set({
    lastStatus: status.message,
    lastSnapshotAt: status.ok ? new Date().toISOString() : '',
  })
}

let snapshotUploadTail = Promise.resolve()
function postSnapshot(payload) {
  const upload = snapshotUploadTail.then(() => postSnapshotNow(payload))
  snapshotUploadTail = upload.catch(() => {})
  return upload
}

async function postSnapshotNow(payload) {
  await writeLog('info', 'posting snapshot', {
    orders: payload?.orders?.length || 0,
    returns: payload?.returns?.length || 0,
    items: [...(payload?.orders || []), ...(payload?.returns || [])].reduce((sum, order) => sum + (order.items?.length || 0), 0),
  })
  const settings = await readSettings()
  const token = String(settings.accessToken || '').trim()
  if (!token) {
    throw new Error('Добавьте токен Satorna в настройках расширения')
  }
  const backendUrl = await apiUrlFromSettings(settings)
  const response = await fetch(`${backendUrl}${SNAPSHOT_PATH}`, {
    method: 'POST',
    redirect: 'error',
    credentials: 'omit',
    headers: {
      'Authorization': authorizationValue(token),
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(45000),
  })
  if (response.status === 405 && looksLikeFrontendUrl(backendUrl)) {
    throw new Error('Фронт ещё не принимает заказы расширения. Обновите деплой фронта или проверьте, что на нём включён proxy в API backend.')
  }
  await writeLog('info', 'snapshot posted', { status: response.status })
  return readApiJson(response, { backendUrl, action: 'post_snapshot' })
}

async function postSnapshotWithFeedback(payload, notify = true) {
  const settings = await readSettings()
  const target = SatornaConnection.destination(settings)
  try {
    const result = await postSnapshot(payload)
    if (result?.ok !== true) throw new Error('Сервер не подтвердил сохранение')
    if (notify) await notifyUploadStatus(true, `Сохранено в Satorna: ${target}. Обновите страницу заказов.`)
    return result
  } catch (error) {
    await notifyUploadStatus(false, `Не сохранено в ${target}. ${error instanceof Error ? error.message : 'Ошибка отправки'}. Проверьте адрес и токен в настройках расширения.`)
    throw error
  }
}

async function uploadOrderPhotos() {
  const settings = await readSettings()
  const backendUrl = await apiUrlFromSettings(settings)
  const headers = { Authorization: authorizationValue(settings.accessToken) }
  const base = `${backendUrl}/api/v1/avito/repricer/photos`
  const contextResponse = await fetch(`${base}/orders/collection-context`, { headers, credentials: 'omit', redirect: 'error', signal: AbortSignal.timeout(15000) })
  const context = await readApiJson(contextResponse, { backendUrl, action: 'order_photo_context' })
  if (!Array.isArray(context.missing)) throw new Error('Satorna не вернула список недостающих фото заказов')
  const result = { total: context.total || 0, saved: 0, missing: 0, errors: [] }
  let cursor = 0, paused = false
  await Promise.all(Array.from({ length: Math.min(5, context.missing.length) }, async () => {
    while (cursor < context.missing.length && !paused) {
      const row = context.missing[cursor++]
      const url = SatornaListingPhotos.imageUrl(row.imageUrl)
      if (!url) { result.missing += 1; continue }
      try {
        const photo = await fetch(url, { credentials: 'omit', redirect: 'error', signal: AbortSignal.timeout(15000) })
        if (photo.status === 429) { paused = true; throw new Error('Avito ограничил загрузку фото (HTTP 429)') }
        if (!photo.ok || !/^image\/(jpeg|png|webp)(?:;|$)/i.test(photo.headers.get('content-type') || '')) throw new Error(`Фото недоступно (HTTP ${photo.status})`)
        if (Number(photo.headers.get('content-length')) > 8 * 1024 * 1024) throw new Error('Фото превышает 8 МБ')
        const body = await photo.blob()
        if (!body.size || body.size > 8 * 1024 * 1024) throw new Error('Фото пустое или превышает 8 МБ')
        const query = new URLSearchParams({ accountId: String(row.accountId || ''), itemId: String(row.itemId || '') })
        const saved = await fetch(`${base}/import?${query}`, { method: 'POST', headers: { ...headers, 'Content-Type': 'application/octet-stream' }, body, credentials: 'omit', redirect: 'error', signal: AbortSignal.timeout(20000) })
        if (!saved.ok || !(await saved.json())?.photoId) throw new Error(`Satorna не сохранила фото (HTTP ${saved.status})`)
        result.saved += 1
      } catch (error) {
        result.missing += 1
        if (result.errors.length < 3) result.errors.push(error instanceof Error ? error.message : String(error))
      }
    }
  }))
  result.missing += Math.max(0, context.missing.length - result.saved - result.missing)
  return result
}

async function notifyUploadStatus(ok, message) {
  try {
    const tabs = await tabsQuery({ url: 'https://www.avito.ru/*' })
    await Promise.all(tabs.map(tab => tabsSendMessage(tab.id, { type: 'AVITO_UPLOAD_STATUS', ok, message }).catch(() => {})))
  } catch { /* Display failures must not turn a successful save into a retry. */ }
}

function tabsQuery(query) {
  return new Promise((resolve) => chrome.tabs.query(query, resolve))
}

function tabsCreate(createProperties) {
  return new Promise((resolve) => chrome.tabs.create(createProperties, resolve))
}

function tabsUpdate(tabId, updateProperties) {
  return new Promise((resolve) => chrome.tabs.update(tabId, updateProperties, resolve))
}

function tabsRemove(tabId) {
  return new Promise((resolve) => chrome.tabs.remove(tabId, resolve))
}

function tabsSendMessage(tabId, message, options) {
  return new Promise((resolve, reject) => {
    chrome.tabs.sendMessage(tabId, message, options || {}, (response) => {
      const error = chrome.runtime.lastError
      if (error) {
        reject(new Error(error.message))
        return
      }
      resolve(response)
    })
  })
}

function executeContentScript(tabId, allFrames = false) {
  return chrome.scripting.executeScript({
    target: { tabId, allFrames },
    files: [
      'src/item-size.js',
      'src/item-photo.js',
      'src/order-chat.js',
      'src/size-policy.js',
      'src/page-state.js',
      'src/shipment-number.js',
      'src/return-details.js',
      'src/order-cache.js',
      'src/content.js',
    ],
  })
}

async function classifyFrames(tabId, expectedTitle) {
  const frames = await chrome.scripting.executeScript({
    target: { tabId, allFrames: true },
    args: [String(expectedTitle || '')],
    func: (rawExpectedTitle) => {
      const text = document.body?.innerText || ''
      const expected = String(rawExpectedTitle || '').toLowerCase()
      const hasExpectedTitle = expected ? text.toLowerCase().includes(expected) : false
      const hasOrderWords = /заказ|трек|доставка|получатель|отправьте|чат|стоимость|итого/i.test(text)
      const hasOnlyPublicShell = text.includes('Продавать') && text.includes('Покупать') && text.includes('Карьера') && !hasExpectedTitle && !hasOrderWords
      return {
        url: location.href,
        documentTitle: document.title,
        bodyLength: text.length,
        bodyPreview: text.slice(0, 1000),
        hasExpectedTitle,
        hasOrderWords,
        hasOnlyPublicShell,
        anchorsCount: document.querySelectorAll('a[href]').length,
        imagesCount: document.querySelectorAll('img').length,
      }
    },
  })
  return frames.map((frame) => ({ frameId: frame.frameId, ...(frame.result || {}) }))
}

function bestFrame(frames, isOrderDetail) {
  if (!Array.isArray(frames) || !frames.length) return null
  const sorted = [...frames].sort((a, b) => {
    const score = (frame) => {
      let value = 0
      if (frame.hasExpectedTitle) value += 100
      if (frame.hasOrderWords) value += 50
      if (!frame.hasOnlyPublicShell) value += 10
      value += Math.min(20, Math.floor((frame.bodyLength || 0) / 500))
      if (!isOrderDetail && /_[0-9]{6,12}/.test(frame.url || '')) value += 100
      return value
    }
    return score(b) - score(a)
  })
  return sorted[0]
}

function waitForTabComplete(tabId, timeoutMs = 30000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener)
      reject(new Error('Avito долго загружается. Откройте страницу заказов и попробуйте ещё раз.'))
    }, timeoutMs)
    const listener = (updatedTabId, changeInfo) => {
      if (updatedTabId !== tabId || changeInfo.status !== 'complete') return
      clearTimeout(timer)
      chrome.tabs.onUpdated.removeListener(listener)
      resolve()
    }
    chrome.tabs.onUpdated.addListener(listener)
    chrome.tabs.get(tabId, (tab) => {
      if (chrome.runtime.lastError) return
      if (tab?.status === 'complete') {
        clearTimeout(timer)
        chrome.tabs.onUpdated.removeListener(listener)
        resolve()
      }
    })
  })
}

async function waitForTabInteractive(tabId, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    const state = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => ({ ready: document.readyState !== 'loading' && Boolean(document.body), host: location.hostname }),
    }).then(results => results[0]?.result).catch(() => null)
    if (state?.ready && state.host === 'www.avito.ru') return
    await new Promise(resolve => setTimeout(resolve, 400))
  }
  throw new Error('Страница Avito не стала доступной за 15 секунд. Уже собранные данные сохранятся; повторите сбор позднее.')
}

function assertAvitoUrl(value) {
  const url = new URL(String(value || ''))
  if (url.protocol !== 'https:' || url.hostname !== 'www.avito.ru') {
    throw new Error('Расширение может открывать детали только на www.avito.ru')
  }
  return url.toString()
}

function isAvitoListingUrl(value) {
  try {
    const url = new URL(String(value || ''))
    if (url.protocol !== 'https:' || url.hostname !== 'www.avito.ru') return false
    if (/\/orders(?:\/|$)/i.test(url.pathname)) return false
    return /_[0-9]{5,}(?:\/)?$/i.test(url.pathname) || /\/items\/[0-9]+(?:\/)?$/i.test(url.pathname)
  } catch (_error) {
    return false
  }
}

async function waitForTabListingUrl(tabId, timeoutMs = 9000) {
  const startedAt = Date.now()
  while (Date.now() - startedAt < timeoutMs) {
    const tab = await chrome.tabs.get(tabId)
    if (isAvitoListingUrl(tab?.url)) return tab.url
    await new Promise((resolve) => setTimeout(resolve, 500))
  }
  return null
}

async function extractPageRuntimeDiagnostics(tabId, frameId, title, pageUrl) {
  const target = Number.isInteger(frameId) ? { tabId, frameIds: [frameId] } : { tabId }
  try {
    const diagnostics = await globalThis.SatornaRuntimeRetry.retryPageStateInspection(async () => {
      await chrome.scripting.executeScript({
        target,
        world: 'MAIN',
        files: ['src/page-state.js'],
      })
      const results = await chrome.scripting.executeScript({
        target,
        world: 'MAIN',
        func: async (expectedTitle, orderPageUrl) => {
          const api = globalThis.SatornaAvitoPageState
          if (!api) {
            return {
              apiAvailable: false,
              candidates: [],
              reactRoots: 0,
              resourceUrls: [],
            }
          }
          const inspected = api.inspectDocument(document, expectedTitle) || {
            candidates: [],
            reactRoots: 0,
            resourceUrls: [],
          }
          inspected.apiAvailable = true
          const stateCandidates = Array.isArray(inspected.candidates) ? inspected.candidates : []
          let timezone = ''
          try {
            timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || ''
          } catch (_error) {
            // The profile order endpoint also works without this hint.
          }
          const directResource = api.profileOrderResourceUrl(orderPageUrl, timezone)
          inspected.resourceUrls = Array.from(new Set([
            ...(inspected.resourceUrls || []),
            directResource,
          ].filter(Boolean)))
          if (inspected.resourceUrls.length) {
            const loaded = await api.loadCandidatesFromOrderResources(inspected.resourceUrls, expectedTitle)
            inspected.candidates = loaded?.candidates?.length ? loaded.candidates : stateCandidates
            inspected.candidateSource = loaded?.candidates?.length ? 'profile-order-api' : 'page-state'
            inspected.resourceRequests = loaded?.requests || []
            inspected.resourceText = loaded?.resourceText || ''
          }
          return inspected
        },
        args: [title || '', pageUrl || ''],
      })
      return results.map((entry) => entry?.result).find((result) => result && typeof result === 'object') || {
        apiAvailable: false,
        candidates: [],
        reactRoots: 0,
        resourceUrls: [],
      }
    }, 3, async () => {
      await new Promise((resolve) => setTimeout(resolve, 250))
    })
    return diagnostics || { candidates: [], reactRoots: 0, resourceUrls: [] }
  } catch (error) {
    await writeLog('warn', 'react page state extraction failed', {
      tabId,
      frameId: Number.isInteger(frameId) ? frameId : null,
      error: error instanceof Error ? error.message : String(error),
    })
    return { candidates: [], reactRoots: 0, resourceUrls: [], error: error instanceof Error ? error.message : String(error) }
  }
}

async function extractDetailsInTab(url, options) {
  if (detailCollectionBlocked) throw new Error('AVITO_BLOCKED: сбор приостановлен до следующего запуска')
  const resolvedUrl = assertAvitoUrl(url)
  await writeLog('info', 'opening detail tab', { url: resolvedUrl })
  const isOrderDetail = /\/orders\//i.test(resolvedUrl)
  const tab = await tabsCreate({ url: resolvedUrl, active: false })
  if (!tab?.id) throw new Error('Не удалось открыть деталку Avito')
  try {
    await waitForTabInteractive(tab.id)
    await executeContentScript(tab.id, true)
    let response = null
    const startedAt = Date.now()
    let selectedFrame = null
    let frameDebug = []
    while (Date.now() - startedAt < 9000) {
      frameDebug = await classifyFrames(tab.id, options?.orderItemTitle || '')
      selectedFrame = bestFrame(frameDebug, isOrderDetail)
      const messageOptions = selectedFrame?.frameId ? { frameId: selectedFrame.frameId } : undefined
      response = await tabsSendMessage(tab.id, { type: 'AVITO_PAGE_EXTRACT_DETAILS', options: options || {} }, messageOptions)
      if (!response?.ok) {
        if (/AVITO_BLOCKED|HTTP (?:429|439)/i.test(response?.error || '')) detailCollectionBlocked = true
        throw new Error(response?.error || 'Не удалось прочитать деталку Avito')
      }
      response.frameDebug = frameDebug
      response.selectedFrame = selectedFrame
      const hasListingLink = Array.isArray(response.itemUrls) && response.itemUrls.length > 0
      const hasListingContent = !isOrderDetail && ((response.description?.length || 0) > 20 || (response.images?.length || 0) > 0)
      const hasOrderContent = !isOrderDetail || selectedFrame?.hasExpectedTitle || selectedFrame?.hasOrderWords
      const waitingForInstruction = isOrderDetail && SatornaAvitoShipmentNumber.shouldWaitForInstruction(
        response, options?.requireShipmentNumber === true, Date.now() - startedAt,
      )
      if (!waitingForInstruction && ((isOrderDetail && hasOrderContent && (hasListingLink || Date.now() - startedAt > 3500)) || hasListingContent)) break
      await new Promise((resolve) => setTimeout(resolve, 700))
    }
    if (!response?.ok) throw new Error(response?.error || 'Не удалось прочитать деталку Avito')
    if (isOrderDetail && !selectedFrame?.hasExpectedTitle && !selectedFrame?.hasOrderWords) {
      response.detailNotRendered = true
      response.detailNotRenderedReason = 'DETAIL_PAGE_NOT_RENDERED'
      await writeLog('warn', 'order detail did not render expected content', {
        url: resolvedUrl,
        expectedTitle: options?.orderItemTitle || '',
        selectedFrame,
        frames: frameDebug,
      })
    }
    if (isOrderDetail && (!Array.isArray(response.itemUrls) || response.itemUrls.length === 0)) {
      const runtime = await extractPageRuntimeDiagnostics(
        tab.id,
        selectedFrame?.frameId,
        options?.orderItemTitle || '',
        resolvedUrl,
      )
      const stateCandidates = Array.isArray(runtime.candidates) ? runtime.candidates : []
      response.reactStateCandidates = stateCandidates.slice(0, 20)
      response.reactItemIds = stateCandidates.map((candidate) => candidate.itemId).filter(Boolean)
      response.runtimeResourceUrls = Array.isArray(runtime.resourceUrls) ? runtime.resourceUrls : []
      if (runtime.resourceText) {
        response.pageText = [response.pageText, runtime.resourceText].filter(Boolean).join('\n')
      }
      await writeLog('info', 'order detail runtime diagnostics', {
        orderUrl: resolvedUrl,
        frameId: Number.isInteger(selectedFrame?.frameId) ? selectedFrame.frameId : null,
        reactRoots: runtime.reactRoots || 0,
        statePropertyKeys: Array.isArray(runtime.statePropertyKeys) ? runtime.statePropertyKeys.slice(0, 40) : [],
        reactCandidates: stateCandidates.length,
        reactItemIds: response.reactItemIds.slice(0, 10),
        resourceUrls: response.runtimeResourceUrls.slice(-20),
        resourceRequests: Array.isArray(runtime.resourceRequests) ? runtime.resourceRequests.slice(-10) : [],
        resourceTextLength: String(runtime.resourceText || '').length,
        candidateSource: runtime.candidateSource || null,
        apiAvailable: Boolean(runtime.apiAvailable),
        inspectorAttempts: runtime.inspectorAttempts || 0,
        error: runtime.error || null,
      })
      const stateItemUrl = stateCandidates.find((candidate) => candidate.itemUrl)?.itemUrl
      if (stateItemUrl) {
        response.itemUrls = [stateItemUrl]
        response.itemUrlCandidates = [
          {
            url: stateItemUrl,
            source: runtime.candidateSource || 'page-state',
            score: stateCandidates[0]?.score || 100,
          },
          ...(response.itemUrlCandidates || []),
        ]
        await writeLog('info', 'order listing url found from order data', {
          orderUrl: resolvedUrl,
          listingUrl: stateItemUrl,
          itemId: stateCandidates[0]?.itemId || null,
        })
      }
    }
    if (isOrderDetail && (!Array.isArray(response.itemUrls) || response.itemUrls.length === 0)) {
      const clicked = await tabsSendMessage(tab.id, {
        type: 'AVITO_OPEN_LISTING_FROM_ORDER',
        title: options?.orderItemTitle || '',
        options: options || {},
      }, selectedFrame?.frameId ? { frameId: selectedFrame.frameId } : undefined)
      await writeLog(clicked?.ok ? 'info' : 'warn', 'order listing click attempted', {
        url: resolvedUrl,
        clicked: Boolean(clicked?.ok),
        title: options?.orderItemTitle || '',
        text: clicked?.text || null,
        error: clicked?.error || null,
      })
      if (clicked?.ok) {
        const listingUrl = await waitForTabListingUrl(tab.id, 9000)
        if (listingUrl) {
          response.itemUrls = [listingUrl]
          await writeLog('info', 'order listing url captured after click', { orderUrl: resolvedUrl, listingUrl })
        }
      }
    }
    await writeLog('info', 'detail extracted', {
      url: resolvedUrl,
      title: response.title,
      images: response.images?.length || 0,
      itemUrls: response.itemUrls?.length || 0,
      itemUrl: response.itemUrls?.[0] || null,
      candidates: response.itemUrlCandidates?.slice?.(0, 5) || [],
      linksCount: response.linksCount || 0,
      imagesCount: response.imagesCount || 0,
      htmlItemIds: response.htmlItemIds || [],
      reactItemIds: response.reactItemIds || [],
      reactStateCandidates: response.reactStateCandidates?.slice?.(0, 5) || [],
      selectedFrame: response.selectedFrame || null,
      detailNotRendered: Boolean(response.detailNotRendered),
      linkSamples: response.linkSamples || [],
    })
    return response
  } catch (error) {
    await writeLog('error', 'detail extraction failed', { url: resolvedUrl, error: error instanceof Error ? error.message : String(error) })
    throw error
  } finally {
    await tabsRemove(tab.id)
    await writeLog('info', 'detail tab closed', { url: resolvedUrl })
  }
}

async function avitoOrdersTab(url = AVITO_ORDERS_URL) {
  const tabs = await tabsQuery({ url: 'https://www.avito.ru/orders*' })
  const existing = tabs.find((tab) => tab.id)
  if (existing?.id) {
    await tabsUpdate(existing.id, { active: true, url })
    return existing
  }
  return tabsCreate({ url, active: true })
}

async function collectFromAvitoOrdersPage(url = AVITO_ORDERS_URL, collectionLabel = 'заказов') {
  const tab = await avitoOrdersTab(url)
  if (!tab?.id) throw new Error('Не удалось открыть страницу заказов Avito')
  await waitForTabComplete(tab.id)
  const settings = await readSettings()
  const backendUrl = await apiUrlFromSettings(settings)
  const cached = await fetch(`${backendUrl}/api/v1/avito/orders/browser-collection-context`, {
    headers: { Authorization: authorizationValue(settings.accessToken) }, credentials: 'omit', redirect: 'error', signal: AbortSignal.timeout(15000),
  }).then(response => readApiJson(response, { backendUrl, action: 'collection_context' }))
  const message = { type: 'AVITO_ORDERS_COLLECT_NOW', options: { ...(settings.collectOptions || {}),
    photoMode: settings.collectOptions?.photoMode === 'two' ? 'two' : 'one',
    sizeMode: ['none', 'description', 'chat_ai'].includes(settings.collectOptions?.sizeMode) ? settings.collectOptions.sizeMode : 'chat_ai',
    colorFromDescription: true, collectionLabel, savedOrders: cached.orders || [] } }
  const orders = new Map(), visited = new Set()
  let partialError = null, unknownStatuses = 0
  let result
  for (let page = 0; page < 100; page++) {
    try { result = await tabsSendMessage(tab.id, message) }
    catch (_error) { await executeContentScript(tab.id); result = await tabsSendMessage(tab.id, message) }
    if (!result?.ok) return result
    if (result.payload?.collector?.checkpoint) partialError ||= result.payload.collector.error || 'Часть заказов не собрана.'
    unknownStatuses += result.payload?.collector?.unknownStatuses || 0
    const pageRows = result.payload?.orders || []
    const signature = pageRows.map(row => row.orderId || row.marketplaceId).sort().join('|')
    if (visited.has(signature)) {
      result.payload.collector.checkpoint = true
      result.payload.collector.error = 'Авито повторило ту же страницу. Сбор сохранён, можно продолжить.'
      break
    }
    visited.add(signature)
    pageRows.forEach(row => orders.set(`${row.accountId || ''}:${row.orderId || row.marketplaceId}`, row))
    if (!result.payload?.collector?.hasNext) break
    const prior = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: () => {
      const selector = '[data-marker="pagination-button/next"], a[rel="next"], button[aria-label="Следующая страница"]'
      const button = document.querySelector(selector)
      if (!button || button.disabled || button.getAttribute('aria-disabled') === 'true') return null
      const text = document.querySelector('[data-marker="order-row"]')?.innerText || document.body.innerText.slice(0, 4000)
      button.click()
      return text
    } })
    if (!prior[0]?.result) break
    let changed = false
    for (let attempt = 0; attempt < 30; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 400))
      const current = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: () =>
        document.querySelector('[data-marker="order-row"]')?.innerText || document.body?.innerText?.slice(0, 4000) }).catch(() => [])
      if (current[0]?.result && current[0].result !== prior[0].result) { changed = true; break }
    }
    if (!changed) break
  }
  if (result?.ok) {
    result.payload.orders = [...orders.values()]
    if (result.payload.collector?.hasNext) partialError ||= result.payload.collector.error || 'Не все страницы заказов проверены: переход на следующую страницу не завершён.'
    if (partialError) Object.assign(result.payload.collector, { status: 'partial', checkpoint: true, error: partialError })
    result.payload.collector.unknownStatuses = unknownStatuses
    if (result.payload.collector) result.payload.collector.orders = orders.size
  }
  return result
}

function combineCollections(ordersPayload, returnsPayload) {
  const orders = Array.isArray(ordersPayload?.orders) ? ordersPayload.orders : []
  const returns = (Array.isArray(returnsPayload?.orders) ? returnsPayload.orders : [])
    .filter((order) => ['on_return', 'closed', 'canceled', 'delivered'].includes(order.status))
  const all = [...orders, ...returns]
  const missing = ['imageUrl', 'size', 'color', 'sellerArticle'].reduce((result, key) => {
    result[key] = (ordersPayload?.collector?.missing?.[key] || 0) + (returnsPayload?.collector?.missing?.[key] || 0)
    return result
  }, {})
  return {
    capturedAt: new Date().toISOString(),
    pageUrl: ordersPayload?.pageUrl || AVITO_ORDERS_URL,
    collector: {
      ...(ordersPayload?.collector || {}),
      status: ordersPayload?.collector?.checkpoint || returnsPayload?.collector?.checkpoint ? 'partial' : 'completed',
      checkpoint: Boolean(ordersPayload?.collector?.checkpoint || returnsPayload?.collector?.checkpoint),
      error: ordersPayload?.collector?.error || returnsPayload?.collector?.error || null,
      unknownStatuses: (ordersPayload?.collector?.unknownStatuses || 0) + (returnsPayload?.collector?.unknownStatuses || 0),
      pages: {
        orders: ordersPayload?.pageUrl || AVITO_ORDERS_URL,
        returns: returnsPayload?.pageUrl || AVITO_RETURNS_URL,
      },
      orders: orders.filter(row => row.items?.length).length,
      returns: returns.filter(row => row.items?.length).length,
      items: all.reduce((sum, order) => sum + (order.items?.length || 0), 0),
      missing,
      options: { ...(ordersPayload?.collector?.options || {}), ...(returnsPayload?.collector?.options || {}) },
      notes: [
        'Собраны данные со страниц заказов и возвратов Avito.',
        'Возвраты собираются тем же способом, что и обычные заказы.',
      ],
    },
    orders,
    returns,
  }
}

async function collectAndPostFromAvito() {
  const startedAt = Date.now()
  detailCollectionBlocked = false
  await writeLog('info', 'collection requested from popup')
  await saveStatus({ ok: false, message: 'Открываем заказы Avito...' })
  const collected = await collectFromAvitoOrdersPage(AVITO_ORDERS_URL, 'заказов')
  if (!collected?.ok) throw new Error(collected?.error || 'Не удалось прочитать страницу заказов Avito')
  await postSnapshot({ ...collected.payload, collector: { ...collected.payload?.collector, checkpoint: true } })
  await saveStatus({ ok: false, message: `Активные заказы собраны: ${(collected.payload?.orders || []).filter(row => row.items?.length).length}. Открываем возвраты Avito...` })
  let collectedReturns, returnsError = ''
  try {
    if (detailCollectionBlocked) throw new Error('Авито запросил проверку безопасности. Повторный сбор возвратов не запущен.')
    collectedReturns = await collectFromAvitoOrdersPage(AVITO_RETURNS_URL, 'возвратов')
    if (!collectedReturns?.ok) throw new Error(collectedReturns?.error || 'Не удалось прочитать возвраты')
    if (collectedReturns.payload?.collector?.checkpoint) returnsError = collectedReturns.payload.collector.error || 'Сбор возвратов завершён частично.'
  } catch (error) {
    returnsError = error instanceof Error ? error.message : 'Возвраты не получены'
    collectedReturns = { payload: { orders: [] } }
  }
  const payload = combineCollections(collected.payload, collectedReturns.payload)
  if (returnsError) payload.collector.checkpoint = true
  const missing = payload?.collector?.missing || {}
  await saveStatus({ ok: false, message: `Нашли заказов: ${payload?.orders?.length || 0}, возвратов: ${payload?.returns?.length || 0}. Не найдено: фото ${missing.imageUrl || 0}, размер ${missing.size || 0}, цвет ${missing.color || 0}. Отправляем в Satorna...` })
  const result = await postSnapshotWithFeedback(payload, false)
  const [photosResult, labelsResult] = await Promise.allSettled([uploadOrderPhotos(), collectLabels(payload)])
  const photos = photosResult.status === 'fulfilled' ? photosResult.value : null
  const photoStatus = photos
    ? ` Фото сохранено: ${photos.saved}; не получено: ${photos.missing}.${photos.errors.length ? ' ' + photos.errors.join('; ') : ''}`
    : ` Фото не сохранены: ${photosResult.reason instanceof Error ? photosResult.reason.message : 'ошибка загрузки'}`
  let labelStatus = ''
  let labelsComplete = false
  if (labelsResult.status === 'fulfilled') {
    const labels = labelsResult.value
    labelsComplete = labels.labels >= labels.expected && !labels.warnings?.length
    labelStatus = ` Этикеток сохранено: ${labels.labels || 0} из ${labels.expected}.${labelsComplete ? '' : ' Часть этикеток требует проверки.'}`
  } else {
    labelStatus = ` Этикетки не завершены: ${labelsResult.reason instanceof Error ? labelsResult.reason.message : 'ошибка получения'}`
  }
  const meta = result?.browserSnapshot
  const ai = meta?.aiExtraction || {}
  const missingItemIds = [...(payload.orders || []), ...(payload.returns || [])]
    .flatMap(order => order.items || []).filter(item => !item.itemId).length
  const missingShipments = (payload.orders || [])
    .filter(order => order.status === 'ready_to_ship' && !order.shipmentNumber).length
  const unknownStatuses = (payload.orders || []).filter(order => !order.status || order.status === 'unknown').length
  const sizeText = payload?.collector?.options?.sizeMode === 'chat_ai'
    ? ` Размеры подтверждены: ${ai.confirmedSizeCount || 0}; нужна проверка: ${ai.missingFinalSizeCount || 0}.`
    : ''
  const chatMode = payload?.collector?.options?.sizeMode === 'chat_ai'
  const itemCount = [...payload.orders, ...payload.returns].flatMap(order => order.items || []).length
  const sizesConfirmed = chatMode ? ai.confirmedSizeCount === itemCount && ai.missingFinalSizeCount === 0
    : [...payload.orders, ...payload.returns].every(order => (order.items || []).every(item => item.size && ['order_row', 'order_detail', 'chat_ai'].includes(item.sources?.size)))
  const colorsConfirmed = [...payload.orders, ...payload.returns].every(order => (order.items || []).every(item => item.color && ['order_row', 'order_detail'].includes(item.sources?.color)))
  const variantsConfirmed = sizesConfirmed && colorsConfirmed
  const complete = !payload.collector.checkpoint && !returnsError && variantsConfirmed && labelsComplete && photos?.missing === 0 && !missing.imageUrl && (chatMode || !missing.size) && !missing.color && !missingItemIds && !missingShipments && !unknownStatuses
  const elapsedSeconds = Math.round((Date.now() - startedAt) / 1000)
  const activeOrdersCount = (payload.orders || []).filter(row => row.items?.length && ['ready_to_ship', 'in_transit', 'on_return'].includes(row.status)).length
  const activeReturnsCount = (payload.returns || []).filter(row => row.items?.length && row.status === 'on_return').length
  const text = `Собрано активных заказов: ${activeOrdersCount}, возвратов: ${activeReturnsCount}. Время: ${elapsedSeconds} с.${sizeText}${photoStatus}${labelStatus}${returnsError ? ` Возвраты: ${returnsError}. Сохранённые данные не потеряны.` : ''}${!variantsConfirmed ? ' Выбранные размер/цвет подтверждены не у всех товаров.' : ''}${missingItemIds ? ` ID товара не найден: ${missingItemIds}.` : ''}${missingShipments ? ` Номер отправления не найден: ${missingShipments}.` : ''}`
  const message = text + (payload.collector.checkpoint && !returnsError ? ` Сбор неполный: ${payload.collector.error || 'часть страниц не проверена'}.` : '')
    + (unknownStatuses ? ` Статус не определён: ${unknownStatuses}; эти заказы не включены в лист отправки.` : '')
  await saveStatus({ ok: complete, message })
  await notifyUploadStatus(complete, message)
  await writeLog(complete ? 'info' : 'warn', 'collection finished', { message, aiExtraction: ai })
  return { ok: true, complete, result, message }
}

let labelsRunning = false
let ordersRunning = false
async function collectLabels(payload) {
  if (labelsRunning) throw new Error('Получение этикеток уже выполняется')
  const settings = await readSettings()
  if (!settings.accessToken) throw new Error('Сначала сохраните подключение к Satorna')
  const backendUrl = await apiUrlFromSettings(settings)
  const headers = { Authorization: authorizationValue(settings.accessToken) }
  const response = await fetch(`${backendUrl}/api/v1/avito/orders/labels/collection-context`, { headers, redirect: 'error', credentials: 'omit', signal: AbortSignal.timeout(15000) })
  const context = await readApiJson(response, { backendUrl, action: 'label_context' })
  const accounts = context.accountIds || []
  if (Array.isArray(context.missing) && !context.missing.length) return { labels: context.saved || 0, expected: context.total || 0, warnings: [], cached: true }
  if (accounts.length !== 1) throw new Error('Сначала соберите заказы одного аккаунта Авито')
  async function stage(value) {
    await fetch(`${backendUrl}/api/v1/avito/orders/labels/collection-status`, {
      method: 'POST', headers: { ...headers, 'Content-Type': 'application/json' }, redirect: 'error', credentials: 'omit',
      body: JSON.stringify({ stage: value }), signal: AbortSignal.timeout(5000),
    }).catch(() => {})
  }
  labelsRunning = true
  let tab
  try {
    await stage('opening')
    await saveStatus({ ok: false, message: 'Заказы сохранены. Авито формирует PDF этикеток…' })
    tab = await chrome.tabs.create({ url: 'https://www.avito.ru/orders/print-labels', active: false })
    await waitForTabComplete(tab.id)
    await chrome.scripting.executeScript({ target: { tabId: tab.id }, world: 'MAIN', func: SatornaLabels.observeDownloads })
    await stage('selecting')
    const generated = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: SatornaLabels.selectAndGenerate, args: [context.missing || []] })
    const expected = generated[0]?.result || 0
    if (expected < 1) throw new Error('Авито не выбрало ни одной этикетки. Проверьте страницу печати.')
    await stage('waiting_pdf')
    let url = null
    for (let i = 0; i < 45 && !url; i++) {
      await new Promise(resolve => setTimeout(resolve, 700))
      const tabs = await chrome.tabs.query({})
      url = tabs.filter(candidate => candidate.id === tab.id || candidate.openerTabId === tab.id)
        .map(candidate => SatornaLabels.downloadUrl(candidate.url || candidate.pendingUrl)).find(Boolean)
      if (url) break
      const results = await chrome.scripting.executeScript({ target: { tabId: tab.id }, world: 'MAIN', func: () => [
        ...(window.__satornaLabels?.urls || []),
        ...performance.getEntriesByType('resource').map(entry => entry.name),
        ...[...document.querySelectorAll('a[href]')].map(a => a.href),
      ] }).catch(() => [])
      // Native Avito sometimes opens the PDF with noopener. Correlate it with
      // an exact task observed in THIS print page, never an unrelated PDF tab.
      url = SatornaLabels.downloadForObservedTask(
        tabs.map(candidate => candidate.url || candidate.pendingUrl), results[0]?.result || [],
      )
    }
    if (!url) throw new Error('Авито не предоставило ссылку на PDF. Страница этикеток оставлена открытой.')
    await stage('downloading')
    const pdf = await SatornaLabels.boundedPdf(await fetch(url, { credentials: 'include', redirect: 'error', signal: AbortSignal.timeout(30000) }))
    await stage('uploading')
    const response = await fetch(`${backendUrl}/api/v1/avito/orders/labels/import?accountId=${encodeURIComponent(accounts[0])}`, {
      method: 'POST', credentials: 'omit', redirect: 'error', signal: AbortSignal.timeout(120000),
      headers: { Authorization: authorizationValue(settings.accessToken), 'Content-Type': 'application/pdf' }, body: pdf,
    })
    if (!response.ok) throw new Error(`Satorna не сохранила этикетки (HTTP ${response.status})`)
    const result = await response.json()
    if (result.ok !== true) throw new Error('Satorna не подтвердила сохранение PDF')
    result.expected = expected
    if (result.labels < expected) result.warnings = [...(result.warnings || []), 'Не все выбранные этикетки распознаны']
    const verifiedResponse = await fetch(`${backendUrl}/api/v1/avito/orders/labels/collection-context`, { headers, credentials: 'omit', redirect: 'error', signal: AbortSignal.timeout(15000) })
    const verified = await readApiJson(verifiedResponse, { backendUrl, action: 'verify_label_binding' })
    const requested = new Set((context.missing || []).map(row => `${row.accountId || ''}:${row.orderId}`))
    const stillMissing = (verified.missing || []).filter(row => requested.has(`${row.accountId || ''}:${row.orderId}`))
    if (stillMissing.length) result.warnings = [...(result.warnings || []), `Без сопоставленной этикетки: ${stillMissing.length}`]
    const labelsComplete = result.labels >= expected && !result.warnings?.length
    await notifyUploadStatus(labelsComplete, labelsComplete
      ? `Этикеток сохранено: ${result.labels}. Обновите заказы в Satorna.`
      : `PDF сохранён, распознано этикеток: ${result.labels} из ${expected}. Требуют проверки: ${(result.warnings || []).join('; ') || 'сопоставление с заказами'}.`)
    return result
  } catch (error) {
    await stage('error')
    await notifyUploadStatus(false, error instanceof Error ? error.message : 'Не удалось получить этикетки')
    throw error
  } finally {
    if (tab?.id) await chrome.scripting.executeScript({ target: { tabId: tab.id }, world: 'MAIN', func: () => window.__satornaLabels?.restore?.() }).catch(() => {})
    labelsRunning = false
  }
}

let listingPhotosRunning = false
let listingPhotosStop = false
async function collectListingPhotos() {
  const settings = await readSettings()
  if (!settings.accessToken) throw new Error('Сначала сохраните подключение к Satorna')
  const backendUrl = await apiUrlFromSettings(settings)
  let batchTab = null, blockedTabId = null
  const detailTabs = new Set()
  const replacementTabs = new Map()
  function onPhotoTabReplaced(addedTabId, removedTabId) {
    replacementTabs.set(removedTabId, addedTabId)
    if (detailTabs.delete(removedTabId)) detailTabs.add(addedTabId)
    if (batchTab?.id === removedTabId) batchTab.id = addedTabId
    if (blockedTabId === removedTabId) blockedTabId = addedTabId
  }
  chrome.tabs.onReplaced.addListener(onPhotoTabReplaced)
  async function pagePhotos(tabId, ids, detailId = null) {
    await chrome.scripting.executeScript({ target: { tabId }, files: ['src/item-photo.js', 'src/listing-photo-page.js'] })
    const result = await chrome.scripting.executeScript({ target: { tabId },
      func: (ids, detail) => globalThis.SatornaListingPhotoPage.read(ids, detail), args: [ids, detailId] })
    return result[0]?.result || { photos: {} }
  }
  function assertPhotoPage(result) {
    if (!result.blocked) return
    const error = new Error('Авито показывает проверку безопасности. Откройте вкладку Авито, пройдите проверку вручную и продолжите сбор. Фото в базе сохранятся.')
    error.stop = true
    throw error
  }
  async function readyPhotos(tabId, ids, detailId = null, previous = null, timeout = 8000) {
    const deadline = Date.now() + timeout
    let latest = { photos: {} }
    let stableSignature = '', stableSince = 0
    while (!listingPhotosStop && Date.now() < deadline) {
      try {
        while (replacementTabs.has(tabId)) tabId = replacementTabs.get(tabId)
        latest = await pagePhotos(tabId, ids, detailId)
        assertPhotoPage(latest)
        if (detailId && latest.photos[detailId]) return latest
        if (!detailId && latest.signature && latest.signature !== previous) {
          if (stableSignature !== latest.signature) { stableSignature = latest.signature; stableSince = Date.now() }
          if (latest.listReady && Date.now() - stableSince >= 750) return latest
        }
      } catch (error) {
        if (error.stop) {
          blockedTabId = tabId
          await chrome.tabs.update(tabId, { active: true })
          throw error
        }
        // A navigation may briefly replace the frame; read the next DOM, not window.load.
      }
      await new Promise(resolve => setTimeout(resolve, 250))
    }
    return latest
  }
  try { return await SatornaListingPhotos.collect({
    backendUrl, authorization: authorizationValue(settings.accessToken),
    shouldStop: () => listingPhotosStop,
    progress: status => chrome.storage.local.set({ listingPhotosStatus: { ...status, updatedAt: Date.now() } }),
    preparePhotos: async (rows, reportFound) => {
      const photos = {}, ids = rows.map(row => row.itemId)
      for (const tab of await chrome.tabs.query({ url: 'https://www.avito.ru/*' })) {
        if (listingPhotosStop) break
        try { Object.assign(photos, (await pagePhotos(tab.id, ids)).photos) } catch { /* Closed/reloading tab. */ }
      }
      await reportFound(Object.keys(photos).length)
      // A tiny remainder is faster to resolve directly than rescanning every list page.
      if (!listingPhotosStop && ids.length - Object.keys(photos).length > 5) {
        batchTab = await chrome.tabs.create({ url: 'https://www.avito.ru/profile/pro/items', active: true })
        let previous = null
        const seen = new Set()
        for (let page = 0; page < 50 && !listingPhotosStop; page++) {
          const result = await readyPhotos(batchTab.id, ids, null, previous, page === 0 ? 25000 : 8000)
          assertPhotoPage(result)
          Object.assign(photos, result.photos)
          await reportFound(Object.keys(photos).length, { pages: page + 1, pageItems: result.count || 0 })
          if (!result.signature || seen.has(result.signature) || Object.keys(photos).length >= ids.length) break
          seen.add(result.signature)
          previous = result.signature
          const advanced = await chrome.scripting.executeScript({ target: { tabId: batchTab.id }, func: () => globalThis.SatornaListingPhotoPage.advance() })
          if (!advanced[0]?.result) break
        }
      }
      return photos
    },
    loadPhoto: async row => {
      const tab = await chrome.tabs.create({ url: row.url, active: false })
      detailTabs.add(tab.id)
      try {
        const result = await readyPhotos(tab.id, [row.itemId], row.itemId, null, 10000)
        return result.photos[row.itemId] || null
      } finally {
        let currentId = tab.id
        while (replacementTabs.has(currentId)) currentId = replacementTabs.get(currentId)
        if (currentId !== blockedTabId) {
          detailTabs.delete(currentId)
          await chrome.tabs.remove(currentId).catch(() => {})
        }
      }
    },
  }) } finally {
    chrome.tabs.onReplaced.removeListener(onPhotoTabReplaced)
    await Promise.all([...detailTabs].filter(id => id !== blockedTabId).map(id => chrome.tabs.remove(id).catch(() => {})))
    if (batchTab && batchTab.id !== blockedTabId) await chrome.tabs.remove(batchTab.id).catch(() => {})
  }
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === 'AVITO_ORDERS_CHECKPOINT') {
    // Only our Avito content script may persist an intermediate snapshot.
    if (!_sender.tab?.url?.startsWith('https://www.avito.ru/orders')) return false
    postSnapshot(message.payload).then(async () => {
      await chrome.storage.local.set({ orderCollectionProgress: { ...message.progress, updatedAt: Date.now() } })
      sendResponse({ ok: true })
    }).catch(() => sendResponse({ ok: false }))
    return true
  }
  if (message?.type === 'AVITO_LISTING_PHOTOS_STATE') {
    sendResponse({ running: listingPhotosRunning })
    return false
  }
  if (message?.type === 'AVITO_LISTING_PHOTOS_STOP') {
    listingPhotosStop = true
    sendResponse({ ok: true })
    return false
  }
  if (message?.type === 'AVITO_LISTING_PHOTOS_COLLECT') {
    if (ordersRunning || labelsRunning) {
      sendResponse({ ok: false, error: 'Сначала дождитесь завершения сбора заказов и этикеток.' })
      return false
    }
    if (!listingPhotosRunning) {
      listingPhotosRunning = true
      listingPhotosStop = false
      void collectListingPhotos().catch(async error => {
        const stored = await chrome.storage.local.get('listingPhotosStatus')
        await chrome.storage.local.set({ listingPhotosStatus: { ...stored.listingPhotosStatus, stage: 'error', message: error.message, updatedAt: Date.now() } })
      }).finally(() => { listingPhotosRunning = false })
    }
    sendResponse({ ok: true })
    return false
  }
  if (message?.type === 'AVITO_LABELS_COLLECT') {
    if (ordersRunning || listingPhotosRunning) {
      sendResponse({ ok: false, error: 'Сбор уже выполняется. Дождитесь результата.' })
      return false
    }
    collectLabels().then(result => sendResponse({ ok: result.labels >= result.expected && !result.warnings?.length, message: `Распознано этикеток: ${result.labels} из ${result.expected}${result.warnings?.length ? `. Требуют проверки: ${result.warnings.join('; ')}` : ''}`, result }))
      .catch(error => sendResponse({ ok: false, error: error instanceof Error ? error.message : 'Не удалось получить этикетки' }))
    return true
  }
  if (message?.type === 'AVITO_LOG') {
    writeLog(message.level || 'info', message.message || 'event', message.data || {})
      .then(() => sendResponse({ ok: true }))
      .catch((error) => sendResponse({ ok: false, error: error instanceof Error ? error.message : String(error) }))
    return true
  }
  if (message?.type === 'AVITO_LOGS_GET') {
    chrome.storage.local.get({ [LOG_KEY]: [] }).then((stored) => sendResponse({ ok: true, logs: stored[LOG_KEY] || [] }))
    return true
  }
  if (message?.type === 'AVITO_LOGS_CLEAR') {
    chrome.storage.local.set({ [LOG_KEY]: [] }).then(() => sendResponse({ ok: true }))
    return true
  }
  if (message?.type === 'AVITO_EXTRACT_DETAILS_TAB') {
    extractDetailsInTab(message.url, message.options)
      .then((response) => sendResponse({ ok: true, details: response }))
      .catch((error) => sendResponse({ ok: false, error: error instanceof Error ? error.message : String(error) }))
    return true
  }
  if (message?.type === 'AVITO_ORDERS_OPEN_AND_COLLECT') {
    if (ordersRunning || labelsRunning || listingPhotosRunning) {
      sendResponse({ ok: false, error: 'Сбор заказов уже выполняется. Дождитесь результата.' })
      return false
    }
    ordersRunning = true
    collectAndPostFromAvito()
      .then(sendResponse)
      .catch((error) => {
        const text = error instanceof Error ? error.message : String(error)
        return saveStatus({ ok: false, message: text }).then(() => sendResponse({ ok: false, error: text }))
      })
      .finally(() => { ordersRunning = false })
    return true
  }
  if (message?.type !== 'AVITO_ORDERS_COLLECTED') return false
  postSnapshotWithFeedback(message.payload, false)
    .then(async (result) => {
      const meta = result?.browserSnapshot
      let labelText = ''
      let complete = false
      try {
        const labels = await collectLabels(message.payload)
        const photos = await uploadOrderPhotos()
        complete = labels.labels >= labels.expected && !labels.warnings?.length && !photos.missing
        labelText = ` Этикеток: ${labels.labels}. Фото не получено: ${photos.missing}.`
      }
      catch (error) { labelText = ` Этикетки не получены: ${error instanceof Error ? error.message : 'ошибка'}` }
      const text = `Собрано заказов: ${meta?.orders ?? message.payload?.orders?.length ?? 0}.${labelText}`
      return saveStatus({ ok: complete, message: text }).then(() => sendResponse({ ok: true, complete, result, message: text }))
    })
    .catch((error) => {
      const text = error instanceof Error ? error.message : String(error)
      return saveStatus({ ok: false, message: text }).then(() => sendResponse({ ok: false, error: text }))
    })
  return true
})
