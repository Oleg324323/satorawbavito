import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { AvitoRepricerMetric, AvitoRepricerPhoto } from './AvitoRepricerMetric'

describe('Avito repricer presentation without a browser', () => {
  it('shows only active and blocked listing counts in the summary', () => {
    const source = readFileSync(new URL('./VellaHtmlParityPage.tsx', import.meta.url), 'utf8')
    const strip = source.slice(source.indexOf('function AvitoRepricerKpiStripIsland('), source.indexOf('function AvitoRepricerToolbarIsland('))
    expect(strip).toContain('Активные объявления')
    expect(strip).toContain('Заблокированные')
    expect(strip).not.toContain('Сумма цен активных')
    expect(strip).not.toContain('activePriceKopecks')
    expect(strip).toContain('repeat(2, minmax(180px, 1fr))')
  })
  it('shows item-specific arrows and keeps missing comparison distinct from zero', () => {
    expect(renderToStaticMarkup(createElement(AvitoRepricerMetric, { value: 12, trend: { direction: 'up', delta: 2, percent: 20 } }))).toContain('↑ +2 (+20%)')
    expect(renderToStaticMarkup(createElement(AvitoRepricerMetric, { value: 12, trend: { direction: 'down', delta: -2, percent: -20 } }))).toContain('↓ -2 (-20%)')
    expect(renderToStaticMarkup(createElement(AvitoRepricerMetric, { value: 12, trend: { direction: 'up', percent: 20 } }))).toContain('Нет сравнения')
    expect(renderToStaticMarkup(createElement(AvitoRepricerMetric, { value: 0 }))).toContain('Нет сравнения')
  })
  it('renders uncropped 66px photos and honest fallback', () => {
    const html = renderToStaticMarkup(createElement(AvitoRepricerPhoto, { url: 'https://example.test/photo.jpg', title: 'Товар' }))
    expect(html).toContain('object-fit:contain')
    expect(html).toContain('width:66px')
    expect(renderToStaticMarkup(createElement(AvitoRepricerPhoto, { title: 'Товар' }))).toContain('Фото не получено')
  })
  it('reserves the full photo width plus a gap and lets long listing text wrap', () => {
    const source = readFileSync(new URL('../../../public/vella-production.html', import.meta.url), 'utf8')
    const cell = source.match(/#tab-avito-repricer \.avito-item-cell\s*\{([^}]+)\}/)?.[1] ?? ''
    const title = source.match(/#tab-avito-repricer \.avito-title-cell\s*\{([^}]+)\}/)?.[1] ?? ''
    const html = renderToStaticMarkup(createElement(AvitoRepricerPhoto, { title: 'Длинное название' }))
    const photoWidth = Number(html.match(/width:(\d+)px/)?.[1])
    const columnWidth = Number(cell.match(/grid-template-columns:\s*(\d+)px/)?.[1])
    expect(columnWidth).toBeGreaterThanOrEqual(photoWidth)
    expect(cell).toContain('minmax(0, 1fr)')
    expect(cell).toContain('gap: 12px')
    expect(title).toContain('min-width: 0')
    expect(title).toContain('overflow-wrap: anywhere')
  })
  it('keeps requested columns and removes superseded columns', () => {
    const source = readFileSync(new URL('./VellaHtmlParityPage.tsx', import.meta.url), 'utf8')
    const table = source.slice(source.indexOf('function AvitoRepricerIsland('), source.indexOf('async function loadLiveAvitoStats('))
    expect(table).toContain('<th className="num">Чаты</th>')
    expect(table).toContain('value={row.contactsMessenger}')
    expect(table).toContain("data-sort-value={row.contactsMessenger ?? ''}")
    expect(table).toContain("data-sort-value={row.views ?? ''}")
    expect(table).not.toContain('value={row.contacts} trend={row.contactsTrend}')
    expect(table).toContain('CR в заказ')
    expect(table).toContain('Средняя цена контакта')
    expect(table).toContain('Расходы на объявление')
    expect(table).not.toContain('<th>Решение</th>')
    expect(table).not.toContain('>Написали</th>')
    expect(table).not.toContain('<th>Основание</th>')
    expect(table).toContain('colSpan={13}')
  })
})
