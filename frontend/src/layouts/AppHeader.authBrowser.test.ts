import { fileURLToPath } from 'node:url'
import react from '@vitejs/plugin-react'
import { chromium, type Route } from 'playwright'
import { createServer } from 'vite'
import { expect, it } from 'vitest'
import { withSyntheticVite } from '@/test-support/syntheticVite'

const root = fileURLToPath(new URL('../../', import.meta.url))
const accessToken = 'real-account-token'
const storageKey = 'ogni.auth.access-token'
const envelope = (data: unknown) => ({ data, timestamp: '2026-09-13T12:00:00Z' })

const cabinetMe = {
  organization: {
    organizationId: 7,
    slug: 'real-company',
    name: 'Реальная компания',
    createdAt: '2026-01-01T00:00:00Z',
  },
  user: {
    userId: 'real-user',
    organizationId: 7,
    email: 'ivan@real.example',
    fullName: 'Иван Петров',
    permissionProfile: 'custom',
    permissions: ['finance:read', 'reviews:read', 'integrations:read'],
    isActive: true,
    createdAt: '2026-01-01T00:00:00Z',
  },
  activeSession: null,
  preferences: {
    userId: 'real-user',
    notificationSettings: {},
    exportSettings: {},
    timezone: 'Europe/Moscow',
    updatedAt: '2026-09-13T12:00:00Z',
  },
}

const browserEntry = `
import React from 'react';
import {createRoot} from 'react-dom/client';
import App from '/src/App.tsx';
import '/src/index.css';
createRoot(document.getElementById('root')).render(React.createElement(App));
`

