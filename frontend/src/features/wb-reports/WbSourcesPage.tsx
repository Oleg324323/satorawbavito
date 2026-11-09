import { useEffect, useMemo, useState } from 'react'
import { useAuth } from '@/features/auth/authContext'
import { authorizationHeaders } from '@/features/auth/authApi'
import { reportRequest } from './reportRequest'
import { useWbTableSorting } from './tableSorting'

export type SourceEvidence = {
  id: string; status: 'ready' | 'partial' | 'stale' | 'failed' | 'loading' | 'unknown' | 'disconnected'
  observedAt?: string | null; dateFrom?: string | null; dateTo?: string | null
  count?: number | null; detail?: string; owner?: string | null
}
export type SourcesStatus = { marketplaceAccountId: number | null; message?: string | null; items: SourceEvidence[] }
const definitions = [
  { id: 'one-c', name: '1С · Движение денежных средств', data: 'поступления и списания ДС по статьям, р/счёт и касса', reports: ['Расходы', 'P&L', 'Планы'], route: 'expenses', type: '1С', action: 'Открыть подключение 1С' },
  { id: 'dds-rules', name: 'Правила статей ДДС', data: 'назначение статьи, P&L, сверка, SKU-драйвер', reports: ['P&L', 'Расходы'], route: 'expenses', type: '1С', action: 'Настроить правило статьи' },
  { id: 'baskets', name: 'Корзины и заказы WB', data: 'корзины, заказы, CR', reports: ['Репрайсер', 'РНП/ABC'], route: 'repricer-stats', type: 'WB', action: 'Открыть статистику' },
  { id: 'ads', name: 'Реклама WB', data: 'расходы, кампании, SKU-связка', reports: ['Реклама', 'РНП/ABC', 'P&L'], route: 'ads', type: 'WB', action: 'Открыть рекламу' },
  { id: 'stocks', name: 'Остатки WB', data: 'остатки, OOS, склады', reports: ['Репрайсер', 'РНП/ABC'], route: 'stock', type: 'WB', action: 'Открыть остатки' },
  { id: 'prices', name: 'Цены и СПП', data: 'наблюдения цен и СПП из расширения', reports: ['Репрайсер', 'P&L'], route: 'repricer-stats', type: 'WB', action: 'Открыть статистику' },
  { id: 'plans', name: 'Планы менеджеров и брендов', data: 'план маржинальной прибыли, SKU-владельцы', reports: ['Дайджест', 'РНП/ABC', 'Планы'], route: 'report-rules', type: 'Правила', action: 'Открыть форму плана' },
  { id: 'assignments', name: 'Назначение SKU менеджеру', data: 'менеджер, SKU, период действия', reports: ['РНП/ABC', 'Планы', 'Расходы'], route: 'report-rules', type: 'Правила', action: 'Назначить SKU' },
  { id: 'excel', name: 'Excel fallback', data: 'аварийная загрузка расходов', reports: ['Расходы', 'P&L'], route: 'expenses', type: 'Ручная загрузка', action: 'Открыть расходы' },
]
const statusLabels: Record<SourceEvidence['status'], string> = {
  ready: 'Свежее', partial: 'Неполные данные', stale: 'Устарело', failed: 'Есть ошибка',
  loading: 'Загрузка', unknown: 'Неизвестно', disconnected: 'Не подключено',
}
const chips = ['Все источники', 'Требует действия', 'Свежее', 'Устарело', 'Неизвестно', 'Не подключено', '1С', 'Ручная загрузка', 'Есть ошибка', 'P&L', 'РНП/ABC', 'Репрайсер', 'Реклама', 'Ответственный']
const reportRoutes: Record<string, string> = { 'P&L': 'pnl', 'РНП/ABC': 'abc', 'Репрайсер': 'repricer-stats', 'Реклама': 'ads', 'Расходы': 'expenses', 'Планы': 'report-rules', 'Дайджест': 'digest' }
const dateTime = (value?: string | null) => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('ru-RU', { timeZone: 'Europe/Moscow' }) : '—'
const period = (row: SourceEvidence) => row.dateFrom && row.dateTo ? `${row.dateFrom} — ${row.dateTo}` : '—'
type Row = SourceEvidence & typeof definitions[number]
export function sourceRows(evidence?: SourcesStatus | null): Row[] {
  return definitions.map(definition => ({ status: 'unknown' as const,
    ...evidence?.items.find(item => item.id === definition.id),
    // Identity and navigation come from the UI registry, never a status response.
    ...definition,
  }))
}
export function filterSources(rows: Row[], search: string, chip: string) {
  const needle = search.trim().toLocaleLowerCase('ru')
  return rows.filter(row => {
    const tags = [row.type, ...row.reports, statusLabels[row.status], ...(row.owner ? ['Ответственный'] : []),
      ...(['partial', 'stale', 'failed', 'disconnected'].includes(row.status) ? ['Требует действия'] : [])]
    return (chip === 'Все источники' || tags.includes(chip)) && (!needle ||
      [row.name, row.data, row.detail, row.owner, row.action, ...tags].join(' ').toLocaleLowerCase('ru').includes(needle))
  })
}
const sortValue = (row: Row, column: number) => [row.name, row.data, row.dateFrom ?? null,
  row.observedAt ? Date.parse(row.observedAt) : null, statusLabels[row.status], row.reports.join(' '), row.owner ?? null, row.action][column]

