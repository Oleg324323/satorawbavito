import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, expect, it, vi } from 'vitest'
import { WeekTableShellIsland, WeekWorkbenchIsland } from './VellaHtmlParityPage'
import { formatWeekMoney, WEEK_FINANCIAL_COLUMNS } from './weekFinancialColumns'

afterEach(() => vi.restoreAllMocks())

it('counts known losses without revenue once, excludes account rows and preserves other attention reasons', () => {
  const rows = [
    { nmId: 101, sales: { kopecks: 0 }, profit: { kopecks: -7904 }, marginPct: { percent: null } },
    { nmId: '00101', profit: { kopecks: -7904 } },
    { nmId: 102, profit: { kopecks: null } },
    { nmId: 103, profit: { kopecks: 0 } },
    { nmId: 104, marginPct: { percent: -10 } },
    { nmId: 105, sales: { deltaPct: -1 } },
    { nmId: 106, wasOutOfStock: true },
    { nmId: null, isUnallocated: true, profit: { kopecks: -1000 }, marginPct: { percent: -10 } },
  ]
  const html = renderToStaticMarkup(createElement(WeekWorkbenchIsland, {
    replacementKey: 'attention', state: { status: 'ready', report: { rows } }, marginUnit: 'п.п.',
  }))
  expect(html).toMatch(/Требуют внимания[\s\S]*?stat-val[^>]*>4<\/div>/)
  const lossOnly = renderToStaticMarkup(createElement(WeekWorkbenchIsland, {
    replacementKey: 'losses', state: { status: 'ready', report: { rows: rows.slice(0, 4) } }, marginUnit: 'п.п.',
  }))
  expect(lossOnly).toMatch(/Требуют внимания[\s\S]*?stat-val[^>]*>1<\/div>/)
})

it('describes financial commission and advertising, not seller list price or campaign fullstats', () => {
  const tip = (key: string) => WEEK_FINANCIAL_COLUMNS.find(column => column.key === key)!.tip
  expect(tip('commissionKopecks')).toContain('retailAmount − forPay − acquiringFee')
  expect(tip('commissionKopecks')).not.toContain('retailPriceWithDisc ×')
  expect(tip('adSpendKopecks')).toContain('deduction')
  expect(tip('adSpendKopecks')).toContain('rrDate')
  expect(tip('adSpendKopecks')).not.toContain('/adv/v3/fullstats')
})

type WeekRows = NonNullable<Extract<Parameters<typeof WeekTableShellIsland>[0]['state'], { status: 'ready' }>['report']['rows']>

function render(rows: WeekRows, exportAll = false) {
  vi.spyOn(console, 'error').mockImplementation(() => undefined)
  return renderToStaticMarkup(createElement(WeekTableShellIsland, {
    replacementKey: 'week-finances', state: { status: 'ready', report: { rows } }, marginUnit: 'п.п.', exportAll,
  }))
}

it('renders all financial columns, both periods and signed rubles without changing profit', () => {
  const values = Object.fromEntries(WEEK_FINANCIAL_COLUMNS.map((column, index) => [column.key,
    { kopecks: index === 0 ? -123 : index === 1 ? 0 : index === 2 ? null : 456,
      previousKopecks: 789, deltaPct: -12.3 }]))
  const html = render([{ nmId: 101, productName: 'Directly matched product', profit: { kopecks: 700 }, ...values }])
  expect((html.match(/<th[ >]/g) ?? [])).toHaveLength(12 + WEEK_FINANCIAL_COLUMNS.length)
  expect((html.match(/<td[ >]/g) ?? [])).toHaveLength(12 + WEEK_FINANCIAL_COLUMNS.length)
  for (const column of WEEK_FINANCIAL_COLUMNS) expect(html).toContain(column.label)
  expect(html).toContain('<strong>-1,23 ₽</strong>')
  expect(html).toContain('<strong>0 ₽</strong>')
  expect(html).toContain('<strong>—</strong>')
  expect(html).toContain('Пред.: 7,89 ₽')
  expect(html).toContain('<strong>7 ₽</strong>')
  expect(html).toContain('Directly matched product')
  expect(html).toContain('WB 101')
})

