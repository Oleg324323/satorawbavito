import test from 'node:test'
import assert from 'node:assert/strict'
import vm from 'node:vm'
import { readFileSync } from 'node:fs'

const source = readFileSync(new URL('../src/content.js', import.meta.url), 'utf8')
const code = source.slice(source.indexOf('async function collectSnapshot('), source.indexOf('\nchrome.runtime.onMessage.addListener', source.indexOf('async function collectSnapshot(')))

test('combined collection preserves a return access restriction and incomplete checkpoint', () => {
  const background = readFileSync(new URL('../src/background.js', import.meta.url), 'utf8')
  const combine = background.slice(background.indexOf('function combineCollections('), background.indexOf('async function collectAndPostFromAvito('))
  const context = vm.createContext({ AVITO_ORDERS_URL: 'https://www.avito.ru/orders', AVITO_RETURNS_URL: 'https://www.avito.ru/orders?status=on_return' })
  vm.runInContext(combine, context)
  const result = context.combineCollections({orders:[],collector:{checkpoint:false}}, {orders:[],collector:{checkpoint:true,error:'AVITO_BLOCKED: security check'}})
  assert.equal(result.collector.checkpoint, true)
  assert.equal(result.collector.status, 'partial')
  assert.equal(result.collector.error, 'AVITO_BLOCKED: security check')
})

for (const scenario of ['closed', 'received-base', 'received-detail']) {
  test(`collector preserves minimal terminal updates without historical enrichment: ${scenario}`, async () => {
    let details = 0
    const row = { orderId: '000123', marketplaceId: '000123', status: scenario === 'closed' ? 'closed' : 'on_return',
      returnStatus: scenario === 'received-base' ? 'received' : null, items: [{ itemId: '000456', title: 'Synthetic' }] }
    const context = vm.createContext({
      normalizeOptions: value => value, location: { href: 'https://www.avito.ru/orders' },
      logEvent() {}, showCollectorOverlay() {}, highlightOrderRows() {},
      loadedOrderCandidates: async () => ({ rows: [{}], complete: true }), collectOrder: () => row,
      dedupeOrders: rows => rows, missingSummary: rows => ({ items: rows.flatMap(row => row.items || []), missing: {} }),
      requestedMissing: () => ({}), enrichOrderFromDetails: async () => {
        details += 1
        return { order: { ...row, returnStatus: 'received' }, checked: true, errors: [] }
      }, chrome: { runtime: { sendMessage: (_message, callback) => callback(), lastError: null } },
    })
    vm.runInContext(code, context)
    const result = await context.collectSnapshot({ collectionLabel: 'возвратов' })
    assert.equal(details, scenario === 'received-detail' ? 1 : 0)
    assert.equal(result.orders.length, 1)
    assert.equal(result.orders[0].orderId, '000123')
    assert.equal(result.orders[0].items.length, 0)
    assert.equal(result.orders[0].shipmentNumber, undefined)
    if (scenario !== 'closed') assert.equal(result.orders[0].returnStatus, 'received')
  })
}

test('chat checkpoints include the selected mode so AI runs in bounded saved batches', async () => {
  const checkpoints = []
  const row = { orderId: 'order', accountId: 'seller', status: 'ready_to_ship', items: [{ itemId: 'item', title: 'Synthetic' }] }
  const context = vm.createContext({
    normalizeOptions: value => value, location: { href: 'https://www.avito.ru/orders' },
    logEvent() {}, showCollectorOverlay() {}, highlightOrderRows() {},
    loadedOrderCandidates: async () => ({ rows: [{}], complete: true }), collectOrder: () => row,
    dedupeOrders: rows => rows, missingSummary: rows => ({ items: rows.flatMap(row => row.items || []), missing: {} }),
    requestedMissing: () => ({}), enrichOrderFromDetails: async () => ({ order: row, checked: true, errors: [] }),
    chrome: { runtime: { sendMessage: (message, callback) => { checkpoints.push(message); callback({ ok: true }) }, lastError: null } },
  })
  vm.runInContext(code, context)
  await context.collectSnapshot({ sizeMode: 'chat_ai', collectionLabel: 'заказов' })
  assert.equal(checkpoints[0].payload.collector.options.sizeMode, 'chat_ai')
  assert.equal(checkpoints[0].payload.orders.length, 1)
})

