import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useAuth } from '@/features/auth/authContext'
import { NotificationPreferencesError, requestNotificationPreferences, type CanonicalNotificationPreferences, type CanonicalPreferenceFlags } from './canonicalNotificationPreferences'

export function CanonicalNotificationPreferencesForm() {
  const { accessToken, cabinetMe } = useAuth()
  const key = `${cabinetMe?.activeSession?.sessionId}:${cabinetMe?.organization.organizationId}:${cabinetMe?.user.userId}`
  const epoch = useRef(0), pending = useRef(false)
  const [saved, setSaved] = useState<CanonicalNotificationPreferences | null>(null)
  const [draft, setDraft] = useState<CanonicalPreferenceFlags | null>(null)
  const [loading, setLoading] = useState(false), [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null), [readback, setReadback] = useState(false)
  const [revision, setRevision] = useState(0)
  const [message, setMessage] = useState<string | null>(null)
  const writeController = useRef<AbortController | null>(null)
  useLayoutEffect(() => {
    epoch.current += 1; pending.current = false; setSaved(null); setDraft(null); setError(null); setSaving(false); setReadback(false); setMessage(null)
    return () => { epoch.current += 1; writeController.current?.abort() }
  }, [key, accessToken])
  useEffect(() => {
    if (!accessToken || !cabinetMe) return
    const controller = new AbortController(), generation = epoch.current
    setLoading(true); setError(null)
    void requestNotificationPreferences(accessToken, controller.signal).then((data) => {
      if (controller.signal.aborted || epoch.current !== generation) return
      setSaved(data); setDraft({ email: { ...data.email }, telegram: { ...data.telegram } }); setReadback(false)
    }).catch((failure) => {
      if (!controller.signal.aborted && epoch.current === generation) {
        if (failure instanceof NotificationPreferencesError && (failure.kind === 'unauthenticated' || failure.kind === 'no-access')) { setSaved(null); setDraft(null) }
        setError(failure instanceof NotificationPreferencesError ? failure.message : 'Не удалось прочитать настройки уведомлений.')
      }
    }).finally(() => { if (!controller.signal.aborted && epoch.current === generation) setLoading(false) })
    return () => controller.abort()
  }, [key, accessToken, revision])
  async function save() {
    if (!accessToken || !saved || !draft || error || loading || pending.current || readback) return
    const generation = ++epoch.current, controller = new AbortController()
    writeController.current = controller; pending.current = true; setSaving(true); setError(null); setMessage(null)
    try {
      const result = await requestNotificationPreferences(accessToken, controller.signal, { expectedVersion: saved.version, values: draft })
      if (epoch.current !== generation) return
      setSaved(result); setDraft({ email: { ...result.email }, telegram: { ...result.telegram } }); setMessage('Настройки сохранены.')
    } catch (failure) {
      if (epoch.current !== generation) return
      if (failure instanceof NotificationPreferencesError && (failure.kind === 'unauthenticated' || failure.kind === 'no-access')) { setSaved(null); setDraft(null) }
      setReadback(true); setError(failure instanceof NotificationPreferencesError ? failure.message : 'Результат сохранения неизвестен. Перечитайте настройки.')
    } finally { if (epoch.current === generation) { pending.current = false; setSaving(false) } }
  }
  return <section className="profile-card" data-canonical-notification-preferences="true">
    <div className="profile-card-head"><div className="profile-card-title">Настройки уведомлений</div></div>
    <div className="profile-card-body">
      <p className="profile-helper-text">Личные предпочтения уведомлений. Подключение адресов и каналов выполняется отдельно.</p>
      {loading ? <div role="status">Читаем сохранённые настройки…</div> : null}
      {error ? <div className="profile-token-feedback is-error" role="alert">{error}</div> : null}
      {message ? <div className="profile-token-feedback" role="status">{message}</div> : null}
      {draft ? <fieldset disabled={loading || saving || readback || Boolean(error)} className="profile-form-grid">
        <label><input type="checkbox" checked={draft.email.enabled} onChange={(event) => setDraft({ ...draft, email: { ...draft.email, enabled: event.target.checked } })} /> Email-уведомления</label>
        <label><input type="checkbox" checked={draft.email.dailyDigest} onChange={(event) => setDraft({ ...draft, email: { ...draft.email, dailyDigest: event.target.checked } })} /> Ежедневный дайджест</label>
        <label><input type="checkbox" checked={draft.email.criticalAlerts} onChange={(event) => setDraft({ ...draft, email: { ...draft.email, criticalAlerts: event.target.checked } })} /> Критичные события</label>
        <label><input type="checkbox" checked={draft.telegram.enabled} onChange={(event) => setDraft({ ...draft, telegram: { ...draft.telegram, enabled: event.target.checked } })} /> Telegram-уведомления</label>
      </fieldset> : null}
      <div className="profile-token-actions"><button type="button" className="btn btn-primary btn-sm" disabled={!draft || loading || saving || readback || Boolean(error)} onClick={() => void save()}>{saving ? 'Сохраняем…' : 'Сохранить настройки уведомлений'}</button><button type="button" className="btn btn-default btn-sm" disabled={loading || saving} onClick={() => { setMessage(null); setRevision((value) => value + 1) }}>Перечитать настройки</button></div>
    </div>
  </section>
}
