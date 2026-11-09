import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { expect, it } from 'vitest'

const html = readFileSync(new URL('../../../public/vella-production.html', import.meta.url), 'utf8')
function extract(start: string, end: string) {
  if (html.split(start).length !== 2 || html.split(end).length !== 2) throw new Error('Nonunique stock filter boundary')
  return html.slice(html.indexOf(start), html.indexOf(end))
}
const source = extract('function normalizeReportFilterText(value){', 'function managerFilterValue(tab){')
  + extract('function rowMatchesChip(tab, row, chipText){', 'function updateReportEmptyState(table){')

it.each([
  ['Риск нехватки', { daysToOos: '4' }, true],
  // The live Stock BFF uses strictly <7 for risk (and available<=0 independently),
  // strictly >60 for surplus: wb_reports_bff.py warehouse/SKU decisions and OOS KPI.
  // These rows carry no explicit risk/surplus tags; test only numeric evidence.
  ['OOS риск', { daysToOos: '7', availableUnits: '10' }, false],
  ['Риск нехватки', { daysToOos: '6.9', availableUnits: '10' }, true],
  ['Риск нехватки', { daysToOos: '7', availableUnits: '10' }, false],
  ['Риск нехватки', { daysToOos: '7.1', availableUnits: '10' }, false],
  ['Риск нехватки', { daysToOos: '7', availableUnits: '0' }, true],
  ['Риск нехватки', { daysToOos: '0', availableUnits: '10' }, true],
  ['Риск нехватки', { daysToOos: '20' }, false],
  ['Избыток', { daysToOos: '59.9', availableUnits: '10' }, false],
  ['Избыток', { daysToOos: '60', availableUnits: '10' }, false],
  ['Избыток', { daysToOos: '60.1', availableUnits: '10' }, true],
  ['Избыток', { daysToOos: '90' }, true],
  ['Избыток', { daysToOos: '20' }, false],
  ['Избыток', {}, false],
  ['Избыток', { daysToOos: '' }, false],
  ['Риск нехватки', {}, false],
  ['Риск нехватки', { availableUnits: '0' }, true],
  ['Риск нехватки', { availableUnits: '5' }, false],
  ['Риск нехватки', { availableUnits: '' }, false],
  ['Риск нехватки', { daysToOos: ' ', availableUnits: ' ' }, false],
  ['Риск нехватки', { daysToOos: 'unknown', availableUnits: '10' }, false],
  ['Избыток', { daysToOos: 'unknown', availableUnits: '10' }, false],
  ['Топ-склады', { warehouse: 'Казань' }, true],
  ['Топ-склады', { warehouse: 'Екатеринбург' }, false],
  ['В акции', {}, false],
] as const)('matches stock chip %s only with available evidence %j', (chipText, dataset, expected) => {
  const actual = runInNewContext(`${source}\nrowMatchesChip(tab, row, chipText)`, {
    tab: { id: 'tab-stock' }, row: { dataset }, chipText,
  }, { timeout: 1000 })
  expect(actual).toBe(expected)
})
