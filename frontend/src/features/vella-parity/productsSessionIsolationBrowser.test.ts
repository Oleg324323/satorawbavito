import path from 'node:path'
import { fileURLToPath } from 'node:url'
import react from '@vitejs/plugin-react'
import { chromium, type Route } from 'playwright'
import { build } from 'vite'
import { expect, it } from 'vitest'

type Account = 'A' | 'B'

const envelope = (data: unknown) => ({ data, timestamp: '2026-09-12T12:00:00Z' })

function cabinetMe(account: Account) {
  const userId = account === 'A' ? '1' : '2'
  const organizationId = account === 'A' ? 7 : 8
  return {
    organization: { organizationId, slug: `org-${account.toLowerCase()}`, name: `Org ${account}`, createdAt: '2026-01-01T00:00:00Z' },
    user: { userId, organizationId, email: `${account.toLowerCase()}@test.local`, fullName: `User ${account}`, permissionProfile: 'admin', permissions: [], isActive: true, createdAt: '2026-01-01T00:00:00Z' },
    activeSession: null,
    preferences: { userId, notificationSettings: {}, exportSettings: {}, timezone: 'Europe/Moscow', updatedAt: '2026-09-12T00:00:00Z' },
  }
}

function product(account: Account, index: number) {
  return {
    meta: {
      articleId: `SKU-${account}-${index}`,
      nmId: (account === 'A' ? 100_000 : 200_000) + index,
      name: `Товар ${account}-${index}`,
      status: 'auto',
      currentPriceKopecks: 200_000,
      basketsLast7d: 1,
      basketNorm: 10,
    },
    settings: { wbCommissionPct: 15, minMarginPct: 20, cogsKopecks: 80_000, logisticsKopecks: 10_000 },
    analytics: { baskets: 1, financeState: 'ok' },
  }
}

function skuPayload(account: Account) {
  const total = account === 'A' ? 3410 : 7
  const items = Array.from({ length: account === 'A' ? 10 : 7 }, (_, index) => product(account, index + 1))
  return {
    items,
    total,
    totalCached: total,
    itemsReturned: items.length,
    page: 1,
    pageSize: 150,
    summary: { skuCount: total, totalBaskets: items.length },
    cache: { pagesCached: 1, totalCached: total, nextOffset: total, pageLimit: 1000, basketsMatchedNmIds: total },
  }
}

function workerStatus(withApproval = false) {
  return {
    organizationId: 7,
    mode: {
      wbApiMode: 'fake',
      realPriceApplyEnabled: false,
      schedulerEnabled: true,
      schedulerPollIntervalMinutes: 5,
      executeIntervalMinutes: 60,
      fullSyncEnabled: true,
      fullSyncIntervalMinutes: 60,
      fullSyncPromotionsEnabled: true,
      workerAutoApplyPricesEnabled: false,
    },
    timing: {
      serverNow: '2026-09-12T12:00:00Z',
      lastSchedulerRunAt: '2026-09-12T11:00:00Z',
      nextSchedulerPollAt: '2026-09-12T12:05:00Z',
      secondsUntilNextSchedulerPoll: 300,
      nextRunAt: '2026-09-12T13:00:00Z',
      secondsUntilNextRun: 3600,
      lastFullSyncAt: '2026-09-12T11:00:00Z',
      nextFullSyncAt: '2026-09-12T13:00:00Z',
      secondsUntilNextFullSync: 3600,
    },
    runs: [],
    pendingApprovals: withApproval ? [{
      approvalId: 'approval-1',
      draftId: 'draft-1',
      jobId: null,
      runId: 'run-2026-09-12',
      trigger: 'scheduler',
      articleId: 'SKU-A-1',
      nmId: 100_001,
      name: 'Товар A-1',
      status: 'pending',
      applyState: 'pending',
      sourceStatus: 'ready',
      wbMutationSent: false,
      wbUploadId: null,
      wbStatus: null,
      statusLabel: 'Ожидает подтверждения',
      blockedReasons: [],
      notes: [],
      rowErrors: [],
      error: null,
      source: 'worker',
      scenario: 'complete',
      frontendStrategyId: 'turnover_control',
      strategyId: 'turnover_control',
      strategyName: 'Контроль оборачиваемости',
      oldPriceKopecks: 200_000,
      recommendedPriceKopecks: 195_000,
      deltaKopecks: -5_000,
      explanation: 'Synthetic approval boundary',
      createdAt: '2026-09-12T11:00:00Z',
      updatedAt: '2026-09-12T11:00:00Z',
    }] : [],
    sync: { state: 'completed', running: false, steps: [] },
    syncHistory: [],
    nightMedian: { enabled: false, eligibleItems: [], items: [] },
  }
}

