import { afterEach, expect, it, vi } from 'vitest'
import { announceConfirmedPrice, CONFIRMED_PRICE_EVENT, watchPriceRefresh } from './priceRefresh'

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

it('refreshes visible idle pages, avoids duplicate reads and cleans up on navigation', async () => {
  vi.useFakeTimers()
  const doc = Object.assign(new EventTarget(), { visibilityState: 'visible' })
  const win = Object.assign(new EventTarget(), { setInterval, clearInterval })
  vi.stubGlobal('document', doc); vi.stubGlobal('window', win)
  let resolve = () => {}, ready = true
  const read = vi.fn(() => new Promise<void>(done => { resolve = done }))
  const errors = vi.fn()
  const stop = watchPriceRefresh(read, () => ready, errors)
  await vi.advanceTimersByTimeAsync(60_000)
  expect(read).toHaveBeenCalledTimes(1)
  await vi.advanceTimersByTimeAsync(120_000)
  win.dispatchEvent(new Event('focus'))
  expect(read).toHaveBeenCalledTimes(1)
  resolve(); await Promise.resolve()
  doc.visibilityState = 'hidden'
  await vi.advanceTimersByTimeAsync(60_000)
  expect(read).toHaveBeenCalledTimes(1)
  doc.visibilityState = 'visible'; ready = false
  doc.dispatchEvent(new Event('visibilitychange'))
  expect(read).toHaveBeenCalledTimes(1)
  ready = true; win.dispatchEvent(new Event('focus'))
  expect(read).toHaveBeenCalledTimes(2)
  resolve(); await Promise.resolve()
  stop(); await vi.advanceTimersByTimeAsync(120_000)
  expect(read).toHaveBeenCalledTimes(2)
  expect(errors).not.toHaveBeenCalled()
})

it('reports a failed refresh without spinning or swallowing the error', async () => {
  vi.useFakeTimers()
  vi.stubGlobal('document', Object.assign(new EventTarget(), { visibilityState: 'visible' }))
  vi.stubGlobal('window', Object.assign(new EventTarget(), { setInterval, clearInterval }))
  const error = new Error('DB unavailable'), read = vi.fn().mockRejectedValue(error), report = vi.fn()
  const stop = watchPriceRefresh(read, () => true, report)
  await vi.advanceTimersByTimeAsync(60_000)
  expect(report).toHaveBeenCalledWith(error)
  expect(read).toHaveBeenCalledTimes(1)
  stop()
})

it('refreshes promptly only for verified receipts and coalesces confirmations during a read', async () => {
  vi.useFakeTimers()
  vi.stubGlobal('document', Object.assign(new EventTarget(), { visibilityState: 'visible' }))
  const win = Object.assign(new EventTarget(), { setInterval, clearInterval })
  vi.stubGlobal('window', win)
  let finish = () => {}
  const read = vi.fn(() => new Promise<void>(resolve => { finish = resolve }))
  const stop = watchPriceRefresh(read, () => true, vi.fn())
  for (const state of ['queued', 'accepted', 'failed', 'blocked', 'local_applied']) announceConfirmedPrice({ job: { state } })
  expect(read).not.toHaveBeenCalled()
  announceConfirmedPrice({ job: { state: 'accepted', lastApplyResult: { priceConfirmed: true } } })
  expect(read).toHaveBeenCalledTimes(1)
  win.dispatchEvent(new Event(CONFIRMED_PRICE_EVENT)); win.dispatchEvent(new Event(CONFIRMED_PRICE_EVENT))
  expect(read).toHaveBeenCalledTimes(1)
  finish(); await Promise.resolve(); await Promise.resolve()
  expect(read).toHaveBeenCalledTimes(2)
  finish(); await Promise.resolve(); stop()
  win.dispatchEvent(new Event(CONFIRMED_PRICE_EVENT))
  expect(read).toHaveBeenCalledTimes(2)
})
