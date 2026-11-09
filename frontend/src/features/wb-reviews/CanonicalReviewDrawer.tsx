import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useAuth } from '@/features/auth/authContext'
import { useWbAccount, WbAccountSelect } from '@/features/wb-live/WbConnection'
import { createCanonicalLocalReviewsClient } from './canonicalLocalReviewsClient'
import type { CanonicalReviewHistoryPage, CanonicalReviewLocalContext, CanonicalReviewScope } from './canonicalLocalReviews'
import { firstManualReviewReady, localReviewReady, prepareReviewDetailCommand } from './canonicalReviewDetail'

export const canonicalReviewSelectionEvent = 'satorna:canonical-review-selected'
export function CanonicalReviewDrawer() {
  const account = useWbAccount(), { cabinetMe } = useAuth()
  const [selection, setSelection] = useState<{ id: string; epoch: number } | null>(null)
  const [target, setTarget] = useState<Element | null>(null)
  useEffect(() => {
    const drawer = document.getElementById('reviewDrawer'), body = drawer?.querySelector('.drawer-body')
    if (!drawer || !body) return
    setTarget(body)
    const selected = (event: Event) => {
      const id = (event as CustomEvent<unknown>).detail
      if (typeof id === 'string' && id.length > 0 && id.length <= 512) setSelection(old => ({ id, epoch: (old?.epoch ?? 0) + 1 }))
    }
    const observer = new MutationObserver(() => { if (!drawer.classList.contains('open')) setSelection(null) })
    observer.observe(drawer, { attributes: true, attributeFilter: ['class'] })
    window.addEventListener(canonicalReviewSelectionEvent, selected)
    return () => { observer.disconnect(); window.removeEventListener(canonicalReviewSelectionEvent, selected) }
  }, [])
  if (!target) return null
  return createPortal(<>
    <style>{'#reviewDrawer .drawer-tabs, #reviewDrawer .drawer-body > .review-drawer-pane { display:none !important; }'}</style>
    <WbAccountSelect account={account} />
    <p>Каноническая карточка выбранного аккаунта. Список отзывов пока использует прежний источник.</p>
    {selection && account.accountId && account.accessToken && cabinetMe ? <LocalDetail
      key={`${account.scope}:${account.accountId}:${selection.epoch}:${selection.id}:${account.accessToken}`}
      token={account.accessToken} externalId={selection.id}
      scope={{ organizationId: cabinetMe.organization.organizationId, marketplaceAccountId: account.accountId, marketplace: 'wb' }}
      permissions={cabinetMe.user.permissions} /> : <p>Выберите аккаунт и отзыв.</p>}
  </>, target)
}

