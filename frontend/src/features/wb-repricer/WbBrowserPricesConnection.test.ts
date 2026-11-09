import path from 'node:path'
import { fileURLToPath } from 'node:url'
import react from '@vitejs/plugin-react'
import { chromium } from 'playwright'
import { build } from 'vite'
import { expect, it } from 'vitest'

const issuedToken = (sequence: number) => `sat_wbp_${String(sequence).padStart(43, 'a')}`
const decodeKey = (value: string) => JSON.parse(Buffer.from(value.slice('satorna-connect-v1.'.length), 'base64url').toString('utf8'))

it('shows a one-time WB extension key only in its issuing scope and recovers uncertain writes by reading status', async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const entry = path.join(root, 'wb-browser-prices-connection-test.jsx')
  const result = await build({ root, configFile: false, envFile: false, logLevel: 'silent',
    plugins: [react(), { name: 'browser-prices-fixture', enforce: 'pre', resolveId: id => id === entry ? entry : null,
      load: id => id === entry ? `
        import { useState } from 'react';
        import { createRoot } from 'react-dom/client';
        import { BrowserRouter } from 'react-router-dom';
        import { AuthContext } from '@/features/auth/authContext';
        import { VellaHtmlParityPage } from '@/features/vella-parity/VellaHtmlParityPage';
        function Fixture() {
          const [session, setSession] = useState(1);
          const [org, setOrg] = useState(2);
          const noop = async () => {};
          const auth = { status: 'authenticated', isAuthenticated: true, accessToken: 'synthetic-' + org + '-' + session,
            profile: null, sessions: [], sessionsStatus: 'idle', login: noop, register: noop, logout: noop,
            refreshProfile: noop, refreshSessions: noop, revokeSession: noop, revokeOtherSessions: async () => 0,
            cabinetMe: { organization: { organizationId: org, name: 'Synthetic', slug: 'synthetic', createdAt: '2026-01-01T00:00:00Z' },
              user: { userId: 'synthetic-user', organizationId: org, email: 'synthetic@example.test', fullName: 'Synthetic',
                permissionProfile: 'admin', permissions: ['integrations:write', 'cabinet:read'], isActive: true, createdAt: '2026-01-01T00:00:00Z' },
              activeSession: { sessionId: 'synthetic-session-' + session, userId: 'synthetic-user', issuedAt: '2026-01-01T00:00:00Z',
                expiresAt: '2099-01-01T00:00:00Z', lastSeenAt: '2026-01-01T00:00:00Z', revokedAt: null, revokedReason: null, userAgent: null, ipAddress: null },
              preferences: { userId: 'synthetic-user', timezone: 'Europe/Moscow', notificationSettings: {}, exportSettings: {}, updatedAt: '2026-01-01T00:00:00Z' } } };
          return <BrowserRouter><AuthContext.Provider value={auth}>
            <button onClick={() => setSession(value => value + 1)}>Сменить тестовую сессию</button>
            <button onClick={() => setOrg(value => value + 1)}>Сменить тестовую организацию</button>
            <VellaHtmlParityPage />
          </AuthContext.Provider></BrowserRouter>;
        }
        createRoot(document.getElementById('root')).render(<Fixture />);
      ` : null }],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""', 'import.meta.env.VITE_WB_LIVE_ENABLED': '"false"' },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: { entry, formats: ['iife'], name: 'WbBrowserPricesConnectionTest' } },
  })
  const bundle = (Array.isArray(result) ? result : [result]).flatMap(output => 'output' in output ? output.output : [])
    .find(output => output.type === 'chunk' && output.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing browser connection fixture')
  const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL })
  let release = () => {}
  try {
    const context = await browser.newContext({ serviceWorkers: 'block', viewport: { width: 1280, height: 960 }, permissions: ['clipboard-read', 'clipboard-write'] })
    const page = await context.newPage()
    page.setDefaultTimeout(5000)
    const errors: string[] = [], unexpected: string[] = [], writes: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    let configured = false, observed = false, delayed = false, failWrite = false, sequence = 0
    let gate = Promise.resolve()
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url()), method = request.method()
      if (request.resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<title>WB extension settings test</title><div id="root"></div>' })
      if (request.resourceType() === 'image') return route.fulfill({ status: 204 })
      if (url.origin === 'https://fonts.googleapis.com') return route.fulfill({ contentType: 'text/css', body: '' })
      if (method === 'GET' && url.pathname === '/api/v1/wb/browser-prices/accounts') {
        return route.fulfill({ json: { data: [11, 12].map(id => ({ marketplaceAccountId: id, provider: 'wb', externalAccountId: `synthetic-${id}`, displayName: `Тестовый WB ${id}`, status: 'active' })) } })
      }
      if (url.pathname === '/api/v1/wb/browser-prices/connection') {
        const id = method === 'POST' ? request.postDataJSON().marketplaceAccountId : Number(url.searchParams.get('marketplaceAccountId'))
        if (method !== 'GET') writes.push(`${method}:${id}`)
        if (method === 'POST') { sequence += 1; if (delayed) await gate }
        if (method === 'POST' && failWrite) return route.fulfill({ status: 503, json: { detail: 'PRIVATE_SYNTHETIC_ERROR_MUST_NOT_RENDER' } })
        if (method === 'POST') configured = true
        if (method === 'DELETE') configured = false
        return route.fulfill({ json: { marketplaceAccountId: id, configured, tokenPrefix: configured ? 'sat_wb_synthetic' : null,
          expiresAt: configured ? '2099-01-01T00:00:00Z' : null,
          lastObservedAt: configured && observed ? '2026-09-16T10:00:00Z' : null, knownPrices: configured && observed ? 2 : 0,
          ...(method === 'POST' ? { token: issuedToken(sequence) } : {}) } })
      }
      if (method === 'GET' && ['/api/v1/cabinet/team/users', '/api/v1/cabinet/integrations'].includes(url.pathname)) return route.fulfill({ json: { data: [] } })
      if (method === 'GET' && url.pathname === '/api/v1/cabinet/audit/events') return route.fulfill({ json: { data: [], total: 0 } })
      if (method === 'GET' && url.pathname === '/api/v1/cabinet/preferences') return route.fulfill({ json: { data: { timezone: 'Europe/Moscow', notificationSettings: {}, exportSettings: {} } } })
      if (method === 'GET' && url.pathname === '/api/v1/cabinet/wb-token') return route.fulfill({ json: { data: { hasToken: false, tokenMasked: null, updatedAt: null } } })
      if (method === 'GET' && url.pathname === '/api/v1/cabinet/avito-credentials') return route.fulfill({ json: { data: { hasCredentials: false, clientIdMasked: null, clientSecretMasked: null, updatedAt: null } } })
      if (method === 'GET' && url.pathname === '/api/v1/cabinet/openai-key') return route.fulfill({ json: { data: { configured: false, storageAvailable: true, serverConfigured: false } } })
      if (method === 'GET' && url.pathname === '/api/v1/wb-repricer/quarterly-tax') return route.fulfill({ json: {
        year: Number(url.searchParams.get('year')), rates: [null, null, null, null], configured: [false, false, false, false], revision: 'a'.repeat(64), timezone: 'Europe/Moscow',
      } })
      unexpected.push(`${method} ${url.pathname}`)
      return route.abort()
    })
    await page.goto('https://satorna.test/settings/profile')
    await page.addScriptTag({ content: bundle.code })
    expect(page.url()).toBe('https://satorna.test/settings/profile')
    expect(await page.title()).toContain('Satorna')
    const card = page.getByRole('region', { name: 'Расширение WB: цены и СПП' })
    await expect.poll(() => card.count()).toBe(1)
    expect(await card.innerText()).toContain('Последние данные СПП сохраняются до получения новых наблюдений по товару.')
    expect(await card.innerText()).not.toContain('через 30 минут')
    const selector = card.getByLabel('Аккаунт WB для расширения'), key = card.getByLabel('Ключ расширения WB', { exact: true })
    await expect.poll(() => selector.locator('option').count()).toBe(3)
    await selector.selectOption('11')
    const issue = () => card.getByRole('button', { name: /^(Получить|Перевыпустить) ключ$/ })
    await expect.poll(() => issue().isEnabled()).toBe(true)
    expect(await key.count()).toBe(0)
    await issue().click()
    await expect.poll(async () => decodeKey(await key.inputValue())).toEqual({ v: 1, origin: 'https://satorna.test', token: issuedToken(1) })
    expect(await card.innerText()).toContain('Цены ещё не получены')
    await card.getByRole('button', { name: 'Копировать ключ', exact: true }).click()
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(await key.inputValue())
    await card.scrollIntoViewIfNeeded()
    await page.screenshot({ path: '/tmp/satorna-wb-browser-prices-connection-key.png' })
    observed = true
    await card.getByRole('button', { name: 'Обновить состояние', exact: true }).click()
    await expect.poll(() => key.count()).toBe(0)
    await expect.poll(() => issue().isEnabled()).toBe(true)
    expect(await card.innerText()).toContain('Цен при последней проверке: 2')

    delayed = true; gate = new Promise<void>(resolve => { release = resolve })
    await issue().click()
    await expect.poll(() => writes.length).toBe(2)
    await selector.selectOption('12')
    release(); delayed = false
    await expect.poll(() => issue().isEnabled()).toBe(true)
    expect(await key.count()).toBe(0)
    expect(await card.innerText()).not.toContain('sat_wb_synthetic_secret_2')
    await issue().click()
    await expect.poll(async () => decodeKey(await key.inputValue())).toEqual({ v: 1, origin: 'https://satorna.test', token: issuedToken(3) })
    await page.getByRole('button', { name: 'Сменить тестовую сессию', exact: true }).click()
    await expect.poll(() => key.count()).toBe(0)
    await selector.selectOption('11')
    await expect.poll(() => issue().isEnabled()).toBe(true)
    await issue().click()
    await expect.poll(async () => decodeKey(await key.inputValue())).toEqual({ v: 1, origin: 'https://satorna.test', token: issuedToken(4) })
    await page.getByRole('button', { name: 'Сменить тестовую организацию', exact: true }).click()
    await expect.poll(() => key.count()).toBe(0)
    await selector.selectOption('11')
    await expect.poll(() => issue().isEnabled()).toBe(true)

    failWrite = true
    await issue().click()
    await expect.poll(() => card.getByRole('alert').count()).toBe(1)
    expect(await issue().isDisabled()).toBe(true)
    expect(await card.innerText()).not.toContain('PRIVATE_SYNTHETIC_ERROR')
    expect(await key.count()).toBe(0)
    await card.scrollIntoViewIfNeeded()
    await page.screenshot({ path: '/tmp/satorna-wb-browser-prices-connection-error.png' })
    failWrite = false
    await card.getByRole('button', { name: 'Обновить состояние', exact: true }).click()
    await expect.poll(() => issue().isEnabled()).toBe(true)
    await card.getByRole('button', { name: 'Отключить', exact: true }).click()
    await expect.poll(() => card.getByRole('button', { name: 'Получить ключ', exact: true }).isEnabled()).toBe(true)
    expect(writes).toEqual(['POST:11', 'POST:11', 'POST:12', 'POST:11', 'POST:11', 'DELETE:11'])
    const download = await card.getByRole('link', { name: /Скачать расширение/ }).getAttribute('href')
    expect(download).toBe('/downloads/satorna-wb-prices-extension-0.1.12.zip')
    const { access } = await import('node:fs/promises')
    await access(path.join(root, 'public', download!.slice(1)))
    await page.setViewportSize({ width: 390, height: 844 })
    await card.scrollIntoViewIfNeeded()
    await page.screenshot({ path: '/tmp/satorna-wb-browser-prices-connection-390.png' })
    expect(await page.locator('vite-error-overlay').count()).toBe(0)
    expect(errors).toEqual([])
    expect(unexpected).toEqual([])
  } finally { release(); await browser.close() }
}, 60_000)
