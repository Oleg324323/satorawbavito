import { afterEach, expect, it, vi } from 'vitest'
import { ApiError, apiRequest } from '@/lib/api'
import { mergeQuarterlyTaxValues, quarterlyTaxError, requestQuarterlyTax } from './quarterlyTaxApi'

vi.mock('@/lib/api', async original => ({ ...await original<typeof import('@/lib/api')>(), apiRequest: vi.fn() }))
vi.mock('@/features/auth/authApi', () => ({ authorizationHeaders: () => ({}) }))
afterEach(() => vi.resetAllMocks())
const view = { year: 2026, rates: [0, null, 7.55, 8], configured: [true, false, true, true], revision: 'a'.repeat(64), timezone: 'Europe/Moscow' }

it('reads the existing plain settings contract, including zero and legacy fallback', async () => {
  vi.mocked(apiRequest).mockResolvedValue(view)
  expect(await requestQuarterlyTax('synthetic', 2026)).toEqual(view)
  expect(apiRequest).toHaveBeenCalledWith('/api/v1/wb-repricer/quarterly-tax?year=2026', expect.objectContaining({ method: 'GET' }))
})

it('saves all four explicit quarter values with the loaded revision', async () => {
  vi.mocked(apiRequest).mockResolvedValue(view)
  await requestQuarterlyTax('synthetic', 2026, { rates: view.rates, revision: view.revision })
  const [url, init] = vi.mocked(apiRequest).mock.calls[0]
  expect(url).toBe('/api/v1/wb-repricer/quarterly-tax')
  expect(init?.method).toBe('PUT')
  expect(JSON.parse(String(init?.body))).toEqual({ year: 2026, rates: [0, null, 7.55, 8], revision: view.revision })
})

it.each([undefined, { data: view }, { ...view, year: 2025 }, { ...view, rates: [0] }, { ...view, rates: [NaN, 0, 0, 0] }])(
  'does not replace invalid or wrong-period settings with zero/defaults', async payload => {
    vi.mocked(apiRequest).mockResolvedValue(payload)
    await expect(requestQuarterlyTax('synthetic', 2026)).rejects.toThrow('неполные налоговые настройки')
  })

it('preserves typed quarters on retry while refreshing unedited quarters and the revision', () => {
  expect(mergeQuarterlyTaxValues(['9,25', '6', '', '0'], [6, 7, 8, 9], [true, false, false, true]))
    .toEqual(['9,25', '7', '8', '0'])
  expect(mergeQuarterlyTaxValues(['6', '6', '6', '6'], view.rates, [false, false, false, false]))
    .toEqual(['0', '', '7.55', '8'])
})

it('distinguishes permission denial, version conflict and temporary failure', () => {
  const denied = new ApiError('synthetic', 403)
  expect(quarterlyTaxError(denied)).toContain('просмотра')
  expect(quarterlyTaxError(denied, true)).toContain('изменения')
  expect(quarterlyTaxError(new ApiError('synthetic', 409), true)).toContain('другом сеансе')
  expect(quarterlyTaxError(new Error('synthetic'), true)).toContain('правки оставлены')
})

it('propagates abort and failed requests rather than creating settings', async () => {
  const controller = new AbortController(), error = new DOMException('cancelled', 'AbortError')
  vi.mocked(apiRequest).mockRejectedValue(error)
  await expect(requestQuarterlyTax('synthetic', 2026, undefined, controller.signal)).rejects.toBe(error)
  expect(vi.mocked(apiRequest).mock.calls[0][1]?.signal).toBe(controller.signal)
})
