import { useLayoutEffect, useRef, useState } from 'react'
import { useAuth } from '@/features/auth/authContext'
import { resetLiveRepricerParityCache } from '@/features/wb-repricer/liveParityData'
import { invalidateCostViews } from '@/features/wb-repricer/costInvalidation'
import { requestQuarterlyTax, quarterlyTaxError, mergeQuarterlyTaxValues, type QuarterlyTaxSettings } from './quarterlyTaxApi'

export type { QuarterlyTaxSettings } from './quarterlyTaxApi'
const quarters = [['I', 'Январь–март'], ['II', 'Апрель–июнь'], ['III', 'Июль–сентябрь'], ['IV', 'Октябрь–декабрь']]

export function parseQuarterlyTaxRates(values: string[]) {
  return values.map(value => {
    if (!value.trim()) return null
    const normalized = value.trim().replace(',', '.')
    if (!/^\d+(?:\.\d{1,2})?$/.test(normalized) || Number(normalized) > 100) {
      throw new Error('Введите ставку от 0 до 100 с точностью до 0,01%.')
    }
    return Number(normalized)
  })
}

export function WbQuarterlyTaxSettings() {
  const { accessToken, cabinetMe } = useAuth()
  const identity = `${cabinetMe?.organization.organizationId}:${cabinetMe?.user.userId}`
  const [year, setYear] = useState(() => Number(new Intl.DateTimeFormat('en', { timeZone: 'Europe/Moscow', year: 'numeric' }).format(new Date())))
  const [saved, setSaved] = useState<QuarterlyTaxSettings | null>(null)
  const [values, setValues] = useState<string[]>(['', '', '', ''])
  const [busy, setBusy] = useState(false), [notice, setNotice] = useState('')
  const [operation, setOperation] = useState<'load' | 'save'>('load')
  const [reload, setReload] = useState(0)
  const generation = useRef(0), pending = useRef(false)
  const loadedScope = useRef(''), dirty = useRef([false, false, false, false])
  const writable = cabinetMe?.user.permissions.includes('settings:write') ?? false

  useLayoutEffect(() => {
    const epoch = ++generation.current, controller = new AbortController()
    const scope = `${identity}:${year}`
    if (loadedScope.current !== scope) {
      loadedScope.current = scope; dirty.current = [false, false, false, false]
      setSaved(null); setValues(['', '', '', ''])
    }
    setNotice(''); setOperation('load'); setBusy(true); pending.current = false
    if (accessToken && cabinetMe) void requestQuarterlyTax(accessToken, year, undefined, controller.signal).then(view => {
      if (epoch !== generation.current) return
      setSaved(view); setValues(current => mergeQuarterlyTaxValues(current, view.rates, dirty.current))
    }).catch(failure => {
      if (!controller.signal.aborted && epoch === generation.current) setNotice(quarterlyTaxError(failure))
    }).finally(() => { if (epoch === generation.current) setBusy(false) })
    else setBusy(false)
    return () => { controller.abort(); generation.current += 1 }
  }, [accessToken, identity, year, reload])

  async function save() {
    if (!saved || !accessToken || !writable || busy || pending.current) return
    let rates: Array<number | null>
    try { rates = parseQuarterlyTaxRates(values) } catch (failure) { setNotice((failure as Error).message); return }
    const epoch = generation.current
    pending.current = true; setOperation('save'); setBusy(true); setNotice('')
    try {
      const view = await requestQuarterlyTax(accessToken, year, { rates, revision: saved.revision })
      if (epoch !== generation.current) return
      if (view.revision !== saved.revision) {
        resetLiveRepricerParityCache(accessToken)
        invalidateCostViews()
      }
      dirty.current = [false, false, false, false]
      setSaved(view); setValues(view.rates.map(rate => rate == null ? '' : String(rate)))
      setNotice('Налог сохранён. Отчёты будут пересчитаны из сохранённых данных при открытии; повторная загрузка WB не требуется.')
    } catch (failure) {
      if (epoch === generation.current) setNotice(quarterlyTaxError(failure, true))
    } finally {
      if (epoch === generation.current) { setBusy(false); pending.current = false }
    }
  }

  return <section className="profile-token-card" aria-label="Квартальный налог WB">
    <div className="profile-token-head"><div className="profile-card-title">Налог WB по кварталам</div></div>
    <div className="profile-field"><label htmlFor="wbTaxYear">Календарный год</label>
      <input id="wbTaxYear" className="profile-input" type="number" min="1900" max="9998" step="1" value={year} disabled={busy}
        onChange={event => { const next = Number(event.currentTarget.value); if (Number.isInteger(next) && next >= 1900 && next <= 9998) setYear(next) }} /></div>
    <p className="profile-helper-text">Для расчётов WB за любой выбранный период используется действующая сейчас ставка из профиля: ставка текущего квартала (Москва), если она задана, иначе общая ставка. Изменение этой ставки пересчитывает прошлые периоды из сохранённых данных.</p>
    <form onSubmit={event => { event.preventDefault(); void save() }}>
      <div className="profile-form-grid">{quarters.map(([roman, months], index) => <div className="profile-field" key={roman}>
        <label htmlFor={`wbTaxQ${index + 1}`}>Налог — {roman} квартал, %</label>
        <input id={`wbTaxQ${index + 1}`} className="profile-input" type="text" inputMode="decimal" placeholder="Не настроен"
          value={values[index]} disabled={busy || !saved || !writable}
          onChange={event => { const next = event.currentTarget.value; dirty.current[index] = true; setValues(current => current.map((value, i) => i === index ? next : value)) }} />
        <span className="profile-helper-text">{months}{saved?.configured[index] ? ' · квартальная ставка' : ' · прежние правила'}</span>
      </div>)}</div>
      <p className="profile-token-feedback" role="status">{busy ? operation === 'save' ? 'Сохраняем настройки…' : 'Загружаем настройки…' : notice || (!writable ? 'Изменение доступно пользователям с правом настройки.' : '0% — допустимая ставка. Пустое поле не означает нулевой налог.')}</p>
      <div className="profile-token-actions">
        <button className="btn btn-primary btn-sm" type="submit" disabled={busy || !saved || !writable}>Сохранить налог</button>
        <button className="btn btn-default btn-sm" type="button" disabled={busy} onClick={() => setReload(value => value + 1)}>Перечитать ставки</button>
      </div>
    </form>
  </section>
}
