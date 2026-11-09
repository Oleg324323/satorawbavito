import test from 'node:test'
import assert from 'node:assert/strict'
import '../src/listing-photos.js'
const { imageUrl, listingUrl, extractPhoto, collect } = globalThis.SatornaListingPhotos
const item = { itemId: '123', accountId: 'a', url: 'https://www.avito.ru/city/item_123' }
const cdn = 'https://70.img.avito.st/photo.jpg'
const options = { backendUrl: 'http://127.0.0.1:58017', authorization: 'Bearer synthetic-test-token' }
const json = value => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })

test('extract only exact listing OpenGraph photo; CDN and URL allowlists', () => {
  assert.equal(imageUrl('https://b00.img.avito.st/image/test'), 'https://b00.img.avito.st/image/test')
  assert.equal(imageUrl('https://b40.img.avito.st/image/test'), 'https://b40.img.avito.st/image/test')
  assert.equal(extractPhoto(`<meta content="${cdn}?x=1&amp;y=2" property="og:image">`, '123'), cdn + '?x=1&y=2')
  assert.equal(extractPhoto(`<meta property="og:url" content="https://www.avito.ru/item_124"><meta property="og:image" content="${cdn}">`, '123'), null)
  assert.equal(imageUrl('https://img.avito.st.evil.test/a'), null)
  assert.equal(imageUrl('https://user:password@70.img.avito.st/a'), null)
  assert.equal(listingUrl('https://evil.test/item_123', '123'), null)
})

test('bulk upload is parallel but bounded to five workers', async () => {
  let active = 0, peak = 0
  const rows = Array.from({ length: 12 }, (_, i) => ({ ...item, itemId: String(100+i), url: `https://www.avito.ru/item_${100+i}`, imageUrl: cdn }))
  const result = await collect({ ...options, fetchImpl: async url => {
    if (url.endsWith('/collection-context')) return json({ total: 12, saved: 0, missing: rows })
    if (url === cdn) {
      active++; peak = Math.max(peak, active)
      await new Promise(r=>setTimeout(r, 5))
      active--
      return new Response('image', {headers:{'Content-Type':'image/jpeg'}})
    }
    return json({photoId: 1})
  } })
  assert.equal(result.saved, 12)
  assert.equal(peak, 5)
})

test('complete database requires just one manifest request, zero Avito fetches', async () => {
  const calls = []
  const state = await collect({ ...options, fetchImpl: async url => { calls.push(url); return json({ total: 721, saved: 721, missing: [] }) } })
  assert.equal(calls.length, 1)
  assert.equal(state.saved, 721)
  assert.equal(state.stage, 'done')
})

test('an in-flight successful upload cannot erase the parallel rate-limit error', async () => {
  const rows = [123,124].map(id => ({ ...item, itemId: String(id), url: `https://www.avito.ru/item_${id}`, imageUrl: cdn + id }))
  const result = await collect({ ...options, fetchImpl: async url => {
    if (url.endsWith('/collection-context')) return json({ total: 2, saved: 0, missing: rows })
    if (url === cdn + '123') return new Response('', { status: 429 })
    if (url === cdn + '124') {
      await new Promise(resolve => setTimeout(resolve, 5))
      return new Response('image', { headers: { 'Content-Type': 'image/jpeg' } })
    }
    return json({ photoId: 1 })
  } })
  assert.equal(result.stage, 'paused')
  assert.match(result.message, /HTTP 429/)
  assert.equal(result.saved, 1)
})

