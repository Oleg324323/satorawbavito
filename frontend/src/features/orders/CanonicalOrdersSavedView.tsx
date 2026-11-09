import { useEffect, useState } from 'react'
import { createCanonicalOrdersClient } from './canonicalOrdersClient'
import { orderMappingStates, orderResolutionStates, orderStatuses, type CanonicalOrderFilters, type CanonicalOrdersPage, type CanonicalOrdersSavedSnapshot, type CanonicalOrdersScope } from './canonicalOrders'

/** Mount keyed by authenticated session + exact selected account IDs. Saved view, never full queue. */
export function CanonicalOrdersSavedView({ scope, token }: { scope: CanonicalOrdersScope; token: string }) {
  const [snapshot, setSnapshot] = useState<CanonicalOrdersSavedSnapshot | null>(null)
  const [error, setError] = useState<string | null>(null), [loading, setLoading] = useState(true), [revision, setRevision] = useState(0)
  const [draft, setDraft] = useState<Record<string, string>>({}), [filters, setFilters] = useState<CanonicalOrderFilters>({})
  useEffect(() => {
    let current = true
    const client = createCanonicalOrdersClient(token, () => current)
    setSnapshot(null); setError(null); setLoading(true)
    void client.discover(scope).then(data => { if (current) setSnapshot(data) })
      .catch(failure => { if (current) setError(failure instanceof Error ? failure.message : 'Снимок недоступен.') })
      .finally(() => { if (current) setLoading(false) })
    return () => { current = false; client.dispose() }
  }, [revision])
  const field = (name: string, label: string, options: readonly string[]) => <label>{label}<select className="profile-input" value={draft[name] ?? ''} onChange={event => setDraft({ ...draft, [name]: event.target.value })}><option value="">Все</option>{options.map(option => <option key={option} value={option}>{option}</option>)}</select></label>
  return <section className="rounded-lg border bg-card p-5 shadow-sm">
    <h1 className="text-2xl font-semibold">Сохранённое представление заказов</h1>
    <p>Ограниченный ранее сохранённый снимок, не вся операционная очередь. Полнота источника не доказывает полноту всех заказов.</p>
    <p>Печать, экспорт, отправка в производство и изменение заказа здесь недоступны. WB Statistics не подтверждает готовность к отгрузке или наличие стикера.</p>
    <button className="btn btn-default" type="button" disabled={loading} onClick={() => setRevision(value => value + 1)}>Найти актуальный сохранённый снимок</button>
    {loading ? <p role="status">Ищем сохранённый снимок…</p> : null}
    {error ? <p role="alert">{error}</p> : null}
    {snapshot ? <>
      <p>Снимок {snapshot.snapshot_id} · сохранён {new Date(snapshot.published_at).toLocaleString('ru-RU')} · строк до фильтров: {snapshot.row_count}</p>
      <p>Покрытие сохранённого источника: {snapshot.coverage_state}. Фильтры ниже только сужают этот снимок.</p>
      {snapshot.account_coverage.map(item => <p key={item.marketplace_account_id}>Аккаунт {item.marketplace_account_id}: {item.state} · {item.source_kind} · версия {item.source_version} · {item.requested_from ?? 'начало периода неизвестно'} — {item.requested_to ?? 'конец периода неизвестен'}</p>)}
      <form className="profile-form-grid" onSubmit={event => { event.preventDefault(); setFilters(Object.fromEntries(Object.entries(draft).filter(([, value]) => value !== '')) as CanonicalOrderFilters) }}>
        {field('marketplace', 'Маркетплейс', ['wb', 'avito'])}{field('canonical_status', 'Канонический статус', orderStatuses)}
        {field('mapping_state', 'Сопоставление статуса', orderMappingStates)}{field('resolution_state', 'Сопоставление каталога', orderResolutionStates)}
        <label>Внешний ID заказа — точное совпадение<input className="profile-input" maxLength={4096} value={draft.external_order_id ?? ''} onChange={event => setDraft({ ...draft, external_order_id: event.target.value })} /></label>
        <label>Исходный статус — точное совпадение<input className="profile-input" maxLength={2048} value={draft.raw_status ?? ''} onChange={event => setDraft({ ...draft, raw_status: event.target.value })} /></label>
        <button className="btn btn-default" type="submit">Применить фильтры</button>
        <button className="btn btn-default" type="button" onClick={() => { setDraft({}); setFilters({}) }}>Сбросить фильтры</button>
      </form>
      <SavedOrdersRows key={`${snapshot.snapshot_id}:${snapshot.query_checksum}:${JSON.stringify(filters)}`} scope={scope} token={token} snapshot={snapshot} filters={filters} />
    </> : null}
  </section>
}

