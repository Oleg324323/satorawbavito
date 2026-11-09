import { useEffect, useId, useRef, useState } from 'react'
import { z } from 'zod'
import { useAuth } from '@/features/auth/authContext'
import { authorizationHeaders } from '@/features/auth/authApi'
import { wbErrorMessage, type WbAccount } from '@/features/wb-live/api'
import { parseWbAccounts } from '@/features/wb-live/validation'
import { apiData, apiRequest, buildApiUrl } from '@/lib/api'
import { encodeWbExtensionConnectionCode } from './extensionConnectionCode'

const connectionSchema = z.object({
  marketplaceAccountId: z.number().int().positive(),
  configured: z.boolean(),
  tokenPrefix: z.string().max(100).nullable(),
  expiresAt: z.string().datetime({ offset: true }).nullable(),
  lastObservedAt: z.string().datetime({ offset: true }).nullable(),
  knownPrices: z.number().int().nonnegative(),
})
type Connection = z.infer<typeof connectionSchema>
const endpoint = '/api/v1/wb/browser-prices/connection'

function connection(value: unknown, accountId: number) {
  const parsed = connectionSchema.parse(value)
  if (parsed.marketplaceAccountId !== accountId) throw new Error('Unexpected account')
  return parsed
}

function formatTime(value: string) {
  return new Intl.DateTimeFormat('ru-RU', { dateStyle: 'short', timeStyle: 'short' }).format(new Date(value))
}

function ConnectionDetails({ accessToken, accountId }: { accessToken: string; accountId: number }) {
  const keyId = useId(), epoch = useRef(0)
  const [data, setData] = useState<Connection | null>(null)
  const [oneTimeKey, setOneTimeKey] = useState<string | null>(null)
  const [loading, setLoading] = useState(true), [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null), [uncertain, setUncertain] = useState(false)
  const [copied, setCopied] = useState(false), [revision, setRevision] = useState(0)
  const path = `${endpoint}?marketplaceAccountId=${accountId}`

  useEffect(() => {
    const controller = new AbortController(), current = ++epoch.current
    setLoading(true); setBusy(false); setData(null); setOneTimeKey(null); setCopied(false); setError(null)
    void apiRequest<unknown>(path, { headers: authorizationHeaders(accessToken), cache: 'no-store', signal: controller.signal })
      .then(value => {
        if (current !== epoch.current) return
        setData(connection(value, accountId)); setUncertain(false)
      })
      .catch(failure => { if (current === epoch.current) setError(wbErrorMessage(failure)) })
      .finally(() => { if (current === epoch.current) setLoading(false) })
    return () => { controller.abort(); epoch.current += 1 }
  }, [accessToken, accountId, path, revision])

  async function mutate(method: 'POST' | 'DELETE') {
    if (busy || loading || uncertain || !data) return
    const current = epoch.current
    setBusy(true); setError(null); setOneTimeKey(null); setCopied(false)
    try {
      const raw = await apiRequest<unknown>(method === 'POST' ? endpoint : path, {
        method, headers: authorizationHeaders(accessToken), cache: 'no-store',
        ...(method === 'POST' ? { body: JSON.stringify({ marketplaceAccountId: accountId }) } : {}),
      })
      if (current !== epoch.current) return
      const next = connection(raw, accountId)
      if (next.configured !== (method === 'POST')) throw new Error('Unexpected connection status')
      const issued = method === 'POST' ? z.object({ token: z.string().min(1).max(1024) }).parse(raw).token : null
      const origin = new URL(buildApiUrl(endpoint), window.location.href).origin
      setData(next); setOneTimeKey(issued ? encodeWbExtensionConnectionCode(origin, issued) : null)
    } catch (failure) {
      if (current === epoch.current) { setError(wbErrorMessage(failure, true)); setUncertain(true) }
    } finally { if (current === epoch.current) setBusy(false) }
  }

  async function copyKey() {
    if (!oneTimeKey) return
    const current = epoch.current
    try {
      await navigator.clipboard.writeText(oneTimeKey)
      if (current === epoch.current) setCopied(true)
    } catch {
      if (current === epoch.current) setError('Не удалось скопировать ключ. Выделите его и скопируйте вручную.')
    }
  }

  return <>
    <div className="profile-token-status" aria-live="polite">
      <div className="profile-token-status-head">
        <b>{loading ? 'Проверяем подключение…' : uncertain ? 'Нужно проверить результат действия' : data?.configured ? 'Ключ активен' : data ? 'Ключ не выдан' : 'Состояние недоступно'}</b>
        {data ? <span>{data.knownPrices > 0 ? `Цен при последней проверке: ${data.knownPrices.toLocaleString('ru-RU')}` : 'Цены ещё не получены'}</span> : null}
      </div>
      {data ? <div className="profile-token-meta-grid">
        <div className="profile-token-meta-item"><b>Последние наблюдения</b><span>{data.lastObservedAt ? formatTime(data.lastObservedAt) : 'ещё не поступали'}</span></div>
        <div className="profile-token-meta-item"><b>Ключ действует до</b><span>{data.expiresAt ? formatTime(data.expiresAt) : '—'}</span></div>
        {data.tokenPrefix ? <div className="profile-token-meta-item"><b>Ключ</b><span>{data.tokenPrefix}…</span></div> : null}
      </div> : null}
    </div>
    {oneTimeKey ? <div className="profile-field">
      <label htmlFor={keyId}>Ключ расширения WB</label>
      <textarea id={keyId} className="profile-input profile-token-textarea" value={oneTimeKey} readOnly rows={2} spellCheck={false} />
      <span className="profile-helper-text">Вставьте весь токен в единственное поле расширения WB версии 0.1.12 или новее. Адрес этой Satorna включён автоматически. После ухода с экрана токен будет скрыт.</span>
      <button className="btn btn-default btn-sm" type="button" onClick={() => void copyKey()}>Копировать ключ</button>
      {copied ? <span role="status">Ключ скопирован</span> : null}
    </div> : null}
    {error ? <div className="profile-token-feedback is-error" role="alert">{error}</div> : null}
    <div className="profile-token-actions">
      <button className="btn btn-primary btn-sm" type="button" disabled={loading || busy || uncertain || !data} onClick={() => void mutate('POST')}>{data?.configured ? 'Перевыпустить ключ' : 'Получить ключ'}</button>
      <button className="btn btn-ghost btn-sm" type="button" disabled={loading || busy || uncertain || !data?.configured} onClick={() => void mutate('DELETE')}>Отключить</button>
      <button className="btn btn-default btn-sm" type="button" disabled={loading || busy} onClick={() => setRevision(value => value + 1)}>Обновить состояние</button>
    </div>
  </>
}

