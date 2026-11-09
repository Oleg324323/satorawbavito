import { useMemo, useState } from 'react'
import { useCanonicalMarketplaceAccounts } from '@/features/marketplace-accounts/canonicalMarketplaceAccounts'
import { useCanonicalNotificationInbox } from './useCanonicalNotificationInbox'

export const canonicalNotificationsEnabled = import.meta.env.VITE_CANONICAL_NOTIFICATIONS_ENABLED === 'true'

export function CanonicalNotificationsIsland() {
  const account = useCanonicalMarketplaceAccounts()
  const { cabinetMe } = account
  const [accountSelection, setAccountSelection] = useState<{ session: string; id: number | null }>({ session: '', id: null })
  const selectedAccount = account.accounts.find(item => item.marketplaceAccountId === (accountSelection.session === account.session ? accountSelection.id : null)) ?? null
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [read, setRead] = useState('all')
  const scope = useMemo(() => cabinetMe && selectedAccount ? {
    organizationId: cabinetMe.organization.organizationId, marketplaceAccountId: selectedAccount.marketplaceAccountId, marketplace: selectedAccount.provider,
  } : null, [cabinetMe?.organization.organizationId, selectedAccount?.marketplaceAccountId, selectedAccount?.provider])
  const inbox = useCanonicalNotificationInbox({ scope, accessToken: account.accessToken, sessionKey: account.session })
  const rows = useMemo(() => inbox.items.filter((item) => (read === 'all' || (read === 'read') === Boolean(item.readAt))
    && `${item.title} ${item.details} ${item.entityId}`.toLocaleLowerCase('ru-RU').includes(query.toLocaleLowerCase('ru-RU'))), [inbox.items, query, read])
  const selected = rows.find((item) => item.id === selectedId) ?? rows[0] ?? null
  const date = (value: string) => new Date(value).toLocaleString('ru-RU')
  return <div className="notif-page" data-canonical-notifications="true">
    <section className="notif-main">
      <div className="profile-page-head"><h1 className="profile-page-title">Центр уведомлений</h1><button type="button" className="btn btn-default btn-sm" disabled={inbox.loading || inbox.writing || !scope} onClick={inbox.refresh}>Обновить список</button></div>
      <div className="profile-field"><label htmlFor="canonical-notification-account">Аккаунт уведомлений</label>
        <select id="canonical-notification-account" className="profile-input" disabled={account.loading || inbox.writing} value={selectedAccount?.marketplaceAccountId ?? ''} onChange={event => setAccountSelection({ session: account.session, id: event.target.value ? Number(event.target.value) : null })}>
          <option value="">{account.loading ? 'Загрузка аккаунтов…' : 'Выберите WB или Avito'}</option>
          {account.accounts.map(item => <option key={item.marketplaceAccountId} value={item.marketplaceAccountId}>{item.provider.toUpperCase()} · {item.displayName || item.externalAccountId} · {item.status}</option>)}
        </select>
        {account.error ? <p role="alert">{account.error}</p> : null}
        {!account.loading && !account.error && account.accounts.length === 0 ? <p>Доступных аккаунтов нет.</p> : null}
        <button type="button" className="btn btn-ghost btn-sm" disabled={account.loading || inbox.writing} onClick={account.reload}>Перечитать аккаунты уведомлений</button>
      </div>
      <p className="profile-helper-text">Уведомления об отзывах выбранного аккаунта. Отметки прочтения сохраняются только для вас. Поиск и статус ниже применяются к текущей странице.</p>
      <div className="notif-toolbar">
        <input className="profile-input" aria-label="Поиск уведомлений на странице" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Текст или идентификатор отзыва" />
        <select className="profile-input" aria-label="Статус прочтения уведомлений" value={read} onChange={(event) => setRead(event.target.value)}><option value="all">Все</option><option value="unread">Непрочитанные</option><option value="read">Прочитанные</option></select>
        <button type="button" className="btn btn-default btn-sm" disabled={!inbox.canMarkRead || !rows.some((item) => !item.readAt)} onClick={() => void inbox.markRead(rows.filter((item) => !item.readAt).map((item) => item.id))}>Прочитать показанные</button>
      </div>
      {inbox.notice ? <div className="profile-token-feedback" role="status">{inbox.notice}</div> : null}
      {inbox.error ? <div className="profile-token-feedback is-error" role="alert">{inbox.error}{inbox.data ? ' Показана предыдущая успешная загрузка этой страницы.' : ''}</div> : null}
      {inbox.loading ? <div className="profile-token-feedback" role="status">Загрузка уведомлений…</div> : null}
      {inbox.writing ? <div className="profile-token-feedback" role="status">Сохраняем отметку прочтения…</div> : null}
      {!scope && !account.loading ? <p>Выберите доступный аккаунт WB или Avito. Список аккаунтов не является разрешением на действия.</p> : null}
      {inbox.data && !inbox.data.capabilities.canRead ? <div role="alert">Нет доступа к уведомлениям выбранного аккаунта.</div> : null}
      {!inbox.loading && !inbox.error && inbox.data?.capabilities.canRead && rows.length === 0 ? <div className="notif-table-wrap"><p>{inbox.items.length === 0 ? 'Уведомлений об отзывах пока нет.' : 'На этой странице нет уведомлений по выбранным условиям.'}</p></div> : null}
      {rows.length > 0 ? <div className="notif-table-wrap" style={{ display: 'block' }}><table className="notif-table"><thead><tr><th>Событие</th><th>Категория</th><th>Менеджер</th><th>Источник</th><th>Время</th><th>Статус</th></tr></thead><tbody>
        {rows.map((item) => <tr key={item.id} className={`${item.id === selected?.id ? 'active ' : ''}${item.readAt ? 'read' : 'unread'}`} tabIndex={0} onClick={() => setSelectedId(item.id)} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); setSelectedId(item.id) } }}>
          <td><div className="notif-event"><span className="notif-sev-icon" aria-label={item.severity === 'warning' ? 'Внимание' : 'Информация'}>{item.severity === 'warning' ? '!' : 'i'}</span><div><div className="notif-event-title">{item.title}</div><div className="notif-event-sub">{item.details}</div></div></div></td>
          <td><span className="notif-category">Отзывы</span></td><td>{item.manager}</td><td>{item.source}</td><td>{date(item.createdAt)}</td><td><span className={`notif-status ${item.readAt ? 'read' : 'unread'}`}>{item.readAt ? 'прочитано' : 'новое'}</span></td>
        </tr>)}
      </tbody></table></div> : null}
      {inbox.data ? <div className="profile-token-actions"><button type="button" className="btn btn-default btn-sm" disabled={inbox.pageIndex === 0 || inbox.loading || inbox.writing} onClick={inbox.previous}>Назад</button><span>Страница {inbox.pageIndex + 1} · до 50 уведомлений</span><button type="button" className="btn btn-default btn-sm" disabled={!inbox.data.nextCursor || inbox.loading || inbox.writing} onClick={inbox.next}>Далее</button></div> : null}
    </section>
    <aside className="notif-side"><div className="notif-detail-card">
      {selected ? <><div className="notif-detail-head"><div className="notif-detail-eyebrow"><span className="notif-category">Отзывы</span><span className={`notif-status ${selected.readAt ? 'read' : 'unread'}`}>{selected.readAt ? 'прочитано' : 'новое'}</span></div><div className="notif-detail-title">{selected.title}</div></div>
        <div className="notif-detail-body"><div className="notif-detail-section"><h4>Что случилось</h4><p>{selected.details}</p></div><div className="notif-detail-section"><h4>Событие</h4><p>{date(selected.createdAt)}</p><p>Отзыв: {selected.entityId}</p>{selected.readAt ? <p>Прочитано: {date(selected.readAt)}</p> : null}</div><div className="notif-detail-section"><h4>Что заблокировано</h4><div className="notif-block-list">{selected.blockedActions.map((action) => <div className="notif-block-item" key={action}>{action}</div>)}</div></div>
        <div className="profile-token-actions"><button type="button" className="btn btn-default" disabled={!inbox.canMarkRead || Boolean(selected.readAt)} onClick={() => void inbox.markRead([selected.id])}>Пометить прочитанным</button><a className="btn btn-primary" href={selected.route}>Открыть отзывы</a></div></div></> : <p>Выберите уведомление для просмотра.</p>}
    </div></aside>
  </div>
}