it('shows already loaded campaign spend separately from unattributed settlement advertising', () => {
  const html = render([{ nmId: 867213853, productName: 'Campaign product',
    campaignAdSpendKopecks: { kopecks: 299994, previousKopecks: 353045, deltaPct: -15 },
    adSpendKopecks: { kopecks: null, previousKopecks: null },
    storageKopecks: { kopecks: null, previousKopecks: null },
    storageCoverage: { selected: 'missing', previous: 'missing' },
  }])
  expect(html).toContain('Расход кампаний, ₽')
  expect(html.replace(/\s/g, ' ')).toContain('2 999,94 ₽')
  expect(html.replace(/\s/g, ' ')).toContain('3 530,45 ₽')
  expect(html).toContain('Неполные данные хранения: нет полной истории остатков или финансовых данных')
  expect(html).toContain('<strong>—</strong>')
})

it('uses identical financial cells when mounting every row for filtered/sorted XLSX export', () => {
  const rows = Array.from({ length: 135 }, (_, index) => ({ nmId: 1000 + index,
    productName: `Product ${index}`, penaltyKopecks: { kopecks: -123, previousKopecks: 456, deltaPct: null } }))
  const windowHtml = render(rows)
  const exportHtml = render(rows, true)
  expect((windowHtml.match(/data-report-row="week"/g) ?? []).length).toBeLessThan(135)
  expect((exportHtml.match(/data-report-row="week"/g) ?? [])).toHaveLength(135)
  const cell = '<strong>-1,23 ₽</strong><span class="subline">Пред.: 4,56 ₽'
  expect(windowHtml).toContain(cell)
  expect(exportHtml.split(cell)).toHaveLength(136)
  expect(exportHtml).toContain('Product 134')
})

it('keeps missing distinct from zero and finite amounts precise to kopecks', () => {
  expect(formatWeekMoney(0)).toBe('0 ₽')
  expect(formatWeekMoney(null)).toBe('—')
  expect(formatWeekMoney(undefined)).toBe('—')
  expect(formatWeekMoney(Number.NaN)).toBe('—')
  expect(formatWeekMoney(-123)).toBe('-1,23 ₽')
  expect(formatWeekMoney(1234567).replace(/\s/g, ' ')).toBe('12 345,67 ₽')
})

it('shows confirmed selected-period profit and margin without inventing a prior comparison', () => {
  const html = render([{ nmId: 867213853, productName: 'Confirmed financial product',
    marginPct: { percent: 27.01, deltaPct: null },
    profit: { kopecks: 4387710, deltaPct: null } }])
  expect(html).toContain('<strong>27.01%</strong>')
  expect(html.replace(/\u00a0/g, ' ')).toContain('<strong>43 877 ₽</strong>')
  expect(html).not.toContain('+0%')
})

it('renders orders before buyouts with identical values in table and export', () => {
  const rows = [{ nmId: 101, orders: { units: 12, kopecks: 60000, deltaPct: 100 },
    sales: { units: 3, kopecks: 15000, deltaPct: 50 }, baskets: { units: 20 },
    storageKopecks: { kopecks: 5000, previousKopecks: null } }]
  for (const html of [render(rows), render(rows, true)]) {
    const headers = [...html.matchAll(/<th\b[^>]*>([\s\S]*?)<\/th>/g)].map(match => match[1])
    expect(headers[1]).toContain('Заказы')
    expect(headers[2]).toContain('Выкупили')
    expect(headers[2]).toContain('rrDate')
    expect(headers[2]).toContain('за вычетом возвратов')
    expect(headers[2]).not.toContain('WB Sales Funnel')
    expect(headers.join('')).not.toContain('Продажи')
    const cells = [...html.matchAll(/<td\b[^>]*>([\s\S]*?)<\/td>/g)].map(match => match[1])
    expect(cells[1]).toContain('12 шт')
    expect(cells[1]).toContain('600 ₽')
    expect(cells[2]).toContain('3 шт')
    expect(cells[2]).toContain('150 ₽')
    expect(cells[3]).toContain('20')
    expect(cells[8]).toContain('50 ₽')
  }
})
