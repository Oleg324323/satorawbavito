import { useState, type FormEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { AuthPageFrame } from './AuthPageFrame'
import { useAuth } from './authContext'

export function RegisterPage() {
  const wbLiveEnabled = import.meta.env.VITE_WB_LIVE_ENABLED === 'true'
  const navigate = useNavigate()
  const { register } = useAuth()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [fullName, setFullName] = useState('')
  const [companyName, setCompanyName] = useState('Огни')
  const [wbToken, setWbToken] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setSubmitting(true)
    setError(null)
    try {
      await register({ email, password, fullName, companyName, wbToken: wbLiveEnabled ? undefined : wbToken || undefined })
      navigate(wbLiveEnabled ? '/settings/profile' : '/wb/repricer', { replace: true })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось зарегистрироваться')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <AuthPageFrame title="Регистрация" subtitle={wbLiveEnabled ? 'Создайте рабочую область. Подключить аккаунт Wildberries можно на следующем шаге.' : 'Создайте рабочую область и передайте WB token в backend через защищенный API.'}>
      <form className="grid gap-4" onSubmit={handleSubmit}>
        <label className="grid gap-1 text-sm">
          <span className="text-slate-300">Имя</span>
          <input className="rounded-lg border border-white/10 bg-white/10 px-3 py-2 text-slate-100 outline-none focus:border-sky-400" value={fullName} onChange={(event) => setFullName(event.target.value)} required />
        </label>
        <label className="grid gap-1 text-sm">
          <span className="text-slate-300">Компания</span>
          <input className="rounded-lg border border-white/10 bg-white/10 px-3 py-2 text-slate-100 outline-none focus:border-sky-400" value={companyName} onChange={(event) => setCompanyName(event.target.value)} required />
        </label>
        <label className="grid gap-1 text-sm">
          <span className="text-slate-300">Email</span>
          <input className="rounded-lg border border-white/10 bg-white/10 px-3 py-2 text-slate-100 outline-none focus:border-sky-400" type="email" value={email} onChange={(event) => setEmail(event.target.value)} required />
        </label>
        <label className="grid gap-1 text-sm">
          <span className="text-slate-300">Пароль</span>
          <input className="rounded-lg border border-white/10 bg-white/10 px-3 py-2 text-slate-100 outline-none focus:border-sky-400" type="password" value={password} onChange={(event) => setPassword(event.target.value)} required />
        </label>
        {!wbLiveEnabled ? <label className="grid gap-1 text-sm">
          <span className="text-slate-300">WB token</span>
          <input className="rounded-lg border border-white/10 bg-white/10 px-3 py-2 text-slate-100 outline-none focus:border-sky-400" value={wbToken} onChange={(event) => setWbToken(event.target.value)} />
        </label> : null}
        {error ? <div className="rounded-lg border border-red-400/30 bg-red-500/10 px-3 py-2 text-sm text-red-200">{error}</div> : null}
        <button className="rounded-lg bg-sky-400 px-4 py-2 text-sm font-semibold text-slate-950 disabled:opacity-60" type="submit" disabled={submitting}>
          {submitting ? 'Создаем...' : 'Создать аккаунт'}
        </button>
      </form>
      <p className="mt-4 text-sm text-slate-400">
        Уже есть аккаунт? <Link className="text-sky-300" to="/auth/login">Войти</Link>
      </p>
    </AuthPageFrame>
  )
}
