import assert from 'node:assert/strict'
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { createServer } from 'node:net'
import { tmpdir } from 'node:os'
import { resolve, join } from 'node:path'

// Optional real Chromium smoke using the workspace's existing Playwright install.
const require = createRequire(new URL('../../frontend/package.json', import.meta.url))
const { chromium } = require('playwright')
const profile = await mkdtemp(join(tmpdir(), 'satorna-wb-extension-check-'))
const output = resolve('output/playwright')
const extensionPath = resolve(process.env.SATORNA_WB_EXTENSION_DIR || 'dist')
const apiBase = (await readFile(join(extensionPath, 'src/background.js'), 'utf8')).match(/const API = '([^']+)'/)?.[1]
assert.ok(['https://api.elfprint-system.ru/api/v1/wb/browser-prices', 'http://127.0.0.1:58017/api/v1/wb/browser-prices'].includes(apiBase))
await mkdir(output, { recursive: true })
const errors = []; const blocked = []
let context
// A local deny-only proxy is installed before Chromium starts, so even the first
// navigation of a tabs.create target cannot escape Playwright interception.
let deniedConnections = 0
const denyProxy = createServer((socket) => { deniedConnections += 1; socket.destroy() })
await new Promise((resolve) => denyProxy.listen(0, '127.0.0.1', resolve))
try {
  context = await chromium.launchPersistentContext(profile, {
    channel: 'chromium', headless: true, viewport: { width: 380, height: 600 },
    proxy: { server: `http://127.0.0.1:${denyProxy.address().port}` },
    args: [`--disable-extensions-except=${extensionPath}`, `--load-extension=${extensionPath}`],
  })
  await context.setOffline(true)
  context.on('page', (page) => { page.on('pageerror', (error) => errors.push(error.message)) })
  await context.route(/https?:\/\//, async (route) => {
    const url = new URL(route.request().url())
    if (url.origin !== 'https://www.wildberries.ru') { blocked.push(url.origin); return route.abort() }
    let payload
    if (url.pathname === '/__internal/u-card/cards/v4/detail' && url.searchParams.get('nm') === '1001') {
      payload = { products: [{ id: 1001, supplierId: 101, sizes: [{ optionId: 11, price: { product: 130800, wallet: 0 } }] }] }
    } else if (url.pathname === '/__internal/u-catalog/sellers/v4/catalog') {
      payload = url.searchParams.get('page') === '2'
        ? { products: [{ id: 4004, supplierId: 101, sizes: [{ optionId: 44, price: { product: 125000 } }] }] }
        : { products: [{ id: 2002, supplierId: 101, sizes: [{ optionId: 22, price: { product: 120000 } }] }] }
    }
    if (payload) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(payload) })
    const request = url.pathname === '/catalog/1001/detail.aspx' ? '/__internal/u-card/cards/v4/detail?curr=rub&nm=1001'
      : url.pathname === '/seller/101' ? '/__internal/u-catalog/sellers/v4/catalog?curr=rub&supplier=101&page=1' : null
    if (!request) { blocked.push(url.pathname); return route.abort() }
    return route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: `<!doctype html><html><head><meta charset="utf-8"><title>Synthetic WB fixture</title></head><body><div style="height:2000px"></div><article data-nm-id="1001">Offline fixture</article><article data-nm-id="2002"><div class="price">1 176 ₽ <del>1 700 ₽</del> с WB Кошельком</div></article><article data-nm-id="4004"><div class="price">1 225 ₽ с WB Кошельком</div></article><script>fetch(${JSON.stringify(request)}).then(r=>r.json());let paged=false;addEventListener('scroll',()=>{if(!paged&&location.pathname==='/seller/101'){paged=true;fetch('/__internal/u-catalog/sellers/v4/catalog?curr=rub&supplier=101&page=2').then(r=>r.json())}})</script></body></html>` })
  })
  const worker = context.serviceWorkers()[0] || await context.waitForEvent('serviceworker')
  const extensionId = new URL(worker.url()).host
  await worker.evaluate((prefix) => {
    self.fixtureCalls = { publicCards: 0, catalog: 0, snapshots: 0 }
    self.fixtureWallets = []
    self.fetch = async (url, options) => {
      if (url.startsWith('https://card.wb.ru/cards/v4/detail?')) {
        if (options.credentials !== 'omit' || options.redirect !== 'error') throw new Error('Public WB fixture destination rejected')
        self.fixtureCalls.publicCards += 1
        return new Response(JSON.stringify({ products: [{ id: 3003, supplierId: 303,
          sizes: [{ optionId: 33, price: { product: 110000, wallet: 0 } }] }] }),
        { status: 200, headers: { 'Content-Type': 'application/json' } })
      }
      if (!url.startsWith(prefix) || options.credentials !== 'omit' || options.redirect !== 'error') throw new Error('Fixture destination rejected')
      const json = (value) => new Response(JSON.stringify(value), { status: 200, headers: { 'Content-Type': 'application/json' } })
      if (url === `${prefix}/catalog?page=1&pageSize=100`) {
        self.fixtureCalls.catalog += 1
        return json({ marketplaceAccountId: 2, goodsRevision: 'a'.repeat(64), page: 1, pageSize: 100, total: 4, maxAgeSeconds: 1800,
          items: [[1001, 11], [2002, 22], [3003, 33], [4004, 44]].map(([nmId, sizeId]) => ({ nmId, sizeId, sellerPriceKopecks: 170000, sellerPriceObservedAt: new Date().toISOString() })) })
      }
      if (url === `${prefix}/snapshots` && options.method === 'POST') {
        const value = JSON.parse(options.body)
        for (const item of value.items) {
          const expected = item.nmId === 2002 ? 117600 : item.nmId === 4004 ? 122500 : null
          if (item.buyerPriceWithWalletKopecks !== expected) {
            self.fixtureMismatch = { nmId: item.nmId, expected, actual: item.buyerPriceWithWalletKopecks }
            throw new Error('Wallet must match displayed exact-size fixture, not raw wallet field')
          }
          if (expected) self.fixtureWallets.push({ nmId: item.nmId, sizeId: item.sizeId, price: expected })
        }
        self.fixtureCalls.snapshots += 1
        return json({ ok: true, accepted: value.items.length, ignored: 0, observedAt: new Date().toISOString() })
      }
      throw new Error('Unexpected fixture request')
    }
  }, apiBase)
  const popup = await context.newPage()
  // Chromium's action popup is not a Playwright Page in headless mode. Keep the
  // production sender guard intact; this local UI bridge calls worker commands.
  await popup.exposeFunction('fixtureCommand', (message) => worker.evaluate(async (value) => {
    await ready
    return command(value)
  }, message))
  await popup.addInitScript(() => { chrome.runtime.sendMessage = (message) => window.fixtureCommand(message) })
  popup.on('pageerror', (error) => errors.push(error.message))
  await popup.goto(`chrome-extension://${extensionId}/src/popup.html`)
  await popup.getByText('Не подключено', { exact: true }).waitFor()
  assert.equal(await popup.locator('#start').isDisabled(), true)
  await popup.locator('#token').fill(`sat_wbp_${'a'.repeat(43)}`)
  await popup.getByRole('button', { name: 'Проверить подключение', exact: true }).click()
  await popup.getByText('Каталог проверен', { exact: true }).waitFor()
  assert.equal(await popup.locator('#token').inputValue(), '')
  await popup.getByRole('button', { name: 'Начать сбор', exact: true }).click()
  await popup.waitForFunction(() => document.getElementById('coverage').textContent === '4 / 4', null, { timeout: 12000 }).catch(async (error) => {
    console.error(JSON.stringify(await worker.evaluate(() => ({ state: publicState(), calls: self.fixtureCalls, fixtureMismatch: self.fixtureMismatch }))))
    console.error(JSON.stringify({ errors, blocked }))
    for (const page of context.pages().filter((item) => item.url().startsWith('https://www.wildberries.ru/'))) {
      console.error(JSON.stringify(await page.evaluate(() => ({ fixturePath: location.pathname, contractInstalled: typeof WbPricesContract,
        fetchWrapped: fetch.toString().includes('Reflect.apply'), resources: performance.getEntriesByType('resource').map((entry) => new URL(entry.name).pathname),
        contentLength: document.body?.textContent.length,
        fixturePrices: Array.from(document.querySelectorAll('.price')).map(p => ({ text: p.innerText, rects: p.getClientRects().length,
          style: { visibility: getComputedStyle(p).visibility, opacity: getComputedStyle(p).opacity }, old: Array.from(p.querySelectorAll('del')).map(n => n.textContent) })) }))))
    }
    await popup.screenshot({ path: join(output, 'popup-failed.png'), fullPage: true })
    throw error
  })
  assert.equal(await popup.locator('#accepted').textContent(), '4')
  await popup.screenshot({ path: join(output, 'popup-running.png'), fullPage: true })
  await popup.getByText('Проход завершён', { exact: true }).waitFor()
  assert.equal(await popup.locator('#stop').isDisabled(), true)
  assert.equal(await popup.locator('#resume').isDisabled(), true)
  assert.match(await popup.locator('#next-run').textContent(), /второго круга нет/)
  await popup.screenshot({ path: join(output, 'popup-completed.png'), fullPage: true })
  await popup.getByRole('button', { name: 'Забыть ключ', exact: true }).click()
  await popup.getByText('Не подключено', { exact: true }).waitFor()
  const local = await worker.evaluate(() => chrome.storage.local.get('token'))
  assert.equal(local.token, undefined)
  assert.deepEqual(await worker.evaluate(() => self.fixtureCalls), { publicCards: 1, catalog: 1, snapshots: 4 })
  assert.deepEqual(await worker.evaluate(() => self.fixtureWallets), [{ nmId: 2002, sizeId: 22, price: 117600 }, { nmId: 4004, sizeId: 44, price: 122500 }])
  assert.deepEqual(errors, [])
  assert.deepEqual(blocked, [])
  const manifest = JSON.parse(await readFile(join(extensionPath, 'manifest.json'), 'utf8'))
  const result = { ok: true, browser: await context.browser().version(), permissions: manifest.permissions,
    externalNetwork: 'offline; all HTTP page requests fulfilled by exact fixtures or aborted',
    checks: ['MV3 loaded', 'public batch plus MAIN observer fallback', 'automatic seller page 2', 'exact offers 4/4', 'server acknowledgements 4', 'completed single pass without automatic repeat', 'local token deletion', 'displayed wallet exact size; absent wallet null'],
    popupTransport: 'local fixture bridge to worker command; native popup sender checked separately by VM tests',
    transportBoundary: 'deny-only loopback proxy; no upstream forwarding', deniedConnections,
    screenshots: ['popup-running.png', 'popup-completed.png'] }
  await writeFile(join(output, 'browser-check.json'), `${JSON.stringify(result, null, 2)}\n`)
  console.log(JSON.stringify(result))
} finally {
  await context?.close()
  await new Promise((resolve) => denyProxy.close(resolve))
  await rm(profile, { recursive: true, force: true })
}
