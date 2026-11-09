import React from 'react'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { renderToString } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

// The public route must render even when the authentication service is unavailable.
vi.mock('../auth/AuthProvider', () => ({
  AuthProvider: () => React.createElement('div', { 'data-auth-required': true }, 'Authentication required'),
}))

import { AppEntryRoutes } from '../../App'

const read = (path: string) => readFileSync(resolve(process.cwd(), path), 'utf8')
const policy = read('public/wb-privacy.html')

describe('public WB privacy policy', () => {
  it.each(['/wb/privacy', '/wb/privacy/', '/wb/privacy?source=extension'])('renders %s without mounting authentication', path => {
    const html = renderToString(<MemoryRouter initialEntries={[path]}><AppEntryRoutes /></MemoryRouter>)
    expect(html).toContain('src="/wb-privacy.html"')
    expect(html).toContain('Политика конфиденциальности Wildberries (WB)')
    expect(html).not.toContain('data-auth-required')
  })

  it.each(['/wb/repricer', '/wb/reports', '/avito/privacy', '/settings/profile'])('keeps %s inside existing authentication', path => {
    const html = renderToString(<MemoryRouter initialEntries={[path]}><AppEntryRoutes /></MemoryRouter>)
    expect(html).toContain('data-auth-required')
    expect(html).not.toContain('<iframe')
  })

  it('preserves the original layout and all twelve sections without Avito references', () => {
    const original = read('public/avito-privacy.html')
    expect(policy.match(/<style>[\s\S]*?<\/style>/)?.[0]).toBe(original.match(/<style>[\s\S]*?<\/style>/)?.[0])
    expect(policy.match(/<h2>/g)).toHaveLength(12)
    expect(policy).not.toMatch(/avito|авито|заказах и возвратах/i)
    expect(policy).toContain('Satorna — цены Wildberries')
    expect(policy).toContain('mailto:masorin777@yandex.ru')
    expect(policy).toContain('href="/wb/privacy" target="_top"')
    expect(policy).not.toMatch(/<script[^>]+src=|fetch\(|localStorage|<form/)
  })

  it('documents the actual WB extension permissions and hosts', () => {
    const manifest = JSON.parse(read('../wb-prices-extension/manifest.json'))
    for (const permission of manifest.permissions) expect(policy).toContain(`<code>${permission}</code>`)
    for (const host of manifest.host_permissions) expect(policy).toContain(`<code>${host}</code>`)
    for (const unused of ['activeTab', 'scripting', 'tabs']) expect(policy).not.toContain(`<code>${unused}</code>`)
  })
})