function AccountConnection({ accessToken }: { accessToken: string }) {
  const titleId = useId(), accountSelectId = useId()
  const [accounts, setAccounts] = useState<WbAccount[]>([]), [accountId, setAccountId] = useState<number | null>(null)
  const [loading, setLoading] = useState(true), [error, setError] = useState<string | null>(null)
  const [revision, setRevision] = useState(0)
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true); setAccounts([]); setError(null)
    void apiData<unknown>('/api/v1/wb/browser-prices/accounts', {
      headers: authorizationHeaders(accessToken), cache: 'no-store', signal: controller.signal,
    }).then(value => {
      if (controller.signal.aborted) return
      const next = parseWbAccounts(value)
      if (next.some(item => item.provider !== 'wb')) throw new Error('Unexpected marketplace')
      setAccounts(next)
      setAccountId(previous => next.some(item => item.marketplaceAccountId === previous) ? previous : next.length === 1 ? next[0].marketplaceAccountId : null)
    }).catch(failure => { if (!controller.signal.aborted) setError(wbErrorMessage(failure)) })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [accessToken, revision])
  const selected = accounts.find(item => item.marketplaceAccountId === accountId)
  return <section className="profile-token-card" aria-labelledby={titleId}>
    <div className="profile-token-head"><div>
      <div className="profile-card-title" id={titleId}>Расширение WB: цены и СПП</div>
      <div className="profile-helper-text">Собирает покупательские цены из вашего браузера. Cookies и API-ключ WB передавать не нужно. Ключ расширения не даёт права изменять цены на WB.</div>
      <div className="profile-helper-text">Для подключения нужен только токен ниже: адрес определяется автоматически. Для локальной и опубликованной платформы используются разные токены.</div>
    </div></div>
    <div className="profile-field">
      <label htmlFor={accountSelectId}>Аккаунт WB для расширения</label>
      <select id={accountSelectId} className="profile-input" value={selected?.marketplaceAccountId ?? ''} disabled={loading} onChange={event => setAccountId(event.target.value ? Number(event.target.value) : null)}>
        <option value="">{loading ? 'Загрузка аккаунтов…' : 'Выберите аккаунт'}</option>
        {accounts.map(item => <option key={item.marketplaceAccountId} value={item.marketplaceAccountId}>{item.displayName || `WB · ${item.externalAccountId}`}</option>)}
      </select>
      {error ? <div className="profile-token-feedback is-error" role="alert">{error} <button className="btn btn-ghost btn-sm" type="button" onClick={() => setRevision(value => value + 1)}>Повторить чтение аккаунтов</button></div> : null}
      {!loading && !error && !accounts.length ? <p>Нет доступных аккаунтов WB. Проверьте подключение и права доступа.</p> : null}
    </div>
    {selected ? <ConnectionDetails key={selected.marketplaceAccountId} accessToken={accessToken} accountId={selected.marketplaceAccountId} /> : null}
    <div className="profile-token-feedback">
      <a href="/downloads/satorna-wb-prices-extension-0.1.12.zip" download>Скачать расширение для Chrome, Edge и Яндекс Браузера</a>
      <ol>
        <li>Распакуйте архив. Откройте страницу расширений браузера (в Chrome: chrome://extensions) и включите «Режим разработчика».</li>
        <li>Нажмите «Загрузить распакованное расширение» и выберите распакованную папку.</li>
        <li>Получите ключ для выбранного аккаунта и вставьте его в настройки расширения. Откройте WB и запустите сбор.</li>
      </ol>
      <p>Последние данные СПП сохраняются до получения новых наблюдений по товару. Запуск расширения не очищает сохранённые данные. Для обновления браузер и расширение должны работать.</p>
    </div>
  </section>
}

export function WbBrowserPricesConnection() {
  const { accessToken, cabinetMe } = useAuth()
  if (!accessToken || !cabinetMe?.activeSession) return null
  const scope = `${cabinetMe.activeSession.sessionId}:${cabinetMe.organization.organizationId}:${cabinetMe.user.userId}`
  return <AccountConnection key={scope} accessToken={accessToken} />
}
