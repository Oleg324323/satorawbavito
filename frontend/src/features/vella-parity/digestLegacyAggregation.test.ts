import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { expect, it } from 'vitest'

const html = readFileSync(new URL('../../../public/vella-production.html', import.meta.url), 'utf8')
const start = 'function aggregateDigestBalanceWeeks(days){'
const end = 'function renderDigestBalance(days = currentDigestBalanceDays){'
if (html.split(start).length !== 2 || html.split(end).length !== 2) throw new Error('Nonunique aggregation boundary')
const aggregate = html.slice(html.indexOf(start), html.indexOf(end))
const sums = html.match(/^function sumNums\(values\)\{[^\n]+$/m)?.[0]
if (!sums) throw new Error('Missing actual sum helper')

// Characterizes the retained standalone renderer, not canonical chart policy.
// Dropping a partial final bucket or losing any metric must fail literal totals.
it.each([
  { days: 0, counts: [], mode: undefined },
  { days: 1, counts: [1], mode: 'day' },
  { days: 14, counts: [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1], mode: 'day' },
  { days: 15, counts: [7, 7, 1], mode: 'week' },
  { days: 30, counts: [7, 7, 7, 7, 2], mode: 'week' },
])('retains every daily metric across $days legacy chart days', ({ days, counts, mode }) => {
  const input = Array.from({ length: days }, (_, index) => {
    const day = String(index + 1).padStart(2, '0')
    return { date: `2026-05-${day}`, label: `${day}.05`, title: `${day}.05.2026, Synthetic`,
      weekday: 'Synthetic', sales: 2, orders: 3, returns: 1 }
  })
  const before = JSON.stringify(input)
  const output = runInNewContext(`${sums}\n${aggregate}\naggregateDigestBalanceWeeks(input)`, { input }, { timeout: 1000 })
  expect(output.map((row: { returns: number }) => row.returns)).toEqual(counts)
  expect(output.map((row: { sales: number }) => row.sales)).toEqual(counts.map(count => count * 2))
  expect(output.map((row: { orders: number }) => row.orders)).toEqual(counts.map(count => count * 3))
  expect(output[0]?.mode).toBe(mode)
  expect(JSON.stringify(input)).toBe(before)
  if (days === 15) {
    expect(output[2]).toMatchObject({ date: '2026-05-15', label: '15-15.05', sales: 2, orders: 3, returns: 1 })
  }
})
