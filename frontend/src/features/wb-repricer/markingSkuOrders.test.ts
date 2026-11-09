import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { describe, expect, it } from 'vitest'

describe('SKU orders prototype without marking gate', () => {
  it('opens Orders and retains estimates and deadline without an invented marking requirement', async () => {
    const root = fileURLToPath(new URL('../../../', import.meta.url))
    const result = await build({
      configFile: false,
      envFile: false,
      root,
      logLevel: 'silent',
      // Match Vitest's development JSX runtime; this is a local test bundle.
      define: { 'process.env.NODE_ENV': JSON.stringify('test') },
      plugins: [react()],
      resolve: { alias: { '@': path.join(root, 'src') } },
      build: {
        write: false,
        minify: false,
        lib: { entry: fileURLToPath(new URL('./__fixtures__/skuOrdersBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'SkuOrdersTest' },
      },
    })
    const outputs = Array.isArray(result) ? result : [result]
    const bundle = outputs.flatMap((output) => 'output' in output ? output.output : [])
      .find((output) => output.type === 'chunk' && output.isEntry)
    if (!bundle || bundle.type !== 'chunk') throw new Error('Missing local test bundle')
    const browser = await chromium.launch({ headless: true })
    try {
      const page = await browser.newPage({ serviceWorkers: 'block' })
      await page.route('**/*', (route) => route.abort())
      const errors: string[] = []
      page.on('pageerror', (error) => errors.push(error.message))
      await page.setContent('<main id="root"></main>')
      await page.addScriptTag({ content: bundle.code })
      await page.getByRole('button', { name: 'Заказы', exact: true }).click({ timeout: 5_000 }).catch((error) => {
        throw new Error(`SKU mount failed: ${JSON.stringify(errors)}; ${error.message}`)
      })
      const table = page.locator('.vella-panel table').filter({ hasText: 'Ожидаемые заказы' })
      await table.waitFor({ state: 'visible' })
      const text = await table.innerText()
      expect(text).toContain('Производственное время')
      expect(text).toContain('FBS дедлайн')
      expect(text).not.toContain('КИЗ')
      expect(text).not.toContain('будущий backend gate')
      expect(errors).toEqual([])
    } finally {
      await browser.close()
    }
  }, 60_000)
})