export function WbSourcesPage() {
  const { accessToken, cabinetMe } = useAuth()
  const scope = `${accessToken}:${cabinetMe?.organization.organizationId ?? ''}`
  const [response, setResponse] = useState<{ scope: string; data: SourcesStatus } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [retry, setRetry] = useState(0)
  const [search, setSearch] = useState('')
  const [chip, setChip] = useState('Все источники')
  const [detailId, setDetailId] = useState<string | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    let active = true
    setLoading(true); setError(null)
    if (accessToken) void reportRequest<SourcesStatus>('/api/v1/wb-repricer/sources/status', {
      headers: authorizationHeaders(accessToken), signal: controller.signal,
    }).then(data => {
      if (!data || !Array.isArray(data.items)) throw new Error('Invalid Sources status response')
      if (active) setResponse({ scope, data })
    }).catch(() => {
      if (active) { setResponse(null); setError('Не удалось прочитать состояние источников. Повторите загрузку.') }
    }).finally(() => { if (active) setLoading(false) })
    else { setResponse(null); setLoading(false); setError('Для чтения источников требуется вход.') }
    return () => { active = false; controller.abort() }
  }, [scope, accessToken, retry])
  const data = response?.scope === scope ? response.data : null
  const all = useMemo(() => sourceRows(data), [data])
  const filtered = useMemo(() => filterSources(all, search, chip), [all, search, chip])
  const table = useWbTableSorting(filtered, sortValue, `wb-sources:${cabinetMe?.organization.organizationId ?? ''}`)
  const known = all.filter(row => row.status !== 'unknown')
  const detail = all.find(row => row.id === detailId)
  const go = (route: string) => window.goSubtab?.(route)
  const reset = () => { setSearch(''); setChip('Все источники') }
  return <div id="tab-sources" className="tab-content active" data-live-sources="true">
    <div className="stats">
      <div className="stat"><div className="stat-label">Источников под контролем</div><div className="stat-val">{known.length || '—'}</div><div className="stat-delta neutral">Подтверждено состояние · неизвестно: {all.length - known.length}</div></div>
      <div className="stat"><div className="stat-label">1С ДДС</div><div className="stat-val">{statusLabels[all[0].status]}</div><div className="stat-delta neutral">Только подтверждённое состояние</div></div>
      <div className="stat"><div className="stat-label">Fallback-загрузки</div><div className="stat-val">Excel</div><div className="stat-delta neutral">{statusLabels[all[8].status]}</div></div>
      <div className="stat"><div className="stat-label">Зависимые отчёты</div><div className="stat-val">P&amp;L / РНП / ABC</div><div className="stat-delta neutral">Переходы из строк</div></div>
    </div>
    <div className="report-source-strip report-source-compact"><div className="source-state-card partial" role="status" aria-busy={loading}>
      <div><div className="source-state-title">{loading ? 'Читаем состояние источников…' : error ?? data?.message ?? 'Состояние сохранённых источников'}</div>
        <div className="source-state-meta">{!loading && !error && data ? `Аккаунт WB: ${data.marketplaceAccountId ?? 'не определён'}. ` : ''}Неизвестное состояние не означает нулевые данные или отсутствие подключения.</div></div>
      <button className="btn btn-default btn-sm" disabled={loading} onClick={() => setRetry(value => value + 1)}>Повторить чтение</button>
    </div></div>
    <div className="toolbar"><div className="search"><input aria-label="Поиск источников" placeholder="Источник, отчёт или действие..." value={search} data-demo-search-bound="1" onChange={event => setSearch(event.target.value)} /></div>
      <div className="chips" role="group" aria-label="Фильтры источников">{chips.map(label => <button key={label} type="button" data-demo-chip-bound="1" className={`chip${label === chip ? ' active' : ''}`} aria-pressed={label === chip} onClick={() => setChip(label)}>{label}</button>)}</div>
      <div className="toolbar-right"><button className="btn btn-default btn-sm" onClick={() => setDetailId('one-c')}>Подключение 1С</button><button className="btn btn-default btn-sm" onClick={() => go('expenses')}>Расходы</button><button className="btn btn-default btn-sm" onClick={() => go('expenses')}>Шаблон Excel</button></div>
    </div>
    <div className="report-filter-summary"><span>Показано <b>{table.rows.length}</b> из {all.length} · фильтры: {chip}</span><button className="btn btn-ghost btn-sm" onClick={reset}>Сбросить</button></div>
    <div className="report-table-wrap"><table className="report-mid"><thead {...table.headerProps}><tr>{['Источник', 'Данные', 'Период', 'Синхронизация', 'Статус', 'Отчёты', 'Ответственный', 'Действие'].map((name, index) => <th key={name} className={index === 0 ? 'report-sticky' : undefined}>{name}</th>)}</tr></thead>
      <tbody id="sourcesBody">{table.rows.map(row => <tr key={row.id} data-source-id={row.id}>
        <td className="report-sticky"><button className="source-open-btn" onClick={() => setDetailId(row.id)}><span className="sku">{row.name}</span><span className="sub">{row.detail ?? 'Нет подтверждённых данных о состоянии источника.'}</span></button></td>
        <td>{row.data}</td><td>{period(row)}</td><td>{dateTime(row.observedAt)}</td><td><span className={`report-tag ${row.status === 'ready' ? 'ok' : row.status === 'unknown' ? 'neutral' : 'warn'}`}>{statusLabels[row.status]}</span>{row.count != null && <span className="sub">Записей: {row.count}</span>}</td>
        <td>{row.reports.map(report => <button key={report} className="report-tag neutral" onClick={() => go(reportRoutes[report] ?? row.route)}>{report}</button>)}</td><td>{row.owner ?? '—'}</td>
        <td><button className="btn btn-default btn-sm" onClick={() => row.id === 'one-c' || row.id === 'dds-rules' ? setDetailId(row.id) : go(row.route)}>{row.action}</button></td>
      </tr>)}{!table.rows.length && <tr><td colSpan={8}>Нет источников, соответствующих фильтрам.</td></tr>}</tbody>
    </table></div>
    {detail && <dialog open aria-label={detail.name} onCancel={() => setDetailId(null)} onKeyDown={event => { if (event.key === 'Escape') setDetailId(null) }}>
      <h2>{detail.name}</h2><p>{detail.data}</p><p>Статус: {statusLabels[detail.status]}</p><p>Период: {period(detail)}</p><p>Синхронизация: {dateTime(detail.observedAt)}</p><p>Ответственный: {detail.owner ?? '—'}</p>
      <p>{detail.detail ?? 'Нет подтверждённых данных о состоянии источника.'}</p>
      <p>{detail.reports.map(report => <button key={report} className="btn btn-default btn-sm" onClick={() => { setDetailId(null); go(reportRoutes[report] ?? detail.route) }}>{report}</button>)}</p>
      <button className="btn btn-default" autoFocus onClick={() => setDetailId(null)}>Закрыть</button>
    </dialog>}
  </div>
}
