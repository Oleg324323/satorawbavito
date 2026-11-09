import { AlertTriangle, BarChart3, CalendarDays, Check, ExternalLink, Filter, RefreshCw } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/authContext'
import '../../components/vella/VellaFoundation.css'
import { loadLiveRepricerStats, type LiveRepricerStatsItem, type LiveRepricerStatsResponse } from './liveParityData'

function numberValue(value: number | null | undefined) {
  return Number.isFinite(Number(value)) ? Number(value) : 0
}

function basketNumber(value: number | null | undefined) {
  return value == null || !Number.isFinite(value) ? '—' : value.toLocaleString('ru-RU')
}

function rubKopecks(value: number | null | undefined) {
  if (value == null) return '—'
  return (Number(value) / 100).toLocaleString('ru-RU', { maximumFractionDigits: 0 }) + ' ₽'
}

function pct(numerator: number | null | undefined, denominator: number | null | undefined) {
  if (!denominator) return '—'
  return `${(numberValue(numerator) / numberValue(denominator) * 100).toFixed(1)}%`
}

function statusClass(status: LiveRepricerStatsItem['sources'] extends { status?: infer T } ? T : string | null | undefined) {
  if (status === 'fresh' || status === 'ready') return 'good'
  if (status === 'partial') return 'warn'
  return 'bad'
}

function sourceStatusLabel(status: LiveRepricerStatsItem['sources'] extends { status?: infer T } ? T : string | null | undefined) {
  if (status === 'fresh' || status === 'ready') return 'свежее'
  if (status === 'partial') return 'частично готово'
  return 'требует действия'
}

function rowHasPromo(row: LiveRepricerStatsItem) {
  const text = [row.promotionStatus, row.promotionStatusText, ...(row.flags ?? [])].join(' ').toLowerCase()
  return /(promo|promotion|акци|sale)/.test(text)
}

function rowHasAds(row: LiveRepricerStatsItem) {
  const metrics = row.metrics
  return numberValue(metrics?.impressions) > 0
    || numberValue(metrics?.clicks) > 0
    || numberValue(metrics?.adSpendKopecks) > 0
}

function rowBlockers(row: LiveRepricerStatsItem) {
  return [
    ...(row.priceProtection?.blockerIds ?? []),
    ...(row.sources?.missing ?? []),
  ].filter(Boolean).join(', ') || '—'
}

function HelpTip({ children }: { children: string }) {
  return (
    <span className="vella-tooltip-wrap">
      <span className="vella-help-dot" tabIndex={0}>?</span>
      <span className="vella-tooltip">{children}</span>
    </span>
  )
}

