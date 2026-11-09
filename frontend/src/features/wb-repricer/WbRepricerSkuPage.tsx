import { useMemo, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  ArrowLeft,
  Bell,
  Download,
  ExternalLink,
  Save,
  ShieldAlert,
  SlidersHorizontal,
  X,
} from 'lucide-react'
import { formatRub } from '../../lib/formatRub'
import '../../components/vella/VellaFoundation.css'
import { computePMinKopecks, marginStatusAt } from './pricing'
import { SKU_LIST } from './skuListFixtures'
import type { AuditActor, SkuAuditEvent, SkuComment, SkuSettingsForm, SkuSettingsResponse } from './schemas'

type Tab = 'settings' | 'analytics' | 'orders' | 'comments' | 'logs'
type Modal = 'save' | 'blocked' | 'liquidation' | 'export' | null
type Popover = 'notifications' | null

const currentActor: AuditActor = { id: 'manager-maria-dudina', name: 'Мария Дудина', role: 'manager' }
const systemActor: AuditActor = { id: 'system', name: 'Система', role: 'system' }

const BASE_NOW = new Date('2026-05-08T12:00:00.000+05:00').getTime()

function isoOffset(hours: number) {
  return new Date(BASE_NOW + hours * 3_600_000).toISOString()
}

function copySku(row: SkuSettingsResponse): SkuSettingsResponse {
  return {
    ...row,
    meta: { ...row.meta },
    settings: { ...row.settings },
    analytics: row.analytics ? { ...row.analytics } : undefined,
    commentSummary: row.commentSummary ? { ...row.commentSummary } : undefined,
    comments: [...row.comments],
    auditEvents: [...row.auditEvents],
  }
}

function findSku(articleId: string) {
  return copySku(SKU_LIST.find((row) => row.meta.articleId === articleId) ?? SKU_LIST[0])
}

function pMin(settings: SkuSettingsForm) {
  return computePMinKopecks(
    settings.cogsKopecks,
    settings.wbCommissionPct,
    settings.logisticsKopecks,
    settings.allowNegativeMargin ? 0 : settings.minMarginPct,
  )
}

function marginPct(settings: SkuSettingsForm, priceKopecks: number) {
  const net = priceKopecks * (1 - settings.wbCommissionPct / 100) - settings.logisticsKopecks - settings.cogsKopecks
  return priceKopecks > 0 ? net / priceKopecks * 100 : 0
}

function statusLabel(row: SkuSettingsResponse) {
  if (row.meta.status === 'warmup') return row.meta.warmupDaysLeft ? `прогрев ${row.meta.warmupDaysLeft}д` : 'прогрев'
  if (row.meta.status === 'manual') return 'ручной'
  if (row.meta.status === 'liquidation') return 'ликвидация'
  return 'авто'
}

function statusClass(status: SkuSettingsResponse['meta']['status']) {
  if (status === 'auto') return 'good'
  if (status === 'warmup') return 'neutral'
  if (status === 'liquidation') return 'bad'
  return 'warn'
}

function initialEvents(row: SkuSettingsResponse): SkuAuditEvent[] {
  if (row.auditEvents.length) return row.auditEvents
  return [
    {
      id: 'event-auto',
      sku: row.meta.articleId,
      createdAt: isoOffset(-4),
      actor: systemActor,
      source: 'system',
      scope: 'sku',
      action: 'Плановый пересчёт',
      oldValue: 'предыдущий цикл',
      newValue: `P_min ${formatRub(pMin(row.settings))}, корзины ${row.meta.basketsLast7d}/${row.meta.basketNorm}`,
    },
  ]
}

function Field({
  label,
  value,
  suffix,
  onChange,
}: {
  label: string
  value: number
  suffix: string
  onChange: (value: number) => void
}) {
  return (
    <label className="vella-threshold-field">
      <span>{label}</span>
      <div className="vella-threshold-input">
        <input type="number" value={value} onChange={(event) => onChange(Number(event.target.value))} />
        <em>{suffix}</em>
      </div>
    </label>
  )
}

