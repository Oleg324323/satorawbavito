import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const page = 'https://www.wildberries.ru/catalog/1025784485/detail.aspx'
const detail = 'https://www.wildberries.ru/__internal/u-card/cards/v4/detail?curr=rub&nm=1025784485'
const flush = () => new Promise((resolve) => setImmediate(resolve))

function load(status = 200, pageUrl = page, requestUrl = detail) {
  const sent = []; const requests = []
  const response = { status, url: requestUrl, clone: () => ({ json: async () => ({ products: [{ id: 1025784485, sizes: [{ optionId: 123, price: { product: 130800, wallet: 0 }, secret: 'hidden' }] }], credentials: 'hidden' }) }) }
  const original = (...args) => { requests.push(args); return Promise.resolve(response) }
  class XHR {
    open(method, url) { this.method = method; this.url = url }
    addEventListener(name, listener) { this.listener = listener }
    send() { this.status = status; this.responseURL = this.url; this.responseType = 'json'; this.response = { products: [{ id: 1025784485, sizes: [{ optionId: 123, price: { product: 130800 } }] }] }; this.listener?.() }
  }
  const listeners = {}
  const context = vm.createContext({ URL, Date, Object, Number, Promise, AbortSignal, location: new URL(pageUrl), fetch: original, XMLHttpRequest: XHR,
    addEventListener: (name, fn) => { listeners[name] = fn }, postMessage: (data, origin) => sent.push({ data: JSON.parse(JSON.stringify(data)), origin }) })
  context.window = context
  for (const name of ['contract.js', 'observer.js']) {
    const path = new URL(`../src/${name}`, import.meta.url)
    assert.ok(existsSync(path), `${name} is implemented`)
    vm.runInContext(readFileSync(path, 'utf8'), context)
  }
  vm.runInContext('globalThis.dispatchForTest = (fn, data) => fn({ source: globalThis, origin: location.origin, data })', context)
  return { context, response, requests, sent, next: (data) => context.dispatchForTest(listeners.message, { source: 'satorna-wb-prices-v1', type: 'catalog_next', ...data }) }
}

test('passive fetch observer preserves the page response and emits only sanitized prices', async () => {
  const { context, response, requests, sent } = load()
  const options = { credentials: 'include', headers: { Authorization: 'page-private' } }
  assert.equal(await context.fetch(detail, options), response)
  await flush()
  assert.equal(requests.length, 1)
  assert.equal(requests[0][1], options)
  assert.deepEqual(sent, [{ origin: 'https://www.wildberries.ru', data: { source: 'satorna-wb-prices-v1', type: 'observations', items: [{ nmId: 1025784485, sizeId: 123, buyerPriceNoWalletKopecks: 130800 }], walletSingleSizeNmIds: [1025784485], loadedNmId: 1025784485, cardOffers: [{ nmId: 1025784485, sizeId: 123 }] } }])
})

test('passive XHR observer handles normal detail responses and does not fetch anything', () => {
  const { context, requests, sent } = load()
  const xhr = new context.XMLHttpRequest()
  xhr.open('GET', detail)
  xhr.send()
  assert.equal(requests.length, 0)
  assert.equal(sent[0].data.items[0].buyerPriceNoWalletKopecks, 130800)
})

test('403 and 429 are reported once without a retry; unrelated requests are ignored', async () => {
  for (const status of [403, 429]) {
    const { context, requests, sent } = load(status)
    await context.fetch(detail)
    await flush()
    assert.equal(requests.length, 1)
    assert.deepEqual(sent[0].data, { source: 'satorna-wb-prices-v1', type: 'blocked', code: `http_${status}` })
  }
  const { context, sent } = load()
  await context.fetch(detail.replace('/detail?', '/other?'))
  await flush()
  assert.deepEqual(sent, [])
})

test('seller observer stays passive; no synthetic next-page fetch or credentials replay', async () => {
  const seller = 'https://www.wildberries.ru/seller/4405572'
  const native = 'https://www.wildberries.ru/__internal/u-catalog/sellers/v4/catalog?curr=rub&supplier=4405572&page=1&dest=1259570991&spp=30&sort=popular'
  const h = load(200, seller, native)
  assert.equal(h.requests.length, 0)
  await h.context.fetch(native); await flush()
  h.response.url = native.replace('page=1', 'page=2')
  await h.context.fetch(h.response.url); await flush()
  assert.equal(h.requests.length, 2, 'only WB page requests are observed')
  assert.equal(h.sent.at(-1).data.catalogPage, 2)
  assert.equal(h.sent.at(-1).data.items[0].buyerPriceNoWalletKopecks, 130800)
})
