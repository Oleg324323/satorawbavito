import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { expect, it } from 'vitest'

it('shows live Avito notification counts and opens details on demand', async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({
    configFile: false, envFile: false, root, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': JSON.stringify('test'), 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('') },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: {
      entry: fileURLToPath(new URL('./__fixtures__/reportLoadingBrowser.tsx', import.meta.url)),
      formats: ['iife'], name: 'AvitoNotificationsTest',
    } },
  })
  const outputs = Array.isArray(result) ? result : [result]
  const bundle = outputs.flatMap(output => 'output' in output ? output.output : [])
    .find(output => output.type === 'chunk' && output.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing Avito notifications test bundle')

  const browser = await chromium.launch({ headless: true })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block' })
    let notificationRequests = 0
    await page.route('**/*', route => {
      const url = new URL(route.request().url())
      if (url.origin === 'http://satorna.test' && url.pathname === '/avito/notifications') {
        return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      }
      if (url.origin === 'http://satorna.test' && url.pathname === '/api/v1/avito/notifications') {
        notificationRequests++
        return route.fulfill({ contentType: 'application/json', body: JSON.stringify({
          status: 'synced', summary: { total: 2, unread: 1, critical: 1, warning: 0, info: 1, sectionsBlocked: 0 },
          items: [
            { id: 'avito-alert', title: 'Avito alert', details: 'Нужно проверить объявление', severity: 'critical',
              category: 'avito', source: 'Объявления Авито', manager: 'Админ', createdAt: new Date().toISOString(),
              readAt: null, entityType: 'account', entityId: '1', route: '/avito/listings', blockedActions: [] },
            { id: 'avito-info', title: 'Avito info', details: 'Данные обновлены', severity: 'info',
              category: 'avito', source: 'Статистика Авито', manager: 'Админ', createdAt: new Date().toISOString(),
              readAt: new Date().toISOString(), entityType: 'account', entityId: '1', route: '/avito/stats', blockedActions: [] },
          ], rulesRows: [], recipientRows: [], channelRows: [], historyRows: [],
        }) })
      }
      return route.abort()
    })
    await page.goto('http://satorna.test/avito/notifications')
    await page.addScriptTag({ content: bundle.code })
    const surface = page.locator('#tab-notifications')
    await surface.locator('#notifTableWrap').getByText('Avito alert', { exact: true }).waitFor({ state: 'visible', timeout: 15_000 })
    expect(await surface.isVisible()).toBe(true)
    expect(await surface.locator('#notifKpiUnread').innerText()).toBe('1')
    expect(await surface.locator('#notifKpiCritical').innerText()).toBe('1')
    expect(await surface.locator('#notifKpiPeriod').innerText()).toBe('2')
    expect(await surface.locator('#notifCategoryChips').getByText('Все Авито', { exact: true }).count()).toBe(1)
    expect(await surface.locator('#notifCategoryChips').getByText('Отчёты', { exact: true }).count()).toBe(0)
    expect(await surface.locator('.notif-side').isVisible()).toBe(false)
    await surface.locator('#notifTableWrap').getByText('Avito alert', { exact: true }).click()
    await surface.locator('.notif-side').waitFor({ state: 'visible' })
    expect(notificationRequests).toBe(1)
  } finally { await browser.close() }
}, 60_000)
