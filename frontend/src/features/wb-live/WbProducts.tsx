import { useEffect, useState } from 'react'
import { ApiError } from '@/lib/api'
import { formatWbPrice, productsPath, readWbData, wbErrorMessage, type WbProductSort, type WbProductsPage } from './api'
import { useWbAccount, useWbSync, WbAccountSelect, WbSyncStatus } from './WbConnection'
import { parseWbProducts } from './validation'
import './WbProducts.css'

const readinessLabels = {
  empty: 'В сохранённых данных пока нет товаров.',
  partial: 'Доступны не все данные источников. Состояние загрузки показано выше.',
  ready: 'Сохранённые данные доступны.',
  error: 'Один или несколько источников завершились с ошибкой. Показаны уже сохранённые данные.',
}

export function WbProducts() {
  const account = useWbAccount()
  const sync = useWbSync(account.accessToken, account.accountId, account.scope)
  const [queryDraft, setQueryDraft] = useState('')
  const [brandDraft, setBrandDraft] = useState('')
  const [filter, setFilter] = useState({ q: '', brand: '', sort: 'nmId' as WbProductSort, direction: 'asc' as 'asc' | 'desc' })
  const [cursors, setCursors] = useState<(string | null)[]>([null])
  const [page, setPage] = useState(0)
  const [revision, setRevision] = useState(0)
  const [snapshot, setSnapshot] = useState<{ key: string; data: WbProductsPage } | null>(null)
  const [loading, setLoading] = useState(false)
  const [failure, setFailure] = useState<{ key: string; message: string } | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const accountKey = `${account.scope}:${account.accountId}`
  const path = account.accountId ? productsPath(account.accountId, { ...filter, cursor: cursors[page] }) : ''
  const requestKey = `${accountKey}:${path}`
  const [pagingAccount, setPagingAccount] = useState(accountKey)
  // The first render after an account change must never request its predecessor's cursor.
  const accountChanged = pagingAccount !== accountKey
  useEffect(() => {
    setCursors([null]); setPage(0); setSnapshot(null); setFailure(null); setNotice(null); setPagingAccount(accountKey)
  }, [accountKey])
  const sourceRevision = sync.data?.sources.map((source) => `${source.source}:${source.processed}:${source.state}:${source.updatedAt}`).join('|') ?? ''
  useEffect(() => {
    const controller = new AbortController()
    if (!account.accessToken || !account.accountId || !path || accountChanged) return
    setLoading(true); setFailure(null)
    void readWbData<WbProductsPage>(account.accessToken, path, controller.signal, (value) => parseWbProducts(value, account.accountId!))
      .then((data) => {
        if (controller.signal.aborted) return
        setSnapshot({ key: requestKey, data })
      })
      .catch((error) => {
        if (controller.signal.aborted) return
        if (error instanceof ApiError && error.status === 409 && error.code === 'WB_PRODUCTS_CHANGED' && page > 0) {
          setNotice('Данные обновились во время просмотра. Возвращаемся к первой странице.')
          setCursors([null]); setPage(0); setSnapshot(null)
          return
        }
        setFailure({ key: requestKey, message: wbErrorMessage(error) })
      })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [account.accessToken, account.accountId, accountChanged, path, requestKey, revision, sourceRevision])
  const data = !accountChanged && snapshot?.key === requestKey ? snapshot.data : null
  const error = failure?.key === requestKey ? failure.message : null
  function changeFilter(next: typeof filter) { setFilter(next); setPage(0); setCursors([null]); setSnapshot(null); setNotice(null) }
  return <div data-wb-live="products">
    <div className="profile-page-head"><h1 className="profile-page-title">Товары WB</h1><button className="btn btn-default btn-sm" type="button" disabled={loading || !account.accountId} onClick={() => setRevision((value) => value + 1)}>Обновить список</button></div>
    <WbAccountSelect account={account} />
    {!account.loading && !account.accountId ? <p>Подключите или выберите аккаунт WB в <a href="/settings/profile">настройках подключения</a>.</p> : null}
    {account.accountId ? <>
      <WbSyncStatus sync={sync} />
      <p className="profile-helper-text">Текущие карточки и цены WB из сохранённых данных выбранного аккаунта. Финансовые показатели, остатки и стратегии в этот источник не входят.</p>
      <form className="profile-token-actions" onSubmit={(event) => { event.preventDefault(); changeFilter({ ...filter, q: queryDraft, brand: brandDraft }) }}>
        <input className="profile-input" aria-label="Поиск товаров WB" placeholder="Название или артикул" value={queryDraft} onChange={(event) => setQueryDraft(event.target.value)} />
        <input className="profile-input" aria-label="Бренд WB" placeholder="Точное название бренда" value={brandDraft} onChange={(event) => setBrandDraft(event.target.value)} />
        <button className="btn btn-default btn-sm" type="submit">Найти</button>
        <select className="profile-input" aria-label="Сортировка товаров WB" value={filter.sort} onChange={(event) => changeFilter({ ...filter, sort: event.target.value as WbProductSort })}><option value="nmId">Артикул WB</option><option value="vendorCode">Артикул продавца</option><option value="title">Название</option><option value="brand">Бренд</option></select>
        <select className="profile-input" aria-label="Направление сортировки" value={filter.direction} onChange={(event) => changeFilter({ ...filter, direction: event.target.value as 'asc' | 'desc' })}><option value="asc">По возрастанию</option><option value="desc">По убыванию</option></select>
      </form>
      {error ? <div className="profile-token-feedback is-error" role="alert">{error}{data ? ' Показана предыдущая успешная загрузка этой страницы.' : ''}</div> : null}
      {notice ? <div className="profile-token-feedback" role="status">{notice}</div> : null}
      {loading ? <div role="status">Загрузка страницы товаров…</div> : null}
      {data ? <><div className="profile-token-feedback" role="status">{readinessLabels[data.readiness]}{data.items.length === 0 && data.readiness !== 'empty' ? ' По выбранным условиям сохранённых товаров нет.' : ''}</div>
        <div className="table-wrap"><table><thead><tr><th scope="col">Товар</th><th scope="col">Артикул WB</th><th scope="col">Артикул продавца</th><th scope="col">Бренд</th><th scope="col">Категория</th><th scope="col">Размеры и цены</th><th scope="col">Обновление</th></tr></thead><tbody>
          {data.items.map((product) => <tr key={product.nmId}><td>{product.title || '—'}{product.truncatedFields?.length ? <div>Часть полей сокращена</div> : null}</td><td>{product.nmId}</td><td>{product.vendorCode || '—'}</td><td>{product.brand || '—'}</td><td>{product.subjectName || '—'}</td><td>{product.sizes.length ? product.sizes.map((size) => <div key={size.chrtId}>{size.techSize || 'Без размера'}: {formatWbPrice(size.discountedPriceKopecks ?? size.priceKopecks)}{size.truncatedFields?.length ? ' · часть полей сокращена' : ''}</div>) : '—'}{product.sizesTruncated ? <div>Показана часть размеров: {product.sizes.length}</div> : null}</td><td>{product.contentUpdatedAt ? new Date(product.contentUpdatedAt).toLocaleString('ru-RU') : '—'}{!product.pricesUpdatedAt ? <div>Цены ещё не загружены</div> : <div>Цены: {new Date(product.pricesUpdatedAt).toLocaleString('ru-RU')}</div>}</td></tr>)}
        </tbody></table></div>
        <div className="profile-token-actions"><button className="btn btn-default btn-sm" type="button" disabled={page === 0 || loading} onClick={() => setPage((value) => value - 1)}>Назад</button><span>Страница {page + 1} · до 50 товаров</span><button className="btn btn-default btn-sm" type="button" disabled={!data.nextCursor || loading} onClick={() => { if (data.nextCursor) { setCursors((values) => [...values.slice(0, page + 1), data.nextCursor]); setPage((value) => value + 1) } }}>Далее</button></div>
      </> : null}
    </> : null}
  </div>
}
