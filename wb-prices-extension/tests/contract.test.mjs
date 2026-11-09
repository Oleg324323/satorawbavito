import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const path = new URL('../src/contract.js', import.meta.url)
const context = vm.createContext({ URL, Date, Object, Number })
if (existsSync(path)) vm.runInContext(readFileSync(path, 'utf8'), context)
const contract = () => {
  assert.ok(context.WbPricesContract, 'the WB observation contract is implemented')
  return context.WbPricesContract
}
const plain = (value) => JSON.parse(JSON.stringify(value))
const page = 'https://www.wildberries.ru/catalog/1025784485/detail.aspx'
const detail = 'https://www.wildberries.ru/__internal/u-card/cards/v4/detail?curr=rub&nm=1025784485&spp=30'
const now = Date.parse('2026-09-16T09:00:00Z')
const goods = {
  marketplaceAccountId: 2, goodsRevision: 'revision-1',
  page: 1, pageSize: 100, total: 1, maxAgeSeconds: 1800,
  items: [{ nmId: 1025784485, sizeId: 123, sellerPriceKopecks: 170000, sellerPriceObservedAt: '2026-09-16T08:59:00Z' }],
}

test('wallet amount is read only from an explicit displayed price; differing or missing size prices are ambiguous', () => {
  const c = contract()
  assert.equal(c.visibleWalletKopecks('1 397 ₽с WB Кошельком', 142600), 139700)
  assert.equal(c.visibleWalletKopecks('1 397,50 ₽ с WB Кошельком', 142600), 139750)
  for (const text of ['1 426 ₽ 1 397 ₽ с WB Кошельком', '1 397 ₽ 4 300 ₽ с WB Кошельком', 'Скидка 2% с WB Кошельком', '1 397 ₽ без кошелька', '0 ₽ с WB Кошельком', '9 999 ₽ с WB Кошельком']) assert.equal(c.visibleWalletKopecks(text, 142600), null)
  const product = { id: 101, sizes: [{ optionId: 201, price: { product: 142600 } }] }
  assert.deepEqual(plain(c.walletSingleSizeNmIds({ products: [product] })), [101])
  product.sizes.push({ optionId: 202, price: { product: 142600 } })
  assert.deepEqual(plain(c.walletSingleSizeNmIds({ products: [product] })), [])
  delete product.sizes[1].price
  assert.deepEqual(plain(c.walletSingleSizeNmIds({ products: [product] })), [])
})

test('MAIN cannot inject wallet amounts; trusted isolated observations validate and preserve exact-size wallet values', () => {
  const c = contract()
  const message = { source: c.SOURCE, type: 'observations', items: [{ nmId: 1025784485, sizeId: 123, buyerPriceNoWalletKopecks: 130800, buyerPriceWithWalletKopecks: 128000 }] }
  assert.equal(c.sanitizePageMessage(message, page).items[0].buyerPriceWithWalletKopecks, undefined)
  const clean = c.sanitizePageMessage(message, page, true)
  assert.equal(c.snapshotItems(clean.items, goods.items, '2026-09-16T09:00:00Z').items[0].buyerPriceWithWalletKopecks, 128000)
  message.items[0].buyerPriceWithWalletKopecks = 999999
  assert.equal(c.sanitizePageMessage(message, page, true).items[0].buyerPriceWithWalletKopecks, undefined)
})

test('only the viewed product detail response supplies positive integer kopecks; wallet is not inferred', () => {
  const payload = { secret: 'never-forward', products: [
    { id: 1025784485, sizes: [
      { optionId: 123, price: { basic: 430000, product: 130800, wallet: 0 } },
      { optionId: 124, price: { product: '120000' } },
      { optionId: 125, price: { product: 0 } },
      { optionId: 126, price: { product: 120000.5 } },
    ] },
    { id: 999, sizes: [{ optionId: 123, price: { product: 1000 } }] },
  ] }
  assert.deepEqual(plain(contract().parseDetail(payload, page, detail)), [
    { nmId: 1025784485, sizeId: 123, buyerPriceNoWalletKopecks: 130800, originalPriceKopecks: 430000 },
  ])
  for (const url of [detail.replace('/detail?', '/other?'), detail.replace('rub', 'usd'), detail.replace('www.wildberries.ru', 'evil.test'), detail.replace('nm=1025784485', 'nm=invalid')]) {
    assert.deepEqual(plain(contract().parseDetail(payload, page, url)), [])
  }
})