function LocalDetail({ token, externalId, scope, permissions }: {
  token: string; externalId: string; scope: CanonicalReviewScope; permissions: string[];
}) {
  const [context, setContext] = useState<CanonicalReviewLocalContext | null>(null)
  const [history, setHistory] = useState<CanonicalReviewHistoryPage | null>(null)
  const [factText, setFactText] = useState<string | null>(null)
  const [text, setText] = useState(''), [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false), [revision, setRevision] = useState(0)
  const clientRef = useRef<ReturnType<typeof createCanonicalLocalReviewsClient> | null>(null)
  const epoch = useRef(0)
  const writing = useRef(false)
  useLayoutEffect(() => () => { ++epoch.current; clientRef.current?.dispose() }, [])
  useEffect(() => {
    const operation = ++epoch.current
    const current = () => epoch.current === operation
    const reader = createCanonicalLocalReviewsClient({ scope, accessToken: token, isCurrent: current })
    clientRef.current = reader
    setContext(null); setHistory(null); setFactText(null); setText(''); setError(null); setBusy(true)
    void (async () => {
      const fact = await reader.fact(externalId)
      if (!current()) return
      if (fact.state !== 'ready') throw new Error(fact.state)
      setFactText(fact.data.text ?? 'Отзыв без текста.')
      if (fact.data.source_order_state !== 'current') throw new Error('conflict')
      const review = { reviewId: fact.data.review_id, externalReviewId: externalId }
      const result = await reader.context(review)
      if (!current()) return
      if (result.state !== 'ready') throw new Error(result.state)
      if (result.data.review?.sourceObservationId !== fact.data.current_observation_id
        || result.data.review.sourceChecksum !== fact.data.content_checksum) throw new Error('conflict')
      const writer = createCanonicalLocalReviewsClient({ scope, accessToken: token,
        actorMembershipId: result.data.actorMembershipId, isCurrent: current })
      reader.dispose(); clientRef.current = writer
      setContext(result.data); setText(result.data.draft?.text ?? '')
      const page = await writer.history({ reviewId: review.reviewId, limit: 50 })
      if (!current()) return
      if (page.state !== 'ready') throw new Error(page.state)
      setHistory(page.data)
    })().catch(() => { if (current()) { setContext(null); setHistory(null); setFactText(null); setText(''); setError('Карточка или журнал недоступны: нет доступа, источник отсутствует либо версия изменилась. Перечитайте данные.'); } })
      .finally(() => { if (current()) setBusy(false) })
    return () => { ++epoch.current; reader.dispose(); clientRef.current?.dispose() }
  }, [revision]) // This component is keyed by the complete session/account/selection identity.
  const ready = context && localReviewReady(context) && !busy && !error
  const canCreate = context && firstManualReviewReady(context) && !busy && !error
  const canAuthor = (ready || canCreate) && permissions.includes('reviews:write')
  const nextHistory = async () => {
    if (busy || !history?.nextAfterVersion || !history.headId || !clientRef.current) return
    const operation = epoch.current
    setBusy(true)
    const page = await clientRef.current.history({ reviewId: history.reviewId, headId: history.headId,
      throughVersion: history.throughVersion, afterVersion: history.nextAfterVersion, limit: 50 })
    if (epoch.current !== operation) return
    if (page.state === 'ready') setHistory(page.data)
    else { setContext(null); setHistory(null); setFactText(null); setText(''); setError('Журнал недоступен или изменился. Перечитайте карточку.') }
    setBusy(false)
  }
  const submit = async (action: 'create' | 'edit' | 'approved' | 'rejected') => {
    if (writing.current || !(action === 'create' ? canCreate : ready) || !context || !clientRef.current
      || !permissions.includes(action === 'edit' || action === 'create' ? 'reviews:write' : 'reviews:approve')) return
    writing.current = true
    const operation = epoch.current, client = clientRef.current
    setBusy(true)
    try {
      const handle = client.prepare(prepareReviewDetailCommand(context, action, text))
      const result = await client.submit(handle)
      if (epoch.current !== operation) return
      // Never replay an unknown mutation. Readback is a new explicit human action.
      setContext(null); setHistory(null); setText('')
      setError(result.state === 'ready' ? 'Локальное изменение сохранено. Перечитайте карточку. Ответ в WB не отправлен.'
        : 'Результат команды не подтверждён или версия изменилась. Перечитайте карточку перед новым решением.')
    } catch { if (epoch.current === operation) setError('Операция недоступна. Перечитайте карточку.') }
    finally { if (epoch.current === operation) { writing.current = false; setBusy(false) } }
  }
  return <section className="d-section">
    <h3>Локальный ответ · без отправки в WB</h3>
    {busy ? <p role="status">Читаем или сохраняем локальное состояние…</p> : null}
    {error ? <p role="status">{error}</p> : null}
    <button type="button" className="btn btn-default btn-sm" disabled={busy} onClick={() => setRevision(value => value + 1)}>Перечитать карточку</button>
    {factText !== null ? <div className="review-detail-box"><h4>Исходный отзыв WB</h4><p>{factText}</p></div> : null}
    {context ? <>
      <p>{context.draft ? `${context.draft.generation.mode === 'fake' ? 'Тестовый черновик, не отправлять' : 'Ручной черновик'} · версия ${context.draft.revision}` : 'Ручной черновик ещё не создан. Текст вводится человеком, без AI-генерации.'}</p>
      <p>{context.decision ? `Локальное решение: ${context.decision.decisionKind === 'approved' ? 'одобрено' : 'отклонено'}. Это не отправка в WB.` : 'Локальное решение ещё не принято.'}</p>
      <textarea aria-label="Ручной черновик ответа" className="review-drawer-textarea" maxLength={100_000} value={text} disabled={!canAuthor} onChange={event => setText(event.target.value)} />
      {!context.policy || !context.policyHead ? <p>Не выбрана серверная политика. Создание черновика закрыто; политика не создаётся автоматически.</p> : null}
      {!ready && !canCreate ? <p>Правка недоступна без актуального источника и политики. Решение требует сохранённого черновика.</p> : null}
      <div className="profile-token-actions">
        <button type="button" className="btn btn-default" disabled={!canAuthor || !text.trim()} onClick={() => void submit(context.draft ? 'edit' : 'create')}>{context.draft ? 'Сохранить локальную правку' : 'Создать ручной черновик'}</button>
        <button type="button" className="btn btn-success" disabled={!ready || text !== context.draft?.text || !permissions.includes('reviews:approve')} onClick={() => void submit('approved')}>Одобрить локально</button>
        <button type="button" className="btn btn-danger" disabled={!ready || text !== context.draft?.text || !permissions.includes('reviews:approve')} onClick={() => void submit('rejected')}>Отклонить локально</button>
      </div>
    </> : null}
    {history ? <div><h4>Локальный журнал · до 50 событий на странице</h4>{history.events.length === 0 ? <p>Локальных действий пока нет.</p> : history.events.map(event => <p key={event.eventId}>{event.occurredAt} · {event.eventKind} · участник {event.actorMembershipId}</p>)}{history.nextAfterVersion ? <button className="btn btn-default btn-sm" type="button" disabled={busy} onClick={() => void nextHistory()}>Следующие события</button> : null}<p>Чтобы вернуться к началу журнала, перечитайте карточку.</p></div> : null}
  </section>
}
