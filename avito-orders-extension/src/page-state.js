{
const PAGE_STATE_API = '__satornaAvitoPageState'

function normalizedTitle(value) {
  return String(value || '')
    .toLocaleLowerCase('ru-RU')
    .replace(/[^a-zа-яё0-9]+/giu, ' ')
    .trim()
}

function slugifyTitle(value) {
  const map = {
    а: 'a', б: 'b', в: 'v', г: 'g', д: 'd', е: 'e', ё: 'e',
    ж: 'zh', з: 'z', и: 'i', й: 'y', к: 'k', л: 'l',
    м: 'm', н: 'n', о: 'o', п: 'p', р: 'r', с: 's',
    т: 't', у: 'u', ф: 'f', х: 'h', ц: 'ts', ч: 'ch',
    ш: 'sh', щ: 'sch', ъ: '', ы: 'y', ь: '', э: 'e',
    ю: 'yu', я: 'ya',
  }
  return String(value || '')
    .toLocaleLowerCase('ru-RU')
    .replace(/[а-яё]/giu, (char) => map[char.toLocaleLowerCase('ru-RU')] ?? char)
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
}

function fallbackItemUrl(title, itemId) {
  return itemId ? `https://www.avito.ru/items/${itemId}` : null
}

function itemIdValue(value) {
  const text = String(value || '').trim()
  if (!/^\d{6,12}$/.test(text) || text.startsWith('70000000') || text.startsWith('103')) return null
  return text
}

function itemUrlValue(value) {
  const text = String(value || '').replace(/\\u002F/g, '/').replace(/\\\//g, '/')
  if (!text) return null
  try {
    const url = new URL(text, 'https://www.avito.ru')
    if (url.hostname !== 'www.avito.ru' && url.hostname !== 'avito.ru') return null
    if (/\/orders(?:\/|$)/i.test(url.pathname)) return null
    if (!/_[0-9]{6,12}(?:\/)?$/i.test(url.pathname) && !/\/items\/[0-9]+(?:\/)?$/i.test(url.pathname)) return null
    url.hash = ''
    return url.toString()
  } catch (_error) {
    return null
  }
}

function titleScore(value, expectedTitle) {
  const actual = normalizedTitle(value)
  const expected = normalizedTitle(expectedTitle)
  if (!actual || !expected) return 0
  if (actual === expected) return 80
  if (actual.includes(expected) || expected.includes(actual)) return 50
  const words = expected.split(' ').filter((word) => word.length >= 3)
  return Math.min(40, words.filter((word) => actual.includes(word)).length * 10)
}

function findCandidates(root, expectedTitle = '') {
  const found = []
  const seen = new WeakSet()
  let visited = 0

  function visit(value, depth) {
    if (!value || typeof value !== 'object' || depth > 14 || visited >= 12000 || seen.has(value)) return
    seen.add(value)
    visited += 1

    if (!Array.isArray(value)) {
      const itemUrl = itemUrlValue(value.itemUrl ?? value.item_url ?? value.url ?? value.canonicalUrl ?? value.canonical_url)
      const title = String(value.title ?? value.name ?? value.itemTitle ?? value.item_title ?? '').trim() || null
      const itemId = itemIdValue(
        value.itemId
        ?? value.itemID
        ?? value.item_id
        ?? value.avitoId
        ?? value.avitoID
        ?? value.avito_id
        ?? value.adId
        ?? value.ad_id
        ?? (title ? value.id : null),
      )
      const matchScore = titleScore(title, expectedTitle)
      const score = (itemId ? 50 : 0) + (itemUrl ? 50 : 0) + matchScore
      const matchesExpectedTitle = !normalizedTitle(expectedTitle) || matchScore > 0
      if ((itemId || itemUrl) && score >= 50 && matchesExpectedTitle) {
        const imageUrls = globalThis.SatornaAvitoItemPhoto?.extractPayloadImages?.(value, 5) || []
        found.push({
          itemId: itemId || itemUrl?.match(/(?:_|\/items\/)(\d{6,12})(?:\/)?$/i)?.[1] || null,
          itemUrl: itemUrl || fallbackItemUrl(title || expectedTitle, itemId),
          title,
          ...(imageUrls.length ? { imageUrl: imageUrls[0], imageUrls } : {}),
          score,
        })
      }
    }

    for (const child of Array.isArray(value) ? value : Object.values(value)) {
      if (child && typeof child === 'object') visit(child, depth + 1)
    }
  }

  visit(root, 0)
  const unique = new Map()
  found.forEach((candidate) => {
    const key = candidate.itemUrl || candidate.itemId
    const previous = unique.get(key)
    if (!previous || candidate.score > previous.score) unique.set(key, candidate)
  })
  return Array.from(unique.values()).sort((left, right) => right.score - left.score)
}

function findOrderPayloadCandidates(root, expectedTitle = '') {
  let source = ''
  try {
    source = JSON.stringify(root)
  } catch (_error) {
    return []
  }
  const candidates = []
  const productPattern = /"quantity":\d+,"title":"((?:\\.|[^"\\])*)"/g
  let productMatch
  while ((productMatch = productPattern.exec(source)) !== null) {
    let title = ''
    try {
      title = JSON.parse(`"${productMatch[1]}"`)
    } catch (_error) {
      title = productMatch[1]
    }
    const matchScore = titleScore(title, expectedTitle)
    if (normalizedTitle(expectedTitle) && matchScore === 0) continue

    const contextStart = Math.max(0, productMatch.index - 1000)
    const contextEnd = Math.min(source.length, productMatch.index + productMatch[0].length + 1000)
    const context = source.slice(contextStart, contextEnd)
    const idMatches = []
    const itemIdPattern = /itemId(?:%3D|=|\\u003[dD]|["']?\s*:\s*["']?)(\d{6,12})/gi
    let idMatch
    while ((idMatch = itemIdPattern.exec(context)) !== null) {
      const itemId = itemIdValue(idMatch[1])
      if (itemId) {
        const absoluteIndex = contextStart + idMatch.index
        idMatches.push({ itemId, distance: Math.abs(absoluteIndex - productMatch.index) })
      }
    }
    idMatches.sort((left, right) => left.distance - right.distance)
    const uniqueIds = [...new Set(idMatches.map(match => match.itemId))]
    const itemId = uniqueIds.length === 1 ? uniqueIds[0] : null
    if (!itemId) continue
    const imageUrls = globalThis.SatornaAvitoItemPhoto?.extractPayloadImages?.(context, 5) || []
    candidates.push({
      itemId,
      itemUrl: fallbackItemUrl(title, itemId),
      title,
      ...(imageUrls.length ? { imageUrl: imageUrls[0], imageUrls } : {}),
      score: 200 + matchScore,
    })
  }
  return candidates
}

function attachedNodeStates(root) {
  const states = []
  const propertyKeys = new Set()
  let reactRoots = 0
  const nodes = [root?.documentElement, root?.body, ...Array.from(root?.querySelectorAll?.('*') || [])].filter(Boolean)
  for (const node of nodes) {
    for (const key of Object.getOwnPropertyNames(node)) {
      if (key.startsWith('__reactProps$') || key.startsWith('__reactFiber$') || key.startsWith('__reactContainer$')) reactRoots += 1
      try {
        const value = node[key]
        if (!value || typeof value !== 'object') continue
        states.push(value)
        propertyKeys.add(key)
      } catch (_error) {
        // A page-owned getter may reject access; continue with other component state.
      }
      if (states.length >= 4000) break
    }
    if (states.length >= 4000) break
  }
  return { states, propertyKeys: Array.from(propertyKeys).slice(0, 80), reactRoots }
}

function collectFromDocument(root, expectedTitle = '') {
  const { states } = attachedNodeStates(root)
  return findCandidates(states, expectedTitle).slice(0, 20)
}

function networkResourceUrls(performanceApi) {
  let entries = []
  try {
    entries = performanceApi?.getEntriesByType?.('resource') || []
  } catch (_error) {
    return []
  }
  return Array.from(new Set(entries
    .map((entry) => String(entry?.name || ''))
    .filter((value) => {
      try {
        const url = new URL(value)
        if (!/(^|\.)avito\.ru$/i.test(url.hostname)) return false
        return /\/(?:api|web)\//i.test(url.pathname) && /order|item|product|delivery|shipment|logistic/iu.test(`${url.pathname}${url.search}`)
      } catch (_error) {
        return false
      }
    })))
    .slice(-30)
}

function profileOrderResourceUrl(pageUrl, timezone = '') {
  try {
    const page = new URL(String(pageUrl || ''))
    if (!/(^|\.)avito\.ru$/i.test(page.hostname)) return null
    const orderId = page.pathname.match(/\/orders\/([^/?#]+)/i)?.[1]
    if (!orderId) return null
    const url = new URL('/web/2/profile/order', page.origin)
    url.searchParams.set('referenceID', orderId)
    url.searchParams.set('templateVersion', '0')
    url.searchParams.set('srcp', page.searchParams.get('source') || 'orders_list')
    if (timezone) url.searchParams.set('location', timezone)
    return url.toString()
  } catch (_error) {
    return null
  }
}

function inspectDocument(root, expectedTitle = '', performanceApi = globalThis.performance) {
  const attached = attachedNodeStates(root)
  let timezone = ''
  try {
    timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || ''
  } catch (_error) {
    // The endpoint also works without a location hint.
  }
  const observedResources = networkResourceUrls(performanceApi)
  const directResource = profileOrderResourceUrl(root?.location?.href || globalThis.location?.href, timezone)
  return {
    candidates: findCandidates(attached.states, expectedTitle).slice(0, 20),
    reactRoots: attached.reactRoots,
    statePropertyKeys: attached.propertyKeys,
    resourceUrls: Array.from(new Set([...observedResources, directResource].filter(Boolean))),
  }
}

function relevantPayloadText(root) {
  const lines = []
  const seen = new WeakSet()
  let visited = 0

  function add(value) {
    const text = String(value || '').replace(/\s+/g, ' ').trim()
    if (!text || /^https?:\/\//i.test(text) || /^\d{6,}$/.test(text)) return
    if (!lines.includes(text)) lines.push(text.slice(0, 5000))
  }

  function visit(value, depth) {
    if (!value || typeof value !== 'object' || depth > 14 || visited >= 15000 || seen.has(value)) return
    seen.add(value)
    visited += 1
    if (!Array.isArray(value)) {
      const label = value.name ?? value.title ?? value.label ?? value.key
      const attributeValue = value.value ?? value.text ?? value.displayValue ?? value.display_value
      if (typeof label === 'string' && (typeof attributeValue === 'string' || typeof attributeValue === 'number')) {
        add(`${label}: ${attributeValue}`)
      }
      for (const key of ['description', 'text', 'value', 'displayValue', 'display_value']) {
        if (typeof value[key] === 'string') add(value[key])
      }
    }
    for (const child of Array.isArray(value) ? value : Object.values(value)) {
      if (child && typeof child === 'object') visit(child, depth + 1)
    }
  }

  visit(root, 0)
  return lines.join('\n').slice(0, 30000)
}

function extractChannelIds(root) {
  let source = ''
  try {
    source = JSON.stringify(root)
  } catch (_error) {
    return []
  }
  const values = []
  const patterns = [
    /"channelId"\s*:\s*"([^"]+)"/gi,
    /channelId(?:%3D|=)(u2[iI]-[a-zA-Z0-9_~%-]+)/gi,
    /(u2[iI]-[a-zA-Z0-9_~-]{8,})(?![a-zA-Z0-9_~%\-])/g,
  ]
  for (const pattern of patterns) {
    for (const match of source.matchAll(pattern)) {
      let value = String(match[1] || match[0] || '')
      try {
        value = decodeURIComponent(value)
      } catch (_error) {
        // Keep the original value.
      }
      value = value.split('&')[0]
      if (/^u2[iI]-[a-zA-Z0-9_~-]{8,}$/.test(value) && !values.includes(value)) values.push(value)
    }
  }
  return values
}

function extractChatBinding(payloads) {
  const sellerIds = new Set(), buyerIds = new Set(), seen = new WeakSet()
  function visit(value, depth = 0) {
    if (!value || typeof value !== 'object' || depth > 10 || seen.has(value)) return
    seen.add(value)
    for (const [key, actor] of Object.entries(value)) {
      if (['seller', 'buyer'].includes(key) && actor && typeof actor === 'object') {
        let id = actor.id ?? actor.userId ?? actor.user_id
        // Native order BDUI identifies the buyer by the public profile userKey.
        if (key === 'buyer' && id == null && typeof actor.deeplink === 'string') {
          try {
            const link = new URL(actor.deeplink)
            if (link.protocol === 'ru.avito:' && link.host === '1' && link.pathname === '/user/profile') id = link.searchParams.get('userKey')
          } catch (_error) { /* Missing identity remains reviewable. */ }
        }
        if (id != null) (key === 'seller' ? sellerIds : buyerIds).add(String(id))
      }
      if (key === 'sellerId' && actor != null && typeof actor !== 'object') sellerIds.add(String(actor))
      if (key === 'buyerId' && actor != null && typeof actor !== 'object') buyerIds.add(String(actor))
      if (actor && typeof actor === 'object') visit(actor, depth + 1)
    }
  }
  payloads.forEach(payload => visit(payload))
  return { sellerId: sellerIds.size === 1 ? [...sellerIds][0] : null, buyerId: buyerIds.size === 1 ? [...buyerIds][0] : null }
}

function readSessionIdentity(root) {
  // Read only the two identity fields, never cookies, email or credentials.
  for (const script of Array.from(root?.scripts || [])) {
    if (script.src) continue
    const match = String(script.textContent || '').match(/window\.__preloadedState__\s*=\s*("(?:\\.|[^"\\])*")/)
    if (!match) continue
    try {
      let state = JSON.parse(match[1])
      if (typeof state === 'string') state = JSON.parse(state)
      const user = state?.user
      if (user?.isEmployee || state?.layout?.accountHierarchy?.isEmployeeMode) return null
      const accountId = String(user?.id || '')
      const authorId = String(user?.hashedId || '')
      return /^\d+$/.test(accountId) && /^[\w~-]+$/.test(authorId) ? { accountId, authorId } : null
    } catch (_error) { return null }
  }
  return null
}

async function loadCandidatesFromOrderResources(resourceUrls, expectedTitle = '', fetchApi = globalThis.fetch) {
  const urls = Array.from(new Set((resourceUrls || [])
    .map((value) => String(value || ''))
    .filter((value) => /\/web\/\d+\/profile\/order(?:\?|$)/i.test(value))))
    .slice(-5)
  const payloads = []
  const requests = []
  for (const url of urls) {
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        const response = await fetchApi(url, {
          method: 'GET',
          credentials: 'include',
          cache: 'no-store',
          headers: { Accept: 'application/json' },
          signal: AbortSignal.timeout(15000),
        })
        if (!response?.ok) {
          requests.push({ url, status: Number(response?.status || 0), candidates: 0 })
          if (attempt === 0 && Number(response?.status) >= 500) {
            await new Promise(resolve => setTimeout(resolve, 500))
            continue
          }
          break
        }
        const payload = await response.json()
        payloads.push(payload)
        const count = findOrderPayloadCandidates(payload, expectedTitle).length
          || findCandidates(payload, expectedTitle).length
        requests.push({ url, status: Number(response.status || 200), candidates: count })
        break
      } catch (_error) {
        requests.push({ url, status: 0, candidates: 0, error: 'order_resource_unavailable' })
        if (attempt === 0) await new Promise(resolve => setTimeout(resolve, 500))
      }
    }
  }
  return {
    candidates: [
      ...findOrderPayloadCandidates(payloads, expectedTitle),
      ...findCandidates(payloads, expectedTitle),
    ].filter((candidate, index, all) => (
      all.findIndex((value) => (value.itemId || value.itemUrl) === (candidate.itemId || candidate.itemUrl)) === index
    )).sort((left, right) => right.score - left.score).slice(0, 20),
    requests,
    channelIds: payloads.flatMap(extractChannelIds).filter((value, index, all) => all.indexOf(value) === index),
    chatBinding: extractChatBinding(payloads),
    resourceText: payloads.map(relevantPayloadText).filter(Boolean).join('\n').slice(0, 30000),
  }
}

async function loadCandidateForOrderPage(
  pageUrl,
  expectedTitle = '',
  fetchApi = globalThis.fetch,
  timezone = '',
) {
  const resourceUrl = profileOrderResourceUrl(pageUrl, timezone)
  if (!resourceUrl) {
    return {
      candidate: null,
      candidates: [],
      requests: [],
      channelIds: [],
      resourceText: '',
      resourceUrl: null,
    }
  }
  const loaded = await loadCandidatesFromOrderResources([resourceUrl], expectedTitle, fetchApi)
  return {
    ...loaded,
    candidate: loaded.candidates[0] || null,
    resourceUrl,
  }
}

globalThis.SatornaAvitoPageState = {
  readSessionIdentity,
  findCandidates,
  findOrderPayloadCandidates,
  collectFromDocument,
  networkResourceUrls,
  extractChannelIds,
  extractChatBinding,
  inspectDocument,
  profileOrderResourceUrl,
  loadCandidatesFromOrderResources,
  loadCandidateForOrderPage,
}
globalThis[PAGE_STATE_API] = true
}
