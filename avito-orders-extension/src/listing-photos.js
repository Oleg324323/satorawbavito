/* Missing-only photo collection. No credentials are persisted or sent to image hosts. */
;(function (root) {
  const MAX_BYTES = 8 * 1024 * 1024
  function imageUrl(value) {
    try {
      const url = new URL(value)
      return url.protocol === 'https:' && /^(?:[a-z\d-]+\.)?img\.avito\.(?:st|ru)$/.test(url.hostname) && !url.username && !url.password && !url.port ? url.href : null
    } catch { return null }
  }
  function listingUrl(value, itemId) {
    try {
      const url = new URL(value)
      return url.protocol === 'https:' && url.hostname === 'www.avito.ru' && !url.username && !url.password && !url.port && /^\d+$/.test(itemId) && new RegExp('(?:_|/items/)' + itemId + '/?$').test(url.pathname) ? url.href : null
    } catch { return null }
  }
  function unescapeHtml(value) {
    return value.replace(/&amp;/gi, '&').replace(/&quot;/gi, '"').replace(/&#39;|&apos;/gi, "'")
      .replace(/&#(\d+);/g, (_, n) => String.fromCodePoint(Number(n))).replace(/&#x([a-f\d]+);/gi, (_, n) => String.fromCodePoint(parseInt(n, 16)))
  }
  function extractPhoto(html, itemId) {
    const values = new Map()
    for (const tag of html.match(/<meta\b[^>]*>/gi) || []) {
      const attrs = Object.fromEntries([...tag.matchAll(/([\w:-]+)\s*=\s*(["'])(.*?)\2/gs)].map(m => [m[1].toLowerCase(), unescapeHtml(m[3])]))
      values.set((attrs.property || attrs.name || '').toLowerCase(), attrs.content)
    }
    // Never attach a recommendation/redirected product photograph to another item.
    if (values.get('og:url') && !listingUrl(values.get('og:url'), itemId)) return null
    return imageUrl(values.get('og:image')) || imageUrl(values.get('twitter:image'))
  }
  async function boundedBody(response, limit) {
    if (Number(response.headers.get('content-length')) > limit) throw new Error('Файл слишком большой')
    const reader = response.body.getReader(), chunks = []
    let size = 0
    try {
      while (true) {
        const { value, done } = await reader.read()
        if (done) break
        size += value.length
        if (size > limit) throw new Error('Файл слишком большой')
        chunks.push(value)
      }
    } finally { await reader.cancel().catch(() => {}) }
    return new Blob(chunks)
  }
  function check(response) {
    if ([401, 403, 429, 439, 449].includes(response.status)) {
      const error = new Error(`Сбор остановлен: HTTP ${response.status}. Сохранённые фото не потеряны. Проверьте вход в Авито и подключение к Satorna.`)
      error.stop = true
      throw error
    }
    if (!response.ok) throw new Error(`Фото не получено: HTTP ${response.status}`)
  }
  async function collect({ backendUrl, authorization, fetchImpl = fetch, progress = async () => {}, shouldStop = () => false, preparePhotos, loadPhoto }) {
    const headers = { Authorization: authorization }
    const api = backendUrl.replace(/\/$/, '') + '/api/v1/avito/repricer/photos'
    const contextResponse = await fetchImpl(api + '/collection-context', { headers, redirect: 'error', credentials: 'omit', signal: AbortSignal.timeout(15000) })
    check(contextResponse)
    const context = await contextResponse.json()
    if (!Array.isArray(context.missing)) throw new Error('Некорректный ответ Satorna')
    const state = { stage: 'running', total: context.total, saved: context.saved, skipped: context.saved, failed: 0, processed: 0 }
    await progress({ ...state })
    const visible = context.missing.length && preparePhotos ? await preparePhotos(context.missing, async (found, details = {}) => {
      state.found = found
      Object.assign(state, details)
      state.phase = 'Поиск фото в списке объявлений'
      await progress({ ...state })
    }) : {}
    state.phase = 'Сохранение фото в базу'
    const missing = context.missing.map(row => ({ ...row, imageUrl: imageUrl(row.imageUrl) || imageUrl(visible[row.itemId]) }))
      .sort((a, b) => Number(Boolean(b.imageUrl)) - Number(Boolean(a.imageUrl)))
    async function processRow(row) {
      if (shouldStop() || state.stage === 'paused') { state.stage = 'paused'; return }
      try {
        const listing = listingUrl(row.url, row.itemId)
        if (!listing) throw new Error('Неизвестный адрес объявления')
        let photo = imageUrl(row.imageUrl)
        if (!photo && loadPhoto) {
          photo = imageUrl(await loadPhoto(row))
          if (!photo) throw new Error(`Фото объявления ${row.itemId} не найдено; сбор продолжается`)
        } else if (!photo) {
          const page = await fetchImpl(listing, { credentials: 'include', signal: AbortSignal.timeout(15000) })
          check(page)
          if (page.url && !listingUrl(page.url, row.itemId)) throw new Error('Авито перенаправил на другую страницу')
          const html = await (await boundedBody(page, MAX_BYTES)).text()
          photo = extractPhoto(html, row.itemId)
          if (!photo) throw new Error('В объявлении не найдена фотография')
        }
        const response = await fetchImpl(photo, { credentials: 'omit', redirect: 'error', signal: AbortSignal.timeout(15000) })
        check(response)
        if (!/^image\/(jpeg|png|webp)(?:;|$)/i.test(response.headers.get('content-type') || '')) throw new Error('Вместо фото получен другой файл')
        const body = await boundedBody(response, MAX_BYTES)
        const query = new URLSearchParams({ accountId: row.accountId, itemId: row.itemId })
        const uploaded = await fetchImpl(api + '/import?' + query, {
          method: 'POST', headers: { ...headers, 'Content-Type': 'application/octet-stream' }, body,
          credentials: 'omit', redirect: 'error', signal: AbortSignal.timeout(15000),
        })
        check(uploaded)
        const result = await uploaded.json()
        if (!result.photoId) throw new Error('Satorna не подтвердила сохранение фото')
        state.saved += 1
        if (state.stage !== 'paused') delete state.message
      } catch (error) {
        state.failed += 1
        if (state.stage !== 'paused' || error.stop) state.message = error.message
        if (error.stop) state.stage = 'paused'
      }
      state.processed += 1
      await progress({ ...state })
    }
    // Bound both CDN uploads and remaining detail-page reads. Stop scheduling
    // additional work as soon as Avito reports a throttle or challenge.
    const ready = missing.filter(row => row.imageUrl)
    let cursor = 0
    await Promise.all(Array.from({ length: Math.min(5, ready.length) }, async () => {
      while (cursor < ready.length && state.stage !== 'paused') await processRow(ready[cursor++])
    }))
    state.phase = 'Проверка оставшихся объявлений'
    const remainder = missing.filter(row => !row.imageUrl)
    cursor = 0
    await Promise.all(Array.from({ length: Math.min(5, remainder.length) }, async () => {
      while (cursor < remainder.length && state.stage !== 'paused') await processRow(remainder[cursor++])
    }))
    if (state.stage === 'running') state.stage = state.failed ? 'partial' : 'done'
    await progress({ ...state })
    return state
  }
  root.SatornaListingPhotos = { imageUrl, listingUrl, extractPhoto, collect }
})(globalThis)
