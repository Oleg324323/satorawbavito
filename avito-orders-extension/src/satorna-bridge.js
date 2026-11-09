// Page actions can request collection, but cannot read the extension token.
;(function () {
  if (window !== window.top) return
  window.addEventListener('message', async event => {
    const request = event.data
    if (event.source !== window || event.origin !== location.origin || request?.source !== 'satorna-web' ||
      request?.type !== 'AVITO_COLLECT_FOR_XLSX' || !/^[a-zA-Z0-9-]{8,80}$/.test(request.id || '')) return
    const reply = (type, data = {}) => window.postMessage({ source: 'satorna-extension', type, id: request.id, ...data }, location.origin)
    reply('AVITO_COLLECTION_ACK')
    try {
      const settings = await globalThis.SatornaConnection.read()
      if (globalThis.SatornaConnection.destination(settings) !== location.origin) {
        throw new Error('Адрес Satorna в расширении не совпадает с открытым сайтом. Исправьте его в настройках расширения.')
      }
      if (!settings.accessToken) throw new Error('В расширении не сохранён токен Satorna.')
      const result = await chrome.runtime.sendMessage({ type: 'AVITO_ORDERS_OPEN_AND_COLLECT' })
      reply('AVITO_COLLECTION_RESULT', result?.ok ? { ok: true, complete: result.complete !== false, message: result.message || '' }
        : { ok: false, message: result?.error || 'Расширение не завершило сбор.' })
    } catch (error) {
      reply('AVITO_COLLECTION_RESULT', { ok: false, message: error instanceof Error ? error.message : String(error) })
    }
  })
})()
