import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import ts from 'typescript'
import { describe, expect, it } from 'vitest'

const page = readFileSync(new URL('./VellaHtmlParityPage.tsx', import.meta.url), 'utf8')
const start = page.indexOf('export async function loadLatestReportCache<T>')
const end = page.indexOf('\nfunction removeLegacyReportTableFallbacks', start)
const compiled = ts.transpileModule(page.slice(start, end).replace('export async', 'async'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None },
}).outputText

async function load(reports: Array<object>, payload: object = { state: 'completed' }) {
  const notices: string[] = []
  let starts = 0
  const loader = runInNewContext(`${compiled}; loadLatestReportCache`, {
    requestLatestReportCache: async () => reports.shift(),
    reportRefreshNotice: (_id: string, message: string) => notices.push(message),
    authorizationHeaders: () => ({}),
    backgroundReportJobStartPath: () => '/jobs',
    reportCacheRequest: async () => { starts += 1; return payload },
    describePnlReportJob: (job: object) => ({ ...job, terminal: true }),
    shouldPollBackgroundReportJob: () => false,
    ApiError: Error,
  })
  const result = await loader('pnl', 'pnl', 'synthetic', 'sku', {
    fromIso: '2026-09-09', toIso: '2026-09-16',
  }, 'financial')
  return { result, notices, starts }
}

describe('report refresh notice lifecycle', () => {
  it('clears the previous notice on an initially fresh cache without starting a job', async () => {
    const fresh = { cache: { fresh: true }, rows: [{ revenueKopecks: 123 }] }
    expect(await load([fresh])).toEqual({ result: fresh, notices: [''], starts: 0 })
  })

  it('clears the notice after a stale preview is replaced by the fresh calculation', async () => {
    const fresh = { cache: { fresh: true }, rows: [{ revenueKopecks: 456 }] }
    const result = await load([{ cache: { fresh: false } }, fresh], {
      state: 'idle', label: 'Данные отчёта изменились. Требуется обновить расчёт.',
    })
    expect(result.result).toEqual(fresh)
    expect(result.notices.at(-1)).toBe('')
  })

  it('keeps the saved data and actionable notice when rebuilding fails', async () => {
    const saved = { cache: { fresh: false }, rows: [{ revenueKopecks: 123 }] }
    const result = await load([saved], { state: 'failed', error: 'WB HTTP 429' })
    expect(result.result).toEqual(saved)
    expect(result.notices.at(-1)).toContain('WB HTTP 429')
  })

  it('does not call an unfinished calculation successful when the polling budget expires', async () => {
    const saved = { cache: { fresh: false }, rows: [{ revenueKopecks: 123 }] }
    const result = await load([saved], { state: 'running' })
    expect(result.result).toEqual(saved)
    expect(result.notices.at(-1)).toContain('Расчёт не завершён')
    expect(result.starts).toBe(1)
  })
})
