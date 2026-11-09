import { readFileSync } from 'node:fs'
import { match } from 'path-to-regexp'
import { describe, expect, it } from 'vitest'

const config = JSON.parse(readFileSync(new URL('../../../vercel.json', import.meta.url), 'utf8'))
const gateway = JSON.parse(readFileSync(new URL('../../../../deploy/vercel-gateway.json', import.meta.url), 'utf8'))

describe('production direct application entry', () => {
  it.each(['/', '/wb/repricer', '/avito/orders', '/assets/entry.js', '/downloads/extension.zip'])(
    'redirects %s before serving large application files through the CDN', path => {
      const redirect = config.redirects.find((rule: { source: string }) => match(rule.source)(path))
      expect(redirect?.destination).toBe('https://app.comcookie.store/:path')
      expect(redirect?.permanent).toBe(false)
      const route = gateway.routes.find((rule: { src: string }) => new RegExp(`^${rule.src}$`).test(path))
      expect(route.status).toBe(307)
      expect(route.headers.Location).toBe('https://app.comcookie.store/$1')
    },
  )

  it.each(['/api/v1/avito/orders/browser-snapshot', '/api/v2/catalog/skus', '/api/wb/reports/pnl', '/health/ready'])(
    'keeps the existing %s proxy without redirects for installed collectors', path => {
      expect(config.redirects.some((rule: { source: string }) => match(rule.source)(path))).toBe(false)
      const route = gateway.routes.find((rule: { src: string }) => new RegExp(`^${rule.src}$`).test(path))
      expect(route.status).toBeUndefined()
      expect(route.dest).toMatch(/^https:\/\/api\.elfprint-system\.ru:8443\//)
      expect(route.headers['Cache-Control']).toBe('no-store')
    },
  )
})
