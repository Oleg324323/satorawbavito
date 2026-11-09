import { useLayoutEffect, useRef, useState } from 'react'
import { useCanonicalMarketplaceAccounts } from '@/features/marketplace-accounts/canonicalMarketplaceAccounts'
import { buildCanonicalAvitoStatisticsPath, CanonicalAvitoStatisticsError, createCanonicalAvitoStatisticsClient,
  type CanonicalAvitoStatistics, type CanonicalAvitoStatisticsAccount } from './canonicalAvitoStatisticsClient'

export const canonicalAvitoStatisticsEnabled = import.meta.env.VITE_CANONICAL_AVITO_STATS_ENABLED === 'true'
const columns = [
  ['impressions', 'Показы'], ['views', 'Просмотры'], ['contactsMessenger', 'Контакты в сообщениях'], ['contacts', 'Контакты'],
  ['contactsShowPhone', 'Показы телефона'], ['contactsShowPhoneAndMessenger', 'Телефон и сообщения'], ['favorites', 'Избранное'],
  ['spendKopecks', 'Расходы, коп.'], ['orders', 'Заказы'], ['buyouts', 'Выкупы'],
] as const

export function CanonicalAvitoStatisticsPage() {
  const discovery = useCanonicalMarketplaceAccounts()
  const [selection, setSelection] = useState<{ session: string; id: number | null }>({ session: '', id: null })
  const accounts = discovery.accounts.filter(account => account.provider === 'avito')
  const selected = selection.session === discovery.session ? accounts.find(account => account.marketplaceAccountId === selection.id) : undefined
  return <div className="tab-content" id="tab-avito-stats" data-canonical-avito-statistics="true">
    <div className="toolbar"><div className="profile-field">
      <label htmlFor="canonical-avito-statistics-account">Аккаунт статистики Авито</label>
      <select className="profile-input" id="canonical-avito-statistics-account" disabled={discovery.loading} value={selected?.marketplaceAccountId ?? ''}
        onChange={event => setSelection({ session: discovery.session, id: event.target.value ? Number(event.target.value) : null })}>
        <option value="">{discovery.loading ? 'Загрузка аккаунтов…' : 'Выберите аккаунт Авито'}</option>
        {accounts.map(account => <option key={account.marketplaceAccountId} value={account.marketplaceAccountId}>{account.displayName || account.externalAccountId} · {account.status}</option>)}
      </select>
      <button type="button" className="btn btn-ghost btn-sm" disabled={discovery.loading} onClick={discovery.reload}>Перечитать аккаунты статистики</button>
      {discovery.error ? <p role="alert">{discovery.error}</p> : null}
      {!discovery.loading && !discovery.error && accounts.length === 0 ? <p>Доступных аккаунтов Авито нет.</p> : null}
    </div></div>
    {selected && discovery.accessToken ? <StatisticsSelection
      key={`${discovery.session}:${discovery.accessToken}:${discovery.revision}:${selected.marketplaceAccountId}:${selected.externalAccountId}`}
      token={discovery.accessToken} account={{ marketplaceAccountId: selected.marketplaceAccountId, provider: 'avito', externalAccountId: selected.externalAccountId }}
      connected={selected.status === 'connected'} /> : <p className="profile-helper-text">Выберите аккаунт и период. Статистика запрашивается только по кнопке «Загрузить статистику».</p>}
    <p className="profile-helper-text">Категории, конверсия и детализация отдельных объявлений этим источником не предоставляются. Это статистика аккаунта, не полная очередь заказов и не расчёт прибыли. Доступ проверяется сервером; наличие аккаунта в списке не является разрешением.</p>
  </div>
}