function SavedOrdersRows({ scope, token, snapshot, filters }: { scope: CanonicalOrdersScope; token: string; snapshot: CanonicalOrdersSavedSnapshot; filters: CanonicalOrderFilters }) {
  const [cursors, setCursors] = useState<(string | undefined)[]>([undefined]), [index, setIndex] = useState(0)
  const [loaded, setLoaded] = useState<{ index: number; page: CanonicalOrdersPage } | null>(null)
  const [loading, setLoading] = useState(true), [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<string | null>(null), [retry, setRetry] = useState(0)
  const cursor = cursors[index], page = loaded?.index === index ? loaded.page : null
  useEffect(() => {
    let current = true
    const client = createCanonicalOrdersClient(token, () => current)
    setLoaded(null); setSelected(null); setLoading(true); setError(null)
    // The returned cursor already binds the snapshot. Do not combine it with snapshot_id.
    void Promise.resolve().then(() => client.page({ organizationId: scope.organizationId, accountIds: scope.accountIds,
      queryChecksum: snapshot.query_checksum, ...(cursor ? { cursor } : { snapshotId: snapshot.snapshot_id }), limit: 50, filters }, snapshot.snapshot_id))
      .then(value => { if (current) setLoaded({ index, page: value }) })
      .catch(failure => { if (current) setError(failure instanceof Error ? failure.message : 'Строки недоступны.') })
      .finally(() => { if (current) setLoading(false) })
    return () => { current = false; client.dispose() }
  }, [cursor, index, retry])
  const key = (row: CanonicalOrdersPage['rows'][number]) => JSON.stringify(row.item_identity)
  const detail = page?.rows.find(row => key(row) === selected)
  return <>
    {loading ? <p role="status">Читаем строки снимка…</p> : null}
    {error ? <p role="alert">{error} <button type="button" className="btn btn-default" onClick={() => setRetry(value => value + 1)}>Повторить чтение страницы</button></p> : null}
    {page?.rows.length === 0 ? <p>В сохранённом представлении нет строк по выбранным условиям. Это не означает отсутствие заказов у маркетплейса.</p> : null}
    {page && page.rows.length > 0 ? <div className="overflow-x-auto"><table className="w-full text-sm"><thead><tr><th>Источник / аккаунт</th><th>Заказ / строка</th><th>Кол-во</th><th>Исходный статус</th><th>Канонический статус</th><th>Каталог</th><th>Готовность / ограничения</th><th>Принт / стикер</th></tr></thead><tbody>
      {page.rows.map(row => <tr key={key(row)}><td>{row.observation.identity.marketplace} · {row.observation.identity.marketplace_account_id}</td>
        <td><button type="button" className="btn btn-ghost" onClick={() => setSelected(key(row))}>{row.observation.identity.external_order_id}</button><p>{row.item_identity.source_line_key}</p></td>
        <td>{row.observation.items.find(item => item.identity.source_line_key === row.item_identity.source_line_key)?.quantity ?? 'неизвестно'}</td>
        <td>{row.observation.status.raw_status ?? 'неизвестно'}</td><td>{row.observation.status.canonical_status ?? 'не сопоставлен'} · {row.observation.status.mapping_state}</td>
        <td>{row.resolution.state} · SKU {row.resolution.catalog_sku_id ?? 'не определён'}</td>
        <td>{row.readiness_blockers.length ? row.readiness_blockers.join(', ') : 'Нет указанных ограничений; не разрешение на действие'}</td><td>Недоступны в этом контракте</td>
      </tr>)}
    </tbody></table></div> : null}
    <div className="profile-token-actions"><button type="button" className="btn btn-default" disabled={loading || index === 0} onClick={() => setIndex(value => value - 1)}>Назад</button><span>Страница {index + 1} · до 50 строк</span><button type="button" className="btn btn-default" disabled={loading || !page?.next_cursor || Boolean(error)} onClick={() => { if (page?.next_cursor) { setCursors(values => [...values.slice(0, index + 1), page.next_cursor!]); setIndex(value => value + 1) } }}>Далее</button></div>
    {detail ? <aside className="rounded-lg border p-4"><h2>Сохранённая строка заказа</h2><p>{detail.observation.identity.external_order_id} · {detail.item_identity.source_line_key} · повтор позиции {detail.item_identity.occurrence_index}</p>
      <p>Версия строки: {detail.row_version} · наблюдение {detail.observation.observed_at} · источник {detail.observation.source_kind}</p>
      <p>Статус: {detail.observation.status.raw_status ?? 'неизвестно'} → {detail.observation.status.canonical_status ?? 'не сопоставлен'} · правило {detail.observation.status.mapping_version} · {detail.observation.status.evidence_source}</p>
      <p>Сопоставление каталога: {detail.resolution.state} · версия {detail.resolution.evidence_version}</p>
      {detail.deadlines.length ? detail.deadlines.map((deadline, n) => <p key={n}>{deadline.kind}: {deadline.source_at ?? deadline.computed_at} · {deadline.timezone} · {deadline.evidence_source}{deadline.rule_id ? ` · правило ${deadline.rule_id}/${deadline.rule_version}` : ''}</p>) : <p>Подтверждённые сроки не предоставлены.</p>}
      <p>Внешние действия, печать, экспорт и производство заблокированы в этом представлении.</p>
    </aside> : null}
  </>
}
