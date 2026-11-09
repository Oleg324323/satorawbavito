import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import { priceBlock, priceNode } from './price-dom-fixture.mjs'

function load({ url = 'https://www.wildberries.ru/seller/4405572', title = 'Wildberries', next = null } = {}) {
  let now = Date.now(); let timer;
  class Clock extends Date { static now() { return now } }
  const sent = []; const scrolled = []; const listeners = {}; let runtimeListener
  const document = { title, readyState: 'complete', body: { innerText: '' }, documentElement: { scrollHeight: 5000 },
    querySelector: () => null, querySelectorAll: () => next ? [next] : [], addEventListener: (name, fn) => { listeners[name] = fn } }
  Object.defineProperty(document, 'cookie', { get() { throw new Error('WB cookies must never be read') } })
  const context = vm.createContext({ URL, Date: Clock, Object, Number, location: new URL(url), document,
    chrome: { runtime: { id: 'ext', sendMessage: async (value) => { sent.push(JSON.parse(JSON.stringify(value))); return { ok: true } }, onMessage: { addListener: (fn) => { runtimeListener = fn } } } },
    MutationObserver: class { observe() {} }, setTimeout: (fn) => { timer = fn; return 1 }, clearTimeout: () => { timer = null },
    getComputedStyle: () => ({ visibility: 'visible', display: 'block', opacity: '1' }),
    addEventListener: (name, fn) => { listeners[name] = fn }, postMessage: (data) => sent.push(JSON.parse(JSON.stringify(data))), innerHeight: 800, scrollBy: (data) => scrolled.push(data) })
  context.window = context; context.top = context
  for (const name of ['contract.js', 'content.js']) {
    const path = new URL(`../src/${name}`, import.meta.url)
    assert.ok(existsSync(path), `${name} is implemented`)
    vm.runInContext(readFileSync(path, 'utf8'), context)
  }
  return { context, sent, scrolled, listeners, hasTimer: () => Boolean(timer), tick: (ms) => { now += ms; timer?.() }, document,
    runtime: (message, ...args) => runtimeListener({ expectedPageUrl: url, ...message }, ...args) }
}

test('isolated bridge ignores foreign windows/origins and strips untrusted fields', () => {
  const h = load()
  const data = { source: 'satorna-wb-prices-v1', type: 'observations', items: [{ nmId: 100, sizeId: 200, buyerPriceNoWalletKopecks: 10000, token: 'secret', buyerPriceWithWalletKopecks: 1 }] }
  h.listeners.message({ source: {}, origin: 'https://www.wildberries.ru', data })
  h.listeners.message({ source: h.context, origin: 'https://evil.test', data })
  assert.equal(h.sent.length, 0)
  vm.runInContext(`window.dispatchForTest = (fn, data) => fn({ source: window, origin: 'https://www.wildberries.ru', data })`, h.context)
  h.context.dispatchForTest(h.listeners.message, data)
  assert.deepEqual(h.sent, [{ source: 'satorna-wb-prices-v1', type: 'observations', items: [{ nmId: 100, sizeId: 200, buyerPriceNoWalletKopecks: 10000 }] }])
})

test('card DOM corrects the exact offer before sending, preserving three distinct prices', () => {
  const h = load({ url: 'https://www.wildberries.ru/catalog/101/detail.aspx' })
  const block = priceBlock([priceNode('price-block__wallet-price', '1 397 ₽'),
    priceNode('price-block__old-price', '4 300 ₽'), priceNode('price-block__final-price', '1 426 ₽')])
  h.document.querySelector = selector => selector.includes('.product-page .price-block') ? block : null
  vm.runInContext('window.dispatchForTest = (fn, data) => fn({ source: window, origin: location.origin, data })', h.context)
  h.context.dispatchForTest(h.listeners.message, { source: 'satorna-wb-prices-v1', type: 'observations', loadedNmId: 101,
    cardOffers: [{ nmId: 101, sizeId: 201 }], items: [{ nmId: 101, sizeId: 201, buyerPriceNoWalletKopecks: 206400 }] })
  h.tick(250)
  assert.deepEqual(h.sent[0].items, [{ nmId: 101, sizeId: 201, buyerPriceNoWalletKopecks: 142600,
    originalPriceKopecks: 430000, buyerPriceWithWalletKopecks: 139700 }])
})

test('DOM cannot guess a size or turn wallet-only markup into a regular price', () => {
  for (const offers of [[{ nmId: 101, sizeId: 201 }, { nmId: 101, sizeId: 202 }], [{ nmId: 101, sizeId: 201 }]]) {
    const h = load({ url: 'https://www.wildberries.ru/catalog/101/detail.aspx' })
    h.document.querySelector = selector => selector.includes('.product-page .price-block')
      ? priceBlock([priceNode('price-block__wallet-price', '1 397 ₽')]) : null
    vm.runInContext('window.dispatchForTest = (fn, data) => fn({ source: window, origin: location.origin, data })', h.context)
    h.context.dispatchForTest(h.listeners.message, { source: 'satorna-wb-prices-v1', type: 'observations', loadedNmId: 101,
      cardOffers: offers, items: [] })
    for (let i = 0; i < 8; i++) h.tick(250)
    assert.deepEqual(h.sent[0].items, [])
  }
})

