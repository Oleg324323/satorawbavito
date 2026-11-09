import fs from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

const reportsPageSource = fs.readFileSync(path.join(process.cwd(), 'src/features/wb-reports/WbReportsPage.tsx'), 'utf8')
const rulesPageSource = fs.readFileSync(path.join(process.cwd(), 'src/features/wb-reports/WbReportRulesPage.tsx'), 'utf8')
const parityPageSource = fs.readFileSync(path.join(process.cwd(), 'src/features/vella-parity/VellaHtmlParityPage.tsx'), 'utf8')

describe('WB reports user-facing copy', () => {
  it('keeps technical loading and source diagnostics out of the report UI', () => {
    expect(reportsPageSource).not.toMatch(/Загружаем данные отчета с backend|Запрашиваем экспорт на backend|по текущим backend данным/i)
    expect(reportsPageSource).not.toMatch(/exact SKU|campaign-SKU|campaign-only|audit trail|write-confirmation|Phase 2/i)
  })

  it('uses user-facing wording on the report rules page', () => {
    expect(rulesPageSource).not.toMatch(/Preview impact|только preview|preview по|audit trail|draft отличается/i)
  })

  it('keeps ABC and digest report copy free of internal labels', () => {
    expect(parityPageSource).not.toMatch(/Текущий срез|Сводка пересчитана|Сводка по строкам/i)
    expect(parityPageSource).not.toMatch(/Источник: backend digest|Ads attribution|Stop\/action|требуют review/i)
    expect(parityPageSource).not.toMatch(/В проекте нет активных менеджеров с рабочим профилем/i)
  })

  it('keeps RNP and P&L loading states user-facing', () => {
    expect(parityPageSource).not.toMatch(/Загружаем РНП с бэкенда|Backend вернул пустой РНП|Загружаем P&L с бэкенда|Бэк вернул пустой P&L/i)
    expect(parityPageSource).not.toMatch(/Нет access token для запроса РНП API|Нет access token для запроса P&L API|Точечно обновить WB-источники|Запросить 1С ДДС/i)
  })
})