test('normal detail nm lists retain recommendations only when their IDs were actually requested', () => {
  const payload = { products: [101, 102, 999].map((id) => ({ id, supplierId: 55, sizes: [{ optionId: id + 100, price: { product: 10000 } }] })) }
  assert.deepEqual(plain(contract().parseDetail(payload, page, detail.replace('nm=1025784485', 'nm=101;102'))), [
    { nmId: 101, sizeId: 201, buyerPriceNoWalletKopecks: 10000, supplierId: 55 },
    { nmId: 102, sizeId: 202, buyerPriceNoWalletKopecks: 10000, supplierId: 55 },
  ])
  assert.throws(() => contract().validateCatalogPage({ ...goods, marketplaceAccountId: '2' }, 1, null, now), /catalog_invalid/)
})

test('public card batches keep only requested exact sizes and positive integer buyer prices', () => {
  const payload = { trace: 'never-forward', products: [
    { id: 101, supplierId: 55, sizes: [
      { optionId: 201, price: { basic: 20000, product: 15000, wallet: 14000 } },
      { optionId: 202, price: { product: 14000 } },
    ] },
    { id: 999, sizes: [{ optionId: 299, price: { product: 10000 } }] },
  ] }
  assert.deepEqual(plain(contract().parsePublicDetail(payload, [101, 102])), [
    { nmId: 101, sizeId: 201, buyerPriceNoWalletKopecks: 15000, originalPriceKopecks: 20000, supplierId: 55 },
    { nmId: 101, sizeId: 202, buyerPriceNoWalletKopecks: 14000, supplierId: 55 },
  ])
  assert.deepEqual(plain(contract().parsePublicDetail({ products: [{ id: 101, sizes: [
    { optionId: 201, price: { product: '15000' } },
  ] }] }, [101])), [])
})

test('page messages cannot choose seller prices, timestamps, wallet prices or an API destination', () => {
  const result = contract().sanitizePageMessage({ source: 'satorna-wb-prices-v1', type: 'observations', items: [
    { nmId: 1025784485, sizeId: 123, buyerPriceNoWalletKopecks: 130800, buyerPriceWithWalletKopecks: 1, sellerPriceKopecks: 999999, token: 'secret', url: 'https://evil.test', observedAt: '2099-01-01' },
  ] }, 1025784485)
  assert.deepEqual(plain(result), { type: 'observations', items: [{ nmId: 1025784485, sizeId: 123, buyerPriceNoWalletKopecks: 130800 }] })
  for (const change of [{ source: 'other' }, { items: [] }, { items: Array(10001).fill({}) }, { type: 'connect', token: 'secret' }]) {
    assert.equal(contract().sanitizePageMessage({ source: 'satorna-wb-prices-v1', type: 'observations', items: [{ nmId: 1025784485, sizeId: 123, buyerPriceNoWalletKopecks: 130800 }], ...change }, 1025784485), null)
  }
})

test('normal seller catalog and recommendation responses preserve exact offers and public supplier IDs', () => {
  const sellerPage = 'https://www.wildberries.ru/seller/4405572'
  const url = 'https://www.wildberries.ru/__internal/u-catalog/sellers/v4/catalog?curr=rub&supplier=4405572&page=2'
  const payload = { products: [{ id: 1025784485, supplierId: 4405572, sizes: [
    { optionId: 123, price: { product: 130800 } }, { optionId: 456, price: { product: 131000 } },
  ] }] }
  assert.deepEqual(plain(contract().parseDetail(payload, sellerPage, url)), [
    { nmId: 1025784485, sizeId: 123, buyerPriceNoWalletKopecks: 130800, supplierId: 4405572 },
    { nmId: 1025784485, sizeId: 456, buyerPriceNoWalletKopecks: 131000, supplierId: 4405572 },
  ])
  assert.deepEqual(plain(contract().parseDetail(payload, sellerPage, url.replace('4405572', '999'))), [])
  const message = contract().sanitizePageMessage({ source: 'satorna-wb-prices-v1', type: 'observations',
    items: [], catalogPage: 3, productCount: 0 }, sellerPage)
  assert.deepEqual(plain(message), { type: 'observations', items: [], catalogPage: 3, productCount: 0 })
  assert.equal(contract().isTrustedPageSender({ id: 'ext', frameId: 0, url: sellerPage, tab: { id: 7, url: sellerPage } }, 'ext', 7, sellerPage), true)
})

