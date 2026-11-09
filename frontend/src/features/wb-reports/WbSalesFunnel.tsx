import { useEffect, useRef, useState } from 'react'
import { authorizationHeaders } from '@/features/auth/authApi'
import { reportRequest } from './reportRequest'
import { formatAggregateFunnelDynamic, formatNativeFunnelDynamic, previousFunnelPeriod } from './funnelDynamics'
import './wbSalesFunnel.css'

export const funnelColumns = [
  ['revenueShare', 'Доля в выручке, %', 'percent'],
  ['visits', 'Перешли в карточку', 'count'],
  ['carts', 'Положили в корзину', 'count'],
  ['cartConversion', 'Конверсия в корзину, %', 'percent'],
  ['orders', 'Заказали товаров, шт.', 'count'],
  ['orderConversion', 'Конверсия в заказ, %', 'percent'],
  ['orderAmount', 'Заказали на сумму, ₽', 'amount'],
  ['buyouts', 'Выкупили товаров, шт.', 'count'],
  ['buyoutPercent', 'Процент выкупа, %', 'percent'],
  ['buyoutAmount', 'Выкупили на сумму, ₽', 'amount'],
] as const

type Metrics = Record<string, number | null | boolean>
type Job = { status: string; message?: string; settled?: boolean }
const selectedCollectionFinished = (job?: Job | null) => !job || ['complete', 'partial'].includes(job.status)
export type FunnelPage = {
  items: { nmId: string; title: string | null; sellerArticle: string | null; imageUrl: string | null; metrics: Metrics; inReport?: boolean | null }[]
  totals: Metrics; total: number; page: number; pageSize: number; complete: boolean
  coveredProducts: number; brands: string[]; dateFrom: string; dateTo: string
  unavailable: Record<string, string>; job?: Job | null
  refreshRequired?: boolean; unreportedProducts?: number; reportComplete?: boolean
  summaryComplete?: boolean; summaryProducts?: number
  catalogState?: 'ready' | 'missing' | 'empty'
  conversionCoverage?: { cartConversion: { products: number; total: number; visits: number | null; carts: number | null } }
}
export type FunnelComparison = Omit<FunnelPage, 'items'> & {
  source: 'wb_sales_funnel_comparison_v1'
  items: { nmId: string; metrics: Metrics; dynamics: Metrics; selectedValues: Metrics; ready: Record<string, boolean> }[]
  selectedDateFrom: string; selectedDateTo: string
  totalsUnavailable?: string
}
export type FunnelTotalsComparison = Pick<FunnelPage, 'dateFrom' | 'dateTo' | 'total' | 'complete' | 'refreshRequired' | 'job'> & {
  source: 'calculated_wb_sales_funnel_totals_v1'
  selectedDateFrom: string; selectedDateTo: string
  totals: Metrics; selectedTotals: Metrics; previousTotals: Metrics
  ready: Record<string, boolean>; unavailable: Record<string, string>
  coverage: Record<string, { products: number; matchedProducts: number }>
}

export function formatFunnelValue(value: unknown, kind = 'count') {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—'
  const number = kind === 'amount' ? value / 100 : value
  const text = number.toLocaleString('ru-RU', { maximumFractionDigits: kind === 'count' ? 0 : 2 })
  return kind === 'amount' ? `${text} ₽` : kind === 'percent' ? `${text}%` : text
}

export function funnelPeriodMatches(data: Pick<FunnelPage, 'dateFrom' | 'dateTo'>, from: string, to: string) {
  return data.dateFrom === from.slice(0, 10) && data.dateTo === to.slice(0, 10)
}

export function funnelNeedsRefresh(data: Pick<FunnelPage, 'total' | 'refreshRequired' | 'complete'>) {
  return data.refreshRequired ?? (data.total > 0 && !data.complete)
}

