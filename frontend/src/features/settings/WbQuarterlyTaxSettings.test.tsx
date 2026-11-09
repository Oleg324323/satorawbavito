import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import { parseQuarterlyTaxRates, WbQuarterlyTaxSettings } from './WbQuarterlyTaxSettings'

vi.mock('@/features/auth/authContext', () => ({ useAuth: () => ({
  accessToken: null,
  cabinetMe: { organization: { organizationId: 1 }, user: { userId: 'synthetic', permissions: ['settings:read'] } },
}) }))

describe('WB quarterly tax profile', () => {
  it('keeps zero, missing and fractional values distinct', () => {
    expect(parseQuarterlyTaxRates(['0', '', '7.55', '8,25'])).toEqual([0, null, 7.55, 8.25])
  })
  it.each(['-1', '101', '7.555', 'NaN', 'Infinity', 'abc', '6e1'])('rejects invalid percentage %s', value => {
    expect(() => parseQuarterlyTaxRates([value, '6', '6', '6'])).toThrow()
  })
  it('renders four accessible labels, calendar year, months and no fabricated default', () => {
    const html = renderToStaticMarkup(createElement(WbQuarterlyTaxSettings))
    for (const roman of ['I', 'II', 'III', 'IV']) expect(html).toContain(`Налог — ${roman} квартал, %`)
    for (const months of ['Январь–март', 'Апрель–июнь', 'Июль–сентябрь', 'Октябрь–декабрь']) expect(html).toContain(months)
    expect(html).toContain('Календарный год')
    expect(html).toContain('Москва')
    expect(html.match(/placeholder="Не настроен"/g)).toHaveLength(4)
    expect(html).not.toContain('value="6"')
    expect(html).toContain('disabled=""')
  })
})
