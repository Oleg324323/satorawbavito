import assert from 'node:assert/strict'
import test from 'node:test'

test('reinstalls the page-state inspector after the Avito MAIN context disappears', async () => {
  delete globalThis.SatornaRuntimeRetry
  await import(`../src/runtime-retry.js?red=${Date.now()}`).catch(() => {})

  const calls = []
  const result = await globalThis.SatornaRuntimeRetry?.retryPageStateInspection?.(
    async (attempt) => {
      calls.push(attempt)
      return attempt === 1
        ? { apiAvailable: false, candidates: [] }
        : { apiAvailable: true, candidates: [{ itemId: '8226657890' }] }
    },
    3,
    async () => {},
  )

  assert.deepEqual(calls, [1, 2])
  assert.equal(result?.inspectorAttempts, 2)
  assert.equal(result?.candidates[0]?.itemId, '8226657890')
})
