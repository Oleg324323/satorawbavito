import test from 'node:test'
import assert from 'node:assert/strict'
import vm from 'node:vm'
import { readFileSync } from 'node:fs'

const source = readFileSync(new URL('../src/background.js', import.meta.url), 'utf8')
const start = source.indexOf('async function uploadOrderPhotos()')
const end = source.indexOf('\nasync function notifyUploadStatus(', start)

test('order photo HTTP 429 stops further work and leaves all missing for retry', async () => {
  const missing = Array.from({ length: 20 }, (_, index) => ({ accountId: '', itemId: String(index + 1), imageUrl: `https://b00.img.avito.st/${index + 1}.jpg` }))
  let imageRequests = 0
  let imports = 0
  const context = vm.createContext({
    readSettings: async () => ({ accessToken: 'synthetic' }),
    apiUrlFromSettings: async () => 'https://satorna-wb.vercel.app',
    authorizationValue: () => 'Bearer synthetic',
    SatornaListingPhotos: { imageUrl: value => value },
    AbortSignal,
    URLSearchParams,
    fetch: async url => {
      if (url.endsWith('/orders/collection-context')) return { ok: true }
      if (url.includes('/import?')) { imports += 1; throw new Error('should not import') }
      imageRequests += 1
      return { status: 429, ok: false }
    },
    readApiJson: async () => ({ total: 20, missing }),
  })
  vm.runInContext(source.slice(start, end), context)
  const result = await context.uploadOrderPhotos()
  assert.ok(imageRequests >= 1 && imageRequests <= 5)
  assert.equal(imports, 0)
  assert.equal(result.saved, 0)
  assert.equal(result.missing, 20)
  assert.match(result.errors[0], /HTTP 429/)
})