function StatisticsSelection({ token, account, connected }: { token: string; account: CanonicalAvitoStatisticsAccount; connected: boolean }) {
  const [dateFrom, setFrom] = useState(''), [dateTo, setTo] = useState(''), [data, setData] = useState<CanonicalAvitoStatistics | null>(null)
  const [busy, setBusy] = useState(false), [error, setError] = useState<string | null>(null)
  const mounted = useRef(0), operation = useRef(0), pending = useRef(false)
  const client = useRef<ReturnType<typeof createCanonicalAvitoStatisticsClient> | null>(null)
  useLayoutEffect(() => {
    const epoch = ++mounted.current
    try { client.current = createCanonicalAvitoStatisticsClient(token, account, () => mounted.current === epoch) }
    catch { setError('Метаданные аккаунта не подходят для канонической статистики. Перечитайте список аккаунтов.') }
    return () => { ++mounted.current; ++operation.current; client.current?.dispose(); client.current = null }
  }, [token, account.marketplaceAccountId, account.externalAccountId])
  const period = { dateFrom, dateTo }
  let valid = false
  try { buildCanonicalAvitoStatisticsPath(account, period); valid = true } catch { /* draft inputs may be incomplete */ }
  function edit(field: 'from' | 'to', value: string) {
    ++operation.current; client.current?.invalidate(); pending.current = false
    setBusy(false); setData(null); setError(null)
    if (field === 'from') setFrom(value); else setTo(value)
  }
  async function load() {
    const api = client.current
    if (!api || pending.current || !valid || !connected) return
    const id = ++operation.current, active = () => operation.current === id && client.current === api
    pending.current = true; setBusy(true); setData(null); setError(null)
    try { const result = await api.load(period); if (active()) setData(result) }
    catch (failure) { if (active()) setError(failure instanceof CanonicalAvitoStatisticsError ? failure.message : 'Статистика Авито недоступна.') }
    finally { if (active()) { pending.current = false; setBusy(false) } }
  }
  return <section className="avito-stats-shell">
    <div className="avito-stats-main"><form className="toolbar" onSubmit={event => { event.preventDefault(); void load() }}>
      <label className="profile-field">Дата начала статистики Авито<input className="profile-input" type="date" value={dateFrom} onChange={event => edit('from', event.target.value)} /></label>
      <label className="profile-field">Дата окончания статистики Авито<input className="profile-input" type="date" value={dateTo} onChange={event => edit('to', event.target.value)} /></label>
      <button className="btn btn-primary" type="submit" disabled={!valid || !connected || busy}>Загрузить статистику</button>
    </form>
    <p className="profile-helper-text">Укажите обе даты: от 1 до 270 календарных дней включительно. Изменение периода отменяет старое чтение и очищает результат. Автоматической загрузки и повторов нет.</p>
    {!connected ? <p role="status">Аккаунт не подключён. Для загрузки нужна действующая связь с Авито.</p> : null}
    {dateFrom && dateTo && !valid ? <p role="alert">Проверьте даты: конец не раньше начала, период не больше 270 дней включительно.</p> : null}
    {busy ? <p role="status">Загружаем статистику выбранного аккаунта…</p> : null}
    {error ? <p role="alert">{error}</p> : null}
    {!data && !busy && !error ? <p>Статистика ещё не загружена. Выберите период и нажмите «Загрузить статистику».</p> : null}
    {data ? <>
      <p role="status">Загруженный период: {data.dateFrom} — {data.dateTo}. Аккаунт Авито: {data.externalAccountId}.</p>
      <p>{data.status === 'partial' ? 'Частичные данные: доступна не вся статистика за период.' : 'Источник вернул статистику за выбранный период.'} Состояние итоговой строки: {data.rows[0].sourceStatus === 'fresh' ? 'свежая' : data.rows[0].sourceStatus === 'partial' ? 'частичная' : 'устаревшая'}.</p>
      <p className="profile-helper-text">«Нет данных» — неизвестное значение, не ноль. Расходы показаны точно в копейках. Итоговая строка получена от источника и не пересчитывается из дневных строк.</p>
      <div className="avito-stats-table table-wrap"><table aria-label="Статистика выбранного аккаунта Авито"><thead><tr><th>Показатель</th><th>Значение за период</th></tr></thead><tbody>
        {columns.map(([key, label]) => <tr key={key}><th scope="row">{label}</th><td style={{ overflowWrap: 'anywhere' }}>{data.rows[0].metrics[key] ?? 'Нет данных'}</td></tr>)}
      </tbody></table></div>
      {data.daily.length ? <details><summary>Дневные наблюдения: {data.daily.length}</summary><div className="avito-stats-table table-wrap"><table aria-label="Дневная статистика Авито"><thead><tr><th>Дата</th>{columns.map(([key, label]) => <th key={key}>{label}</th>)}</tr></thead><tbody>
        {data.daily.map(row => <tr key={row.date}><th scope="row">{row.date}</th>{columns.map(([key]) => <td key={key}>{row.metrics[key] ?? 'Нет данных'}</td>)}</tr>)}
      </tbody></table></div></details> : <p>Дневные наблюдения не предоставлены. График не строится из предполагаемых нулей.</p>}
    </> : null}
    </div>
  </section>
}
