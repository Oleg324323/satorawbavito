import { useEffect, useRef, useState } from 'react'
import { buildApiUrl } from '@/lib/api'
import { authorizationHeaders } from '@/features/auth/authApi'

export function AvitoRepricerPhoto({ url, title, photoId, accessToken }: { url?: string | null; title: string; photoId?: number | null; accessToken?: string | null }) {
  const container = useRef<HTMLSpanElement>(null)
  const [storedPhoto, setStoredPhoto] = useState<{ id: number; url: string } | null>(null)
  const [failedUrl, setFailedUrl] = useState<string | null>(null)
  useEffect(() => {
    if (!photoId || !accessToken || !container.current) return
    const controller = new AbortController()
    let blobUrl = '', started = false
    const load = async () => {
      if (started) return
      started = true
      try {
        const response = await fetch(buildApiUrl(`/api/v1/avito/repricer/photos/${photoId}`), {
          headers: authorizationHeaders(accessToken), signal: controller.signal,
        })
        if (!response.ok) return
        const blob = await response.blob()
        if (controller.signal.aborted) return
        blobUrl = URL.createObjectURL(blob)
        setStoredPhoto({ id: photoId, url: blobUrl })
      } catch { /* A missing photo must not hide the listing. */ }
    }
    const observer = typeof IntersectionObserver === 'undefined' ? null : new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) { observer?.disconnect(); void load() }
    }, { rootMargin: '200px' })
    if (observer) observer.observe(container.current)
    else void load()
    return () => { controller.abort(); observer?.disconnect(); if (blobUrl) URL.revokeObjectURL(blobUrl) }
  }, [photoId, accessToken])
  const src = photoId ? (storedPhoto?.id === photoId ? storedPhoto.url : null) : url
  const style = { width: 66, height: 66, flexShrink: 0, borderRadius: 8, objectFit: 'contain' as const }
  return <span ref={container} style={{ ...style, display: 'inline-flex' }}>{src && src !== failedUrl ? <img src={src} alt={title} loading="lazy" style={style} onError={() => setFailedUrl(src)} />
    : <span style={{ ...style, display: 'grid', placeItems: 'center', background: 'var(--gray-50)', fontSize: 10, textAlign: 'center' }}>{photoId ? 'Фото' : 'Фото не получено'}</span>}</span>
}

export type RepricerTrend = { direction: 'up' | 'down' | 'flat' | 'missing'; percent: number | null; delta?: number | null }
export function AvitoRepricerMetric({ value, trend }: { value?: number | null; trend?: RepricerTrend }) {
  const direction = trend?.delta == null && trend?.direction !== 'flat' ? 'missing' : (trend?.direction ?? 'missing')
  const absolute = trend?.delta == null ? '' : `${trend.delta > 0 ? '+' : ''}${trend.delta.toLocaleString('ru-RU')} `
  const label = direction === 'missing' ? 'Нет сравнения' : direction === 'flat' ? 'Без изменений'
    : trend?.percent == null ? `${absolute}(с 0)` : `${absolute}(${trend.percent > 0 ? '+' : ''}${trend.percent.toLocaleString('ru-RU')}%)`
  return <div style={{ display: 'grid', gap: 5, justifyItems: 'end' }}>
    <b>{value == null ? '—' : value.toLocaleString('ru-RU')}</b>
    <span title="Последние 3 завершённых дня против предыдущих 3" style={{ fontSize: 11, whiteSpace: 'nowrap', color: direction === 'up' ? '#15803d' : direction === 'down' ? '#dc2626' : 'var(--gray-500)' }}>
      {direction === 'up' ? '↑ ' : direction === 'down' ? '↓ ' : '— '}{label}
    </span>
  </div>
}
