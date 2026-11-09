import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

describe('canonical API Vercel routing', () => {
  it('routes /api/v2 to the backend before the generic SPA fallback', () => {
    const configPath = fileURLToPath(new URL('../../../vercel.json', import.meta.url))
    const config = JSON.parse(readFileSync(configPath, 'utf8')) as { rewrites: Array<{ source: string; destination: string }> }
    const v2Index = config.rewrites.findIndex((rewrite) => rewrite.source === '/api/v2/(.*)')
    const genericIndex = config.rewrites.findIndex((rewrite) => rewrite.source === '/api/(.*)')

    expect(v2Index).toBeGreaterThanOrEqual(0)
    expect(v2Index).toBeLessThan(genericIndex)
    expect(config.rewrites[v2Index]?.destination).toBe('https://api.elfprint-system.ru:8443/api/v2/$1')
  })
})
