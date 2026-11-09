import { useLayoutEffect, useRef, useState } from 'react'
import { ApiError } from '@/lib/api'
import { readWbData, syncPath, wbErrorMessage, type WbSync } from './api'
import { parseWbSync } from './validation'
import { prepareWbHistory, startWbHistory, validWbHistoryDate, wbHistorySource } from './history'

/** Keyed by session/token/account in WbConnection. Initialization, not cursor reset or retry policy. */
export function WbHistoryInitialization({ token, accountId, sync, credentialActive, blocked, onBusyChange, refresh }: {
  token: string; accountId: number; sync: WbSync | null; credentialActive: boolean; blocked: boolean;
  onBusyChange: (busy: boolean) => void; refresh: () => void;
}) {
  const [date, setDate] = useState(''), [busy, setBusy] = useState(false), [readbackRequired, setReadbackRequired] = useState(false)
  const [error, setError] = useState<string | null>(null), [confirmed, setConfirmed] = useState<WbSync | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const epoch = useRef(0), pending = useRef(false), controller = useRef<AbortController | null>(null)
  const intent = useRef<ReturnType<typeof prepareWbHistory> | null>(null)
  useLayoutEffect(() => () => { ++epoch.current; controller.current?.abort(); onBusyChange(false) }, [])
  const sourceExists = Boolean(sync?.sources.some(source => source.source === wbHistorySource) || confirmed?.sources.some(source => source.source === wbHistorySource))
  const ready = Boolean(sync || confirmed) && credentialActive && !sourceExists && !busy && !blocked && !readbackRequired
  async function run(readOnly: boolean) {
    if (pending.current || blocked || (!readOnly && (!ready || !validWbHistoryDate(date)))) return
    const operation = ++epoch.current, active = () => epoch.current === operation
    const request = new AbortController(); controller.current = request; pending.current = true
    setBusy(true); onBusyChange(true); setError(null); setNotice(null)
    try {
      if (!readOnly && intent.current?.dateFrom !== date) intent.current = prepareWbHistory(date)
      const result = readOnly
        ? await readWbData(token, syncPath(accountId), request.signal, value => parseWbSync(value, accountId))
        : await startWbHistory(token, accountId, intent.current!, request.signal)
      if (!active()) return
      setConfirmed(result); setReadbackRequired(false); refresh()
      setNotice(readOnly ? 'Сохранённое состояние перечитано. Автоматический повтор записи не выполнялся.' : 'Запрос инициализации принят. Ход загрузки показан в состоянии источников.')
    } catch (failure) {
      if (!active()) return
      setReadbackRequired(true)
      setError(failure instanceof ApiError && failure.status === 409
        ? 'Инициализация конфликтует с текущей историей или её контрольной точкой. Перечитайте состояние; сброс курсора здесь не выполняется.'
        : wbErrorMessage(failure, !readOnly))
    } finally { if (active()) { pending.current = false; setBusy(false); onBusyChange(false) } }
  }
  return <section className="profile-token-card" data-wb-history="initialization">
    <h3>Начальная загрузка истории WB</h3>
    <p>Укажите дату сами. Загружаются предварительные сведения WB Statistics, а не полная операционная очередь, готовность к отгрузке или окончательная прибыль.</p>
    <p>Первый запуск может добавить загрузку карточек и цен. Доступный период и полнота зависят от текущих ограничений хранения и выдачи WB; выбранная дата не гарантирует получение всех исторических записей.</p>
    {sourceExists ? <p>История уже инициализирована. Управление существующей контрольной точкой, смена периода и сброс курсора в этом интерфейсе недоступны.</p> : <>
      <label htmlFor="wb-history-date">Начальная дата истории (по московскому времени)</label>
      <input id="wb-history-date" className="profile-input" type="date" value={date} disabled={busy || blocked || readbackRequired} onChange={event => setDate(event.target.value)} />
      <button type="button" className="btn btn-default" disabled={!ready || !validWbHistoryDate(date)} onClick={() => void run(false)}>Инициализировать историю с выбранной даты</button>
    </>}
    {!sync && !confirmed ? <p>Перед запуском необходимо прочитать состояние загрузки.</p> : null}
    {busy ? <p role="status">Выполняем запрос истории…</p> : null}{error ? <p role="alert">{error}</p> : null}{notice ? <p role="status">{notice}</p> : null}
    <button type="button" className="btn btn-ghost" disabled={busy || blocked} onClick={() => void run(true)}>Перечитать состояние истории</button>
  </section>
}
