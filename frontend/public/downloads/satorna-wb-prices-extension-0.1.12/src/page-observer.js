(() => {
  'use strict'
  const WB_ORIGIN = 'https://www.wildberries.ru'
  const SOURCE = 'satorna-wb-prices-v1'
  const integer = (value) => Number.isSafeInteger(value) && value > 0
  const key = (item) => `${item.nmId}:${item.sizeId}`

  function cardNmId(value) {
    try {
      const url = new URL(value)
      const match = url.pathname.match(/^\/catalog\/(\d+)\/detail\.aspx$/)
      const id = Number(match?.[1])
      return url.origin === WB_ORIGIN && !url.username && !url.password && integer(id) ? id : null
    } catch { return null }
  }

  function sellerId(value) {
    try {
      const url = new URL(value)
      const id = Number(url.pathname.match(/^\/seller\/(\d+)\/?$/)?.[1])
      return url.origin === WB_ORIGIN && !url.username && !url.password && integer(id) ? id : null
    } catch { return null }
  }

  function pageIdentity(value) {
    if (integer(value)) return `card:${value}`
    const nmId = cardNmId(value)
    if (nmId) return `card:${nmId}`
    const supplier = sellerId(value)
    return supplier ? `seller:${supplier}` : null
  }

  function catalogPage(value, pageUrl) {
    try {
      const url = new URL(value, pageUrl)
      const supplier = sellerId(pageUrl)
      const page = Number(url.searchParams.get('page'))
      return supplier && url.origin === WB_ORIGIN && !url.username && !url.password
        && url.pathname === '/__internal/u-catalog/sellers/v4/catalog' && url.searchParams.get('curr') === 'rub'
        && url.searchParams.get('supplier') === String(supplier) && integer(page) ? page : null
    } catch { return null }
  }

  function isDetailRequest(value, pageUrl) {
    try {
      const url = new URL(value, pageUrl)
      if (catalogPage(value, pageUrl)) return true
      if (!pageIdentity(pageUrl) || url.origin !== WB_ORIGIN || url.username || url.password || url.searchParams.get('curr') !== 'rub') return false
      const ids = url.searchParams.get('nm') || ''
      return ['/__internal/u-card/cards/v4/detail', '/__internal/u-card/cards/v4/list'].includes(url.pathname)
        && /^\d+(;\d+)*$/.test(ids) && ids.split(';').length <= 1000 && ids.split(';').every((id) => integer(Number(id)))
    } catch { return false }
  }

  function parseProducts(payload, requested) {
    if (!Array.isArray(payload?.products) || payload.products.length > 1000) return []
    const seen = new Set()
    const items = []
    for (const product of payload.products) {
      if (!integer(product?.id) || (requested && !requested.has(product.id)) || !Array.isArray(product.sizes) || product.sizes.length > 100) continue
      for (const size of product.sizes) {
        const offer = `${product.id}:${size?.optionId}`
        if (!integer(size?.optionId) || !integer(size?.price?.product) || seen.has(offer)) continue
        seen.add(offer)
        // price.wallet is not a final wallet price; product is already kopecks.
        const item = { nmId: product.id, sizeId: size.optionId, buyerPriceNoWalletKopecks: size.price.product }
        if (integer(size.price.basic) && size.price.basic >= size.price.product) item.originalPriceKopecks = size.price.basic
        if (integer(product.supplierId)) item.supplierId = product.supplierId
        items.push(item)
        if (items.length > 10000) return []
      }
    }
    return items
  }

  function parseDetail(payload, pageUrl, requestUrl) {
    if (!isDetailRequest(requestUrl, pageUrl)) return []
    const requested = catalogPage(requestUrl, pageUrl) ? null : new Set(new URL(requestUrl, pageUrl).searchParams.get('nm').split(';').map(Number))
    return parseProducts(payload, requested)
  }

  function parsePublicDetail(payload, nmIds) {
    if (!Array.isArray(nmIds) || !nmIds.length || nmIds.length > 100
      || nmIds.some((id) => !integer(id)) || new Set(nmIds).size !== nmIds.length) return []
    return parseProducts(payload, new Set(nmIds))
  }

  function walletSingleSizeNmIds(payload) {
    if (!Array.isArray(payload?.products) || payload.products.length > 1000) return []
    return payload.products.filter(product => integer(product?.id) && Array.isArray(product.sizes)
      // A catalog tile has no selected optionId; identical regular prices do
      // not establish identical wallet prices across different sizes.
      && product.sizes.length === 1
      && new Set(product.sizes.map(size => size?.optionId)).size === product.sizes.length
      && product.sizes.every(size => integer(size?.optionId) && integer(size?.price?.product)
        && size.price.product === product.sizes[0].price.product)).map(product => product.id)
  }

  function visibleWalletKopecks(text, buyer) {
    if (typeof text !== 'string' || !/с\s+WB\s+Кошельком/i.test(text) || text.length > 300) return null
    // Read the first explicitly displayed amount, not a percentage or old price.
    const amount = text.match(/^\s*(\d[\d\s\u00a0\u202f]*(?:[,.]\d{1,2})?)\s*₽/)
    if (!amount || (text.match(/₽/g) || []).length !== 1) return null
    const value = Math.round(Number(amount[1].replace(/[\s\u00a0\u202f]/g, '').replace(',', '.')) * 100)
    return integer(value) && value <= buyer ? value : null
  }

  function amountKopecks(text) {
    const match = String(text || '').match(/^\s*(\d[\d\s\u00a0\u202f]*(?:[,.]\d{1,2})?)\s*₽\s*$/)
    const value = match && Math.round(Number(match[1].replace(/[\s\u00a0\u202f]/g, '').replace(',', '.')) * 100)
    return integer(value) ? value : null
  }

  // Only dedicated semantic price nodes, never the first/lowest amount in a card.
  function readPriceBlock(root, visible) {
    if (!root?.querySelectorAll) return null
    const walletSelector = '.price-block__wallet-price, .price__wallet-price, [data-testid="wallet-price"], [data-price-type="wallet"]'
    const oldSelector = 'del, s, .price-block__old-price, .price__old-price, [data-price-type="original"]'
    const read = (selector, exclude = '') => {
      const amounts = new Set()
      for (const node of root.querySelectorAll(selector)) {
        if (!visible(node) || (exclude && (node.closest(exclude) || node.querySelector(exclude)))) continue
        const value = amountKopecks(node.innerText ?? node.textContent)
        if (value != null) amounts.add(value)
      }
      return amounts.size === 1 ? [...amounts][0] : null
    }
    const buyer = read('.price-block__final-price, .price-block__price, .price__lower-price, [data-testid="price-without-wallet"], [data-price-type="without-wallet"]',
      `${walletSelector}, ${oldSelector}, [class*="wallet"], [class*="Wallet"]`)
    if (buyer == null) return null
    const original = read(oldSelector)
    if (original != null && buyer > original) return null
    const wallet = read(walletSelector)
    return { buyerPriceNoWalletKopecks: buyer,
      ...(original != null ? { originalPriceKopecks: original } : {}),
      ...(wallet != null && wallet <= buyer ? { buyerPriceWithWalletKopecks: wallet } : {}) }
  }

  function cardOffers(payload, page) {
    const id = cardNmId(page)
    const product = payload?.products?.find?.(item => item?.id === id)
    if (!id || !Array.isArray(product?.sizes) || product.sizes.length > 100) return []
    return [...new Set(product.sizes.map(size => size?.optionId).filter(integer))].map(sizeId => ({ nmId: id, sizeId }))
  }

  function sanitizePageMessage(value, page, allowWallet = false) {
    if (!pageIdentity(page) || value?.source !== SOURCE) return null
    if (value.type === 'blocked' && ['http_403', 'http_429', 'challenge'].includes(value.code)) {
      return { type: 'blocked', code: value.code, ...(value.code === 'http_429'
        && integer(value.retryAfterSeconds) && value.retryAfterSeconds <= 86400 ? { retryAfterSeconds: value.retryAfterSeconds } : {}) }
    }
    const isCatalog = Boolean(sellerId(page) && integer(value.catalogPage) && Number.isSafeInteger(value.productCount) && value.productCount >= 0 && value.productCount <= 1000)
    const loadedNmId = integer(value.loadedNmId) && value.loadedNmId === cardNmId(page) ? value.loadedNmId : null
    if (value.type !== 'observations' || !Array.isArray(value.items) || (!value.items.length && !isCatalog && !loadedNmId) || value.items.length > 10000) return null
    const seen = new Set()
    const items = []
    for (const item of value.items) {
      if (!integer(item?.nmId) || !integer(item.sizeId) || !integer(item.buyerPriceNoWalletKopecks) || seen.has(key(item))) return null
      seen.add(key(item))
      const clean = { nmId: item.nmId, sizeId: item.sizeId, buyerPriceNoWalletKopecks: item.buyerPriceNoWalletKopecks }
      if (integer(item.originalPriceKopecks) && item.originalPriceKopecks >= item.buyerPriceNoWalletKopecks) clean.originalPriceKopecks = item.originalPriceKopecks
      if (integer(item.supplierId)) clean.supplierId = item.supplierId
      if (allowWallet && integer(item.buyerPriceWithWalletKopecks)
        && item.buyerPriceWithWalletKopecks <= item.buyerPriceNoWalletKopecks) clean.buyerPriceWithWalletKopecks = item.buyerPriceWithWalletKopecks
      items.push(clean)
    }
    const uniform = Array.isArray(value.walletSingleSizeNmIds) && value.walletSingleSizeNmIds.length <= 1000
      ? [...new Set(value.walletSingleSizeNmIds.filter(id => integer(id) && items.some(item => item.nmId === id)))] : []
    const offers = loadedNmId && Array.isArray(value.cardOffers) && value.cardOffers.length <= 100
      ? value.cardOffers.filter(item => item?.nmId === loadedNmId && integer(item.sizeId)).map(item => ({ nmId: loadedNmId, sizeId: item.sizeId })) : []
    return { type: 'observations', items, ...(offers.length ? { cardOffers: offers } : {}), ...(uniform.length ? { walletSingleSizeNmIds: uniform } : {}), ...(isCatalog ? { catalogPage: value.catalogPage, productCount: value.productCount } : {}), ...(loadedNmId ? { loadedNmId } : {}) }
  }

  function validateCatalogPage(value, page, expected = null, now = Date.now()) {
    if (!value || !integer(value.marketplaceAccountId) || typeof value.goodsRevision !== 'string'
      || !value.goodsRevision || value.goodsRevision.length > 256 || value.page !== page || value.pageSize !== 100
      || !Number.isSafeInteger(value.total) || value.total < 0 || value.total > 100000 || !integer(value.maxAgeSeconds)
      || value.maxAgeSeconds > 1800 || !Array.isArray(value.items)
      || value.items.length !== Math.min(100, Math.max(0, value.total - (page - 1) * 100))) throw new Error('catalog_invalid')
    if (expected && ['marketplaceAccountId', 'goodsRevision', 'total', 'maxAgeSeconds'].some((field) => expected[field] !== value[field])) throw new Error('catalog_changed')
    const seen = new Set()
    const items = value.items.map((item) => {
      if (!integer(item?.nmId) || !integer(item.sizeId) || !integer(item.sellerPriceKopecks) || seen.has(key(item))) throw new Error('catalog_invalid')
      seen.add(key(item))
      const observed = Date.parse(item.sellerPriceObservedAt)
      if (!Number.isFinite(observed) || now - observed > value.maxAgeSeconds * 1000 || observed > now + 30000) throw new Error('catalog_stale')
      return { nmId: item.nmId, sizeId: item.sizeId, sellerPriceKopecks: item.sellerPriceKopecks, sellerPriceObservedAt: item.sellerPriceObservedAt }
    })
    return { marketplaceAccountId: value.marketplaceAccountId, goodsRevision: value.goodsRevision, page, pageSize: 100, total: value.total, maxAgeSeconds: value.maxAgeSeconds, items }
  }

  function snapshotItems(observations, catalog, observedAt) {
    const offers = new Map(catalog.map((item) => [key(item), item]))
    const seen = new Set()
    const items = []
    for (const observation of observations) {
      const offer = offers.get(key(observation))
      if (!offer || !integer(observation.buyerPriceNoWalletKopecks)
        || observation.buyerPriceNoWalletKopecks > offer.sellerPriceKopecks || seen.has(key(observation))) continue
      seen.add(key(observation))
      items.push({ nmId: offer.nmId, sizeId: offer.sizeId, sellerPriceKopecks: offer.sellerPriceKopecks,
        ...(integer(observation.originalPriceKopecks) && observation.originalPriceKopecks >= observation.buyerPriceNoWalletKopecks
          ? { originalPriceKopecks: observation.originalPriceKopecks } : {}),
        buyerPriceNoWalletKopecks: observation.buyerPriceNoWalletKopecks,
        buyerPriceWithWalletKopecks: integer(observation.buyerPriceWithWalletKopecks)
          && observation.buyerPriceWithWalletKopecks <= observation.buyerPriceNoWalletKopecks ? observation.buyerPriceWithWalletKopecks : null, observedAt })
    }
    return { items, ignored: observations.length - items.length }
  }

  function isTrustedPopupSender(sender, extensionId) {
    const url = `chrome-extension://${extensionId}/src/popup.html`
    return sender?.id === extensionId && sender.url === url
      && (!sender.tab || (sender.frameId === 0 && sender.tab.url === url))
  }

  function isTrustedPageSender(sender, extensionId, tabId, page) {
    const expected = pageIdentity(page)
    return sender?.id === extensionId && sender.frameId === 0 && expected !== null
      && sender.tab?.id === tabId && pageIdentity(sender.url) === expected && pageIdentity(sender.tab.url) === expected
  }

  globalThis.WbPricesContract = Object.freeze({ SOURCE, WB_ORIGIN, integer, key, cardNmId, sellerId, pageIdentity, catalogPage, isDetailRequest,
    parseDetail, parsePublicDetail, walletSingleSizeNmIds, visibleWalletKopecks, amountKopecks, readPriceBlock, cardOffers, sanitizePageMessage, validateCatalogPage, snapshotItems, isTrustedPopupSender, isTrustedPageSender })
})()