for (const status of [null, 'awaiting_confirmation', 'on_return']) {
  test(`collector does not certify silently skipped unknown status: ${status}`, async () => {
    let details = 0
    const row = { orderId: 'return-order', status, returnStatus: status === 'on_return' ? 'in_transit' : null,
      deliveryService: 'СДЭК', items: [{ itemId: 'item', title: 'Synthetic' }] }
    const overlays = []
    const context = vm.createContext({
      normalizeOptions: value => value, location: { href: 'https://www.avito.ru/orders' },
      logEvent() {}, showCollectorOverlay: (...args) => overlays.push(args), highlightOrderRows() {},
      loadedOrderCandidates: async () => ({ rows: [{}], complete: true }), collectOrder: () => row,
      dedupeOrders: rows => rows, missingSummary: rows => ({ items: rows.flatMap(row => row.items || []), missing: {} }),
      requestedMissing: () => ({}), enrichOrderFromDetails: async () => { details++; return { order: row, checked: true, errors: [] } },
      chrome: { runtime: { sendMessage: (_message, callback) => callback(), lastError: null } },
    })
    vm.runInContext(code, context)
    const result = await context.collectSnapshot({ sizeMode: 'chat_ai', collectionLabel: 'возвратов' })
    assert.equal(result.collector.checkpoint, status === null)
    assert.equal(result.collector.status, status === null ? 'partial' : 'completed')
    assert.equal(result.collector.unknownStatuses, status === null ? 1 : 0)
    assert.equal(details, status === 'on_return' ? 1 : 0)
    if (status === null) {
      assert.match(result.collector.error, /Статус не распознан/)
      assert.equal(overlays.at(-1)[1], 'warn')
    } else if (status === 'on_return') assert.equal(result.orders[0].deliveryService, 'СДЭК')
  })
}

for (const scenario of ['earlier-unknown', 'missing-next', 'unchanged-next', 'page-cap']) {
  test(`pagination cannot erase incomplete collection: ${scenario}`, async () => {
    const background = readFileSync(new URL('../src/background.js', import.meta.url), 'utf8')
    const fn = background.slice(background.indexOf('async function collectFromAvitoOrdersPage('), background.indexOf('function combineCollections('))
    let page = 0, scripts = 0
    const context = vm.createContext({
      AVITO_ORDERS_URL: 'https://www.avito.ru/orders', avitoOrdersTab: async () => ({ id: 1 }),
      waitForTabComplete: async () => {}, readSettings: async () => ({ accessToken: 'synthetic' }),
      apiUrlFromSettings: async () => 'http://127.0.0.1', authorizationValue: () => 'synthetic',
      fetch: async () => ({}), readApiJson: async () => ({ orders: [] }),
      AbortSignal, setTimeout: callback => callback(),
      tabsSendMessage: async () => { page++; return { ok: true, payload: { orders: [{ orderId: String(page) }], collector: {
        hasNext: scenario !== 'earlier-unknown' || page === 1,
        checkpoint: scenario === 'earlier-unknown' && page === 1,
        unknownStatuses: scenario === 'earlier-unknown' && page === 1 ? 1 : 0,
        error: scenario === 'earlier-unknown' && page === 1 ? 'Статус не распознан' : null,
      } } } },
      chrome: { scripting: { executeScript: async () => {
        scripts++
        if (scenario === 'missing-next') return [{ result: null }]
        return [{ result: scenario === 'unchanged-next' || scripts % 2 ? 'old' : 'new' }]
      } } },
    })
    vm.runInContext(fn, context)
    const result = await context.collectFromAvitoOrdersPage()
    assert.equal(result.payload.collector.status, 'partial')
    assert.equal(result.payload.collector.checkpoint, true)
    assert.ok(result.payload.collector.error)
    if (scenario === 'earlier-unknown') assert.equal(result.payload.collector.unknownStatuses, 1)
    if (scenario === 'page-cap') assert.equal(page, 100)
  })
}