export function FunnelChange({ value, previous, ready, period, kind = 'count', pending = false, unavailableReason, calculated = false, products }: {
  value: unknown; previous?: unknown; ready: boolean; period: { dateFrom: string; dateTo: string } | null; kind?: string; pending?: boolean; unavailableReason?: string; calculated?: boolean; products?: number
}) {
  const delta = (calculated ? formatAggregateFunnelDynamic : formatNativeFunnelDynamic)(value, ready)
  const dates = period ? `${period.dateFrom.split('-').reverse().join('.')} — ${period.dateTo.split('-').reverse().join('.')}` : ''
  const tip = `Сравнение: ${dates}. ${delta ? `${calculated ? `Динамика рассчитана из полных итогов WB обоих периодов, без ограничения локальным каталогом${products === undefined ? '' : ` (товаров в выбранном отчёте: ${products})`}.` : 'Динамика передана API WB.'}${typeof previous === 'number' && Number.isFinite(previous) ? ` Ранее: ${formatFunnelValue(previous, kind)}.` : ''}` : pending ? calculated ? 'Сравнение итогов загружается после выбранного периода.' : 'Готовая динамика WB загружается после выбранного периода.' : unavailableReason || (calculated ? 'Нет сопоставимых итогов для расчёта динамики.' : 'WB не передал готовую сопоставимую динамику за эту пару периодов.')}`
  return <span className={`sf-change sf-change-${delta?.direction ?? 'unknown'}`} title={tip} aria-label={`${delta?.text ?? (pending ? 'Сравнение загружается' : 'Сравнение недоступно')}. ${tip}`} tabIndex={0}>{delta?.text ?? (pending ? '…' : '—')}</span>
}

export function FunnelStages({ data, comparison = null, pending = false }: { data: FunnelPage | null; comparison?: FunnelTotalsComparison | null; pending?: boolean }) {
  const stages = [['visits', 'Перешли в карточку'], ['carts', 'Положили в корзину'], ['orders', 'Заказали товар'], ['buyouts', 'Выкупили товар']] as const
  return <div className="sf-funnel" aria-label="Этапы воронки продаж">
    {stages.map(([key, label], index) => {
      const rate = key === 'carts' ? { label: 'CR', key: 'cartConversion', tip: 'Конверсия из перехода в карточку товара в добавление в корзину.' } : null
      const value = rate ? data?.totals[rate.key] : null
      const observed = typeof value === 'number' && Number.isFinite(value)
      const reason = rate ? data?.unavailable[rate.key] || 'Нет полного покрытия переходов и корзин за выбранный период.' : ''
      const partial = observed && (data?.summaryComplete ?? data?.complete) !== true
      const cohort = rate?.key === 'cartConversion' ? data?.conversionCoverage?.cartConversion : null
      const tip = rate ? `${rate.tip} ${observed ? rate.key === 'cartConversion'
        ? `Итог из сохранённых сумм cartCount и openCount WB, не среднее процентов товаров.${cohort ? ` Одинаковый период и состав: ${cohort.products} из ${cohort.total} товаров; корзины: ${cohort.carts}, переходы: ${cohort.visits}.` : ''}` : '' : reason}${partial ? ' Данные неполные или требуют обновления; это не подтверждённый итог всего периода.' : ''}` : ''
      return <div key={key} className={`sf-stage sf-stage-${index}`} style={{ width: `${100 - index * 12}%` }}>
        <span>{label}</span><div className="sf-stage-value"><span className="sf-stage-metric"><strong>{formatFunnelValue(data?.totals[key])}</strong>
          <FunnelChange value={comparison?.totals[key]} previous={comparison?.previousTotals[key]} ready={comparison?.ready[key] === true && comparison.selectedTotals[key] === data?.totals[key]} calculated products={comparison?.coverage[key]?.products} period={data ? previousFunnelPeriod(data.dateFrom, data.dateTo) : null} pending={pending} unavailableReason={comparison?.unavailable[key]} />
        </span>
          {rate && <span className="sf-stage-rate" title={tip} aria-label={`${rate.label}: ${observed ? formatFunnelValue(value, 'percent') : 'недоступно'}. ${tip}`}>
            {rate.label} {observed ? formatFunnelValue(value, 'percent') : '—'}
            <FunnelChange value={comparison?.totals[rate.key]} previous={comparison?.previousTotals[rate.key]} ready={comparison?.ready[rate.key] === true && comparison.selectedTotals[rate.key] === data?.totals[rate.key]} calculated products={comparison?.coverage[rate.key]?.products} kind="percent" period={data ? previousFunnelPeriod(data.dateFrom, data.dateTo) : null} pending={pending} unavailableReason={comparison?.unavailable[rate.key]} />
          </span>}
        </div>
      </div>
    })}
  </div>
}