export function WbRepricerStatsPage() {
  const navigate = useNavigate()
  const { accessToken } = useAuth()
  const [promoOnly, setPromoOnly] = useState(false)
  const [adsOnly, setAdsOnly] = useState(false)
  const [toast, setToast] = useState<string | null>(null)
  const [payload, setPayload] = useState<LiveRepricerStatsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError(null)
    if (!accessToken) {
      setPayload(null)
      setLoading(false)
      setError('Нет авторизации для загрузки статистики.')
      return () => controller.abort()
    }
    loadLiveRepricerStats(accessToken, controller.signal, { periodDays: 7, page: 1, pageSize: 150 })
      .then((nextPayload) => {
        if (!controller.signal.aborted) setPayload(nextPayload)
      })
      .catch((loadError) => {
        if (!controller.signal.aborted) {
          setPayload(null)
          setError(loadError instanceof Error ? loadError.message : 'Не удалось загрузить статистику репрайсера.')
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [accessToken, reloadKey])

  const rows = useMemo(
    () => (payload?.items ?? [])
      .filter((row) => !promoOnly || rowHasPromo(row))
      .filter((row) => !adsOnly || rowHasAds(row)),
    [adsOnly, payload?.items, promoOnly],
  )
  const totals = useMemo(() => rows.reduce(
    (acc, row) => ({
      impressions: acc.impressions + numberValue(row.metrics?.impressions),
      clicks: acc.clicks + numberValue(row.metrics?.clicks),
      baskets: acc.baskets == null || row.metrics?.baskets == null
        ? null : acc.baskets + numberValue(row.metrics.baskets),
      orders: acc.orders + numberValue(row.metrics?.orders),
      stock: acc.stock + numberValue(row.metrics?.stockUnits),
    }),
    { impressions: 0, clicks: 0, baskets: 0 as number | null, orders: 0, stock: 0 },
  ), [rows])
  const summary = payload?.summary
  const kpis = {
    impressions: summary?.impressions ?? totals.impressions,
    clicks: summary?.clicks ?? totals.clicks,
    baskets: summary?.baskets !== undefined ? summary.baskets : totals.baskets,
    orders: summary?.orders ?? totals.orders,
  }

  function pushToast(text: string) {
    setToast(text)
    window.setTimeout(() => setToast(null), 2400)
  }

  return (
    <div className="vella-root">
      <div className="vella-shell">
        <aside className="vella-sidebar">
          <div className="vella-brand">
            <img className="vella-brand-logo" src="/brand/satorna-logo-white.svg" alt="Satorna" />
          </div>
          <div className="vella-nav-label">WB</div>
          <button className="vella-nav-button vella-nav-parent" type="button" onClick={() => navigate('/wb/repricer')}><span className="vella-nav-dot" />Репрайсер</button>
          <div className="vella-nav-group">
            <button className="vella-nav-button" type="button" onClick={() => navigate('/wb/repricer')}><span className="vella-nav-dot" />Все товары</button>
            <button className="vella-nav-button active" type="button"><span className="vella-nav-dot" />Статистика</button>
            <button className="vella-nav-button" type="button" onClick={() => navigate('/wb/liquidation')}><span className="vella-nav-dot" />Ликвидация</button>
            <button className="vella-nav-button" type="button" onClick={() => navigate('/wb/algorithm')}><span className="vella-nav-dot" />Правила</button>
          </div>
          <div className="vella-nav-label">Данные</div>
          <button className="vella-nav-button" type="button" onClick={() => navigate('/wb/sources')}><span className="vella-nav-dot" />Источники и загрузки</button>
          <button className="vella-nav-button" type="button" onClick={() => navigate('/wb/reports/rnp')}><span className="vella-nav-dot" />РНП</button>
          <button className="vella-nav-button" type="button" onClick={() => navigate('/wb/reports/abc')}><span className="vella-nav-dot" />ABC</button>
          <button className="vella-nav-button" type="button" onClick={() => navigate('/wb/reports/pnl')}><span className="vella-nav-dot" />P&L</button>
        </aside>

        <main className="vella-main">
          <header className="vella-topbar">
            <div className="vella-breadcrumb">WB <span>/</span> Репрайсер <span>/</span> <b>Статистика</b></div>
            <div className="vella-top-actions">
              <button className="vella-button" type="button" onClick={() => pushToast('Период: 7 дней')}><CalendarDays size={16} /> 7 дней</button>
              <button className="vella-button" type="button" onClick={() => setReloadKey((value) => value + 1)}><RefreshCw size={16} /> Обновить</button>
            </div>
          </header>

          <section className="vella-content">
            <div className="vella-report-head">
              <div>
                <h1>Статистика репрайсера</h1>
                <p>Рекламные показы и клики, корзины и заказы WB за выбранный период, остатки и ориентир цены.</p>
              </div>
              <span className={`vella-badge ${error ? 'bad' : loading ? 'warn' : 'good'}`}>{error ? 'ошибка загрузки' : loading ? 'загрузка' : 'backend live'}</span>
            </div>

            <div className="vella-kpis">
              <div className="vella-kpi"><div className="vella-kpi-label">Показы рекламы <HelpTip>Показы рекламных кампаний WB за выбранный период.</HelpTip></div><div className="vella-kpi-value">{kpis.impressions.toLocaleString('ru-RU')}</div></div>
              <div className="vella-kpi"><div className="vella-kpi-label">CTR рекламы</div><div className="vella-kpi-value">{summary?.adCtrPct == null ? pct(kpis.clicks, kpis.impressions) : `${summary.adCtrPct.toFixed(1)}%`}</div></div>
              <div className="vella-kpi"><div className="vella-kpi-label">Корзины</div><div className="vella-kpi-value">{basketNumber(kpis.baskets)}</div><div className="vella-kpi-delta good">{kpis.orders.toLocaleString('ru-RU')} заказов</div></div>
              <div className="vella-kpi"><div className="vella-kpi-label">Защита цены <HelpTip>Цены не отправляются при устаревших или неподтверждённых финансах, СПП или источниках.</HelpTip></div><div className="vella-kpi-value">{(summary?.priceBlocked ?? 0).toLocaleString('ru-RU')} SKU</div><div className="vella-kpi-delta warn">{(summary?.canRecalculate ?? 0).toLocaleString('ru-RU')} можно пересчитать</div></div>
            </div>

            <div className="vella-toolbar">
              <div className="vella-toolbar-left">
                <button className={`vella-chip ${promoOnly ? 'active' : ''}`} type="button" onClick={() => setPromoOnly((value) => !value)}><Filter size={14} /> В акции</button>
                <button className={`vella-chip ${adsOnly ? 'active' : ''}`} type="button" onClick={() => setAdsOnly((value) => !value)}><BarChart3 size={14} /> В рекламе</button>
                <button className="vella-chip" type="button" onClick={() => navigate('/wb/sources')}><AlertTriangle size={14} /> Источники и блокеры</button>
              </div>
              <div className="vella-toolbar-right">
                <button className="vella-button" type="button" onClick={() => navigate('/wb/reports/rnp')}><ExternalLink size={14} /> РНП</button>
                <button className="vella-button" type="button" onClick={() => navigate('/wb/reports/abc')}><ExternalLink size={14} /> ABC</button>
                <button className="vella-button" type="button" onClick={() => navigate('/wb/reports/pnl')}><ExternalLink size={14} /> P&L</button>
              </div>
            </div>

            <div className="vella-panel">
              <div className="vella-table-wrap">
                <table className="vella-table">
                  <thead>
                    <tr>
                      <th className="sticky">SKU</th>
                      <th>Статус</th>
                      <th>Менеджер</th>
                      <th>Фильтры</th>
                      <th className="num">Показы рекламы</th>
                      <th className="num">Клики рекламы</th>
                      <th className="num">Корзины</th>
                      <th className="num">Заказы</th>
                      <th className="num">CR корзины→заказ</th>
                      <th className="num">Остаток</th>
                      <th className="num">Медиана цены</th>
                      <th>Статус источников</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((row) => (
                      <tr key={row.articleId}>
                        <td className="sticky"><span className="vella-mono">{row.articleId}</span></td>
                        <td>{row.decision?.label || row.status || '—'}</td>
                        <td>{row.managerName || row.managerId || '—'}</td>
                        <td>{rowHasPromo(row) ? 'акция' : 'без акции'} · {rowHasAds(row) ? 'реклама' : 'без рекламы'}</td>
                        <td className="num">{numberValue(row.metrics?.impressions).toLocaleString('ru-RU')}</td>
                        <td className="num">{numberValue(row.metrics?.clicks).toLocaleString('ru-RU')}</td>
                        <td className="num">{basketNumber(row.metrics?.baskets)}</td>
                        <td className="num">{numberValue(row.metrics?.orders).toLocaleString('ru-RU')}</td>
                        <td className="num">{row.metrics?.cartToOrderCrPct !== undefined
                          ? row.metrics.cartToOrderCrPct == null ? '—' : `${row.metrics.cartToOrderCrPct.toFixed(1)}%`
                          : pct(row.metrics?.orders, row.metrics?.baskets)}</td>
                        <td className="num">{numberValue(row.metrics?.stockUnits).toLocaleString('ru-RU')}</td>
                        <td className="num">{rubKopecks(row.metrics?.medianPriceKopecks)}</td>
                        <td><span className={`vella-badge ${statusClass(row.sources?.status)}`}>{sourceStatusLabel(row.sources?.status)}</span> {rowBlockers(row)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {loading && <div className="vella-empty">Загружаем статистику репрайсера…</div>}
                {error && <div className="vella-empty">{error}</div>}
                {!loading && !error && rows.length === 0 && <div className="vella-empty">Нет SKU под текущие фильтры.</div>}
              </div>
            </div>

            <div className="vella-report-grid">
              <div className="vella-section">
                <div className="vella-section-title">Проверка защиты цены</div>
                <button className="vella-plan-row" type="button" onClick={() => navigate('/wb/sources')}>
                  <span>СПП или источники устарели</span>
                  <strong>цены не отправлять</strong>
                  <em>WB-06 / WB-22 / WB-23</em>
                </button>
                <button className="vella-plan-row" type="button" onClick={() => navigate('/wb/reports/expenses')}>
                  <span>Финансы и расходы требуют действия</span>
                  <strong>P&L только черновой слой</strong>
                  <em>WB-12 / WB-13 / WB-24</em>
                </button>
              </div>
              <div className="vella-section">
                <div className="vella-section-title">Связанные отчёты</div>
                <button className="vella-plan-row" type="button" onClick={() => navigate('/wb/reports/rnp')}><span>РНП</span><strong>воронка и рекламные пороги</strong><em>открыть</em></button>
                <button className="vella-plan-row" type="button" onClick={() => navigate('/wb/reports/abc')}><span>ABC</span><strong>предварительная прибыль и статусы SKU</strong><em>открыть</em></button>
                <button className="vella-plan-row" type="button" onClick={() => navigate('/wb/reports/pnl')}><span>P&L</span><strong>финансовый контекст требует действия</strong><em>открыть</em></button>
              </div>
            </div>
          </section>
        </main>
      </div>
      {toast && <div className="vella-toast-stack"><div className="vella-toast"><Check size={16} /> {toast}</div></div>}
    </div>
  )
}
