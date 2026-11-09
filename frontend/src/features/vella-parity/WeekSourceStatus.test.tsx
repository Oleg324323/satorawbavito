import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it } from 'vitest'
import { WeekSourceStatus, weekWarningGroups } from './WeekSourceStatus'
import { financeBlockerReasons } from '../wb-finance/canonicalAbcPnl'

it('deduplicates exact source messages and separates period details without discarding diagnostics', () => {
  const warning = 'Выбранный период: нет истории. Предыдущий период: нет рекламы. Выбранный период: нет истории. Сравнение неполное: comparison-pending'
  expect(weekWarningGroups(warning)).toEqual({ selected: ['Выбранный период: нет истории.'],
    previous: ['Предыдущий период: нет рекламы.'], other: ['Сравнение неполное: comparison-pending'] })
  const html = renderToStaticMarkup(createElement(WeekSourceStatus, { warning }))
  expect(html).toContain('<details>'); expect(html).not.toContain('<details open')
  expect(html).toContain('comparison-pending')
  expect(html).toContain('Прочерк не означает ноль')
  expect(renderToStaticMarkup(createElement(WeekSourceStatus, { warning: '' }))).toBe('')
})

it('does not mislabel missing mapping, assumed costs and daily reconciliation as a date entry request', () => {
  const reasons = financeBlockerReasons(['WB_PNL_COST_MAPPING_MISSING', 'WB_PNL_COST_ASSUMED',
    'WB_PNL_COST_DAILY_COVERAGE_MISMATCH', 'WB_PNL_TAX_UNCONFIRMED'])
  expect(reasons.join(' ')).toContain('не связан')
  expect(reasons.join(' ')).toContain('примерная')
  expect(reasons.join(' ')).toContain('не сходятся')
  expect(reasons.join(' ')).not.toContain('Уточните себестоимость и её даты')
})
