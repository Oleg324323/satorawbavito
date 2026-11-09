import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import { VellaHtmlRepricer } from '@/components/vella-system/html/VellaHtmlRepricer'
import { WbRepricerPage } from './WbRepricerPage'

describe('repricer navigation without marking prototype', () => {
  it.each([{ name: 'HTML repricer', Page: VellaHtmlRepricer }, { name: 'repricer page', Page: WbRepricerPage }])('preserves repricer controls without a marking counter in $name', ({ Page }) => {
    vi.stubGlobal('localStorage', { getItem: () => null })
    try {
      const html = renderToStaticMarkup(createElement(Page))
      expect(html).toContain('Все товары')
      expect(html).toContain('Стратегии')
      expect(html).toContain('Уведомления')
      expect(html).not.toContain('КИЗ')
    } finally {
      vi.unstubAllGlobals()
    }
  })
})
