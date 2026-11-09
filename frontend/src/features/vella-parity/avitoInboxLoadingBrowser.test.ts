import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { expect, it } from 'vitest'

it('shows a chat API failure instead of a false empty inbox and recovers on refresh', async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({
    configFile: false, envFile: false, root, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': JSON.stringify('test'), 'import.meta.env.VITE_API_BASE_URL': JSON.stringify('') },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: {
      entry: fileURLToPath(new URL('./__fixtures__/reportLoadingBrowser.tsx', import.meta.url)),
      formats: ['iife'], name: 'AvitoInboxLoadingTest',
    } },
  })
  const outputs = Array.isArray(result) ? result : [result]
  const bundle = outputs.flatMap(output => 'output' in output ? output.output : [])
    .find(output => output.type === 'chunk' && output.isEntry)
  if (!bundle || bundle.type !== 'chunk') throw new Error('Missing inbox bundle')
  const browser = await chromium.launch({ headless: true })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block' })
    const errors: string[] = [], unexpected: string[] = []
    let requests = 0
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url())
      if (request.method() === 'GET' && url.origin === 'http://satorna.test' && url.pathname === '/avito/inbox') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      if (request.method() === 'GET' && request.resourceType() === 'image') return route.fulfill({ contentType: 'image/gif', body: Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64') })
      if (request.method() === 'GET' && url.origin === 'https://fonts.googleapis.com') return route.fulfill({ contentType: 'text/css', body: '' })
      if (request.method() !== 'GET' || url.origin !== 'http://satorna.test' || url.pathname !== '/api/v1/avito/chats') {
        unexpected.push(`${request.method()} ${url.pathname}`); return route.abort()
      }
      requests++
      const success = requests > 1
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({
        status: success ? 'synced' : 'blocked',
        summary: { total: success ? 1 : 0, unread: 0, withItems: 0, messages: success ? 1 : 0 },
        account: { accountId: '123', accountName: 'Test account' },
        chats: success ? [{ chatId: 'u2i~test-chat', accountId: '123', accountName: 'Test account', buyerName: 'Test buyer', preview: 'Hello', unread: false }] : [],
        messages: success ? { 'u2i~test-chat': [{ messageId: 'message-1', chatId: 'u2i~test-chat', direction: 'in', type: 'text', text: 'Loaded conversation text', isRead: true }] } : {},
        source: { diagnostics: {}, error: success ? null : { code: 'transport_error', message: 'Avito transport error' } },
      }) })
    })
    await page.goto('http://satorna.test/avito/inbox')
    await page.addScriptTag({ content: bundle.code })
    const stream = page.locator('#avitoMessageStream')
    await stream.getByText('Сообщения временно недоступны', { exact: true }).waitFor({ state: 'visible', timeout: 15_000 })
    expect(await stream.getByText('Сообщений нет', { exact: true }).count()).toBe(0)
    await page.getByRole('button', { name: 'Обновить', exact: true }).click()
    await stream.getByText('Loaded conversation text', { exact: true }).waitFor({ state: 'visible' })
    expect(await stream.getByText('Сообщения временно недоступны', { exact: true }).count()).toBe(0)
    expect(requests).toBe(2)
    expect(unexpected).toEqual([])
    expect(errors).toEqual([])
  } finally { await browser.close() }
}, 60_000)
