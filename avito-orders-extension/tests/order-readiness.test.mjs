import test from 'node:test'
import assert from 'node:assert/strict'
import vm from 'node:vm'
import { readFileSync } from 'node:fs'

const source = readFileSync(new URL('../src/content.js', import.meta.url), 'utf8')
const code = source.slice(source.indexOf('function collectionBlocked()'), source.indexOf('\nfunction orderDetailsLink('))
function harness({ text = '', readyAfter = 2, next = false } = {}) {
  let polls = 0
  const context = vm.createContext({
    setTimeout: callback => callback(),
    document: { body: { innerText: text }, documentElement: { scrollHeight: 1000 }, querySelector: () => next ? { getAttribute: () => null } : null },
    window: { scrollTo() {} },
    orderCandidates: () => ++polls > readyAfter ? [{ id: '1' }] : [],
    orderIdFromLink: row => row.id, orderIdentity: () => null, textOf: () => '',
  })
  vm.runInContext(code, context)
  return { run: () => context.loadedOrderCandidates(), polls: () => polls }
}
test('waits for hydrated rows and does not treat initial empty DOM as completed', async () => {
  const h = harness(); const result = await h.run()
  assert.equal(result.rows.length, 1); assert.equal(result.complete, true); assert.ok(h.polls() >= 6)
})
test('empty loading state never replaces saved orders', async () => {
  await assert.rejects(harness({ readyAfter: 100 }).run(), /не загрузил список/)
})
test('unvisited pagination produces a checkpoint, never a complete snapshot', async () => {
  const result = await harness({ next: true }).run()
  assert.equal(result.complete, false); assert.equal(result.hasNext, true)
})
test('security page stops immediately without extracting products', async () => {
  const h = harness({ text: 'Доступ ограничен: проверка безопасности' })
  await assert.rejects(h.run(), /AVITO_BLOCKED/); assert.equal(h.polls(), 0)
})
