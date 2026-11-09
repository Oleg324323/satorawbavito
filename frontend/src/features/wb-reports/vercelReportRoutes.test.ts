import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

describe('WB report API Vercel routing', () => {
  it('leaves report endpoints to the backend rewrite without shadowing functions', () => {
    const config = JSON.parse(readFileSync(new URL('../../../vercel.json', import.meta.url), 'utf8')) as {
      rewrites: Array<{ source: string; destination: string }>
    }
    const reportRewrite = config.rewrites.find(({ source }) => new RegExp(`^${source}$`).test('/api/wb/reports/pnl'))
    expect(reportRewrite?.destination).toBe('https://api.elfprint-system.ru:8443/api/wb/$1')

    // Vercel checks filesystem functions before applying rewrites.
    const apiDirectory = fileURLToPath(new URL('../../../api/', import.meta.url))
    const reportFunctions = readdirSync(apiDirectory, { recursive: true, encoding: 'utf8' }).filter((path) => (
      path.startsWith('wb/reports/') && statSync(join(apiDirectory, path)).isFile()
    ))
    expect(reportFunctions).toEqual([])
  })
})
