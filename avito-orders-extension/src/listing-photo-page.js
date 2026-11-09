/* Runs in the extension's isolated world on rendered Avito pages. */
;(function (root) {
  function read(itemIds, detailId = null) {
    const wanted = new Set(itemIds.map(String)), photos = {}
    const heading = [document.title, document.querySelector('h1')?.textContent].filter(Boolean).join(' ')
    const challenge = /проверка безопасности|доступ ограничен|подтвердите.*(?:человек|робот)|captcha/i.test(heading)
      || Boolean(document.querySelector('iframe[src*="captcha"], form[action*="captcha"]'))
    if (challenge) return { photos, blocked: true }
    const imageApi = root.SatornaAvitoItemPhoto
    const idFromUrl = value => {
      try { return new URL(value, location.href).pathname.match(/(?:_|\/items\/)(\d+)\/?$/)?.[1] } catch { return null }
    }
    function add(id, value) {
      const url = imageApi?.normalizeImageUrl(value)
      if (id && wanted.has(String(id)) && url && !photos[id]) photos[id] = url
    }
    function media(node) {
      const urls = [...node.querySelectorAll('img')].map(img => imageApi?.imageSource(img))
      for (const element of node.querySelectorAll('[style]')) {
        if (element.closest?.('[data-marker*="avatar"], [class*="avatar"]')) continue
        for (const match of (element.style?.backgroundImage || '').matchAll(/url\(["']?([^"')]+)["']?\)/g)) urls.push(match[1])
      }
      return urls.map(value => imageApi?.normalizeImageUrl(value)).filter(Boolean)
    }
    // Match each photograph to its own product link/card, never a neighbouring row.
    for (const link of document.querySelectorAll('a[href]')) {
      const id = idFromUrl(link.href)
      if (!wanted.has(id)) continue
      const roots = [link]
      // Avito Pro and statistics put the image beside (not inside) the title link.
      // Stop before an ancestor containing a different listing.
      let card = link.parentElement
      for (let level = 0; card && level < 7; level++, card = card.parentElement) {
        const cardIds = new Set([...card.querySelectorAll('a[href]')].map(a => idFromUrl(a.href)).filter(Boolean))
        if (cardIds.size > 1) break
        if (cardIds.size === 1 && cardIds.has(id) && media(card).length) { roots.push(card); break }
      }
      for (const node of roots) for (const url of media(node)) add(id, url)
    }
    // Structured product objects can supply multiple cards without opening each one.
    let visited = 0
    function visit(value, depth = 0) {
      if (!value || typeof value !== 'object' || depth > 18 || ++visited > 20000) return
      const id = String(value.itemId ?? value.item_id ?? value.id ?? '')
      if (wanted.has(id)) {
        // Only explicit image fields of this object, not recommendations below it.
        const media = [value.imageUrl, value.photoUrl, value.image, value.images, value.photos, value.picture, value.pictures]
        for (const field of media) for (const url of imageApi?.extractPayloadImages(field, 1) || []) add(id, url)
      }
      for (const child of Object.values(value)) visit(child, depth + 1)
    }
    for (const script of document.querySelectorAll('script[type="application/json"], script[type="application/ld+json"], script#__NEXT_DATA__')) {
      try { visit(JSON.parse(script.textContent || 'null')) } catch { /* Not a JSON product payload. */ }
    }
    if (detailId && idFromUrl(location.href) === String(detailId)) {
      const canonical = document.querySelector('meta[property="og:url"]')?.content
      if (!canonical || idFromUrl(canonical) === String(detailId)) {
        for (const meta of document.querySelectorAll('meta[property="og:image"], meta[name="twitter:image"]')) add(detailId, meta.content)
        for (const img of document.querySelectorAll('[data-marker="item-view/gallery"] img, [data-marker="image-frame"] img, [data-marker="item-view/image"] img, [data-marker="image-frame/image"]')) add(detailId, imageApi?.imageSource(img))
      }
    }
    const ids = [...new Set([...document.querySelectorAll('a[href]')].map(a => idFromUrl(a.href)).filter(Boolean))]
    const listReady = [...document.querySelectorAll('button')].some(b =>
      b.getAttribute('aria-label') === 'Следующая страница' || (b.textContent || '').trim() === 'Показать ещё')
    return { photos, blocked: false, signature: ids.join(','), count: ids.length, listReady }
  }
  function advance() {
    if (!/^\/profile(?:\/pro\/items|\/statistics\/dynamics)?\/?$/.test(location.pathname)) return false
    const next = [...document.querySelectorAll('button')].find(b => b.getAttribute('aria-label') === 'Следующая страница')
      || [...document.querySelectorAll('button')].find(b => (b.textContent || '').trim() === 'Показать ещё')
    if (!next || next.disabled || next.getAttribute('aria-disabled') === 'true') return false
    next.click()
    return true
  }
  root.SatornaListingPhotoPage = { read, advance }
})(globalThis)