test('snapshot joins exact offer and uses only the fresh CRM seller reference', () => {
  const catalog = contract().validateCatalogPage(goods, 1, null, now)
  const result = contract().snapshotItems([
    { nmId: 1025784485, sizeId: 123, buyerPriceNoWalletKopecks: 130800 },
    { nmId: 1025784485, sizeId: 999, buyerPriceNoWalletKopecks: 10000 },
    { nmId: 999, sizeId: 123, buyerPriceNoWalletKopecks: 10000 },
    { nmId: 1025784485, sizeId: 123, buyerPriceNoWalletKopecks: 180000 },
  ], catalog.items, '2026-09-16T09:00:00.000Z')
  assert.deepEqual(plain(result), { items: [{ nmId: 1025784485, sizeId: 123, sellerPriceKopecks: 170000, buyerPriceNoWalletKopecks: 130800, buyerPriceWithWalletKopecks: null, observedAt: '2026-09-16T09:00:00.000Z' }], ignored: 3 })
  assert.throws(() => contract().validateCatalogPage({ ...goods, items: [{ ...goods.items[0], sellerPriceObservedAt: '2026-09-16T08:00:00Z' }] }, 1, null, now), /catalog_stale/)
  assert.throws(() => contract().validateCatalogPage({ ...goods, goodsRevision: 'new' }, 1, goods, now), /catalog_changed/)
  assert.throws(() => contract().validateCatalogPage({ ...goods, items: [{ ...goods.items[0], sellerPriceKopecks: 1.2 }] }, 1, null, now), /catalog_invalid/)
})

test('catalog metadata cannot request an unbounded number of pages', () => {
  const items = Array.from({ length: 100 }, (_, index) => ({ ...goods.items[0], nmId: 1000 + index }))
  assert.equal(contract().validateCatalogPage({ ...goods, total: 100000, items }, 1, null, now).total, 100000)
  assert.throws(() => contract().validateCatalogPage({ ...goods, total: 100001, items }, 1, null, now), /catalog_invalid/)
})

test('only an extension popup or the owned top-level WB card can issue their respective messages', () => {
  const c = contract()
  assert.equal(c.isTrustedPopupSender({ id: 'ext', url: 'chrome-extension://ext/src/popup.html' }, 'ext'), true)
  assert.equal(c.isTrustedPopupSender({ id: 'ext', url: page, tab: { id: 7 } }, 'ext'), false)
  const popupUrl = 'chrome-extension://ext/src/popup.html'
  assert.equal(c.isTrustedPopupSender({ id: 'ext', url: popupUrl, frameId: 0, tab: { id: 8, url: popupUrl } }, 'ext'), true)
  assert.equal(c.isTrustedPopupSender({ id: 'ext', url: popupUrl, frameId: 1, tab: { id: 8, url: popupUrl } }, 'ext'), false)
  assert.equal(c.sanitizePageMessage({ source: c.SOURCE, type: 'observations', items: [], loadedNmId: 1025784485 }, page).loadedNmId, 1025784485)
  assert.equal(c.sanitizePageMessage({ source: c.SOURCE, type: 'observations', items: [], loadedNmId: 999 }, page), null)
  const sender = { id: 'ext', frameId: 0, url: page, tab: { id: 7, url: page } }
  assert.equal(c.isTrustedPageSender(sender, 'ext', 7, 1025784485), true)
  assert.equal(c.isTrustedPageSender({ ...sender, frameId: 1 }, 'ext', 7, 1025784485), false)
  assert.equal(c.isTrustedPageSender(sender, 'ext', 8, 1025784485), false)
  assert.equal(c.isTrustedPageSender({ ...sender, url: 'https://evil.test' }, 'ext', 7, 1025784485), false)
})
