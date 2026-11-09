import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { expect, it } from 'vitest'

const html = readFileSync(new URL('../../../public/vella-production.html', import.meta.url), 'utf8')
const start = 'async function hydrateOrdersPrintList(options = {}){'
const end = '\nasync function hydrateOrdersPrintSkus(){'
if (html.split(start).length !== 2 || html.split(end).length !== 2) throw new Error('Expected unique legacy hydration function boundaries')
const functionStart = html.indexOf(start), functionEnd = html.indexOf(end)
if (functionEnd <= functionStart) throw new Error('Invalid legacy hydration function order')
const hydrateSource = html.slice(functionStart, functionEnd)

async function exercise(source: 'avito' | 'wb', archive: boolean, marker: { id: string; island: string } | null) {
  const events: unknown[] = []
  const fallback = { kind: 'synthetic-fallback' }
  const payload = { kind: 'synthetic-response' }
  const selectors: string[] = []
  const context = {
    document: {
      querySelector(selector: string) {
        selectors.push(selector)
        // Match the real root and ownership attribute exactly; unrelated/malformed markers do not own it.
        if (selector !== '#tab-orders-print[data-vella-island="avito-orders"]') return null
        return marker?.id === 'tab-orders-print' && marker.island === 'avito-orders' ? marker : null
      },
    },
    ordersPrintListLoaded: false, ordersPrintLoadKey: '', ordersPrintSource: 'initial',
    ordersArchiveMode: false, ordersSelectedDate: 'initial', ordersPrintPayload: null,
    ORDERS_PRINT_FALLBACK: fallback,
    getOrdersActiveSource() { throw new Error('Explicit source must be used') },
    isOrdersArchiveMode() { throw new Error('Explicit archive must be used') },
    getOrdersSelectedDate() { throw new Error('Explicit date must be used') },
    renderOrdersPrintList(data: unknown, options: unknown) { events.push(['render', data, options]) },
    async hydrateOrdersPrintSkus() { events.push(['hydrate-skus']) },
    async hydrateOrdersReturnsSyncSettings() { events.push(['hydrate-returns']) },
    async fetch(url: string, options: unknown) {
      events.push(['fetch', url, options])
      return { ok: true, async json() { return payload } }
    },
    avitoOrdersPayloadToPickingPayload(data: unknown, date: string) {
      events.push(['map-avito', data, date])
      return { kind: 'synthetic-avito-picking' }
    },
    options: { source, archive, date: '2026-08-01', force: true },
  }
  await runInNewContext(`${hydrateSource}\nhydrateOrdersPrintList(options)`, context, { timeout: 1000 })
  return { events, selectors, context }
}

it('does not run legacy helpers, reads, renders or state writes for the React-owned Avito root', async () => {
  const { events, context } = await exercise('avito', false, { id: 'tab-orders-print', island: 'avito-orders' })
  expect(events).toEqual([])
  expect([context.ordersPrintListLoaded, context.ordersPrintLoadKey, context.ordersPrintSource, context.ordersSelectedDate])
    .toEqual([false, '', 'initial', 'initial'])
})

it.each([
  { name: 'standalone Avito', source: 'avito' as const, archive: false, marker: null },
  { name: 'Avito with unrelated root', source: 'avito' as const, archive: false, marker: { id: 'different-root', island: 'avito-orders' } },
  { name: 'Avito with legacy ownership', source: 'avito' as const, archive: false, marker: { id: 'tab-orders-print', island: 'orders-print-legacy' } },
  { name: 'WB current even with Avito marker', source: 'wb' as const, archive: false, marker: { id: 'tab-orders-print', island: 'avito-orders' } },
  { name: 'WB archive even with Avito marker', source: 'wb' as const, archive: true, marker: { id: 'tab-orders-print', island: 'avito-orders' } },
])('preserves existing local hydration for $name', async ({ source, archive, marker }) => {
  const { events, context } = await exercise(source, archive, marker)
  const options = { source, archive, date: '2026-08-01' }
  expect(events).toEqual([
    ['render', { kind: 'synthetic-fallback' }, options],
    ['hydrate-skus'],
    ['render', { kind: 'synthetic-fallback' }, options],
    ...(source === 'avito' ? [
      ['hydrate-returns'],
      ['fetch', '/api/v1/avito/orders?dateFrom=2026-08-01&periodDays=30&limit=20', { headers: { Accept: 'application/json' } }],
      ['map-avito', { kind: 'synthetic-response' }, '2026-08-01'],
      ['render', { kind: 'synthetic-avito-picking' }, options],
    ] : [
      ['fetch', `/api/v1/production/print-list${archive ? '/archive' : ''}?source=wb&date=2026-08-01`, { headers: { Accept: 'application/json' } }],
      ['render', { kind: 'synthetic-response' }, options],
    ]),
  ])
  expect(context.ordersPrintLoadKey).toBe(`${source}:${archive ? 'archive' : 'current'}:2026-08-01`)
})
