import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { OrdersPrintListPage } from './OrdersPrintListPage'

describe('print list without marking prototype', () => {
  it('preserves the order, product and WB sticker columns without the unused marking column', () => {
    const html = renderToStaticMarkup(createElement(OrdersPrintListPage))
    expect(html).toContain('WB-184902')
    expect(html).toContain('FBBT_42')
    expect(html).toContain('стикер WB готов')
    expect(html).toContain('Кол-во')
    expect(html).not.toContain('КИЗ')
    expect(html).not.toContain('поле под внешний код')
  })
})
