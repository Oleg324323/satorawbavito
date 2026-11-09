import { useEffect, useRef, useState } from 'react'
import { useAuth } from '@/features/auth/authContext'
import { authorizationHeaders } from '@/features/auth/authApi'
import { apiData } from '@/lib/api'

type KeyStatus = { configured: boolean; storageAvailable: boolean; serverConfigured: boolean }
const endpoint = '/api/v1/cabinet/openai-key'

export function OpenAiKeySettings() {
  const { accessToken, cabinetMe } = useAuth()
  const [status, setStatus] = useState<KeyStatus | null>(null)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const epoch = useRef(0)
  const writable = cabinetMe?.user.permissions.includes('integrations:write') ?? false

  useEffect(() => {
    let cancelled = false
    epoch.current += 1
    setStatus(null); setDraft(''); setNotice(''); setBusy(false)
    if (accessToken) void apiData<KeyStatus>(endpoint, { headers: authorizationHeaders(accessToken) })
      .then(value => { if (!cancelled) setStatus(value) })
      .catch(() => { if (!cancelled) setNotice('Не удалось загрузить настройки OpenAI.') })
    return () => { cancelled = true; epoch.current += 1 }
  }, [accessToken])

  async function update(remove = false) {
    if (!accessToken || !writable || busy) return
    const apiKey = draft.trim()
    if (!remove && (!apiKey.startsWith('sk-') || apiKey.length < 40 || apiKey.length > 512)) {
      setNotice('Вставьте полный OpenAI API key, начинающийся с sk-.'); return
    }
    setBusy(true); setNotice(''); setDraft('')
    const current = epoch.current
    try {
      const value = await apiData<KeyStatus>(endpoint, { method: remove ? 'DELETE' : 'PUT',
        headers: authorizationHeaders(accessToken), ...(remove ? {} : { body: JSON.stringify({ apiKey }) }) })
      if (current !== epoch.current) return
      setStatus(value)
      setNotice(remove ? 'Ключ компании удалён.' : 'Ключ сохранён. Будет использован при следующем сборе Авито.')
    } catch {
      if (current === epoch.current) setNotice('Не удалось сохранить настройку. Проверьте доступ и настройку шифрования backend.')
    } finally { if (current === epoch.current) setBusy(false) }
  }

  return <section className="profile-token-card" aria-label="OpenAI API key">
    <div className="profile-token-head">
      <div><div className="profile-card-title">OpenAI · поля Авито</div>
        <div className="profile-helper-text">Размеры из чатов распознаются бесплатно без ключа. Ключ нужен только для необязательного извлечения цвета и артикула из описания. Хранится зашифрованным на сервере, не в расширении.</div></div>
      <span className={`access-badge ${status?.configured || status?.serverConfigured ? 'ok' : ''}`}>
        {!status ? 'проверяем' : status.configured ? 'настроен' : status.serverConfigured ? 'ключ сервера' : 'не настроен'}
      </span>
    </div>
    <form onSubmit={event => { event.preventDefault(); void update() }}>
      <div className="profile-field"><label htmlFor="profileOpenAiKey">OpenAI API key</label>
        <div className="profile-input-wrap"><input id="profileOpenAiKey" className="profile-input"
          type="password" autoComplete="off" spellCheck={false} maxLength={512} placeholder="sk-…"
          value={draft} onChange={event => setDraft(event.currentTarget.value)}
          disabled={busy || !writable || !status?.storageAvailable} /></div></div>
      {status && !status.storageAvailable ? <p className="profile-token-feedback">Для ввода ключа администратору сервера нужно включить защищённое хранилище. VPS-конфигурация проекта включает его автоматически.</p> : null}
      {!writable ? <p className="profile-token-feedback">Изменять ключ может администратор интеграций.</p> : null}
      <p className="profile-token-feedback" role="status">{notice || 'Сохранённый ключ не возвращается в браузер. Сохранение не проверяет ключ у OpenAI и не запускает платный запрос.'}</p>
      <div className="profile-token-actions">
        <button className="btn btn-primary btn-sm" type="submit" disabled={busy || !writable || !status?.storageAvailable || !draft.trim()}>{busy ? 'Сохраняем…' : 'Сохранить ключ'}</button>
        <button className="btn btn-ghost btn-sm" type="button" disabled={busy || !writable || !status?.configured} onClick={() => void update(true)}>Удалить ключ</button>
      </div>
    </form>
  </section>
}