;
(() => {
  'use strict'
  const contract = globalThis.WbPricesContract
  const post = globalThis.postMessage.bind(globalThis)
  const emit = (message) => post({ source: contract.SOURCE, ...message }, contract.WB_ORIGIN)

  function report(status, payload, requestUrl, retryHeader = null) {
    if (!contract.isDetailRequest(requestUrl, location.href)) return
    if (status === 403 || status === 429) {
      const seconds = /^\d+$/.test(retryHeader || '') ? Number(retryHeader)
        : retryHeader ? Math.ceil((Date.parse(retryHeader) - Date.now()) / 1000) : null
      const retryAfterSeconds = Number.isSafeInteger(seconds) && seconds > 0 && seconds <= 86400 ? seconds : 60
      return emit({ type: 'blocked', code: `http_${status}`, ...(status === 429 && retryHeader ? { retryAfterSeconds } : {}) })
    }
    if (status !== 200) return
    const items = contract.parseDetail(payload, location.href, requestUrl)
    const uniform = contract.walletSingleSizeNmIds(payload)
    const uniformMetadata = uniform.length ? { walletSingleSizeNmIds: uniform } : {}
    const catalogPage = contract.catalogPage(requestUrl, location.href)
    if (catalogPage && Array.isArray(payload?.products) && payload.products.length <= 1000) {
      emit({ type: 'observations', items, ...uniformMetadata, catalogPage, productCount: payload.products.length })
    } else {
      const nmId = contract.cardNmId(location.href)
      const requested = new URL(requestUrl, location.href).searchParams.get('nm')?.split(';').map(Number) || []
      const loadedNmId = nmId && requested.includes(nmId) && Array.isArray(payload?.products)
        && (payload.products.length === 0 || payload.products.some((product) => product.id === nmId && Array.isArray(product.sizes))) ? nmId : null
      if (items.length || loadedNmId) emit({ type: 'observations', items, ...uniformMetadata,
        ...(loadedNmId ? { loadedNmId, cardOffers: contract.cardOffers(payload, location.href) } : {}) })
    }
  }

  const originalFetch = globalThis.fetch
  globalThis.fetch = function (...args) {
    const result = Reflect.apply(originalFetch, this, args)
    const requestUrl = typeof args[0] === 'string' || args[0] instanceof URL ? String(args[0]) : args[0]?.url
    const method = String(args[1]?.method || args[0]?.method || 'GET').toUpperCase()
    if (method === 'GET' && contract.isDetailRequest(requestUrl, location.href)) {
      void result.then(async (response) => {
        if (response.url && !contract.isDetailRequest(response.url, location.href)) return
        if (response.status === 403 || response.status === 429) return report(response.status, null, requestUrl, response.headers?.get('Retry-After'))
        if (response.status === 200) report(200, await response.clone().json(), requestUrl)
      }).catch(() => {})
    }
    return result
  }

  const originalOpen = XMLHttpRequest.prototype.open
  XMLHttpRequest.prototype.open = function (method, url, ...args) {
    if (String(method).toUpperCase() === 'GET' && contract.isDetailRequest(String(url), location.href)) {
      this.addEventListener('load', () => {
        if (!contract.isDetailRequest(this.responseURL, location.href)) return
        try {
          const payload = this.status === 200
            ? (this.responseType === 'json' ? this.response : JSON.parse(this.responseText)) : null
          report(this.status, payload, this.responseURL, this.status === 429 ? this.getResponseHeader?.('Retry-After') : null)
        } catch { /* A non-JSON response is not a price observation. */ }
      }, { once: true })
    }
    return Reflect.apply(originalOpen, this, [method, url, ...args])
  }
})()