export function WbRepricerSkuPage() {
  const navigate = useNavigate()
  const { articleId = SKU_LIST[0].meta.articleId } = useParams()
  const [row, setRow] = useState(() => findSku(articleId))
  const [settings, setSettings] = useState<SkuSettingsForm>(() => findSku(articleId).settings)
  const [targetPriceRub, setTargetPriceRub] = useState(String(Math.round(findSku(articleId).meta.currentPriceKopecks / 100)))
  const [tab, setTab] = useState<Tab>('settings')
  const [modal, setModal] = useState<Modal>(null)
  const [popover, setPopover] = useState<Popover>(null)
  const [toast, setToast] = useState<string | null>(null)
  const [events, setEvents] = useState<SkuAuditEvent[]>(() => initialEvents(findSku(articleId)))
  const [comments, setComments] = useState<SkuComment[]>(() => findSku(articleId).comments)
  const [commentDraft, setCommentDraft] = useState('')
  const [actionReason, setActionReason] = useState('')

  const currentPMin = useMemo(() => pMin(settings), [settings])
  const targetPriceKopecks = Math.round((Number(targetPriceRub.replace(',', '.')) || 0) * 100)
  const currentMargin = marginPct(settings, row.meta.currentPriceKopecks)
  const targetMargin = targetPriceKopecks > 0 ? marginPct(settings, targetPriceKopecks) : null
  const marginStatus = marginStatusAt(row.meta.currentPriceKopecks, settings.cogsKopecks, settings.wbCommissionPct, settings.logisticsKopecks)
  const targetBlocked = targetPriceKopecks > 0 && targetPriceKopecks < currentPMin && !settings.allowNegativeMargin
  const basketProgress = Math.min(100, Math.round(row.meta.basketsLast7d / Math.max(row.meta.basketNorm, 1) * 100))
  const estimatedOrders = Math.max(1, Math.round(row.meta.basketsLast7d * 0.42))
  const estimatedProductionMin = estimatedOrders * 7

  function pushToast(message: string) {
    setToast(message)
    window.setTimeout(() => setToast(null), 2400)
  }

  function addEvent(action: string, reason: string, oldValue?: string, newValue?: string) {
    setEvents((current) => [
      {
        id: `event-${Date.now()}`,
        sku: row.meta.articleId,
        createdAt: new Date().toISOString(),
        actor: currentActor,
        source: 'manager',
        scope: 'sku',
        action,
        reason,
        oldValue,
        newValue,
      },
      ...current,
    ])
  }

  function addComment(text: string, source: 'comment' | 'action' = 'comment') {
    const value = text.trim()
    if (!value) {
      pushToast(source === 'action' ? 'Укажите причину действия' : 'Введите комментарий')
      return false
    }
    setComments((current) => [
      ...current,
      {
        id: `comment-${Date.now()}`,
        sku: row.meta.articleId,
        author: currentActor,
        createdAt: new Date().toISOString(),
        text: value,
      },
    ])
    if (source === 'comment') addEvent('Комментарий', value, undefined, value)
    return true
  }

  function updateSetting<K extends keyof SkuSettingsForm>(key: K, value: SkuSettingsForm[K]) {
    setSettings((current) => ({ ...current, [key]: value }))
  }

  function toggleAutomation() {
    const reason = actionReason.trim()
    if (!reason) {
      pushToast('Укажите причину изменения автоматики')
      return
    }
    const next = !settings.automationEnabled
    setSettings((current) => ({ ...current, automationEnabled: next }))
    setRow((current) => ({ ...current, meta: { ...current.meta, status: next ? 'auto' : 'manual' } }))
    addComment(reason, 'action')
    addEvent(next ? 'Автоматика включена' : 'Автоматика выключена', reason, settings.automationEnabled ? 'включено' : 'выключено', next ? 'включено' : 'выключено')
    setActionReason('')
    pushToast(next ? 'Автоматика включена' : 'Автоматика выключена')
  }

  function saveSettings() {
    if (targetBlocked) {
      setModal('blocked')
      return
    }
    const reason = actionReason.trim()
    if (!reason) {
      pushToast('Укажите причину сохранения карточки')
      return
    }
    setRow((current) => ({
      ...current,
      meta: {
        ...current.meta,
        currentPriceKopecks: targetPriceKopecks > 0 ? targetPriceKopecks : current.meta.currentPriceKopecks,
        lastSavedAt: isoOffset(0),
      },
      settings,
    }))
    addComment(reason, 'action')
    addEvent('Настройки сохранены', reason, formatRub(row.meta.currentPriceKopecks), formatRub(targetPriceKopecks || row.meta.currentPriceKopecks))
    setActionReason('')
    setModal(null)
    pushToast('Карточка SKU сохранена в mock-state')
  }

  function confirmLiquidation() {
    const reason = actionReason.trim()
    if (!reason) {
      pushToast('Укажите причину ликвидационного флага')
      return
    }
    setSettings((current) => ({ ...current, allowNegativeMargin: true, automationEnabled: false }))
    setRow((current) => ({ ...current, meta: { ...current.meta, status: 'liquidation' } }))
    addComment(reason, 'action')
    addEvent('Включён ликвидационный флаг', reason, 'обычный режим', 'ликвидация')
    setActionReason('')
    setModal(null)
    pushToast('Ликвидационный режим включён')
  }

  return (
    <div className="vella-root">
      <div className="vella-shell">
        <aside className="vella-sidebar">
          <div className="vella-brand">
            <div className="vella-brand-mark">S</div>
            <div>
              <div className="vella-brand-name">Satorna</div>
              <div className="vella-brand-sub">SKU card</div>
            </div>
          </div>
          <div className="vella-nav-group">
            <div className="vella-nav-label">Репрайсер</div>
            <button className="vella-nav-button" type="button" onClick={() => navigate('/wb/repricer')}><span className="vella-nav-dot" />Все товары</button>
            <button className="vella-nav-button" type="button" onClick={() => navigate('/wb/repricer/changelog')}><span className="vella-nav-dot" />История</button>
            <button className="vella-nav-button" type="button" onClick={() => navigate('/wb/promotions')}><span className="vella-nav-dot" />Акции WB</button>
            <button className="vella-nav-button" type="button" onClick={() => navigate('/wb/liquidation')}><span className="vella-nav-dot" />Ликвидация</button>
          </div>
        </aside>

        <main className="vella-main">
          <header className="vella-topbar">
            <div className="vella-breadcrumb">WB <span>/</span> Репрайсер <span>/</span> <b>{row.meta.articleId}</b></div>
            <div className="vella-top-actions">
              <button className="vella-icon-button" type="button" aria-label="Назад" onClick={() => navigate('/wb/repricer')}><ArrowLeft size={16} /></button>
              <div className="vella-position">
                <button className="vella-icon-button" type="button" aria-label="Уведомления" onClick={() => setPopover(popover === 'notifications' ? null : 'notifications')}><Bell size={16} /></button>
                {popover === 'notifications' && (
                  <div className="vella-popover">
                    <button className="vella-popover-row" type="button" onClick={() => setTab('logs')}>Логи карточки: {events.length}</button>
                    <button className="vella-popover-row" type="button" onClick={() => setModal(targetBlocked ? 'blocked' : 'save')}>Проверить сохранение</button>
                  </div>
                )}
              </div>
              <button className="vella-button" type="button" onClick={() => setModal('export')}><Download size={16} /> Экспорт</button>
              <button className="vella-button primary" type="button" onClick={() => setModal(targetBlocked ? 'blocked' : 'save')}><Save size={16} /> Сохранить</button>
            </div>
          </header>

          <section className="vella-content">
            <div className="vella-report-head">
              <div>
                <h1>{row.meta.articleId} · {row.meta.name}</h1>
                <p>Deep SKU runtime без fetch/MSW: настройки, расчёт P_min, аналитика, заказы и audit меняются локально.</p>
              </div>
              <a className="vella-button" href={row.meta.nmId ? `https://www.wildberries.ru/catalog/${row.meta.nmId}/detail.aspx` : '#'} target="_blank" rel="noreferrer">
                <ExternalLink size={15} /> WB
              </a>
            </div>

            <div className="vella-kpis">
              <div className="vella-kpi"><div className="vella-kpi-label">Статус</div><div className="vella-kpi-value"><span className={`vella-badge ${statusClass(row.meta.status)}`}>{statusLabel(row)}</span></div><div className="vella-kpi-delta">{settings.automationEnabled ? 'авто включено' : 'ручной контроль'}</div></div>
              <div className="vella-kpi"><div className="vella-kpi-label">Цена / P_min</div><div className="vella-kpi-value">{formatRub(row.meta.currentPriceKopecks)}</div><div className="vella-kpi-delta">{formatRub(currentPMin)}</div></div>
              <div className="vella-kpi"><div className="vella-kpi-label">Маржа</div><div className={`vella-kpi-value ${marginStatus === 'negative' ? 'metric-down' : marginStatus === 'thin' ? 'vella-warn-text' : 'metric-up'}`}>{currentMargin.toFixed(1)}%</div><div className="vella-kpi-delta">{settings.minMarginPct}% минимум</div></div>
              <div className="vella-kpi"><div className="vella-kpi-label">Корзины</div><div className="vella-kpi-value">{row.meta.basketsLast7d}/{row.meta.basketNorm}</div><div className="vella-kpi-delta">{basketProgress}% нормы</div></div>
            </div>

            <div className="vella-tabs">
              {[
                ['settings', 'Настройки'],
                ['analytics', 'Аналитика'],
                ['orders', 'Заказы'],
                ['comments', 'Комментарии'],
                ['logs', 'Логи'],
              ].map(([value, label]) => (
                <button className={`vella-tab ${tab === value ? 'active' : ''}`} key={value} type="button" onClick={() => setTab(value as Tab)}>{label}</button>
              ))}
            </div>

            {tab === 'settings' && (
              <div className="vella-report-grid">
                <section className="vella-section">
                  <div className="vella-section-title">Финансовые параметры</div>
                  <div className="vella-threshold-grid">
                    <Field label="Себестоимость" value={settings.cogsKopecks / 100} suffix="₽" onChange={(value) => updateSetting('cogsKopecks', Math.round(value * 100))} />
                    <Field label="Комиссия WB" value={settings.wbCommissionPct} suffix="%" onChange={(value) => updateSetting('wbCommissionPct', value)} />
                    <Field label="Логистика" value={settings.logisticsKopecks / 100} suffix="₽" onChange={(value) => updateSetting('logisticsKopecks', Math.round(value * 100))} />
                    <Field label="Мин. маржа" value={settings.minMarginPct} suffix="%" onChange={(value) => updateSetting('minMarginPct', value)} />
                    <Field label="P_max" value={settings.pMaxKopecks / 100} suffix="₽" onChange={(value) => updateSetting('pMaxKopecks', Math.round(value * 100))} />
                    <Field label="Норма корзин" value={settings.basketNormManual ?? row.meta.basketNorm} suffix="шт" onChange={(value) => updateSetting('basketNormManual', Math.round(value))} />
                  </div>
                </section>

                <section className="vella-section">
                  <div className="vella-section-title">Калькулятор цены</div>
                  <div className="vella-price-grid">
                    <span>Текущая цена</span><b>{formatRub(row.meta.currentPriceKopecks)}</b>
                    <span>P_min</span><b>{formatRub(currentPMin)}</b>
                    <span>Целевая цена, ₽</span><input className="vella-number-input" value={targetPriceRub} onChange={(event) => setTargetPriceRub(event.target.value)} />
                    <span>Маржа после</span><b className={targetBlocked ? 'metric-down' : 'metric-up'}>{targetMargin == null ? '—' : `${targetMargin.toFixed(1)}%`}</b>
                  </div>
                  {targetBlocked && <div className="vella-blocked-note"><ShieldAlert size={15} /> Целевая цена ниже P_min. Нужен ликвидационный флаг.</div>}
                  <div className="vella-drawer-actions">
                    <button className="vella-button" type="button" onClick={() => setModal('liquidation')}>Ликвидационный флаг</button>
                    <button className="vella-button primary" type="button" onClick={() => setModal(targetBlocked ? 'blocked' : 'save')}>Применить</button>
                  </div>
                </section>

                <section className="vella-section">
                  <div className="vella-section-title">Режимы</div>
                  <div className="vella-rule-map">
                    <button className={`vella-rule-map-item ${settings.repricerMode === 'baskets' ? 'active' : ''}`} type="button" onClick={() => updateSetting('repricerMode', 'baskets')}>
                      <div><b>Корзины</b><span>основной сигнал WB</span></div><span className="vella-badge neutral">{settings.repricerMode === 'baskets' ? 'выбран' : 'режим'}</span>
                    </button>
                    <button className={`vella-rule-map-item ${settings.repricerMode === 'revenue' ? 'active' : ''}`} type="button" onClick={() => updateSetting('repricerMode', 'revenue')}>
                      <div><b>Выручка</b><span>сравнение {settings.revenueComparisonDays} дней</span></div><span className="vella-badge neutral">{settings.repricerMode === 'revenue' ? 'выбран' : 'режим'}</span>
                    </button>
                  </div>
                  <div className="vella-drawer-actions">
                    <button className="vella-button" type="button" onClick={toggleAutomation}><SlidersHorizontal size={15} /> {settings.automationEnabled ? 'Выключить авто' : 'Включить авто'}</button>
                    <button className={`vella-button ${settings.allowNegativeMargin ? 'primary' : ''}`} type="button" onClick={() => updateSetting('allowNegativeMargin', !settings.allowNegativeMargin)}>
                      {settings.allowNegativeMargin ? 'Минус разрешён' : 'Разрешить минус'}
                    </button>
                  </div>
                </section>

                <section className="vella-section">
                  <div className="vella-section-title">Причина изменения</div>
                  <textarea
                    className="vella-textarea"
                    value={actionReason}
                    placeholder="Обязательная причина для сохранения настроек, автоматики и ликвидации"
                    onChange={(event) => setActionReason(event.target.value)}
                  />
                </section>
              </div>
            )}

            {tab === 'analytics' && (
              <div className="vella-report-grid">
                <section className="vella-section">
                  <div className="vella-section-title">Маркетплейс-сигналы</div>
                  <div className="vella-price-grid">
                    <span>ABC</span><b>{row.analytics?.abcCode ?? '—'}</b>
                    <span>Статус товара</span><b>{row.analytics?.productStatus ?? '—'}</b>
                    <span>В акции</span><b>{row.analytics?.promotionStatus === 'yes' ? 'да' : 'нет'}</b>
                    <span>Выкуп</span><b>{row.analytics ? `${row.analytics.buyoutPct.toFixed(1)}%` : '—'}</b>
                    <span>Остаток WB</span><b>{row.analytics?.wbStockUnits.toLocaleString('ru-RU') ?? '—'} шт</b>
                    <span>Средняя цена СПП</span><b>{row.analytics ? formatRub(row.analytics.avgPriceWithSppKopecks) : '—'}</b>
                  </div>
                </section>
                <section className="vella-section">
                  <div className="vella-section-title">Корзины к норме</div>
                  <div className="vella-liquid-progress"><i style={{ width: `${basketProgress}%` }} /></div>
                  <p className="vella-muted">Если корзины падают ниже 25% нормы, SKU попадает в проблемную очередь и может стать кандидатом на снижение цены.</p>
                </section>
              </div>
            )}

            {tab === 'orders' && (
              <div className="vella-panel">
                <div className="vella-table-wrap">
                  <table className="vella-table">
                    <thead><tr><th>Показатель</th><th>Значение</th><th>Комментарий</th></tr></thead>
                    <tbody>
                      <tr><td>Ожидаемые заказы</td><td>{estimatedOrders} шт</td><td>mock на основе корзин 7д</td></tr>
                      <tr><td>Производственное время</td><td>{estimatedProductionMin} мин</td><td>7 минут на полный цикл заказа</td></tr>
                      <tr><td>FBS дедлайн</td><td>120 часов</td><td>контроль штрафного риска</td></tr>
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            {tab === 'comments' && (
              <div className="vella-report-grid">
                <section className="vella-section">
                  <div className="vella-section-title">История комментариев</div>
                  <div className="vella-audit-list">
                    {comments.length ? comments.map((comment) => (
                      <div className="vella-audit-row" key={comment.id}>
                        <span>{new Date(comment.createdAt).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })} · {comment.author.name}</span>
                        <b>Комментарий</b>
                        <span>{comment.text}</span>
                      </div>
                    )) : (
                      <div className="vella-empty">Комментариев пока нет.</div>
                    )}
                  </div>
                </section>
                <section className="vella-section">
                  <div className="vella-section-title">Новый комментарий</div>
                  <textarea className="vella-textarea" value={commentDraft} placeholder="Добавить комментарий менеджера..." onChange={(event) => setCommentDraft(event.target.value)} />
                  <div className="vella-drawer-actions">
                    <button className="vella-button primary" type="button" onClick={() => {
                      if (addComment(commentDraft)) {
                        setCommentDraft('')
                        pushToast('Комментарий сохранён')
                      }
                    }}>Добавить комментарий</button>
                  </div>
                </section>
              </div>
            )}

            {tab === 'logs' && (
              <div className="vella-panel">
                <div className="vella-audit-list">
                  {events.map((event) => (
                    <div className="vella-audit-row" key={event.id}>
                      <span>{new Date(event.createdAt).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })} · {event.actor.name}</span>
                      <b>{event.action}</b>
                      <span>{event.reason ?? 'Системное событие'}{event.oldValue || event.newValue ? ` · ${event.oldValue ?? '—'} → ${event.newValue ?? '—'}` : ''}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </section>
        </main>
      </div>

      {modal && (
        <div className="vella-modal-overlay" role="dialog" aria-modal="true">
          <div className="vella-modal">
            <div className="vella-modal-head">
              <div className="vella-modal-title">{modal === 'save' ? 'Сохранить карточку' : modal === 'blocked' ? 'Цена ниже P_min' : modal === 'liquidation' ? 'Ликвидационный флаг' : 'Экспорт SKU'}</div>
              <button className="vella-icon-button" type="button" aria-label="Закрыть" onClick={() => setModal(null)}><X size={16} /></button>
            </div>
            <div className="vella-modal-body">
              {modal === 'save' && `Сохранить настройки ${row.meta.articleId}: цена ${formatRub(targetPriceKopecks || row.meta.currentPriceKopecks)}, P_min ${formatRub(currentPMin)}?`}
              {modal === 'blocked' && 'Целевая цена ниже P_min. Сначала включите ликвидационный флаг или разрешите отрицательную маржу.'}
              {modal === 'liquidation' && 'Включить ликвидационный режим для SKU? Автоматика отключится, отрицательная маржа будет разрешена в mock-state.'}
              {modal === 'export' && `Будет выгружена карточка ${row.meta.articleId}: настройки, аналитика, заказы и ${events.length} событий audit.`}
            </div>
            <div className="vella-modal-foot">
              <button className="vella-button" type="button" onClick={() => setModal(null)}>Отмена</button>
              <button className="vella-button primary" type="button" onClick={() => {
                if (modal === 'save') saveSettings()
                else if (modal === 'liquidation') confirmLiquidation()
                else if (modal === 'export') {
                  setModal(null)
                  pushToast('Экспорт SKU поставлен в очередь')
                } else setModal(null)
              }}>{modal === 'blocked' ? 'Понятно' : 'Подтвердить'}</button>
            </div>
          </div>
        </div>
      )}

      {toast && <div className="vella-toast-stack"><div className="vella-toast">{toast}</div></div>}
    </div>
  )
}
