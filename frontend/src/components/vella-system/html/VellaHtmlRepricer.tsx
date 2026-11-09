import { useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import './vella-html-system.css'

type Row = {
  id: string
  sku: string
  wb: string
  name: string
  size: string
  color: 'white' | 'black'
  status: 'локомотив' | 'ручной' | 'новинка'
  abc: 'AA' | 'BA' | 'CC'
  promo: boolean
  price: number
  spp: number
  sppPercent: number
  margin: number
  commission: number
  baskets: number
  orders: number
  buyout: number
}

const rows: Row[] = [
  { id: 'fbbt-38', sku: 'НВВТ_38', wb: '100043703', name: 'Футболка белая «Принт 38»', size: 'M', color: 'white', status: 'локомотив', abc: 'BA', promo: false, price: 2150, spp: 2043, sppPercent: 5, margin: 34, commission: 15, baskets: 16, orders: 20, buyout: 81 },
  { id: 'hcbt-51', sku: 'НВВТ_51', wb: '100018084', name: 'Худи белое «Принт 51»', size: 'S', color: 'white', status: 'ручной', abc: 'BA', promo: false, price: 2100, spp: 1793, sppPercent: 12, margin: 37, commission: 16, baskets: 17, orders: 21, buyout: 68 },
  { id: 'hcbt-44', sku: 'НСВТ_44', wb: '100022742', name: 'Худи чёрное «Принт 44»', size: 'L', color: 'black', status: 'локомотив', abc: 'AA', promo: false, price: 2050, spp: 1845, sppPercent: 10, margin: 39, commission: 18, baskets: 28, orders: 35, buyout: 82 },
  { id: 'hbbt-14', sku: 'НВВТ_14', wb: '100055348', name: 'Худи белое «Принт 14»', size: 'S', color: 'white', status: 'локомотив', abc: 'BA', promo: true, price: 2000, spp: 1742, sppPercent: 12, margin: 33, commission: 16, baskets: 20, orders: 25, buyout: 84 },
  { id: 'hcbt-77', sku: 'НСВТ_77', wb: '100062335', name: 'Худи чёрное «Принт 77»', size: 'XL', color: 'black', status: 'новинка', abc: 'BA', promo: true, price: 1990, spp: 1875, sppPercent: 5, margin: 36, commission: 15, baskets: 24, orders: 17, buyout: 73 },
  { id: 'hbbt-02', sku: 'НВВТ_02', wb: '100029729', name: 'Худи белое «Принт 2»', size: 'XL', color: 'white', status: 'локомотив', abc: 'BA', promo: false, price: 1980, spp: 1762, sppPercent: 11, margin: 35, commission: 17, baskets: 14, orders: 18, buyout: 71 },
  { id: 'hcbt-17', sku: 'НСВТ_17', wb: '100008768', name: 'Худи чёрное «Принт 17»', size: 'M', color: 'black', status: 'ручной', abc: 'BA', promo: false, price: 1890, spp: 1739, sppPercent: 8, margin: 41, commission: 16, baskets: 23, orders: 29, buyout: 75 },
]

const savedViews = [
  { id: 'all', label: 'Все', count: '1 482' },
  { id: 'problem', label: 'Проблемные SKU', count: '3' },
  { id: 'nopmin', label: 'Без P_min', count: '47' },
  { id: 'loss', label: 'Loss margin', count: '2' },
  { id: 'changed', label: 'Изменения 24ч', count: '216' },
  { id: 'illiquid', label: 'Неликвид', count: '8' },
]

function formatRub(value: number) {
  return new Intl.NumberFormat('ru-RU').format(value) + ' ₽'
}

function Icon({ name }: { name: string }) {
  return <span className="vh-icon" aria-hidden="true">{name}</span>
}

function Button({
  children,
  className = '',
  onClick,
}: {
  children: ReactNode
  className?: string
  onClick?: () => void
}) {
  return <button className={`btn ${className}`} type="button" onClick={onClick}>{children}</button>
}

export function VellaHtmlRepricer() {
  const [selected, setSelected] = useState(() => new Set(['hcbt-51']))
  const [query, setQuery] = useState('')
  const [managerOpen, setManagerOpen] = useState(false)
  const [manager, setManager] = useState('Все менеджеры')
  const [activeView, setActiveView] = useState('all')
  const [density, setDensity] = useState<'comfortable' | 'compact'>(
    () => (localStorage.getItem('vella-density') === 'compact' ? 'compact' : 'comfortable'),
  )
  const [modal, setModal] = useState<string | null>(null)
  const [toast, setToast] = useState('Открыт saved view: Все')

  const visibleRows = useMemo(() => {
    const normalized = query.trim().toLowerCase()
    return rows.filter((row) => {
      if (activeView === 'problem' && row.margin > 0 && row.buyout > 70) return false
      if (activeView === 'loss' && row.margin >= 0) return false
      if (activeView === 'illiquid' && row.baskets > 16) return false
      if (!normalized) return true
      return `${row.sku} ${row.wb} ${row.name}`.toLowerCase().includes(normalized)
    })
  }, [activeView, query])

  const selectedCount = selected.size

  function toggle(id: string) {
    setSelected((current) => {
      const next = new Set(current)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  function setDensityMode(next: 'comfortable' | 'compact') {
    setDensity(next)
    localStorage.setItem('vella-density', next)
  }

  function chooseView(id: string) {
    setActiveView(id)
    const view = savedViews.find((item) => item.id === id)
    setToast(`Открыт saved view: ${view?.label ?? id}`)
  }

  return (
    <div className={`vella-html-runtime density-${density}`}>
      <aside className="sidebar">
        <div className="logo">
          <img className="logo-full" src="/brand/satorna-logo.svg" alt="Satorna" />
          <img className="logo-icon-only" src="/brand/satorna-icon.svg" alt="" />
        </div>
        <div className="nav-item"><Icon name="▦" /><span className="nav-label">Дашборд</span></div>
        <div className="nav-section">WB</div>
        <div className="nav-item active"><Icon name="⌁" /><span className="nav-label">Репрайсер</span><span className="chevron open">›</span></div>
        <div className="nav-sub nav-sub-open">
          <div className="nav-item nav-sub-active"><Icon name="⬡" /><span className="nav-label">Все товары</span><span className="nav-badge">1 482</span></div>
          <div className="nav-item"><Icon name="⚡" /><span className="nav-label">Правила</span></div>
          <div className="nav-item"><Icon name="▣" /><span className="nav-label">Стратегии</span><span className="nav-badge">5</span></div>
          <div className="nav-item"><Icon name="◷" /><span className="nav-label">История</span></div>
          <div className="nav-item"><Icon name="⌁" /><span className="nav-label">Ликвидация</span><span className="nav-badge alert">8</span></div>
          <div className="nav-item"><Icon name="%" /><span className="nav-label">Акции WB</span><span className="nav-badge alert">2</span></div>
        </div>
        <div className="nav-item"><Icon name="▥" /><span className="nav-label">Отчёты</span><span className="chevron">›</span></div>
        <div className="nav-item"><Icon name="☆" /><span className="nav-label">Отзывы WB</span><span className="nav-badge soon">скоро</span></div>
        <div className="nav-section">Авито</div>
        <div className="nav-item"><Icon name="□" /><span className="nav-label">Чаты</span><span className="nav-badge alert">3</span></div>
        <div className="nav-item"><Icon name="▯" /><span className="nav-label">Объявления</span><span className="nav-badge">214</span></div>
        <div className="nav-item"><Icon name="▱" /><span className="nav-label">Кошельки</span><span className="nav-badge soon">скоро</span></div>
        <div className="nav-section">Система</div>
        <div className="nav-item"><Icon name="♧" /><span className="nav-label">Уведомления</span><span className="nav-badge alert">7</span></div>
        <div className="nav-item"><Icon name="⚙" /><span className="nav-label">Настройки</span></div>
      </aside>

      <main className="main-area">
        <header className="topbar">
          <div className="nav-btns">
            <button className="nav-btn" type="button">‹</button>
            <button className="nav-btn" type="button">›</button>
          </div>
          <div className="breadcrumb"><span>WB</span><span className="sep">/</span><span className="current">Все товары</span></div>
          <div className="topbar-actions">
            <button className="report-period active" type="button">1 день</button>
            <button className="report-period selected" type="button">7 дней</button>
            <button className="report-period" type="button">14 дней</button>
            <button className="report-period" type="button">30 дней</button>
            <button className="report-date" type="button">25.04 — 01.05 <span>⌄</span></button>
            <button className="icon-btn" type="button">☾</button>
            <button className="icon-btn" type="button">♧<span className="dot-alert" /></button>
            <Button className="btn-default">⇩ Excel</Button>
            <div className="user-chip"><div className="user-av">МФ</div><span className="user-name">Мария Ф.</span></div>
          </div>
        </header>

        <nav className="subtabs">
          <button className="subtab active" type="button"><Icon name="⬡" />Все товары <span className="subtab-count">1 482</span></button>
          <button className="subtab" type="button"><Icon name="⚡" />Правила</button>
          <button className="subtab" type="button"><Icon name="▣" />Стратегии <span className="subtab-count">5</span></button>
          <button className="subtab" type="button"><Icon name="◷" />История</button>
          <span className="subtab-divider" />
          <button className="subtab" type="button"><Icon name="⌁" />Ликвидация <span className="subtab-count danger">8</span></button>
          <button className="subtab" type="button"><Icon name="%" />Акции WB <span className="subtab-count warn">2!</span></button>
        </nav>

        <section className="tab-content active">
          <div className="stats repricer-kpis">
            <Stat label="Выручка за период" value="633 288 ₽" delta="+12.4% к прошлому периоду" />
            <Stat label="Средняя маржа %" value="26.8%" delta="+ 2.1 пп к прошлому периоду" />
            <Stat label="Маржа ₽" value="161 589 ₽" delta="после СПП, комиссии, логистики и хранения" neutral />
            <Stat label="Цен изменено за период" value="216" delta="+18% к прошлому периоду" />
            <Stat label="Корзины за период" value="472" delta="активный спрос" />
            <Stat label="SKU в продаже" value="1 478" delta="33 склада WB" neutral />
            <Stat label="% участия в акциях" value="44%" delta="цены акции из XLSX" neutral />
          </div>

          <div className="toolbar">
            <label className="search"><span>⌕</span><input placeholder="Артикул или название..." value={query} onChange={(event) => setQuery(event.target.value)} /></label>
            <div className="chips">
              <button className="chip active" type="button">Все 25</button>
              <button className="chip" type="button">Локомотивы <span className="chip-count">15</span></button>
              <button className="chip" type="button">Новинки <span className="chip-count">2</span></button>
              <button className="chip" type="button">Неликвид <span className="chip-count">0</span></button>
            </div>
            <div className={`dd ${managerOpen ? 'open' : ''}`}>
              <button className="btn btn-default manager-btn" type="button" onClick={() => setManagerOpen((open) => !open)}>{manager}<span>⌄</span></button>
              <div className="dd-menu">
                {['Все менеджеры', 'Мария Д.', 'Анна П.', 'Светлана В.'].map((item) => (
                  <button
                    className="dd-item"
                    key={item}
                    type="button"
                    onClick={() => {
                      setManager(item)
                      setManagerOpen(false)
                      setToast(`Фильтр по менеджеру: ${item}`)
                    }}
                  >
                    <span>{manager === item ? '✓' : ''}</span>{item}
                  </button>
                ))}
              </div>
            </div>
            <button className="view-btn" type="button">⌯</button>
            <button className="view-btn" type="button">≡</button>
            <button className="view-btn" type="button">#</button>
            <div className="toolbar-right">
              <span className="row-count">Показано 25 из 1 482</span>
              <Button className="btn-default">Проблемные SKU · 3</Button>
              <Button className="btn-primary">Применить цены · 47</Button>
            </div>
          </div>

          <div className="saved-views-panel">
            <div className="saved-views-left">
              {savedViews.map((view) => (
                <button className={`saved-view-btn ${activeView === view.id ? 'active' : ''}`} key={view.id} type="button" onClick={() => chooseView(view.id)}>
                  {view.label}<span className="saved-view-count">{view.count}</span>
                </button>
              ))}
            </div>
            <div className="saved-views-side">
              <div className="saved-views-meta"><b>system</b> · 25.04 — 01.05 · бренд: Все</div>
              <button className="btn btn-default" type="button" onClick={() => setModal('segment')}>Сохранить как сегмент</button>
              <div className="density-toggle">
                <button className={`density-btn ${density === 'comfortable' ? 'active' : ''}`} type="button" onClick={() => setDensityMode('comfortable')}>Комфортно</button>
                <button className={`density-btn ${density === 'compact' ? 'active' : ''}`} type="button" onClick={() => setDensityMode('compact')}>Компактно</button>
              </div>
            </div>
          </div>

          <div className="filter-line">
            <b>Фильтры:</b>
            <button type="button">Футболка</button>
            <button type="button">Худи</button>
            <button type="button">Белый</button>
            <button type="button">Loss / ниже P_min</button>
            <button className="filter-reset" type="button">Сбросить</button>
          </div>

          <div className="table-wrap">
            <table id="mainTable">
              <thead>
                <tr>
                  <th><button className={`cb ${selectedCount ? 'indet' : ''}`} type="button" onClick={() => setSelected(selectedCount ? new Set() : new Set(rows.map((row) => row.id)))} /></th>
                  <th className="col-sku">Фото / Артикул</th>
                  <th>WB-арт.</th>
                  <th>Статус товара <span className="tip-i">?</span></th>
                  <th>ABC <span className="tip-i">?</span></th>
                  <th>Акция <span className="tip-i">?</span></th>
                  <th className="num sorted">Цена до СПП ↓ <span className="sort-rank">1</span></th>
                  <th className="num">Средняя с СПП</th>
                  <th className="num">% СПП</th>
                  <th className="num">Маржа <span className="tip-i">?</span></th>
                  <th className="num">Комиссия</th>
                  <th className="num">Корзины <span className="tip-i">?</span></th>
                  <th className="num">Заказы</th>
                  <th className="num">Выкуп <span className="tip-i">?</span></th>
                  <th className="num">Остаток WB</th>
                  <th>Действия</th>
                </tr>
              </thead>
              <tbody>
                {visibleRows.map((row) => (
                  <tr className={selected.has(row.id) ? 'sel' : ''} key={row.id}>
                    <td><button className={`cb ${selected.has(row.id) ? 'on' : ''}`} type="button" onClick={() => toggle(row.id)}>{selected.has(row.id) ? '✓' : ''}</button></td>
                    <td>
                      <div className="sku-wrap">
                        <div className={`thumb ${row.color === 'black' ? 'thumb-b' : 'thumb-w'}`}><span className="shirt-shape" /></div>
                        <div>
                          <div className="sku-row"><button className="sku" type="button" onClick={() => setModal(`drawer:${row.id}`)}>{row.sku}</button><a className="sku-wb" href="#wb">↗</a></div>
                          <div className="sku-size">{row.size}</div>
                        </div>
                      </div>
                    </td>
                    <td><button className="sku wb-link" type="button">{row.wb}</button></td>
                    <td><span className={`status ${row.status === 'ручной' ? 's-manual' : row.status === 'новинка' ? 's-new' : 's-loko'}`}><span className="dot" />{row.status}</span></td>
                    <td><span className={`abc ${row.abc.toLowerCase()}`}>{row.abc}</span></td>
                    <td><span className={`promo ${row.promo ? 'yes' : 'no'}`}>{row.promo ? '● да' : '● нет'}</span></td>
                    <td className="num"><button className="price-cell" type="button" onClick={() => setModal(`price:${row.id}`)}><b>{formatRub(row.price)}</b><span>без изм.</span></button></td>
                    <td className="num"><b>{formatRub(row.spp)}</b><div className="metric-sub">с СПП</div></td>
                    <td className="num"><b>{row.sppPercent}%</b></td>
                    <td className="num"><span className={row.margin < 0 ? 'down' : 'up'}><b>{row.margin > 0 ? '+' : ''}{row.margin}%</b></span><div className="metric-sub">+730 ₽</div></td>
                    <td className="num"><b>{row.commission}%</b></td>
                    <td className="num">{row.baskets} ↑ <span className="bars blue" /></td>
                    <td className="num">{row.orders} ↑ <span className="bars yellow" /></td>
                    <td className={`num ${row.buyout < 75 ? 'warn-tx' : 'up'}`}><b>{row.buyout}%</b></td>
                    <td className="num"><b>{row.buyout + 12}%</b></td>
                    <td><button className="act" type="button" onClick={() => setModal(`drawer:${row.id}`)}>↗</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="pagination">
            <span className="page-info">Показано <b>1-25</b> из <b>1 482</b></span>
            <span className="spacer" />
            <span>Строк на странице:</span>
            <button className="page-btn" type="button">25⌄</button>
            <div className="page-jump">
              <button className="page-btn" disabled type="button">‹</button>
              <button className="page-btn active" type="button">1</button>
              <button className="page-btn" type="button">2</button>
              <button className="page-btn" type="button">3</button>
              <button className="page-btn" type="button">...</button>
              <button className="page-btn" type="button">60</button>
              <button className="page-btn" type="button">›</button>
            </div>
          </div>
        </section>
      </main>

      <div className={`bulk-bar ${selectedCount ? 'show' : ''}`}>
        <span className="bulk-count"><b>{selectedCount}</b> выделено</span>
        <button className="bulk-btn" type="button" onClick={() => setModal('strategy')}>▣ Применить стратегию⌄</button>
        <button className="bulk-btn" type="button" onClick={() => setModal('pmin')}>— Задать P_min</button>
        <button className="bulk-btn" type="button" onClick={() => setToast('Выбранные SKU переведены в ручной режим')}>⦿ Перевести в ручной</button>
        <button className="bulk-btn danger" type="button" onClick={() => setModal('liquidation')}>⌁ Ликвидировать</button>
        <button className="bulk-btn" type="button" onClick={() => setToast('Экспорт выбранных SKU сформирован')}>⇩ Экспорт</button>
        <button className="bulk-close" type="button" onClick={() => setSelected(new Set())}>×</button>
      </div>

      {modal ? <VellaModal modal={modal} onClose={() => setModal(null)} onToast={setToast} /> : null}
      {toast ? <div className="vh-toast" onAnimationEnd={() => setToast('')}>ⓘ {toast}</div> : null}
    </div>
  )
}

function Stat({ label, value, delta, neutral }: { label: string; value: string; delta: string; neutral?: boolean }) {
  return (
    <div className="stat">
      <div className="stat-label">{label} <span className="stat-tip">i</span></div>
      <div className="stat-val">{value}</div>
      <div className={`stat-delta ${neutral ? 'neutral' : 'up'}`}>{neutral ? '' : '↑ '}{delta}</div>
    </div>
  )
}

function VellaModal({
  modal,
  onClose,
  onToast,
}: {
  modal: string
  onClose: () => void
  onToast: (message: string) => void
}) {
  const isDrawer = modal.startsWith('drawer:')
  const isPrice = modal.startsWith('price:')

  if (isDrawer) {
    return (
      <div className="drawer open">
        <div className="drawer-head">
          <div>
            <div className="drawer-sku">{modal.replace('drawer:', '').toUpperCase()}</div>
            <h2>Карточка SKU</h2>
          </div>
          <button className="icon-btn" type="button" onClick={onClose}>×</button>
        </div>
        <div className="drawer-tabs">
          <button className="active" type="button">Сводка</button>
          <button type="button">Правила</button>
          <button type="button">Комментарии</button>
          <button type="button">История</button>
        </div>
        <div className="drawer-body">
          <div className="d-card">
            <h3>Текущая цена до СПП</h3>
            <div className="d-price-meta"><b>2 150 ₽</b> — цена не менялась за 24 ч</div>
            <button className="d-price-edit" type="button" onClick={() => onToast('Открыт inline edit цены в карточке SKU')}>Изменить P_min</button>
          </div>
          <div className="d-card">
            <h3>RecommendationExplanation</h3>
            <p><b>Trigger:</b> корзины растут 7 дней подряд.</p>
            <p><b>Freshness:</b> WB API обновлён сегодня 08:46.</p>
            <p><b>Rule:</b> ночная медиана + защита P_min.</p>
            <p><b>Guardrail:</b> не снижать ниже маржи 15% без подтверждения.</p>
          </div>
          <div className="d-card">
            <h3>Audit trail</h3>
            <p>Мария Д. · сегодня 14:32 · цена рассчитана вручную</p>
            <div className="audit-diff">old 2 000 ₽ → new 2 150 ₽</div>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="modal-overlay" onMouseDown={onClose}>
      <div className="modal" onMouseDown={(event) => event.stopPropagation()}>
        <div className="modal-head">
          <h2>{isPrice ? 'Ручное изменение цены' : modal === 'segment' ? 'Сохранить как сегмент' : 'Предпросмотр действия'}</h2>
          <button className="icon-btn" type="button" onClick={onClose}>×</button>
        </div>
        <div className="modal-body">
          {isPrice ? (
            <label className="modal-field">Цена до СПП<input autoFocus defaultValue="2150" /></label>
          ) : (
            <>
              <p>Перед применением показываем affected SKU, текущие и новые значения, маржу до/после, blocker и reason-to-act.</p>
              <table className="preview-table">
                <thead><tr><th>SKU</th><th>Текущее</th><th>Новое</th><th>Маржа</th><th>Blocker</th></tr></thead>
                <tbody><tr><td>НВВТ_38</td><td>1 850 ₽</td><td>1 920 ₽</td><td>+31%</td><td>нет</td></tr></tbody>
              </table>
            </>
          )}
        </div>
        <div className="modal-actions">
          <button className="btn btn-default" type="button" onClick={onClose}>Отмена</button>
          <button
            className="btn btn-primary"
            type="button"
            onClick={() => {
              onToast('Действие применено в mock state и записано в audit')
              onClose()
            }}
          >
            Применить
          </button>
        </div>
      </div>
    </div>
  )
}