it('shows the authenticated profile and keeps the session when logout must be retried', async () => {
  const server = await createServer({
    root,
    configFile: false,
    envFile: false,
    cacheDir: '/tmp/satorna-header-auth-proof/vite-cache',
    resolve: { alias: { '@': `${root}src` } },
    define: {
      'import.meta.env.VITE_API_BASE_URL': JSON.stringify(''),
      'import.meta.env.VITE_AUTH_BYPASS': JSON.stringify('false'),
      'import.meta.env.VITE_CANONICAL_NOTIFICATIONS_ENABLED': JSON.stringify('true'),
      'import.meta.env.VITE_WB_LIVE_ENABLED': JSON.stringify('false'),
    },
    plugins: [
      react(),
      {
        name: 'header-auth-browser-entry',
        transformIndexHtml(html) {
          return html.replace('/src/main.tsx', '/__header-auth-entry.tsx')
        },
        resolveId(id) {
          if (id === '/__header-auth-entry.tsx') return '\0header-auth-entry.tsx'
        },
        load(id) {
          if (id === '\0header-auth-entry.tsx') return browserEntry
        },
      },
    ],
    server: { host: '127.0.0.1', port: 0, strictPort: true },
  })

  const browser = await chromium.launch({ headless: true })
  try {
    await withSyntheticVite(server, 'header-auth', async origin => {
      const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1440, height: 900 } })
      page.setDefaultTimeout(15_000)
      const errors: string[] = []
      let logoutAttempts = 0
      let releaseProfile: (() => Promise<void>) | undefined
      page.on('pageerror', error => errors.push(error.message))
      page.on('console', message => {
        if (message.type() !== 'error') return
        if (message.text().includes('status of 503')) return
        errors.push(message.text())
      })

      await page.addInitScript(({ key, token }) => {
        if (sessionStorage.getItem('header-auth-seeded')) return
        localStorage.setItem(key, token)
        sessionStorage.setItem('header-auth-seeded', '1')
      }, { key: storageKey, token: accessToken })

      await page.route('**/*', async (route: Route) => {
        const request = route.request()
        const url = new URL(request.url())
        if (url.origin === 'https://fonts.googleapis.com') return route.fulfill({ contentType: 'text/css', body: '' })
        if (url.origin !== origin) return route.abort()
        if (url.pathname === '/api/v1/cabinet/me') {
          expect(request.headers().authorization).toBe(`Bearer ${accessToken}`)
          releaseProfile = () => route.fulfill({ json: envelope(cabinetMe) })
          return
        }
        if (url.pathname === '/api/v1/cabinet/sessions') return route.fulfill({ json: envelope([]) })
        if (url.pathname === '/api/v1/auth/logout') {
          logoutAttempts += 1
          expect(request.headers().authorization).toBe(`Bearer ${accessToken}`)
          if (logoutAttempts === 1) {
            return route.fulfill({
              status: 503,
              json: { error: { code: 'LOGOUT_UNAVAILABLE', message: 'Logout is temporarily unavailable' } },
            })
          }
          return route.fulfill({ json: envelope({ status: 'ok' }) })
        }
        if (url.pathname.startsWith('/api/')) return route.fulfill({ json: envelope([]) })
        return route.continue()
      })

      try {
        await page.goto(`${origin}/wiki/algorithm`)
        await page.getByText('Проверяем сессию...', { exact: true }).waitFor()
        expect(await page.getByText(/Мария|maria@ogni\.example/i).count()).toBe(0)

        await expect.poll(() => Boolean(releaseProfile)).toBe(true)
        await releaseProfile!()
        await page.getByRole('heading', { name: 'Настройки алгоритма', exact: true }).waitFor()

        const profileTrigger = page.getByRole('button', { name: 'Профиль Иван Петров', exact: true })
        await profileTrigger.click()
        await page.getByText('Индивидуальный профиль · ivan@real.example', { exact: true }).waitFor()
        await page.getByText('WB: ограниченный доступ', { exact: true }).waitFor()
        await page.getByText('Репрайсер, Отчеты, P&L, Отзывы', { exact: true }).waitFor()
        await page.getByText('Авито: просмотр', { exact: true }).waitFor()
        expect(await page.getByText('Авито: 0 аккаунта · просмотр', { exact: true }).count()).toBe(0)
        expect(await page.getByText('Approval items', { exact: true }).count()).toBe(0)
        expect(await page.getByText(/Мария|maria@ogni\.example/i).count()).toBe(0)

        const artifactDir = process.env.HEADER_AUTH_ARTIFACT_DIR
        if (artifactDir) {
          const profileMenu = page.getByRole('menu')
          await expect.poll(() => profileMenu.evaluate(element => (
            element.getAnimations({ subtree: true }).every(animation => animation.playState === 'finished')
          ))).toBe(true)
          await page.screenshot({ path: `${artifactDir}/real-profile.png`, fullPage: true })
          await page.setViewportSize({ width: 390, height: 844 })
          expect(await profileMenu.evaluate(element => {
            const bounds = element.getBoundingClientRect()
            return bounds.left >= 0 && bounds.right <= innerWidth
          })).toBe(true)
          await page.screenshot({ path: `${artifactDir}/real-profile-mobile-390x844.png`, fullPage: true })
        }

        await page.getByRole('menuitem', { name: /Выйти/ }).click()
        await expect.poll(() => logoutAttempts).toBe(1)
        const logoutAlert = page.getByRole('alert').filter({ hasText: 'Не удалось выйти. Повторите попытку.' })
        await logoutAlert.waitFor()
        expect(await logoutAlert.getAttribute('aria-live')).toBe('assertive')
        expect(new URL(page.url()).pathname).toBe('/wiki/algorithm')
        expect(await page.evaluate(key => localStorage.getItem(key), storageKey)).toBe(accessToken)

        if (artifactDir) {
          await page.screenshot({ path: `${artifactDir}/logout-rejected-mobile-390x844.png`, fullPage: true })
        }

        await page.getByRole('menuitem', { name: /Выйти/ }).click()
        await expect.poll(() => logoutAttempts).toBe(2)
        await page.waitForURL('**/auth/login')
        expect(await page.evaluate(key => localStorage.getItem(key), storageKey)).toBeNull()
        expect(errors).toEqual([])
      } finally {
        await page.close()
      }
    })
  } finally {
    await browser.close()
  }
}, 30_000)