it.each([
  { label: 'cached products', holdAccountARefresh: false, expireLogout: false, rejectLogout: false },
  { label: 'late account A refresh', holdAccountARefresh: true, expireLogout: false, rejectLogout: false },
  { label: 'expired logout', holdAccountARefresh: false, expireLogout: true, rejectLogout: false },
  { label: 'rejected logout retry', holdAccountARefresh: false, expireLogout: false, rejectLogout: true },
])('isolates account B products from $label after logout', async ({ label, holdAccountARefresh, expireLogout, rejectLogout }) => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({
    configFile: false,
    envFile: false,
    root,
    logLevel: 'silent',
    plugins: [react()],
    define: {
      'process.env.NODE_ENV': JSON.stringify('test'),
      'import.meta.env.VITE_API_BASE_URL': JSON.stringify(''),
      'import.meta.env.VITE_WB_LIVE_ENABLED': JSON.stringify('false'),
      'import.meta.env.VITE_AUTH_BYPASS': JSON.stringify('false'),
    },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: {
      write: false,
      minify: false,
      lib: {
        entry: fileURLToPath(new URL('./__fixtures__/productsSessionIsolationBrowser.tsx', import.meta.url)),
        formats: ['iife'],
        name: 'ProductsSessionIsolationTest',
      },
    },
  })
  const bundle = (Array.isArray(result) ? result : [result])
    .flatMap(output => 'output' in output ? output.output : [])
    .find(output => output.type === 'chunk' && output.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing products session isolation bundle')
  const styles = (Array.isArray(result) ? result : [result])
    .flatMap(output => 'output' in output ? output.output : [])
    .flatMap(output => output.type === 'asset' && output.fileName.endsWith('.css')
      ? [typeof output.source === 'string' ? output.source : new TextDecoder().decode(output.source)]
      : [])
    .join('\n')
  if (!styles) throw new Error('Missing products session isolation styles')

  const browser = await chromium.launch({ headless: true })
  let releaseAccountB: () => void = () => undefined
  let releaseAccountARefresh: () => void = () => undefined
  try {
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1440, height: 1000 } })
    const errors: string[] = []
    const expectedUnauthorizedErrors: string[] = []
    const expectedUnavailableErrors: string[] = []
    const skuRequests: Account[] = []
    const refreshRequests: Account[] = []
    const logoutEvents: string[] = []
    const approvalDecisions: string[] = []
    let accountASessionActive = true
    let approvalPending = false
    const accountBResponse = new Promise<void>((resolve) => { releaseAccountB = resolve })
    const accountARefreshResponse = new Promise<void>((resolve) => { releaseAccountARefresh = resolve })
    page.on('pageerror', error => errors.push(error.message))
    page.on('console', message => {
      if (message.type() !== 'error' || message.text().startsWith('An empty string')) return
      if (expireLogout && message.text() === 'Failed to load resource: the server responded with a status of 401 (Unauthorized)') {
        expectedUnauthorizedErrors.push(message.text())
        return
      }
      if (rejectLogout && message.text() === 'Failed to load resource: the server responded with a status of 503 (Service Unavailable)') {
        expectedUnavailableErrors.push(message.text())
        return
      }
      errors.push(message.text())
    })
    await page.route('**/*', async (route: Route) => {
      const request = route.request()
      const url = new URL(request.url())
      if (request.method() === 'GET' && url.origin === 'http://satorna.test' && !url.pathname.startsWith('/api/')) {
        return route.fulfill({ contentType: 'text/html', body: '<title>Products session isolation</title><div id="root"></div>' })
      }
      if (request.resourceType() === 'image') {
        return route.fulfill({ contentType: 'image/gif', body: Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64') })
      }
      if (url.origin === 'https://fonts.googleapis.com') return route.fulfill({ contentType: 'text/css', body: '' })
      if (!url.pathname.startsWith('/api/')) return route.abort()

      const account: Account = request.headers().authorization === 'Bearer account-b-token' ? 'B' : 'A'
      if (request.method() === 'POST' && url.pathname.startsWith('/api/v1/wb-repricer/price-approvals/')) {
        approvalDecisions.push(url.pathname)
      }
      if (url.pathname === '/api/v1/auth/logout') {
        if (rejectLogout && logoutEvents.length === 0) {
          expect(request.headers().authorization).toBe('Bearer account-a-token')
          expect(accountASessionActive).toBe(true)
          logoutEvents.push('rejected')
          return route.fulfill({
            status: 503,
            json: { error: { code: 'LOGOUT_UNAVAILABLE', message: 'Synthetic upstream detail' } },
          })
        }
        if (expireLogout && request.headers().authorization === 'Bearer account-a-token') {
          expect(accountASessionActive).toBe(true)
          logoutEvents.push('expired')
          return route.fulfill({ status: 401, json: { detail: 'INVALID_AUTH_TOKEN:TOKEN_EXPIRED' } })
        }
        if (expireLogout) {
          expect(request.headers().authorization).toBe('Bearer account-a-refreshed-token')
          expect(account).toBe('A')
          expect(accountASessionActive).toBe(true)
          logoutEvents.push('revoked')
          accountASessionActive = false
        }
        if (rejectLogout) {
          expect(request.headers().authorization).toBe('Bearer account-a-token')
          expect(account).toBe('A')
          expect(accountASessionActive).toBe(true)
          logoutEvents.push('revoked')
          accountASessionActive = false
        }
        return route.fulfill({ json: envelope({ status: 'ok' }) })
      }
      if (url.pathname === '/api/v1/auth/login') return route.fulfill({ json: envelope({ accessToken: 'account-b-token', expiresIn: 3600, tokenType: 'bearer' }) })
      if (url.pathname === '/api/v1/auth/refresh') {
        if (expireLogout && logoutEvents.at(-1) === 'expired') {
          expect(accountASessionActive).toBe(true)
          logoutEvents.push('refresh')
          return route.fulfill({ json: envelope({ accessToken: 'account-a-refreshed-token', expiresIn: 3600, tokenType: 'bearer' }) })
        }
        return route.fulfill({ status: 401, json: { error: { code: 'AUTH_REQUIRED', message: 'No synthetic refresh session' } } })
      }
      if (url.pathname === '/api/v1/cabinet/me') return route.fulfill({ json: envelope(cabinetMe(account)) })
      if (url.pathname === '/api/v1/cabinet/sessions') return route.fulfill({ json: envelope([]) })
      if (request.method() === 'POST' && url.pathname === '/api/v1/wb-repricer/sku/refresh') {
        refreshRequests.push(account)
        if (account === 'A') await accountARefreshResponse
        return route.fulfill({ json: skuPayload(account) })
      }
      if (url.pathname === '/api/v1/wb-repricer/sku') {
        skuRequests.push(account)
        if (account === 'B') await accountBResponse
        return route.fulfill({ json: skuPayload(account) })
      }
      if (url.pathname === '/api/v1/wb-repricer/strategies/catalog') return route.fulfill({ json: { items: [], total: 0 } })
      if (url.pathname === '/api/v1/wb-repricer/sku-groups') return route.fulfill({ json: { items: [], total: 0 } })
      if (url.pathname === '/api/v1/wb-repricer/sync/status') return route.fulfill({ json: { state: 'completed', running: false, steps: [] } })
      if (url.pathname === '/api/v1/wb-repricer/worker/status') return route.fulfill({ json: workerStatus(approvalPending) })
      if (request.method() === 'POST' && url.pathname === '/api/v1/wb-repricer/price-approvals/approval-1/reject') {
        approvalPending = false
        return route.fulfill({ json: { approval: { ...workerStatus(true).pendingApprovals[0], status: 'rejected' }, job: null } })
      }
      if (url.pathname === '/api/v1/wb-repricer/simulator') {
        return route.fulfill({ json: {
          mode: {
            wbApiMode: 'fake',
            realPriceApplyEnabled: false,
            localPriceApplyEnabled: false,
            schedulerEnabled: true,
            executeIntervalMinutes: 60,
            simulationAllowed: true,
            inputOverridesAllowed: true,
            simulatorRunApplyAllowed: false,
            simulatorRunMode: 'preview_only',
          },
          summary: { activeTotal: 0, strategyCount: 0, liquidationCount: 0 },
          worker: { beatTask: 'beat', orgTask: 'org', intervalMinutes: 60, queue: 'repricer', selectionRule: 'active', priceApplyRule: 'approval' },
          items: [],
        } })
      }
      if (url.pathname === '/api/v1/wb-repricer/changelog') return route.fulfill({ json: { items: [], total: 0 } })
      if (url.pathname === '/api/v1/cabinet/team/users') return route.fulfill({ json: envelope([]) })
      if (url.pathname === '/api/v1/cabinet/wb-token') return route.fulfill({ json: envelope({ userId: account === 'A' ? '1' : '2', hasToken: false, tokenMasked: null, updatedAt: null }) })
      if (url.pathname === '/api/v1/cabinet/avito-credentials') return route.fulfill({ json: envelope({ userId: account === 'A' ? '1' : '2', hasCredentials: false, clientIdMasked: null, clientSecretMasked: null, accessTokenExpiresAt: null, updatedAt: null }) })
      return route.fulfill({ json: envelope([]) })
    })

    await page.goto('http://satorna.test/wb/repricer')
    if (label === 'cached products') await page.clock.install()
    await page.evaluate(() => localStorage.setItem('ogni.auth.access-token', 'account-a-token'))
    await page.addStyleTag({ content: styles })
    await page.addScriptTag({ content: bundle.code })
    const products = page.locator('#tab-products')
    await expect.poll(() => products.locator('#totalCount').innerText(), { timeout: 15_000 }).toBe('3410')
    await expect.poll(() => products.locator('[data-sku]:visible').allInnerTexts()).toContainEqual(expect.stringContaining('SKU-A-1'))
    const profile = page.locator('.user-chip')
    expect(await page.locator('.worker-layout, .worker-overlay').count()).toBe(0)
    expect(await page.locator('.vella-html-parity-root').evaluate(element => element.getBoundingClientRect().top)).toBe(0)
    if (label === 'cached products') {
      approvalPending = true
      await page.clock.fastForward(15_000)
      await expect.poll(() => page.getByRole('dialog', { name: 'Нужно подтвердить изменение цены' }).isVisible()).toBe(true)
      expect(approvalDecisions).toEqual([])
      await page.getByRole('button', { name: 'Не менять цену' }).click()
      await expect.poll(() => page.getByRole('dialog', { name: 'Нужно подтвердить изменение цены' }).count()).toBe(0)
      expect(approvalDecisions).toEqual(['/api/v1/wb-repricer/price-approvals/approval-1/reject'])
    }
    if (label === 'cached products') {
      const screenshotDir = process.env.WORKER_LAYOUT_SCREENSHOT_DIR
      await page.setViewportSize({ width: 390, height: 844 })
      expect(await page.locator('.worker-layout, .worker-overlay').count()).toBe(0)
      expect(await page.locator('.vella-html-parity-root').evaluate(element => element.getBoundingClientRect().top)).toBe(0)
      if (screenshotDir) await page.screenshot({ path: path.join(screenshotDir, 'products-mobile-390x844.png') })
      for (const viewport of [{ width: 1440, height: 1000 }, { width: 1366, height: 768 }]) {
        await page.setViewportSize(viewport)
        await profile.click()
        await expect.poll(() => page.locator('#ddUser').evaluate(element => element.classList.contains('open'))).toBe(true)
        const profilePanel = page.locator('#profilePanel')
        expect(await profilePanel.evaluate((element) => {
          const bounds = element.getBoundingClientRect()
          return bounds.top >= 0 && bounds.bottom <= innerHeight + 1
        })).toBe(true)
        const logout = page.getByRole('button', { name: /Выйти/ })
        await logout.scrollIntoViewIfNeeded()
        const logoutReceivesPointer = await logout.evaluate((element) => {
          const bounds = element.getBoundingClientRect()
          return bounds.top >= 0
            && bounds.bottom <= innerHeight
            && document.elementFromPoint(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2)?.closest('button') === element
        })
        expect(logoutReceivesPointer).toBe(true)
        await logout.click({ trial: true })
        expect(await page.locator('.vella-html-parity-root').evaluate((element) => element.clientHeight === innerHeight)).toBe(true)
        if (screenshotDir) await page.screenshot({ path: path.join(screenshotDir, `products-profile-${viewport.width}x${viewport.height}.png`) })
        await profile.click()
        const notification = page.locator('.topbar-actions button[aria-label="Уведомления"]')
        expect(await notification.evaluate((element) => {
          const bounds = element.getBoundingClientRect()
          return document.elementFromPoint(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2)?.closest('button') === element
        })).toBe(true)
        await notification.click()
        await expect.poll(() => page.locator('#ddNotif').evaluate(element => element.classList.contains('open'))).toBe(true)
        expect(await page.locator('#notifPanel').evaluate((element) => element.getBoundingClientRect().bottom <= innerHeight + 1)).toBe(true)
        await notification.click()
        if (viewport.width === 1366) {
          await profile.click()
          await expect.poll(() => page.locator('#ddUser').evaluate(element => element.classList.contains('open'))).toBe(true)
          await page.getByRole('button', { name: /Выйти/ }).scrollIntoViewIfNeeded()
        }
      }
    }
    if (holdAccountARefresh) {
      await page.evaluate(() => {
        const probe = window as typeof window & { __sessionIsolationRefreshPromise?: Promise<unknown> }
        probe.__sessionIsolationRefreshPromise = Promise.resolve(window.__vellaRefreshLiveRepricerProducts?.(0)).catch(error => error)
      })
      await expect.poll(() => refreshRequests.includes('A')).toBe(true)
    }

    if (label !== 'cached products') await profile.click()
    await page.getByRole('button', { name: /Выйти/ }).click()
    if (rejectLogout) {
      await expect.poll(() => logoutEvents).toEqual(['rejected'])
      expect(new URL(page.url()).pathname).toBe('/wb/repricer')
      expect(accountASessionActive).toBe(true)
      expect(await page.evaluate(() => localStorage.getItem('ogni.auth.access-token'))).toBe('account-a-token')
      await page.getByRole('alert').filter({ hasText: 'Не удалось выйти. Повторите попытку.' }).waitFor({ timeout: 3000 })
      expect(errors).toEqual([])
      expect(await page.locator('#ddUser').evaluate(element => element.classList.contains('open'))).toBe(true)
      await page.getByRole('button', { name: /Выйти/ }).click()
    }
    await page.waitForURL('**/auth/login')
    if (expireLogout) {
      expect(logoutEvents).toEqual(['expired', 'refresh', 'revoked'])
      expect(await page.evaluate(() => localStorage.getItem('ogni.auth.access-token'))).toBeNull()
      expect(await page.evaluate(async () => (await fetch('/api/v1/auth/refresh', { method: 'POST', credentials: 'include' })).status)).toBe(401)
      expect(expectedUnauthorizedErrors).toHaveLength(2)
    }
    if (rejectLogout) {
      expect(logoutEvents).toEqual(['rejected', 'revoked'])
      expect(expectedUnavailableErrors).toHaveLength(1)
    }
    await page.getByLabel('Email').fill('b@test.local')
    await page.getByLabel('Пароль').fill('password')
    await page.getByRole('button', { name: 'Войти' }).click()
    await page.waitForURL('**/wb/repricer')
    const accountAGetCountAfterLogin = skuRequests.filter(account => account === 'A').length

    await expect.poll(() => skuRequests.includes('B'), { timeout: 3000 }).toBe(true)
    expect(await products.locator('[data-sku]:visible').allInnerTexts()).not.toContainEqual(expect.stringContaining('SKU-A-1'))
    releaseAccountB()
    await expect.poll(() => products.locator('#totalCount').innerText()).toBe('7')
    await expect.poll(() => products.locator('[data-sku]:visible').allInnerTexts()).toContainEqual(expect.stringContaining('SKU-B-1'))
    expect(await products.locator('[data-sku]:visible').allInnerTexts()).not.toContainEqual(expect.stringContaining('SKU-A-1'))
    if (holdAccountARefresh) {
      releaseAccountARefresh()
      await page.evaluate(async () => {
        const probe = window as typeof window & { __sessionIsolationRefreshPromise?: Promise<unknown> }
        await probe.__sessionIsolationRefreshPromise
      })
      expect(skuRequests.filter(account => account === 'A')).toHaveLength(accountAGetCountAfterLogin)
      expect(await products.locator('#totalCount').innerText()).toBe('7')
      expect(await products.locator('[data-sku]:visible').allInnerTexts()).toContainEqual(expect.stringContaining('SKU-B-1'))
      expect(await products.locator('[data-sku]:visible').allInnerTexts()).not.toContainEqual(expect.stringContaining('SKU-A-1'))
    }
    const accountBGetCountBeforeRefresh = skuRequests.filter(account => account === 'B').length
    const accountBRefreshCountBefore = refreshRequests.filter(account => account === 'B').length
    await page.evaluate(() => window.__vellaRefreshLiveRepricerProducts?.(0))
    expect(refreshRequests.filter(account => account === 'B')).toHaveLength(accountBRefreshCountBefore + 1)
    expect(skuRequests.filter(account => account === 'B')).toHaveLength(accountBGetCountBeforeRefresh + 1)
    expect(await products.locator('#totalCount').innerText()).toBe('7')
    await expect.poll(() => products.locator('[data-sku]:visible').allInnerTexts()).toContainEqual(expect.stringContaining('SKU-B-1'))
    expect(await products.locator('[data-sku]:visible').allInnerTexts()).not.toContainEqual(expect.stringContaining('SKU-A-1'))
    if (label === 'cached products') {
      await page.evaluate(() => {
        history.pushState({}, '', '/wb/repricer/simulator')
        window.dispatchEvent(new PopStateEvent('popstate'))
      })
      await expect.poll(() => page.getByRole('heading', { name: 'Симулятор WB-товаров' }).isVisible()).toBe(true)
      expect(await page.locator('.repricer-sim-page').evaluate(element => element.getBoundingClientRect().bottom <= innerHeight + 1)).toBe(true)
      const screenshotDir = process.env.WORKER_LAYOUT_SCREENSHOT_DIR
      if (screenshotDir) await page.screenshot({ path: path.join(screenshotDir, 'desktop-simulator.png') })
      expect(await page.locator('.worker-layout, .worker-overlay').count()).toBe(0)
      expect(await page.locator('.repricer-sim-page').evaluate(element => element.getBoundingClientRect().top)).toBe(0)
      await page.reload()
      await page.addStyleTag({ content: styles })
      await page.addScriptTag({ content: bundle.code })
      await expect.poll(() => page.getByRole('heading', { name: 'Симулятор WB-товаров' }).isVisible()).toBe(true)
      expect(await page.locator('.worker-layout, .worker-overlay').count()).toBe(0)
      expect(await page.locator('.repricer-sim-page').evaluate(element => element.getBoundingClientRect().top)).toBe(0)
      await page.evaluate(() => {
        history.pushState({}, '', '/wb/repricer')
        window.dispatchEvent(new PopStateEvent('popstate'))
      })
      await expect.poll(() => products.locator('#totalCount').innerText()).toBe('7')
      expect(await page.locator('.worker-layout, .worker-overlay').count()).toBe(0)
      expect(await page.locator('.vella-html-parity-root').evaluate(element => element.getBoundingClientRect().top)).toBe(0)
    }
    expect(approvalDecisions).toEqual(label === 'cached products' ? ['/api/v1/wb-repricer/price-approvals/approval-1/reject'] : [])
    expect(errors).toEqual([])
  } finally {
    releaseAccountB()
    releaseAccountARefresh()
    await browser.close()
  }
}, 60_000)
