import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it } from 'vitest'
import { WeekWorkbenchIsland } from './VellaHtmlParityPage'

type Report = Extract<Parameters<typeof WeekWorkbenchIsland>[0]['state'], { status: 'ready' }>['report']
const render = (report: Report) => renderToStaticMarkup(createElement(WeekWorkbenchIsland, {
  replacementKey: 'coverage', state: { status: 'ready', report }, marginUnit: '%',
})).split('Выкупили')[0]

it('does not headline a surviving -100% subset as a complete comparison', () => {
  const html=render({rows:[
    {nmId:101,orders:{units:0,previousUnits:4,kopecks:0,deltaPct:-100}},
    {nmId:102,orders:{units:0,previousUnits:3,kopecks:0,deltaPct:-100}},
    {nmId:103,orders:{units:null,previousUnits:10,kopecks:null,deltaPct:null}},
  ]})
  expect(html).not.toContain('-100%')
  expect(html).toContain('Сравнение неполное: 2 из 3')
})

it('excludes non-product account rows from orders totals and preserves a true -100%', () => {
  const html=render({rows:[
    {nmId:101,orders:{units:0,previousUnits:3,kopecks:0,deltaPct:-100}},
    {isUnallocated:true,nmId:null,orders:{units:null,kopecks:null}},
  ]})
  expect(html).toContain('-100%')
  expect(html).toContain('0 шт · 0 ₽')
  expect(html).not.toContain('Сравнение неполное')
})

it('does not suppress a complete applicable total because a financial-only product has no funnel row', () => {
  const html=render({rows:[
    {nmId:101,funnelPopulation:{selected:true,previous:true},orders:{units:52,previousUnits:64,kopecks:9360000,deltaPct:-18.8}},
    {nmId:102,funnelPopulation:{selected:false,previous:false},orders:{units:null,kopecks:null}},
  ]})
  expect(html.replace(/\s/g,' ')).toContain('52 шт · 93 600 ₽')
  expect(html).not.toContain('Сравнение неполное')
})

it('keeps unknown applicable products unknown but excludes an explicit zero previous denominator', () => {
  const html=render({rows:[
    {nmId:101,orders:{units:5,previousUnits:0,kopecks:500,deltaPct:null}},
    {nmId:102,orders:{units:2,previousUnits:1,kopecks:200,deltaPct:100}},
    {nmId:103,orders:{units:null,previousUnits:2,kopecks:null,deltaPct:null}},
  ]})
  expect(html).toContain('Сравнение неполное: 1 из 2')
  expect(html).not.toContain('7 шт')
})

it('does not claim totals or complete comparison when seller population is incomplete', () => {
  const html=render({metricCoverage:{selected:{funnelPopulationComplete:false},previous:{funnelPopulationComplete:true}},
    rows:[{nmId:101,orders:{units:2,previousUnits:1,kopecks:200,deltaPct:100}}]})
  expect(html).not.toContain('+100%')
  expect(html).not.toContain('2 шт')
  expect(html).toContain('Сравнение неполное')
})

it('preserves confirmed empty seller totals without inferring zero for an unknown population', () => {
  const complete=render({metricCoverage:{selected:{funnelPopulationComplete:true},previous:{funnelPopulationComplete:true}},
    rows:[{isUnallocated:true,nmId:null}]})
  const unknown=render({rows:[{isUnallocated:true,nmId:null}]})
  expect(complete).toContain('0 шт · 0 ₽')
  expect(unknown).not.toContain('0 шт')
})
