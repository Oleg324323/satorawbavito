import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { expect, it } from 'vitest'

it('opens the real Avito schedule modal and discards edits on cancel without writing settings', async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({
    configFile: false, envFile: false, root, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': JSON.stringify('test'), 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('') },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false,
      lib: { entry: fileURLToPath(new URL('./__fixtures__/avitoSettingsBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'AvitoSettingsTest' } },
  })
  const outputs = Array.isArray(result) ? result : [result]
  const bundle = outputs.flatMap(output => 'output' in output ? output.output : [])
    .find(output => output.type === 'chunk' && output.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing synthetic settings bundle')
  const browser = await chromium.launch({ headless: true })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block' })
    const unexpected: string[] = []
    const errors: string[] = []
    const apiReads: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', async route => {
      const request = route.request()
      const url = new URL(request.url())
      if (url.origin !== 'http://satorna.test' || request.method() !== 'GET') {
        unexpected.push(`${request.method()} ${url.pathname}`)
        return route.abort()
      }
      if (url.pathname === '/settings-test') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (url.pathname === '/favicon.ico') return route.fulfill({ status: 204 })
      let payload: unknown
      if (url.pathname === '/api/v1/avito/repricer/settings') {
        payload = {
          settings: { enabled: false, autoApplyPricesEnabled: false, executeIntervalMinutes: 60,
            periodDays: 7, activeWindowEnabled: false, activeWindowStartHour: 9,
            activeWindowEndHour: 18, timezone: 'Europe/Moscow', maxChangesPerRun: 10 },
          mode: { workerEnvEnabled: false, priceApplyEnvEnabled: false },
          workerTiming: { lastRunAt: null, nextRunAt: null, secondsUntilNextRun: null, intervalMinutes: 60, ready: false },
        }
      } else if (url.pathname === '/api/v1/avito/repricer/price-approvals/pending') {
        payload = { items: [], loadedAt: '2026-09-09T00:00:00Z' }
      } else {
        unexpected.push(`${request.method()} ${url.pathname}`)
        return route.abort()
      }
      apiReads.push(url.pathname)
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify(payload) })
    })
    await page.goto('http://satorna.test/settings-test')
    await page.addScriptTag({ content: bundle.code })
    const open = page.getByRole('button', { name: 'Настроить время', exact: true })
    await open.waitFor({ state: 'visible', timeout: 10_000 })
    expect(await page.getByLabel('Время до следующего запуска репрайсера Авито').innerText()).toContain('выключен')
    await open.click()
    const modal = page.getByRole('dialog', { name: 'Настройка времени запуска репрайсера Авито' })
    await modal.waitFor({ state: 'visible', timeout: 3000 })
    const input = modal.getByLabel('Интервал в минутах')
    expect(await input.inputValue()).toBe('60')
    expect(await input.getAttribute('min')).toBe('5')
    expect(await input.getAttribute('max')).toBe('1440')
    await modal.getByRole('button', { name: '30 мин', exact: true }).click()
    expect(await input.inputValue()).toBe('30')
    await modal.getByRole('button', { name: 'Отмена', exact: true }).click()
    await modal.waitFor({ state: 'hidden', timeout: 3000 })
    expect(await modal.count()).toBe(0)
    await open.click()
    await modal.waitFor({ state: 'visible', timeout: 3000 })
    expect(await input.inputValue()).toBe('60')
    await modal.getByRole('button', { name: 'Закрыть', exact: true }).click()
    await modal.waitFor({ state: 'hidden', timeout: 3000 })
    expect(await modal.count()).toBe(0)
    expect(apiReads.sort()).toEqual(['/api/v1/avito/repricer/price-approvals/pending', '/api/v1/avito/repricer/settings'])
    expect(unexpected).toEqual([])
    expect(errors).toEqual([])
  } finally { await browser.close() }
}, 60_000)