export function WbSalesFunnel({ dateFrom, dateTo, accessToken }: { dateFrom: string; dateTo: string; accessToken: string | null }) {
  const [snapshot, setSnapshot] = useState<{ scope: string; data: FunnelPage } | null>(null)
  const [comparisonSnapshot, setComparisonSnapshot] = useState<{ scope: string; data: FunnelComparison } | null>(null)
  const [comparisonError, setComparisonError] = useState('')
  const [comparisonLoading, setComparisonLoading] = useState(false)
  const [totalsSnapshot, setTotalsSnapshot] = useState<{ scope: string; data: FunnelTotalsComparison } | null>(null)
  const [totalsError, setTotalsError] = useState('')
  const [totalsLoading, setTotalsLoading] = useState(false)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [sort, setSort] = useState('orders')
  const [direction, setDirection] = useState('desc')
  const [page, setPage] = useState(1)
  const [retry, setRetry] = useState(0)
  const attempted = useRef(new Set<string>())
  const scope = `${accessToken}:${dateFrom}:${dateTo}`
  const data = snapshot?.scope === scope && funnelPeriodMatches(snapshot.data, dateFrom, dateTo) ? snapshot.data : null
  const previous = previousFunnelPeriod(dateFrom, dateTo)
  const selectedPhaseReady = data && !funnelNeedsRefresh(data) && selectedCollectionFinished(data.job)
  const comparison = selectedPhaseReady && comparisonSnapshot?.scope === scope ? comparisonSnapshot.data : null
  const totalsComparison = (selectedPhaseReady || data?.summaryComplete === true) && totalsSnapshot?.scope === scope ? totalsSnapshot.data : null
  const previousItems = new Map(comparison?.items.map(item => [item.nmId, item]))
  useEffect(() => { setPage(1); setSnapshot(null); setComparisonSnapshot(null); setTotalsSnapshot(null); setError(''); setComparisonError(''); setTotalsError('') }, [scope])
  useEffect(() => {
    if (!accessToken) return
    const abort = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    let pendingStart: string | null = null
    let totalsTimer: ReturnType<typeof setTimeout> | undefined
    let pendingTotalsStart: string | null = null
    // Existing demand leases recognize from/to; preserve their cancellation
    // protocol while the typed endpoint uses explicit dateFrom/dateTo.
    const period = new URLSearchParams({ dateFrom, dateTo, from: dateFrom, to: dateTo })
    const params = new URLSearchParams({ dateFrom, dateTo, sort, direction, page: String(page), pageSize: '50' })
    const headers = authorizationHeaders(accessToken)
    const loadTotals = async (selected: FunnelPage) => {
      if (!previousFunnelPeriod(dateFrom, dateTo) || abort.signal.aborted) return
      const totalsScope = `totals:${scope}`
      setTotalsLoading(true)
      try {
        const params = new URLSearchParams({ dateFrom, dateTo })
        const result = await reportRequest<FunnelTotalsComparison>(`/api/wb/reports/sales-funnel/totals-comparison?${params}`, { headers, signal: abort.signal })
        if (abort.signal.aborted) return
        const past = previousFunnelPeriod(dateFrom, dateTo)!
        if (!result || result.source !== 'calculated_wb_sales_funnel_totals_v1' || !funnelPeriodMatches(result, past.dateFrom, past.dateTo) || result.selectedDateFrom !== dateFrom || result.selectedDateTo !== dateTo) throw new Error('Сервер не подтвердил период или источник сравнения итогов.')
        setTotalsSnapshot({ scope, data: result }); setTotalsError('')
        if (Object.keys(result.ready).some(key => result.ready[key] && result.selectedTotals[key] !== selected.totals[key])) {
          totalsTimer = setTimeout(() => { void load() }, 0); return
        }
        // Saved seller-wide dynamics do not depend on catalog recovery. New
        // comparison jobs still wait for the selected collection to finish.
        if (funnelNeedsRefresh(selected) || !selectedCollectionFinished(selected.job)) return
        if (funnelNeedsRefresh(result) && !attempted.current.has(totalsScope)) {
          attempted.current.add(totalsScope); pendingTotalsStart = totalsScope
          const period = new URLSearchParams({ dateFrom, dateTo, aggregateComparison: 'true', from: dateFrom, to: dateTo })
          const job = await reportRequest<Job>(`/api/wb/reports/sales-funnel/refresh?${period}`, { method: 'POST', headers, signal: abort.signal })
          if (abort.signal.aborted) return
          pendingTotalsStart = null
          if (!job) throw new Error('Очередь WB не подтвердила загрузку прошлых итогов.')
          result.job = job; setTotalsSnapshot({ scope, data: { ...result } })
          if (job.settled || job.status === 'complete') { totalsTimer = setTimeout(() => { void loadTotals(selected) }, 0); return }
        }
        if (['queued', 'running'].includes(result.job?.status ?? '')) totalsTimer = setTimeout(() => { void loadTotals(selected) }, 2500)
        else if (funnelNeedsRefresh(result)) setTotalsError(result.job?.message || 'Нет полных сопоставимых итогов прошлого периода.')
      } catch (reason) {
        if (!abort.signal.aborted) setTotalsError(reason instanceof Error ? reason.message : 'Не удалось загрузить сравнение итогов.')
      } finally { if (!abort.signal.aborted) setTotalsLoading(false) }
    }
    const loadComparison = async (selected: FunnelPage) => {
      const previous = previousFunnelPeriod(dateFrom, dateTo)
      if (!previous || abort.signal.aborted) return
      const comparisonScope = `native:${scope}`
      const params = new URLSearchParams({ dateFrom, dateTo })
      selected.items.forEach(item => params.append('nmIds', item.nmId))
      setComparisonLoading(true)
      try {
        const result = await reportRequest<FunnelComparison>(`/api/wb/reports/sales-funnel/comparison?${params}`, { headers, signal: abort.signal })
        if (abort.signal.aborted) return
        if (!result || !funnelPeriodMatches(result, previous.dateFrom, previous.dateTo) || result.selectedDateFrom !== dateFrom || result.selectedDateTo !== dateTo) throw new Error('Сервер вернул сравнение за другой период.')
        if (result.source !== 'wb_sales_funnel_comparison_v1') throw new Error('Сервер не подтвердил источник готовой динамики WB.')
        setComparisonSnapshot({ scope, data: result }); setComparisonError('')
        const selectedById = new Map(selected.items.map(item => [item.nmId, item.metrics]))
        if (result.items.some(item => funnelColumns.some(([key]) => item.ready[key] && selectedById.has(item.nmId) && item.selectedValues[key] !== selectedById.get(item.nmId)?.[key]))) {
          // The provider revised base values during comparison collection.
          // Read their saved projection before publishing the matching delta.
          timer = setTimeout(() => { void load() }, 0); return
        }
        if (funnelNeedsRefresh(result) && !attempted.current.has(comparisonScope)) {
          attempted.current.add(comparisonScope)
          pendingStart = comparisonScope
          // from/to retain the SELECTED demand view. A comparison must not
          // rotate its lease or cancel the main period's work.
          const period = new URLSearchParams({ dateFrom, dateTo, nativeComparison: 'true', from: dateFrom, to: dateTo })
          const job = await reportRequest<Job>(`/api/wb/reports/sales-funnel/refresh?${period}`, { method: 'POST', headers, signal: abort.signal })
          if (abort.signal.aborted) return
          pendingStart = null
          if (!job) throw new Error('Очередь WB не подтвердила загрузку сравнения')
          result.job = job
          setComparisonSnapshot({ scope, data: { ...result } })
          if (job.settled || job.status === 'complete') { timer = setTimeout(() => { void loadComparison(selected) }, 0); return }
        }
        if (['queued', 'running'].includes(result.job?.status ?? '')) timer = setTimeout(() => { void loadComparison(selected) }, 2500)
        else if (funnelNeedsRefresh(result)) setComparisonError(result.job?.message || 'Сравнение загружено не полностью.')
      } catch (reason) {
        if (!abort.signal.aborted) setComparisonError(reason instanceof Error ? reason.message : 'Не удалось загрузить сравнение WB')
      } finally { if (!abort.signal.aborted) setComparisonLoading(false) }
    }
    const load = async () => {
      setLoading(true)
      try {
        const result = await reportRequest<FunnelPage>(`/api/wb/reports/sales-funnel?${params}`, { headers, signal: abort.signal })
        if (abort.signal.aborted) return
        if (!result) throw new Error('WB вернул пустой ответ воронки')
        if (!funnelPeriodMatches(result, dateFrom, dateTo)) throw new Error('Сервер вернул воронку за другой период. Повторите загрузку.')
        setSnapshot({ scope, data: result }); setError('')
        if (result.summaryComplete === true) void loadTotals(result)
        const unsuccessful = !selectedCollectionFinished(result.job) && !['queued', 'running'].includes(result.job?.status ?? '')
        if ((funnelNeedsRefresh(result) || unsuccessful) && !attempted.current.has(scope)) {
          if (attempted.current.size > 20) attempted.current.clear()
          attempted.current.add(scope)
          pendingStart = scope
          const job = await reportRequest<Job>(`/api/wb/reports/sales-funnel/refresh?${period}`, { method: 'POST', headers, signal: abort.signal })
          if (abort.signal.aborted) return
          pendingStart = null
          if (!job) throw new Error('Очередь WB не подтвердила запуск загрузки')
          result.job = job
          setSnapshot({ scope, data: { ...result } })
          // Another reader/worker may finish after our GET but before this POST.
          if (job.settled || job.status === 'complete') { timer = setTimeout(() => { void load() }, 0); return }
        }
        if (['queued', 'running'].includes(result.job?.status ?? '')) timer = setTimeout(() => { void load() }, 2500)
        else if (!funnelNeedsRefresh(result) && selectedCollectionFinished(result.job)) {
          if (result.total > 0) void loadComparison(result)
          if (result.summaryComplete !== true) void loadTotals(result)
        }
      } catch (reason) {
        if (!abort.signal.aborted) setError(reason instanceof Error ? reason.message : 'Не удалось загрузить воронку WB')
      } finally { if (!abort.signal.aborted) setLoading(false) }
    }
    void load()
    return () => { abort.abort(); clearTimeout(timer); clearTimeout(totalsTimer); if (pendingStart) attempted.current.delete(pendingStart); if (pendingTotalsStart) attempted.current.delete(pendingTotalsStart) }
  }, [accessToken, dateFrom, dateTo, scope, sort, direction, page, retry])
  const needsRefresh = data ? funnelNeedsRefresh(data) : false
  const settled = data && !needsRefresh && !data.complete
  const running = ['queued', 'running'].includes(data?.job?.status ?? '')
  const comparisonPending = loading || running || comparisonLoading || ['queued', 'running'].includes(comparison?.job?.status ?? '')
  function sortBy(key: string) {
    if (sort === key) setDirection(direction === 'desc' ? 'asc' : 'desc')
    else { setSort(key); setDirection('desc') }
    setPage(1)
  }
  return <section className="wb-sales-funnel" aria-label="Воронка продаж WB" aria-busy={loading || running}>
    <div className="sf-period">{dateFrom.split('-').reverse().join('.')} — {dateTo.split('-').reverse().join('.')}
      {previous && <small className="sf-comparison-period">Сравнение: {previous.dateFrom.split('-').reverse().join('.')} — {previous.dateTo.split('-').reverse().join('.')}
        {comparisonError && <span role="status" title={comparisonError}> · Сравнение недоступно <button type="button" title={comparisonError} onClick={() => { attempted.current.delete(`native:${scope}`); setRetry(value => value + 1) }}>Повторить сравнение</button></span>}
        {totalsError && <span role="status" title={totalsError}> · Сравнение итогов недоступно <button type="button" title={totalsError} onClick={() => { attempted.current.delete(`totals:${scope}`); setRetry(value => value + 1) }}>Повторить сравнение итогов</button></span>}
      </small>}
    </div>
    <div className="sf-state" role="status" aria-live="polite">
      {!accessToken ? 'Нужна авторизация.' : error ? <span role="alert">{error}</span> : running ? data?.job?.message || 'Загружаем недостающие показатели воронки WB…' : loading ? 'Читаем сохранённые данные…' : !selectedCollectionFinished(data?.job) ? data?.job?.message || 'Загрузка выбранного периода не завершена. Сохранённые значения доступны.' : !data?.total ? data?.catalogState === 'empty' ? 'В подключённом аккаунте нет товаров.' : data?.catalogState === 'ready' ? 'По выбранным фильтрам товары не найдены.' : 'Каталог товаров WB ещё не загружен. Требуется восстановление списка товаров.' : data.complete || data.reportComplete ? data.coveredProducts > 0 ? 'Данные за выбранный период загружены.' : 'В отчёте WB нет показателей по товарам этого каталога за выбранный период.' : settled ? 'Отчёт WB загружен. Некоторые показатели не переданы WB.' : data.job?.message || 'Данные за выбранный период загружены частично.'}
      {data && needsRefresh && data.catalogState !== 'missing' && <span> Полные данные: {data.coveredProducts.toLocaleString('ru-RU')} из {data.total.toLocaleString('ru-RU')} товаров.{data.summaryComplete !== true && ' Суммы доступной части не являются итогом всего периода.'}</span>}
      {!running && (error || needsRefresh || (data && !selectedCollectionFinished(data.job))) && <button type="button" onClick={() => { attempted.current.delete(scope); setRetry(value => value + 1) }}>Повторить загрузку</button>}
    </div>
    <FunnelStages data={data} comparison={totalsComparison} pending={loading || running || totalsLoading || ['queued', 'running'].includes(totalsComparison?.job?.status ?? '')} />
    <div className="sf-table-wrap" tabIndex={0} role="region" aria-label="Товары воронки продаж">
      <table className="sf-table report-table"><thead><tr><th className="sf-product" onClick={event => { if (event.target === event.currentTarget) sortBy('sku') }} aria-sort={sort === 'sku' ? direction === 'asc' ? 'ascending' : 'descending' : 'none'}><button type="button" onClick={() => sortBy('sku')}>Товар <span className="sf-sort-arrow" aria-hidden="true">{sort === 'sku' ? direction === 'asc' ? '↑' : '↓' : '↕'}</span></button></th>{funnelColumns.map(([key, label]) => <th key={key} className={key === 'cartConversion' || key === 'orderConversion' ? 'sf-conversion' : undefined} onClick={event => { if (event.target === event.currentTarget) sortBy(key) }} aria-sort={sort === key ? direction === 'asc' ? 'ascending' : 'descending' : 'none'}><button type="button" onClick={() => sortBy(key)}>{label} <span className="sf-sort-arrow" aria-hidden="true">{sort === key ? direction === 'asc' ? '↑' : '↓' : '↕'}</span></button></th>)}</tr></thead>
        <tbody>{data?.items.map(product => <tr key={product.nmId}>
          <td className="sf-product"><div className="sf-identity">{product.imageUrl && <img src={product.imageUrl} alt="" loading="lazy" />}<div><a href={`/wb/repricer/sku/${encodeURIComponent(product.sellerArticle || product.nmId)}`}>{product.sellerArticle || product.nmId}</a><span title={product.title || ''}>{product.title || 'Без названия'}</span><a href={`https://www.wildberries.ru/catalog/${product.nmId}/detail.aspx`} target="_blank" rel="noreferrer">WB: {product.nmId}</a></div></div></td>
          {funnelColumns.map(([key, , kind]) => <td key={key} title={product.inReport === false ? 'Нет записи в отчёте WB за выбранный период.' : data.unavailable[key]}>{formatFunnelValue(product.metrics[key], kind)}
            <FunnelChange value={previousItems.get(product.nmId)?.dynamics[key]} previous={previousItems.get(product.nmId)?.metrics[key]} ready={previousItems.get(product.nmId)?.ready[key] === true && previousItems.get(product.nmId)?.selectedValues[key] === product.metrics[key]} period={previous} kind={kind} pending={comparisonPending} />
          </td>)}
        </tr>)}</tbody>
      </table>
    </div>
    <div className="sf-pagination"><span>Всего товаров: {(data?.total ?? 0).toLocaleString('ru-RU')}</span><button type="button" disabled={page <= 1} onClick={() => setPage(value => value-1)}>Назад</button><span>Страница {page} из {Math.max(1, Math.ceil((data?.total ?? 0)/50))}</span><button type="button" disabled={!data || page*50 >= data.total} onClick={() => setPage(value => value+1)}>Далее</button></div>
  </section>
}
