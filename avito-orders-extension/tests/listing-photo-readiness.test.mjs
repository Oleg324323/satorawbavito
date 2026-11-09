import test from 'node:test'
import assert from 'node:assert/strict'
import vm from 'node:vm'
import { readFileSync } from 'node:fs'

// Execute the actual background reader, with a deterministic clock and DOM samples.
const background = readFileSync(new URL('../src/background.js', import.meta.url), 'utf8')
const start = background.indexOf('  async function readyPhotos(')
const end = background.indexOf('\n  try { return await SatornaListingPhotos.collect', start)
function reader(sample, replacements = new Map()) {
  let time = 0
  const activated = []
  const context = vm.createContext({
    Date: { now: () => time }, listingPhotosStop: false, blockedTabId: null, replacementTabs: replacements,
    setTimeout: fn => { time += 250; fn() },
    pagePhotos: async id => sample(time, id),
    assertPhotoPage: page => { if (page.blocked) throw Object.assign(new Error('Manual security check'), { stop: true }) },
    chrome: { tabs: { update: async (id, options) => activated.push([id, options.active]) } },
  })
  vm.runInContext(background.slice(start, end), context)
  return { read: context.readyPhotos, time: () => time, activated }
}

test('list reader waits for pagination to mount after the first product cards', async () => {
  const r = reader(t => ({ photos: {}, signature: '1,2', listReady: t >= 1500 }))
  assert.equal((await r.read(7, ['1', '2'])).signature, '1,2')
  assert.equal(r.time(), 1500)
})

test('pagination waits for a changed and stable listing signature', async () => {
  const r = reader(t => ({ photos: {}, signature: t < 1000 ? '1,2' : '3,4', listReady: true }))
  assert.equal((await r.read(7, ['3', '4'], null, '1,2')).signature, '3,4')
  assert.equal(r.time(), 1750)
})

test('detail photo is usable immediately without waiting for window load', async () => {
  const r = reader(() => ({ photos: { 123: 'https://b00.img.avito.st/photo' } }))
  assert.ok((await r.read(7, ['123'], '123')).photos['123'])
  assert.equal(r.time(), 0)
})

test('security challenge stops polling and leaves its tab visible for manual verification', async () => {
  const r = reader(() => ({ photos: {}, blocked: true }))
  await assert.rejects(r.read(7, ['123']), { stop: true })
  assert.deepEqual(r.activated, [[7, true]])
  assert.equal(r.time(), 0)
})

test('reader follows a Chrome prerender replacement without losing the photo', async () => {
  const r = reader((_t, id) => {
    assert.equal(id, 9)
    return { photos: { 123: 'https://b00.img.avito.st/photo' } }
  }, new Map([[7, 8], [8, 9]]))
  assert.ok((await r.read(7, ['123'], '123')).photos['123'])
})

test('each detail photo uses its own tab and closes it after reading', async () => {
  const from = background.indexOf('    loadPhoto: async row => {')
  const to = background.indexOf('\n  }) } finally', from)
  let created = 0
  const removed = []
  const context = vm.createContext({
    detailTabs: new Set(), replacementTabs: new Map(), blockedTabId: null,
    chrome: { tabs: {
      create: async options => { assert.equal(options.url, 'https://www.avito.ru/item_123'); created++; return { id: 8 } },
      remove: async id => removed.push(id),
    } },
    readyPhotos: async id => { assert.equal(id, 8); return { photos: { 123: 'https://b00.img.avito.st/photo' } } },
  })
  const loader = vm.runInContext('({' + background.slice(from, to) + '})', context)
  assert.equal(await loader.loadPhoto({ itemId: '123', url: 'https://www.avito.ru/item_123' }), 'https://b00.img.avito.st/photo')
  assert.equal(created, 1)
  assert.deepEqual(removed, [8])
})

test('order detail proceeds when DOM is interactive, without waiting for image load', async () => {
  const from = background.indexOf('async function waitForTabInteractive(')
  const to = background.indexOf('\nfunction assertAvitoUrl(', from)
  assert.ok(from >= 0 && to > from)
  let attempts = 0
  const context = vm.createContext({
    Date: { now: () => attempts * 400 },
    setTimeout: callback => callback(),
    chrome: { scripting: { executeScript: async () => [{ result: { ready: ++attempts >= 2, host: 'www.avito.ru' } }] } },
  })
  vm.runInContext(background.slice(from, to), context)
  await context.waitForTabInteractive(7)
  assert.equal(attempts, 2)
})
