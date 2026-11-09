import { useEffect, useRef, useState } from 'react'
import { useAuth } from '@/features/auth/authContext'
import { authorizationHeaders } from '@/features/auth/authApi'
import { ApiError, apiData } from '@/lib/api'
import { invalidateCostViews } from './costInvalidation'

type Cost = { amountKopecks: number | null; costVersionId: number | null; effectiveFrom: string | null; source?: string | null }
type Context = { catalogSkuId: number | null; linkedProductCount: number; canWrite: boolean; currentCost: Cost }
const format = (value: number | null) => value == null ? '' : `${Math.trunc(value / 100)},${String(value % 100).padStart(2, '0')}`

export function parseCostRubles(raw: string): number {
  const value = raw.trim()
  if (!/^(?:\d+|\d{1,3}(?:[ \u00a0\u202f]\d{3})+)(?:[,.]\d{1,2})?$/.test(value)) throw new Error('Введите неотрицательную сумму, не больше двух знаков после запятой')
  const [whole, fraction = ''] = value.replace(/[ \u00a0\u202f]/g, '').split(/[,.]/)
  const amount = BigInt(whole) * 100n + BigInt(fraction.padEnd(2, '0'))
  if (amount > 2147483647n) throw new Error('Максимальная себестоимость — 21 474 836,47 ₽')
  return Number(amount)
}