test('fetch missing photo once, persist bytes, next run resumes without rescanning', async () => {
  const calls = []
  let saved = false
  const fetchImpl = async (url, args) => {
    calls.push(url)
    if (url.endsWith('/collection-context')) return json({ total: 2, saved: saved ? 2 : 1, missing: saved ? [] : [item] })
    if (url === item.url) return new Response(`<meta property="og:image" content="${cdn}">`)
    if (url === cdn) {
      assert.equal(args.credentials, 'omit')
      assert.equal(args.headers, undefined)
      return new Response('synthetic-image-bytes', { headers: { 'Content-Type': 'image/jpeg' } })
    }
    assert.match(url, /\/import\?accountId=a&itemId=123$/)
    assert.equal(await args.body.text(), 'synthetic-image-bytes')
    saved = true
    return json({ photoId: 1, created: true })
  }
  assert.equal((await collect({ ...options, fetchImpl })).saved, 2)
  assert.equal(calls.length, 4)
  assert.equal((await collect({ ...options, fetchImpl })).saved, 2)
  assert.equal(calls.length, 5)
})

test('existing image URL avoids HTML; rate limit stops without repeated requests', async () => {
  const calls = []
  const state = await collect({ ...options, fetchImpl: async url => {
    calls.push(url)
    if (url.endsWith('/collection-context')) return json({ total: 2, saved: 0, missing: [{ ...item, imageUrl: cdn }, item] })
    assert.equal(url, cdn)
    return new Response('', { status: 429 })
  } })
  assert.equal(calls.length, 2)
  assert.equal(state.stage, 'paused')
  assert.equal(state.saved, 0)
})

test('unconfirmed upload is not counted and is available for subsequent retry', async () => {
  const state = await collect({ ...options, fetchImpl: async url => {
    if (url.endsWith('/collection-context')) return json({ total: 1, saved: 0, missing: [{ ...item, imageUrl: cdn }] })
    if (url === cdn) return new Response('bytes', { headers: { 'Content-Type': 'image/png' } })
    return new Response('', { status: 500 })
  } })
  assert.equal(state.saved, 0)
  assert.equal(state.failed, 1)
  assert.equal(state.stage, 'partial')
})

test('three or more missing pictures never stop the rest of the collection', async () => {
  const seen = [], uploads = []
  const rows = Array.from({ length: 6 }, (_, i) => ({ ...item, itemId: String(123 + i), url: `https://www.avito.ru/item_${123 + i}` }))
  const state = await collect({ ...options,
    preparePhotos: async () => ({}),
    loadPhoto: async row => { seen.push(row.itemId); return row.itemId === '128' ? cdn : null },
    fetchImpl: async (url, args) => {
      if (url.endsWith('/collection-context')) return json({ total: 6, saved: 0, missing: rows })
      if (url === cdn) return new Response('photo', { headers: { 'Content-Type': 'image/jpeg' } })
      uploads.push(url)
      assert.equal(args.method, 'POST')
      return json({ photoId: 2 })
    },
  })
  assert.equal(seen.length, 6)
  assert.equal(uploads.length, 1)
  assert.equal(state.saved, 1)
  assert.equal(state.failed, 5)
  assert.equal(state.processed, 6)
  assert.equal(state.stage, 'partial')
})

test('rendered batch photos avoid per-item navigation and are saved first', async () => {
  let navigations = 0
  const events = []
  const state = await collect({ ...options,
    preparePhotos: async rows => { assert.equal(rows.length, 2); return { '124': cdn } },
    loadPhoto: async row => { navigations++; events.push(row.itemId); return null },
    fetchImpl: async url => {
      if (url.endsWith('/collection-context')) return json({ total: 2, saved: 0, missing: [item, { ...item, itemId: '124', url: 'https://www.avito.ru/item_124' }] })
      if (url === cdn) return new Response('photo', { headers: { 'Content-Type': 'image/jpeg' } })
      events.push('uploaded')
      return json({ photoId: 3 })
    },
  })
  assert.deepEqual(events, ['uploaded', '123'])
  assert.equal(navigations, 1)
  assert.equal(state.saved, 1)
})

test('security challenge stops with an actionable message, not missing-photo error', async () => {
  const state = await collect({ ...options,
    loadPhoto: async () => { const error = new Error('Пройдите проверку безопасности'); error.stop = true; throw error },
    fetchImpl: async () => json({ total: 1, saved: 0, missing: [item] }),
  })
  assert.equal(state.stage, 'paused')
  assert.match(state.message, /проверку безопасности/)
})
