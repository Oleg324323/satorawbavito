export type AvitoCollectionResult = { state: 'complete' | 'partial' | 'unavailable' | 'error'; message: string }

export function collectAvitoForXlsx(timeoutMs = 180_000): Promise<AvitoCollectionResult> {
  if (typeof window === 'undefined') return Promise.resolve({ state: 'unavailable', message: 'Расширение Авито недоступно.' })
  const id = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`
  return new Promise(resolve => {
    let acknowledged = false
    let finished = false
    const finish = (result: AvitoCollectionResult) => {
      if (finished) return
      finished = true
      clearTimeout(ackTimer)
      clearTimeout(resultTimer)
      window.removeEventListener('message', onMessage)
      resolve(result)
    }
    const onMessage = (event: MessageEvent) => {
      const data = event.data
      if (event.source !== window || event.origin !== window.location.origin || data?.source !== 'satorna-extension' || data.id !== id) return
      if (data.type === 'AVITO_COLLECTION_ACK') acknowledged = true
      if (data.type === 'AVITO_COLLECTION_RESULT') finish({
        state: data.ok ? (data.complete ? 'complete' : 'partial') : 'error',
        message: typeof data.message === 'string' ? data.message : '',
      })
    }
    const ackTimer = setTimeout(() => {
      if (!acknowledged) finish({ state: 'unavailable', message: 'Расширение Авито не отвечает. Откройте расширение и нажмите «Собрать всё», затем повторите экспорт.' })
    }, 1500)
    const resultTimer = setTimeout(() => finish({ state: 'error', message: 'Обновление ещё не подтверждено. Дождитесь окончания сбора в расширении и повторите экспорт.' }), timeoutMs)
    window.addEventListener('message', onMessage)
    window.postMessage({ source: 'satorna-web', type: 'AVITO_COLLECT_FOR_XLSX', id }, window.location.origin)
  })
}