test('wallet enrichment comes from matching visible product, not MAIN-injected wallet or guessed discount', () => {
  const h = load()
  const tile = { getClientRects: () => [1], querySelector: () => ({ innerText: '1 397 ₽ с WB Кошельком', getClientRects: () => [1], querySelectorAll: () => [] }) }
  h.document.querySelector = selector => selector === 'article[data-nm-id="101"]' ? tile : null
  vm.runInContext('window.dispatchForTest = (fn, data) => fn({ source: window, origin: location.origin, data })', h.context)
  h.context.dispatchForTest(h.listeners.message, { source: 'satorna-wb-prices-v1', type: 'observations',
    walletSingleSizeNmIds: [101], items: [{ nmId: 101, sizeId: 201, buyerPriceNoWalletKopecks: 142600, buyerPriceWithWalletKopecks: 1 }] })
  assert.equal(h.sent.length, 0, 'wallet capture precedes ACK/navigation')
  h.tick(250)
  assert.equal(h.sent[0].items[0].buyerPriceWithWalletKopecks, 139700)
  assert.equal(h.sent[0].items[0].sizeId, 201)
})

test('hidden wallet DOM stays unknown and regular observation is published after bounded wait', () => {
  const h = load()
  h.document.querySelector = () => ({ getClientRects: () => [1], querySelector: () => ({ innerText: '1 397 ₽ с WB Кошельком', getClientRects: () => [] }) })
  vm.runInContext('window.dispatchForTest = (fn, data) => fn({ source: window, origin: location.origin, data })', h.context)
  h.context.dispatchForTest(h.listeners.message, { source: 'satorna-wb-prices-v1', type: 'observations',
    walletSingleSizeNmIds: [101], items: [{ nmId: 101, sizeId: 201, buyerPriceNoWalletKopecks: 142600 }] })
  for (let i = 0; i < 8; i++) h.tick(250)
  assert.equal(h.sent.length, 1)
  assert.equal(h.sent[0].items[0].buyerPriceWithWalletKopecks, undefined)
})

test('collector scroll is a bounded native action and foreign commands do nothing', () => {
  const h = load(); const replies = []
  h.runtime({ type: 'scroll' }, { id: 'other' }, (value) => replies.push(value))
  assert.equal(h.scrolled.length, 0)
  h.runtime({ type: 'scroll' }, { id: 'ext' }, (value) => replies.push(value))
  assert.equal(h.scrolled.length, 1)
  assert.equal(h.scrolled[0].top, 640)
  assert.equal(replies.at(-1).ok, true)
  assert.equal(h.sent.length, 0)
  h.document.querySelectorAll = (selector) => {
    assert.ok(selector.includes('pagination') || selector === '.catalog-page__main article[data-nm-id]')
    return selector.includes('pagination') ? [] : [{ getClientRects: () => [1], scrollIntoView: (options) => h.scrolled.push(options) }]
  }
  h.runtime({ type: 'scroll' }, { id: 'ext' }, () => {})
  assert.equal(h.scrolled.at(-1).block, 'center')
})

test('an actually present next-page control is used without constructing a seller page URL', () => {
  let clicked = 0
  const next = { textContent: 'Следующая страница', disabled: false, href: 'https://www.wildberries.ru/seller/4405572?page=2',
    getClientRects: () => [1], getAttribute: () => null, click: () => clicked++ }
  const h = load({ next })
  h.runtime({ type: 'scroll' }, { id: 'ext' }, () => {})
  assert.equal(clicked, 1)
  assert.equal(h.scrolled.length, 0)
})

test('visibility only sends a fixed notification; no automatic marketplace HTTP requests', () => {
  const h = load()
  h.document.visibilityState = 'hidden'; h.listeners.visibilitychange()
  assert.equal(h.sent.length, 0)
  h.document.visibilityState = 'visible'; h.listeners.visibilitychange()
  assert.deepEqual(h.sent, [{ source: 'satorna-wb-prices-v1', type: 'collector_visible' }])
})

test('duplicate catalog responses do not cancel native pagination; progress is read-only and sender-bound', () => {
  const h = load()
  h.runtime({ type: 'scroll' }, { id: 'ext' }, () => {})
  const data = { source: 'satorna-wb-prices-v1', type: 'observations', items: [], catalogPage: 1, productCount: 200 }
  vm.runInContext('window.dispatchForTest = (fn, data) => fn({ source: window, origin: location.origin, data })', h.context)
  h.context.dispatchForTest(h.listeners.message, data)
  assert.equal(h.hasTimer(), false)
  h.runtime({ type: 'scroll' }, { id: 'ext' }, () => {})
  h.context.dispatchForTest(h.listeners.message, data)
  assert.equal(h.hasTimer(), true)
  let response
  h.document.visibilityState = 'hidden'
  h.runtime({ type: 'catalog_progress' }, { id: 'other' }, value => { response = value })
  assert.equal(response, undefined)
  h.runtime({ type: 'catalog_progress' }, { id: 'ext' }, value => { response = value })
  assert.equal(response.visible, false)
  assert.equal(response.cards, 0)
  const count = h.scrolled.length
  h.runtime({ type: 'scroll', expectedPageUrl: 'https://www.wildberries.ru/seller/999' }, { id: 'ext' }, () => {})
  assert.equal(h.scrolled.length, count, 'a different user-selected seller cannot be scrolled')
})

test('a challenge only emits a fixed stop code, without page text or cookies', () => {
  const h = load({ title: 'Access denied' })
  assert.equal(h.sent.length, 0)
  h.tick(10000)
  assert.deepEqual(h.sent, [{ source: 'satorna-wb-prices-v1', type: 'blocked', code: 'challenge' }])
  const transient = load({ title: 'Проверка браузера' })
  transient.document.title = 'Товар Wildberries'
  transient.tick(10000)
  assert.equal(transient.sent.length, 0)
})
