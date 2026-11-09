import { readFileSync } from 'node:fs'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import { AbcKpiStripIsland } from './VellaHtmlParityPage'

const source = readFileSync(new URL('./VellaHtmlParityPage.tsx', import.meta.url), 'utf8')

describe('ABC auth and help source', () => {
  it('removes the manual refresh action only from ABC and keeps loading/error states', () => {
    const abcPage = source.slice(source.indexOf('function AbcReportIsland('), source.indexOf('const shellReplacements'))
    expect(abcPage).not.toContain('ReportSourcesRefreshBar')
    expect(abcPage).toContain('state.report?.warning')
    expect(abcPage).toContain('Готовим отчет по товарам')
    expect(source).toContain('reportId="digest"')
  })
  it('shows a reauthorization panel and hides report blocks when ABC auth token is expired', () => {
    expect(source).toContain('function isAbcAuthExpiredError')
    expect(source).toContain('window.__vellaAbcLiveAuthExpired = authExpired')
    expect(source).toContain('data-vella-island="abc-auth-expired-panel"')
    expect(source).toContain('Переавторизоваться')
  })

  it('hides existing report KPIs on expired authorization, not just on an empty report', () => {
    const state = {
      __vellaAbcLiveLoading: false,
      __vellaAbcLiveAuthExpired: false,
      __vellaAbcLiveRows: [],
      __vellaAbcLiveReport: {
        filteredSummary: { attentionCount: 2, ordersCount: 0, profitKopecks: 0 },
        rows: [{ abcCode: 'AA', salesComposite: { kopecks: 0 } }],
      },
    }
    vi.stubGlobal('window', state)
    try {
      const render = () => renderToStaticMarkup(createElement(AbcKpiStripIsland, { replacementKey: 'auth-test' }))
      expect(render()).toContain('Требуют внимания')
      state.__vellaAbcLiveAuthExpired = true
      expect(render()).toBe('')
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('adds customer-facing help and formulas to ABC KPIs and table headers', () => {
    expect(source).toContain('help: \'Две буквы: первая по доле продаж')
    expect(source).toContain('Формула: продажи минус себестоимость')
    expect(source).toContain('Конверсия корзины в заказ. Формула: заказы / корзины * 100%.')
    expect(source).toContain('className="stat-tip abc-header-help"')
    expect(source).toContain('data-tip={stat.help}')
  })
})
