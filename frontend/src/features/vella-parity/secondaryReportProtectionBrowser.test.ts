import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { beforeAll, expect, it } from 'vitest'

let code: string
beforeAll(async () => {
  const mutation = process.env.SATORNA_TEST_DROP_SECONDARY_PROTECTION ?? ''
  if (mutation !== '' && mutation !== '1') throw new Error('Unknown secondary protection mutation')
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const entry = path.join(root, 'secondary-report-protection-test-entry.js')
  const result = await build({
    root, configFile: false, envFile: false, logLevel: 'silent',
    plugins: [{
      name: 'test-only-secondary-bridge-export', enforce: 'pre',
      resolveId(id) { if (id === entry) return entry },
      load(id) {
        if (id === entry) return `import { installSecondaryReportRowsBridge } from ${JSON.stringify(path.join(root, 'src/features/vella-parity/VellaHtmlParityPage.tsx'))}; window.installSecondaryBridgeForTest = installSecondaryReportRowsBridge;`
      },
      transform(source, id) {
        if (!id.endsWith('/VellaHtmlParityPage.tsx')) return
        const declaration = 'function installSecondaryReportRowsBridge() {'
        if (source.split(declaration).length !== 2) throw new Error('Expected exactly one bridge declaration')
        let transformed = source.replace(declaration, `export ${declaration}`)
        // Opt-in mutation is applied in memory only; normal verification uses original runtime.
        if (mutation === '1') {
          const guard = 'if (protectedLiveTabs.has(tab)) return'
          if (transformed.split(guard).length !== 2) throw new Error('Expected exactly one protected-body guard')
          transformed = transformed.replace(guard, '')
        }
        return transformed
      },
    }, react()],
    define: { 'process.env.NODE_ENV': JSON.stringify('test'), 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('') },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: { entry, formats: ['iife'], name: 'SecondaryProtectionTest' } },
  })
  const outputs = Array.isArray(result) ? result : [result]
  const bundle = outputs.flatMap(output => 'output' in output ? output.output : [])
    .find(output => output.type === 'chunk' && output.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing secondary protection test bundle')
  code = bundle.code
}, 60_000)

it.each([false, true])('preserves live report rows and restores legacy hooks when renderer throws=%s', async throws => {
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block' })
    const network: string[] = [], errors: string[] = []
    await page.route('**/*', route => { network.push(route.request().url()); return route.abort() })
    page.on('pageerror', error => errors.push(error.message))
    await page.setContent('<main></main>')
    await page.addScriptTag({ content: code })
    const observed = await page.evaluate(async shouldThrow => {
      const runtime = window as unknown as {
        installSecondaryBridgeForTest(): void
        renderSecondaryReports(): void
        applyGenericReportFilter(tab: HTMLElement): void
        __vellaSecondaryReportRowsSnapshot(tab: string): { html: string; count: number }
        __vellaReactRenderSecondaryReportRows?: () => void
      }
      const protectedTabs = ['repricer-stats', 'rnp', 'ads', 'stock', 'pnl']
      const allTabs = [...protectedTabs, 'week', 'other']
      document.querySelector('main')!.innerHTML = allTabs.map(tab =>
        `<section class="tab-content" id="tab-${tab}"><table><tbody><tr><td>live ${tab}</td></tr></tbody></table></section>`,
      ).join('')
      const body = (tab: string) => document.querySelector<HTMLTableSectionElement>(`#tab-${tab} tbody`)!
      const initialRows = protectedTabs.map(tab => body(tab).firstElementChild)
      const descriptor = Object.getOwnPropertyDescriptor(Element.prototype, 'innerHTML')!
      const filtered: string[] = []
      const originalFilter = (tab: HTMLElement) => { filtered.push(tab.id) }
      runtime.applyGenericReportFilter = originalFilter
      let publications = 0, renders = 0, legacyCalls = 0
      window.addEventListener('vella:secondary-report-rows-updated', () => { publications++ })
      runtime.__vellaReactRenderSecondaryReportRows = () => { renders++ }
      runtime.renderSecondaryReports = () => {
        legacyCalls++
        for (const tab of allTabs) {
          body(tab).innerHTML = `<tr><td>legacy ${tab}</td></tr>`
        }
        runtime.applyGenericReportFilter(document.getElementById('tab-pnl')!)
        runtime.applyGenericReportFilter(document.getElementById('tab-other')!)
        if (shouldThrow) throw new Error('synthetic legacy failure')
      }
      runtime.installSecondaryBridgeForTest()
      runtime.installSecondaryBridgeForTest() // Reinstallation must not duplicate wrapping/publication.
      let caught = ''
      try { runtime.renderSecondaryReports() } catch (error) { caught = (error as Error).message }
      await new Promise(resolve => window.setTimeout(resolve, 10))
      const afterDescriptor = Object.getOwnPropertyDescriptor(Element.prototype, 'innerHTML')!
      const preserved = protectedTabs.map((tab, index) => ({
        tab, text: body(tab).textContent, sameNode: body(tab).firstElementChild === initialRows[index],
      }))
      const week = runtime.__vellaSecondaryReportRowsSnapshot('week')
      const weekText = body('week').textContent
      const otherText = body('other').textContent
      // Normal DOM writes and the original filter must work after either exit path.
      body('pnl').innerHTML = '<tr><td>post-render replacement</td></tr>'
      runtime.applyGenericReportFilter(document.getElementById('tab-pnl')!)
      return { preserved, caught, week, weekText, otherText, publications, renders, legacyCalls, filtered,
        postRenderText: body('pnl').textContent,
        descriptorRestored: ['get', 'set', 'configurable', 'enumerable'].every(key =>
          descriptor[key as keyof PropertyDescriptor] === afterDescriptor[key as keyof PropertyDescriptor]),
        filterRestored: runtime.applyGenericReportFilter === originalFilter,
      }
    }, throws)
    expect(observed.preserved).toEqual([
      { tab: 'repricer-stats', text: 'live repricer-stats', sameNode: true },
      { tab: 'rnp', text: 'live rnp', sameNode: true },
      { tab: 'ads', text: 'live ads', sameNode: true },
      { tab: 'stock', text: 'live stock', sameNode: true },
      { tab: 'pnl', text: 'live pnl', sameNode: true },
    ])
    expect(observed.weekText).toBe('live week')
    expect(observed.week).toEqual({ html: `<tr><td>${throws ? 'live' : 'legacy'} week</td></tr>`, count: 1 })
    expect(observed.otherText).toBe('legacy other')
    expect(observed.caught).toBe(throws ? 'synthetic legacy failure' : '')
    expect(observed.legacyCalls).toBe(1)
    expect(observed.publications).toBe(throws ? 0 : 1)
    expect(observed.renders).toBe(throws ? 0 : 1)
    expect(observed.filtered).toEqual(throws ? ['tab-other', 'tab-pnl'] : ['tab-other', 'tab-week', 'tab-pnl'])
    expect(observed.descriptorRestored).toBe(true)
    expect(observed.filterRestored).toBe(true)
    expect(observed.postRenderText).toBe('post-render replacement')
    expect(network).toEqual([])
    expect(errors).toEqual([])
  } finally { await browser.close() }
}, 30_000)
