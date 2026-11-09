import { useEffect, useRef, useState } from 'react'
import { useAuth } from '@/features/auth/authContext'
import { authorizationHeaders } from '@/features/auth/authApi'
import { apiRequest } from '@/lib/api'
import { invalidateCostViews } from './costInvalidation'

type Row = { row: number; nmId: number | null; oldAmountKopecks?: number | null; amountKopecks: number | null; error: string | null; alreadyApplied?: boolean; replacesExample?: boolean }
type Result = { preview: boolean; previewHash: string; accountId: number; ready: number; saved: number; errors: number; skipped: number; rows: Row[] }
const money = (value: number | null | undefined) => value == null ? '—' : `${(value / 100).toLocaleString('ru-RU', { maximumFractionDigits: 2 })} ₽`

export function CostImportCard() {
  const { accessToken } = useAuth()
  const [file, setFile] = useState<File | null>(null)
  const [result, setResult] = useState<Result | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [replaceExamples, setReplaceExamples] = useState(false)
  const generation = useRef(0)
  const [accounts, setAccounts] = useState<Array<{ id: number; name: string }>>([])
  const [accountId, setAccountId] = useState('')
  useEffect(() => {
    if (!accessToken) return
    const controller = new AbortController()
    void apiRequest<Array<{ id: number; name: string }>>('/api/v2/wb/costs/accounts', { headers: authorizationHeaders(accessToken), signal: controller.signal })
      .then(items => { const available = items ?? []; setAccounts(available); setAccountId(available.length === 1 ? String(available[0].id) : '') })
      .catch(e => { if (!controller.signal.aborted) setError(e instanceof Error ? e.message : 'Не удалось получить кабинеты') })
    return () => controller.abort()
  }, [accessToken])
  const upload = async (preview: boolean) => {
    if (!file || !accessToken || !accountId || busy || (!preview && !result?.preview)) return
    const current = generation.current
    setBusy(true); setError('')
    try {
      const body = new FormData(); body.append('file', file)
      const query = new URLSearchParams({ preview: String(preview), accountId, replaceExamples: String(replaceExamples) })
      if (!preview && result) { query.set('previewHash', result.previewHash); query.set('accountId', String(result.accountId)) }
      const value = await apiRequest<Result>(`/api/v2/wb/costs/import?${query}`, { method: 'POST', headers: authorizationHeaders(accessToken), body })
      if (!value) throw new Error('Сервер не подтвердил результат импорта. Проверьте файл повторно.')
      if (!preview && value.saved > 0) invalidateCostViews()
      if (current === generation.current) setResult(value)
    } catch (e) {
      if (current === generation.current) { setError(e instanceof Error ? e.message : 'Ошибка импорта'); setResult(null) }
    } finally { if (current === generation.current) setBusy(false) }
  }
  return <section className="settings-card" aria-label="Импорт себестоимости">
    <h2>Себестоимость</h2>
    <p>Одна строка — один артикул WB. Стоимость за единицу в рублях. {replaceExamples ? 'Примерные значения заменяются за всю историю; остальные — с момента сохранения.' : 'Изменения действуют с момента сохранения и не переписывают прошлые дни.'}</p>
    <a className="btn btn-default btn-sm" href="/downloads/wb-cost-template.xlsx" download>Скачать XLSX-шаблон</a>
    <label style={{ display: 'block', marginTop: 16 }}>Кабинет WB <select aria-label="Кабинет для импорта" value={accountId} disabled={busy} onChange={event => { setAccountId(event.target.value); setResult(null) }}><option value="">Выберите кабинет</option>{accounts.map(account => <option key={account.id} value={account.id}>{account.name}</option>)}</select></label>
    <label style={{ display: 'block', padding: 20, marginTop: 16, border: '2px dashed #cbd5e1', borderRadius: 10 }}>
      Выберите XLSX-файл
      <input type="file" accept=".xlsx" disabled={busy} style={{ display: 'block', marginTop: 12, maxWidth: '100%' }} onChange={event => {
        generation.current++; setFile(event.target.files?.[0] ?? null); setResult(null); setError('')
      }} />
    </label>
    <p>Сначала проверьте сопоставление. Пустые значения и строки с ошибками не применяются. Повтор того же файла не изменит стоимость повторно.</p>
    <label style={{ display: 'block', marginBottom: 16 }}>
      <input type="checkbox" checked={replaceExamples} disabled={busy} onChange={event => { setReplaceExamples(event.target.checked); setResult(null) }} />{' '}
      Заменить примерную себестоимость за всю историю
      <span style={{ display: 'block', color: '#64748b', marginTop: 4 }}>Только для значений, отмеченных как пример. Для остальных товаров изменения действуют с момента сохранения.</span>
    </label>
    <button className="btn btn-default" disabled={!file || !accountId || busy || !accessToken} onClick={() => void upload(true)}>{busy ? 'Обработка…' : 'Проверить файл'}</button>
    {result?.preview && <button className="btn btn-primary" style={{ marginLeft: 8 }} disabled={busy || !result.ready} onClick={() => void upload(false)}>Сохранить {result.ready} строк</button>}
    {error && <p role="alert">{error}</p>}
    {result && <>
      <p role="status">{result.preview ? `Готово к сохранению: ${result.ready}` : `Сохранено: ${result.saved}`} · Ошибки: {result.errors} · Уже применено: {result.skipped}</p>
      <div style={{ overflow: 'auto', maxHeight: 420 }}><table className="report-table"><thead><tr><th>Строка</th><th>nmID</th><th>Было</th><th>Будет</th><th>Результат</th></tr></thead>
        <tbody>{result.rows.map(row => <tr key={row.row}><td>{row.row}</td><td>{row.nmId ?? '—'}</td><td>{money(row.oldAmountKopecks)}</td><td>{money(row.amountKopecks)}</td><td>{row.error || (row.alreadyApplied ? 'Уже сохранено' : result.preview ? row.replacesExample ? 'Заменит пример за всю историю' : 'Можно сохранить' : 'Сохранено')}</td></tr>)}</tbody>
      </table></div>
    </>}
  </section>
}
