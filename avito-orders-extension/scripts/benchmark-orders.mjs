// Deterministic collector comparison, NOT a live Avito speed claim.
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import { performance } from 'node:perf_hooks'

const current = readFileSync(new URL('../src/content.js', import.meta.url), 'utf8')
const baseline = process.argv[2] ? readFileSync(process.argv[2], 'utf8') : current
const cache = readFileSync(new URL('../src/order-cache.js', import.meta.url), 'utf8')
const count = 20
const rows = Array.from({ length: count }, (_, i) => ({ orderId: String(i), accountId: 'test', status: 'ready_to_ship',
  shipmentNumber: String(1000 + i), items: [{ itemId: String(i + 1), lineIndex: 0, title: 'Test', quantity: 1,
    imageUrl: 'https://b00.img.avito.st/test.jpg', size: 'M', color: 'black', sources: { size: 'order_detail', color: 'order_detail' } }] }))
async function run(source, reuse) {
  let active = 0, peak = 0, detailCalls = 0
  const context = vm.createContext({ console, Date, setTimeout, location: { href: 'https://www.avito.ru/orders' },
    sleep: ms => new Promise(resolve => setTimeout(resolve, ms)),
    normalizeOptions: () => ({ collectionLabel: 'заказов', photoMode: 'one', sizeMode: 'description' }),
    orderCandidates: () => rows, loadedOrderCandidates: async () => ({ rows, complete: true }),
    collectOrder: row => ({ ...row, items: row.items.map(item => ({ ...item, size: null, color: null })) }),
    enrichOrderFromDetails: async row => {
      detailCalls++; peak = Math.max(peak, ++active)
      await new Promise(resolve => setTimeout(resolve, 40)); active--
      return { order: rows.find(value => value.orderId === row.orderId), checked: true, itemPages: 1, errors: [] }
    },
    logEvent() {}, showCollectorOverlay() {}, highlightOrderRows() {},
    dedupeOrders: values => [...new Map(values.map(value => [value.orderId, value])).values()],
    requestedMissing: () => ({}), missingSummary: orders => ({ items: orders.flatMap(row => row.items), missing: {} }),
    chrome: { runtime: { sendMessage(_message, done) { done() } } },
  })
  vm.runInContext(cache, context)
  vm.runInContext(source.slice(source.indexOf('async function collectSnapshot('), source.indexOf('\nchrome.runtime.onMessage.addListener', source.indexOf('async function collectSnapshot('))), context)
  const started = performance.now()
  await context.collectSnapshot({ savedOrders: reuse ? rows : [] })
  return { milliseconds: Math.round(performance.now() - started), detailCalls, maxParallel: peak }
}
const before = await run(baseline, false), after = await run(current, false), cached = await run(current, true)
console.log(JSON.stringify({ kind: 'synthetic collector; 20 rows, 40ms per detail, no network/DB', before, after, cached,
  speedup: Number((before.milliseconds / Math.max(1, after.milliseconds)).toFixed(2)) }, null, 2))