export function CurrentCostEditor({ nmId, initialRubles, onSaved }: { nmId: number; initialRubles?: number | null; onSaved: () => Promise<void> }) {
  const { accessToken } = useAuth()
  const [context, setContext] = useState<Context | null>(null)
  const [text, setText] = useState(initialRubles == null ? '' : String(initialRubles).replace('.', ','))
  const [message, setMessage] = useState('')
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [sharedConfirmed, setSharedConfirmed] = useState(false)
  const [history, setHistory] = useState<Cost[]>([])
  const input = useRef(text)
  const dirty = useRef(false)
  const contextRef = useRef<Context | null>(null)
  const loadPromise = useRef<Promise<Context | null> | null>(null)
  const savingRef = useRef(false)
  const conflicted = useRef(false)
  const lifetime = useRef({ epoch: 0, controller: new AbortController() })
  const command = useRef<{ amount: number; version: number | null; key: string } | null>(null)
  const updateText = (value: string) => { input.current = value; setText(value) }

  useEffect(() => {
    lifetime.current.controller.abort()
    const epoch = lifetime.current.epoch + 1
    lifetime.current = { epoch, controller: new AbortController() }
    contextRef.current = null; loadPromise.current = null; command.current = null
    dirty.current = false; savingRef.current = false; conflicted.current = false
    setContext(null); setHistory([]); setSharedConfirmed(false); setMessage(''); setLoading(false); setSaving(false)
    return () => { lifetime.current.controller.abort(); lifetime.current.epoch++ }
  }, [nmId, accessToken])
  useEffect(() => {
    if (!dirty.current && !contextRef.current) updateText(initialRubles == null ? '' : String(initialRubles).replace('.', ','))
  }, [initialRubles, nmId, accessToken])

  const load = (preserveInput = false): Promise<Context | null> => {
    if (!accessToken || !nmId) return Promise.resolve(null)
    if (loadPromise.current) return loadPromise.current
    const { epoch, controller } = lifetime.current
    const active = () => epoch === lifetime.current.epoch && !controller.signal.aborted
    setLoading(true)
    const promise = (async () => {
      try {
        const value = await apiData<Context>(`/api/v2/wb/products/${nmId}/current-cost`, { headers: authorizationHeaders(accessToken), signal: controller.signal })
        if (!active()) return null
        contextRef.current = value; setContext(value)
        if (!preserveInput && !dirty.current) updateText(format(value.currentCost.amountKopecks))
        if (preserveInput) setMessage(`Конфликт. Сейчас сохранено: ${format(value.currentCost.amountKopecks) || 'не указано'}. Ваш ввод сохранён; проверьте перед повтором.`)
        if (value.catalogSkuId != null) {
          // History must not delay saving or prevent an otherwise valid write.
          void apiData<Cost[]>(`/api/v2/catalog/skus/${value.catalogSkuId}/cost-history`, { headers: authorizationHeaders(accessToken), signal: controller.signal })
            .then(items => { if (active()) setHistory(items) }).catch(() => {})
        }
        return value
      } catch (error) {
        if (active()) setMessage(error instanceof Error ? error.message : 'Не удалось загрузить себестоимость')
        return null
      } finally { if (active()) { setLoading(false); loadPromise.current = null } }
    })()
    loadPromise.current = promise
    return promise
  }

  const save = async (explicit = false) => {
    if (!dirty.current || !accessToken || !nmId || savingRef.current || (conflicted.current && !explicit)) return
    const { epoch, controller } = lifetime.current
    const active = () => epoch === lifetime.current.epoch && !controller.signal.aborted
    savingRef.current = true; setSaving(true)
    try {
      const amount = parseCostRubles(input.current)
      const value = contextRef.current ?? await load()
      if (!active() || !value) return
      if (!value.canWrite) throw new Error('Нет прав на изменение себестоимости')
      if (value.linkedProductCount > 1 && !sharedConfirmed) throw new Error('Подтвердите изменение общей себестоимости связанных карточек')
      const version = value.currentCost.costVersionId
      if (!command.current || command.current.amount !== amount || command.current.version !== version) command.current = {
        amount, version, key: Array.from(crypto.getRandomValues(new Uint8Array(16)), value => value.toString(16).padStart(2, '0')).join(''),
      }
      const saved = await apiData<Cost>(`/api/v2/wb/products/${nmId}/current-cost`, {
        method: 'POST', headers: authorizationHeaders(accessToken), signal: controller.signal,
        body: JSON.stringify({ amountKopecks: amount, expectedCostVersionId: version, sourceReference: command.current.key }),
      })
      if (!active()) return
      command.current = null; dirty.current = false; conflicted.current = false
      contextRef.current = { ...value, currentCost: saved }; setContext(contextRef.current)
      updateText(format(saved.amountKopecks))
      setMessage('Себестоимость сохранена. Обновляем расчёт…')
      invalidateCostViews()
      try { await onSaved(); if (active()) setMessage('Сохранено в БД. Текущая стоимость применяется за все периоды.') }
      catch { if (active()) setMessage('Стоимость сохранена в БД. Расчёт не обновился — обновите список.') }
    } catch (error) {
      if (!active()) return
      if (error instanceof ApiError && error.status === 409) { conflicted.current = true; command.current = null; await load(true) }
      else setMessage(`Не сохранено: ${error instanceof Error ? error.message : 'ошибка записи'}`)
    } finally { if (active()) { savingRef.current = false; setSaving(false) } }
  }
  return <div style={{ minWidth: 150, maxWidth: 220 }} onClick={event => event.stopPropagation()}>
    <input aria-label={`Себестоимость ${nmId}, рублей за штуку`} inputMode="decimal" value={text}
      readOnly={saving || context?.canWrite === false} aria-busy={loading || saving}
      placeholder={loading ? '…' : '—'} title="Себестоимость одной единицы, ₽. Сохраняется при выходе из поля или по Enter. Escape — отменить ввод."
      style={{ width: 105, minHeight: 34, padding: '6px 10px', border: '1px solid #2563eb', borderRadius: 6, background: '#eff6ff', color: '#1d4ed8', fontWeight: 600, outlineOffset: 2 }}
      onFocus={() => { if (!contextRef.current) void load() }}
      onBlur={() => { if (dirty.current) void save() }}
      onChange={event => { dirty.current = true; updateText(event.target.value); setMessage('Не сохранено') }}
      onKeyDown={event => {
        if (event.key === 'Enter') { event.preventDefault(); void save(true) }
        if (event.key === 'Escape') { dirty.current = false; conflicted.current = false; updateText(format(contextRef.current?.currentCost.amountKopecks ?? (initialRubles == null ? null : Math.round(initialRubles * 100)))); setMessage('') }
      }} />
    {context && <>
      {context.currentCost.source === 'user-example' && <small style={{ display: 'block', color: '#92400e' }}>Примерная себестоимость, не фактическая</small>}
      <button type="button" aria-label={`Сохранить себестоимость ${nmId}`} disabled={saving || !context.canWrite} onClick={() => void save(true)}>{saving ? '…' : '✓'}</button>
      {context.linkedProductCount > 1 && <label style={{ display: 'block' }}><input type="checkbox" checked={sharedConfirmed} onChange={event => setSharedConfirmed(event.target.checked)} />Общая для {context.linkedProductCount} карточек</label>}
      {history.length > 0 && <small style={{ display: 'block', color: '#64748b' }}>История: {history.slice(0, 3).map(item => `${item.effectiveFrom ? new Date(item.effectiveFrom).toLocaleDateString('ru-RU', { timeZone: 'Europe/Moscow' }) : '—'} — ${item.amountKopecks === null ? '—' : `${format(item.amountKopecks)} ₽`}`).join(' · ')}</small>}
    </>}
    <small role="status" style={{ display: 'block', whiteSpace: 'normal' }}>{message}</small>
  </div>
}
