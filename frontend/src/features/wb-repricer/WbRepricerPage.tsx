import { type Dispatch, type MouseEvent, type SetStateAction, useMemo, useState } from 'react'
import './WbRepricerPreview.css'
import type { AuditActor, SkuAuditEvent, SkuComment } from './schemas'

type ProductStatus = 'auto' | 'manual' | 'new'

type ProductRow = {
  sku: string
  wbId: string
  photoUrl?: string
  size: string
  color: 'white' | 'black'
  garment: 'tee' | 'hoodie'
  name: string
  status: ProductStatus
  abc: 'AA' | 'BA'
  promo: boolean
  price: number
  avgPrice: number
  spp: number
  margin: number
  marginRub: number
  commission: number
  baskets: number
  orders: number
  buyout: number
  stock: number
  stockValue: number
  managerId?: string | null
  managerName?: string
  assignmentSource?: 'manual' | 'xlsx' | 'none'
  assignedAt?: string | null
  strategy?: 'Агрессивный' | 'Ночная медиана' | 'Консервативный' | 'Запуск новинки' | 'Ликвидация'
  selected?: boolean
}

type BulkActionDraft = {
  title: string
  action: 'strategy' | 'p_min' | 'manual_mode' | 'liquidation' | 'assign_manager'
  newValue: string
  managerId?: string | null
} | null

type IconName =
  | 'activity'
  | 'box'
  | 'grid'
  | 'clock'
  | 'trendDown'
  | 'percent'
  | 'sliders'
  | 'barChart'
  | 'star'
  | 'message'
  | 'columns'
  | 'wallet'
  | 'tag'
  | 'bell'
  | 'settings'
  | 'chevronLeft'
  | 'chevronRight'
  | 'moon'
  | 'download'
  | 'filter'
  | 'sort'
  | 'plus'
  | 'file'

type OpenMenu = 'calendar' | 'notifications' | 'export' | 'sort' | 'columns' | null
type ModalName = 'night' | 'apply' | 'applyProgress' | 'problemQueue' | 'bulkPmin' | 'bulkLiq' | 'bulkAction' | null
type DrawerTab = 'overview' | 'rules' | 'misc'
type DrawerMiscTab = 'orders' | 'logs'
type FilterPreset = 'all' | 'locomotive' | 'dead' | 'liquidation' | 'noPmin'
type SortMode = 'baskets-desc' | 'margin-desc' | 'price-desc' | 'stock-asc' | 'sku-asc'
type SubtabName = 'Все товары' | 'Стратегии' | 'История' | 'Ликвидация' | 'Акции WB' | 'Правила'

const defaultVisibleColumns = new Set([
  'Артикул',
  'WB-артикул',
  'Статус',
  'Ответственный',
  'ABC и акция',
  'Цена / СПП',
  'Маржа',
  'Комиссия WB',
  'Корзины',
  'Заказы',
  'Остаток',
  'Стратегия',
  'Комментарий',
])

const defaultAdvancedFilters = {
  marginFrom: '',
  marginTo: '',
  basketsFrom: '',
  basketsTo: '',
  priceFrom: '',
  priceTo: '',
  strategy: 'Все',
  stock: 'Любой',
}

const defaultGarmentFilters = new Set(['tee', 'hoodie', 'longsleeve'])
const defaultColorFilters = new Set(['female', 'male', 'white', 'black'])
const repricerManagers = [
  { id: 'manager-maria-dudina', name: 'Мария Дудина', active: true },
  { id: 'manager-maria-f', name: 'Мария Ф.', active: true },
  { id: 'manager-maxim', name: 'Максим', active: true },
  { id: 'manager-irina', name: 'Ирина', active: true },
]
const managerOptions = [
  { value: 'all', label: 'Все менеджеры' },
  { value: 'mine', label: 'Мои SKU' },
  { value: 'unassigned', label: 'Без ответственного' },
  ...repricerManagers.map((manager) => ({ value: manager.id, label: manager.name })),
]
const currentActor: AuditActor = { id: 'manager-maria-dudina', name: 'Мария Дудина', role: 'manager' }
const systemActor: AuditActor = { id: 'system', name: 'Система', role: 'system' }

const baseRows: ProductRow[] = [
  { sku: 'HBBT_38', wbId: '100043703', size: 'M', color: 'white', garment: 'hoodie', name: 'Худи белое «Принт 38»', status: 'auto', abc: 'BA', promo: false, price: 2150, avgPrice: 2043, spp: 5, margin: 34, marginRub: 730, commission: 15, baskets: 16, orders: 20, buyout: 81, stock: 55, stockValue: 118250, strategy: 'Агрессивный', selected: true },
  { sku: 'HBBT_51', wbId: '100018084', size: 'S', color: 'white', garment: 'hoodie', name: 'Худи белое «Принт 51»', status: 'manual', abc: 'BA', promo: false, price: 2100, avgPrice: 1793, spp: 12, margin: 37, marginRub: 780, commission: 16, baskets: 17, orders: 21, buyout: 68, stock: 58, stockValue: 121800, selected: true },
  { sku: 'HCBT_44', wbId: '100022742', size: 'L', color: 'black', garment: 'hoodie', name: 'Худи чёрное «Принт 44»', status: 'auto', abc: 'AA', promo: false, price: 2050, avgPrice: 1845, spp: 10, margin: 39, marginRub: 800, commission: 18, baskets: 28, orders: 35, buyout: 82, stock: 71, stockValue: 145550, strategy: 'Агрессивный', selected: true },
  { sku: 'HBBT_14', wbId: '100055348', size: 'S', color: 'white', garment: 'hoodie', name: 'Худи белое «Принт 14»', status: 'auto', abc: 'BA', promo: true, price: 2000, avgPrice: 1742, spp: 12, margin: 33, marginRub: 660, commission: 16, baskets: 20, orders: 25, buyout: 84, stock: 48, stockValue: 96000, strategy: 'Ночная медиана' },
  { sku: 'HCBT_77', wbId: '100062335', size: 'XL', color: 'black', garment: 'hoodie', name: 'Худи чёрное «Принт 77»', status: 'new', abc: 'BA', promo: true, price: 1990, avgPrice: 1875, spp: 5, margin: 36, marginRub: 716, commission: 15, baskets: 24, orders: 17, buyout: 73, stock: 53, stockValue: 105470, strategy: 'Агрессивный' },
  { sku: 'HBBT_02', wbId: '100029729', size: 'XL', color: 'white', garment: 'hoodie', name: 'Худи белое «Принт 2»', status: 'auto', abc: 'BA', promo: false, price: 1980, avgPrice: 1762, spp: 11, margin: 35, marginRub: 693, commission: 17, baskets: 14, orders: 18, buyout: 71, stock: 67, stockValue: 132660, strategy: 'Консервативный' },
  { sku: 'HCBT_17', wbId: '100008768', size: 'M', color: 'black', garment: 'hoodie', name: 'Худи чёрное «Принт 17»', status: 'auto', abc: 'BA', promo: false, price: 1890, avgPrice: 1739, spp: 8, margin: 41, marginRub: 780, commission: 16, baskets: 23, orders: 29, buyout: 72, stock: 42, stockValue: 79380, strategy: 'Запуск новинки' },
  { sku: 'HCBT_22', wbId: '100074221', size: 'M', color: 'black', garment: 'hoodie', name: 'Худи чёрное «Принт 22»', status: 'manual', abc: 'BA', promo: true, price: 1480, avgPrice: 1384, spp: 6, margin: -8, marginRub: -118, commission: 16, baskets: 3, orders: 5, buyout: 61, stock: 18, stockValue: 26640, strategy: 'Ликвидация' },
  { sku: 'FBBT_42', wbId: '100006439', size: 'S', color: 'white', garment: 'tee', name: 'Футболка белая «Принт 42»', status: 'auto', abc: 'AA', promo: false, price: 1290, avgPrice: 1198, spp: 7, margin: 28, marginRub: 255, commission: 15, baskets: 38, orders: 31, buyout: 88, stock: 62, stockValue: 79980 },
]

const rows: ProductRow[] = Array.from({ length: 25 }, (_, index) => {
  const source = baseRows[index % baseRows.length]
  if (index < baseRows.length) return source

  const printNumber = String(40 + index).padStart(2, '0')
  const priceDelta = (index % 5) * 20
  return {
    ...source,
    sku: source.sku.replace(/\d+$/, printNumber),
    wbId: String(Number(source.wbId) + index * 317),
    name: source.name.replace(/«Принт \d+»/, `«Принт ${printNumber}»`),
    price: source.price - priceDelta,
    avgPrice: source.avgPrice - Math.round(priceDelta * 0.8),
    baskets: Math.max(1, source.baskets - (index % 7)),
    orders: Math.max(1, source.orders - (index % 6)),
    stock: source.stock + (index % 9),
    stockValue: (source.stock + (index % 9)) * (source.price - priceDelta),
    selected: false,
  }
})

const initialComments: Record<string, SkuComment[]> = {
  FBBT_42: [
    { id: 'comment-fbbt-42-1', sku: 'FBBT_42', author: currentActor, createdAt: new Date(Date.now() - 26 * 3600_000).toISOString(), text: 'Проверить, почему рост корзин не даёт такой же рост продаж.' },
    { id: 'comment-fbbt-42-2', sku: 'FBBT_42', author: { id: 'finance-maxim', name: 'Максим', role: 'finance' }, createdAt: new Date(Date.now() - 3 * 3600_000).toISOString(), text: 'Перед повышением цены сверить ДРР и маржу после СПП.' },
  ],
  HCBT_22: [
    { id: 'comment-hcbt-22-1', sku: 'HCBT_22', author: currentActor, createdAt: new Date(Date.now() - 5 * 3600_000).toISOString(), text: 'Маржа отрицательная, нужна причина для каждого шага ликвидации.' },
  ],
  HCBT_19: [
    { id: 'comment-hcbt-19-1', sku: 'HCBT_19', author: currentActor, createdAt: new Date(Date.now() - 4 * 3600_000).toISOString(), text: 'Низкие корзины, не запускать снижение без проверки рекламы.' },
  ],
}

const initialAuditEvents: Record<string, SkuAuditEvent[]> = rows.reduce<Record<string, SkuAuditEvent[]>>((acc, row) => {
  acc[row.sku] = [
    {
      id: `audit-${row.sku}-sync`,
      sku: row.sku,
      createdAt: new Date(Date.now() - 2 * 3600_000).toISOString(),
      actor: systemActor,
      source: 'system',
      scope: 'sku',
      action: 'Пересчёт репрайсера',
      oldValue: 'предыдущий цикл',
      newValue: 'актуальные корзины и маржа',
    },
  ]
  return acc
}, {})

const repricerNav = [
  { label: 'Все товары', count: '1 482', icon: 'box' as const },
  { label: 'Стратегии', count: '5', icon: 'grid' as const },
  { label: 'История', icon: 'clock' as const },
  { label: 'Ликвидация', count: '8', icon: 'trendDown' as const, alert: true },
  { label: 'Акции WB', count: '2', icon: 'percent' as const, alert: true },
  { label: 'Правила', icon: 'sliders' as const },
]

const sideNav = [
  { section: 'Авито', items: [
    { label: 'Чаты', count: '3', icon: 'message' as const, alert: true },
    { label: 'Объявления', count: '214', icon: 'columns' as const },
    { label: 'Кошельки', count: 'скоро', icon: 'wallet' as const, soon: true },
  ] },
  { section: 'Система', items: [
    { label: 'Уведомления', count: '7', icon: 'bell' as const, alert: true },
    { label: 'Настройки', icon: 'settings' as const },
  ] },
]

const calendarDays = Array.from({ length: 35 }, (_, index) => index + 1)

function Icon({ name, className, size = 16 }: { name: IconName; className?: string; size?: number }) {
  const common = {
    className,
    width: size,
    height: size,
    viewBox: '0 0 24 24',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 2,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
    'aria-hidden': true,
  }
  switch (name) {
    case 'activity':
      return <svg {...common}><polyline points="22 12 18 12 15 21 9 3 6 12 2 12" /></svg>
    case 'box':
      return <svg {...common}><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" /></svg>
    case 'grid':
      return <svg {...common}><rect x="3" y="3" width="18" height="18" rx="2" /><line x1="3" y1="9" x2="21" y2="9" /><line x1="9" y1="21" x2="9" y2="9" /></svg>
    case 'clock':
      return <svg {...common}><circle cx="12" cy="12" r="10" /><polyline points="12 6 12 12 16 14" /></svg>
    case 'trendDown':
      return <svg {...common}><polyline points="23 18 13.5 8.5 8.5 13.5 1 6" /><polyline points="17 18 23 18 23 12" /></svg>
    case 'percent':
      return <svg {...common}><path d="M19 5 5 19" /><circle cx="6.5" cy="6.5" r="2.5" /><circle cx="17.5" cy="17.5" r="2.5" /></svg>
    case 'sliders':
      return <svg {...common}><path d="M4 21v-7" /><path d="M4 10V3" /><path d="M12 21v-9" /><path d="M12 8V3" /><path d="M20 21v-5" /><path d="M20 12V3" /><path d="M2 14h4" /><path d="M10 8h4" /><path d="M18 16h4" /></svg>
    case 'barChart':
      return <svg {...common}><line x1="18" y1="20" x2="18" y2="10" /><line x1="12" y1="20" x2="12" y2="4" /><line x1="6" y1="20" x2="6" y2="14" /></svg>
    case 'star':
      return <svg {...common}><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2" /></svg>
    case 'message':
      return <svg {...common}><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" /></svg>
    case 'columns':
      return <svg {...common}><rect x="3" y="3" width="18" height="18" rx="2" /><line x1="9" y1="3" x2="9" y2="21" /></svg>
    case 'wallet':
      return <svg {...common}><rect x="2" y="7" width="20" height="14" rx="2" /><path d="M16 7V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v2" /></svg>
    case 'tag':
      return <svg {...common}><path d="M20.59 13.41 13.42 20.58a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z" /></svg>
    case 'bell':
      return <svg {...common}><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" /><path d="M13.73 21a2 2 0 0 1-3.46 0" /></svg>
    case 'settings':
      return <svg {...common}><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06A1.65 1.65 0 0 0 15 19.4a1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.6 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.6a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09A1.65 1.65 0 0 0 15 4.6a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9c.12.32.29.63.6 1h1a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" /></svg>
    case 'chevronLeft':
      return <svg {...common}><polyline points="15 18 9 12 15 6" /></svg>
    case 'chevronRight':
      return <svg {...common}><polyline points="9 18 15 12 9 6" /></svg>
    case 'moon':
      return <svg {...common}><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" /></svg>
    case 'download':
      return <svg {...common}><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" /></svg>
    case 'filter':
      return <svg {...common}><polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3" /></svg>
    case 'sort':
      return <svg {...common}><line x1="3" y1="6" x2="21" y2="6" /><line x1="6" y1="12" x2="18" y2="12" /><line x1="9" y1="18" x2="15" y2="18" /></svg>
    case 'plus':
      return <svg {...common}><path d="M12 5v14" /><path d="M5 12h14" /></svg>
    case 'file':
      return <svg {...common}><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><polyline points="14 2 14 8 20 8" /></svg>
  }
}

function rub(value: number) {
  return new Intl.NumberFormat('ru-RU').format(value) + ' ₽'
}

function shortDateTime(iso: string) {
  if (iso === 'сейчас') return iso
  return new Date(iso).toLocaleString('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function wbBasketNumber(vol: number) {
  if (vol <= 143) return 1
  if (vol <= 287) return 2
  if (vol <= 431) return 3
  if (vol <= 719) return 4
  if (vol <= 1007) return 5
  if (vol <= 1061) return 6
  if (vol <= 1115) return 7
  if (vol <= 1169) return 8
  if (vol <= 1313) return 9
  if (vol <= 1601) return 10
  if (vol <= 1655) return 11
  if (vol <= 1919) return 12
  if (vol <= 2045) return 13
  if (vol <= 2189) return 14
  if (vol <= 2405) return 15
  if (vol <= 2621) return 16
  return 17
}

function wbPhotoUrlByNm(nmId: string | number | null | undefined) {
  const id = Number(String(nmId || '').replace(/\D/g, ''))
  if (!Number.isFinite(id) || id <= 0) return ''
  const vol = Math.floor(id / 100000)
  const part = Math.floor(id / 1000)
  return `https://basket-${String(wbBasketNumber(vol)).padStart(2, '0')}.wbbasket.ru/vol${vol}/part${part}/${id}/images/c516x688/1.webp`
}

function ShirtThumb({ row }: { row: ProductRow }) {
  const photoUrl = row.photoUrl || wbPhotoUrlByNm(row.wbId)
  if (photoUrl) {
    return (
      <div className="thumb thumb-w">
        <img src={photoUrl} alt="" loading="lazy" />
      </div>
    )
  }
  const dark = row.color === 'black'
  const fill = dark ? '#2D3850' : '#FFF'
  const stroke = dark ? '#3D4D64' : '#CBD5E1'
  return (
    <div className={`thumb ${dark ? 'thumb-b' : 'thumb-w'}`}>
      <svg width="44" height="56" viewBox="0 0 44 56" fill="none" aria-hidden="true">
        {row.garment === 'hoodie' ? (
          <>
            <path d="M14,22 Q16,5 22,4 Q28,5 30,22" stroke={stroke} strokeWidth="1.2" fill={dark ? '#2D3850' : '#F0F2F5'} />
            <path d="M14,22 Q22,16 30,22 L38,18 L43,27 L38,30 L38,51 L6,51 L6,30 L1,27 L6,18 Z" fill={fill} stroke={stroke} strokeWidth="0.8" />
            <rect x="15" y="38" width="14" height="8" rx="3" fill={dark ? '#232E42' : '#F0F2F5'} stroke={dark ? '#232E42' : '#CBD5E1'} strokeWidth="0.6" />
          </>
        ) : (
          <>
            <path d="M17,14 Q22,8 27,14 L36,10 L43,20 L37,23 L37,51 L7,51 L7,23 L1,20 L8,10 Z" fill={fill} stroke={stroke} strokeWidth="1" />
            {!dark && <ellipse cx="22" cy="14" rx="5" ry="3.5" fill="#F0F2F5" stroke="#CBD5E1" strokeWidth="0.8" />}
          </>
        )}
      </svg>
    </div>
  )
}

function StatusBadge({ status }: { status: ProductStatus }) {
  if (status === 'manual') return <span className="status s-manual"><span className="dot dot-man" />ручной</span>
  if (status === 'new') return <span className="status s-new"><span className="dot dot-warm" />новинка</span>
  return <span className="status s-loko"><span className="dot dot-auto" />локомотив</span>
}

function MiniBars() {
  return <span className="bars" aria-hidden="true"><i /><i /><i /><i /><i /></span>
}

function DrawerSection({ title, rows: sectionRows }: { title: string; rows: Array<[string, string]> }) {
  return (
    <section className="drawer-kv-section">
      <h3>{title} <span className="tip-i">?</span></h3>
      <div className="drawer-kv-grid">
        {sectionRows.map(([label, value]) => (
          <div className="drawer-kv" key={`${title}-${label}`}>
            <span>{label}</span>
            <b>{value}</b>
          </div>
        ))}
      </div>
    </section>
  )
}

function SecondaryTabContent({
  tab,
  notify,
  showModal,
}: {
  tab: Exclude<SubtabName, 'Все товары'>
  notify: (message: string) => void
  showModal: (name: Exclude<ModalName, null>) => void
}) {
  const content: Record<Exclude<SubtabName, 'Все товары'>, { title: string; desc: string; rows: Array<[string, string, string]> }> = {
    'Стратегии': {
      title: 'Стратегии',
      desc: 'Правила, по которым алгоритм меняет цену. Все действия меняют mock-state и показывают результат.',
      rows: [
        ['Агрессивный', '15 SKU', 'поднимать цену при корзинах ≥ 30'],
        ['Консервативный', '8 SKU', 'мягкое изменение с лимитом 3%'],
        ['Ночная медиана', '36 SKU', 'заморозка шума 23:00–04:00'],
        ['Запуск новинки', '2 SKU', 'первичная цена для набора корзин'],
      ],
    },
    'История': {
      title: 'История изменений',
      desc: 'Audit log ручных и автоматических действий с ответственным человеком там, где действие было ручным.',
      rows: [
        ['14:32', 'Мария Дудина', 'применила 47 цен'],
        ['14:28', 'WB API', 'обновил корзины и остатки'],
        ['13:50', 'Система', 'перевела HCBT_22 в риск отрицательной маржи'],
        ['23:00', 'Ночная медиана', 'остановила 36 SKU до 04:00'],
      ],
    },
    'Ликвидация': {
      title: 'Ликвидация',
      desc: 'Кандидаты по низким корзинам, большому остатку и слабой марже. Запуск требует подтверждения.',
      rows: [
        ['HCBT_22', '18 шт', 'маржа -8%, требуется подтверждение'],
        ['HBBT_14', '48 шт', 'низкие корзины, акция WB'],
        ['FBBT_42', '62 шт', 'медленная оборачиваемость'],
      ],
    },
    'Акции WB': {
      title: 'Акции WB',
      desc: 'Защита через P_min и XLSX-цены акции. API управления автоакциями не предполагается.',
      rows: [
        ['HBBT_14', 'акция активна', 'P_min защищает нижнюю границу'],
        ['HCBT_77', 'акция активна', 'контроль маржи после СПП'],
        ['HCBT_22', 'риск', 'цена ниже расчетной границы'],
      ],
    },
    'Правила': {
      title: 'Правила ценообразования',
      desc: 'Глобальные лимиты алгоритма, ночная медиана, P_min и защита от резких изменений.',
      rows: [
        ['Порог высокого спроса', '30 корзин', 'поднимать цену'],
        ['Порог низкого спроса', '5 корзин', 'снижать цену'],
        ['Шаг изменения', '5%', 'максимум за цикл'],
        ['Ночная медиана', '23:00–04:00', 'собирать данные без изменения цены'],
      ],
    },
  }
  const state = content[tab]

  return (
    <div className="secondary-workspace">
      <div className="secondary-head">
        <div>
          <h1>{state.title}</h1>
          <p>{state.desc}</p>
        </div>
        <div className="secondary-actions">
          <button className="btn btn-default" type="button" onClick={() => notify(`${state.title}: XLSX поставлен в очередь`)}>Экспорт</button>
          <button className="btn btn-primary" type="button" onClick={() => tab === 'Ликвидация' ? showModal('bulkLiq') : notify(`${state.title}: изменения сохранены`)}>
            {tab === 'Ликвидация' ? 'Запустить' : 'Сохранить'}
          </button>
        </div>
      </div>
      <div className="secondary-grid">
        {state.rows.map(([name, value, note]) => (
          <button className="secondary-row" type="button" key={`${tab}-${name}`} onClick={() => notify(`${name}: ${note}`)}>
            <span className="secondary-row-main">{name}</span>
            <b>{value}</b>
            <span>{note}</span>
          </button>
        ))}
      </div>
    </div>
  )
}

export function WbRepricerPage({
  initialSubtab = 'Все товары',
  initialDrawerOpen = false,
}: {
  initialSubtab?: SubtabName
  initialDrawerOpen?: boolean
} = {}) {
  const reviewState = typeof window !== 'undefined'
    ? new URLSearchParams(window.location.search).get('reviewState')
    : null
  const [selected, setSelected] = useState(() => new Set(rows.filter((row) => row.selected).map((row) => row.sku)))
  const [status, setStatus] = useState<'all' | 'new' | 'manual'>('all')
  const [filterPreset, setFilterPreset] = useState<FilterPreset>('all')
  const [sortMode, setSortMode] = useState<SortMode>('price-desc')
  const [visibleColumns, setVisibleColumns] = useState(() => new Set(defaultVisibleColumns))
  const [advancedFilters, setAdvancedFilters] = useState(defaultAdvancedFilters)
  const [garmentFilters, setGarmentFilters] = useState(() => new Set(defaultGarmentFilters))
  const [colorFilters, setColorFilters] = useState(() => new Set(defaultColorFilters))
  const [manager, setManager] = useState('all')
  const [managerAssignments, setManagerAssignments] = useState<Record<string, string | null>>(() => (
    Object.fromEntries(rows.map((row) => [row.sku, row.managerId ?? legacyManagerFor(row)?.id ?? null]))
  ))
  const [priceOverrides, setPriceOverrides] = useState<Record<string, number>>({})
  const [editingPriceSku, setEditingPriceSku] = useState<string | null>(null)
  const [draftPrice, setDraftPrice] = useState('')
  const [query, setQuery] = useState('')
  const [activePeriod, setActivePeriod] = useState('7 дней')
  const [calendarStart, setCalendarStart] = useState('25.04.2026')
  const [calendarEnd, setCalendarEnd] = useState('01.05.2026')
  const [calendarOffset, setCalendarOffset] = useState(0)
  const [activeSubtab, setActiveSubtab] = useState<SubtabName>(initialSubtab)
  const [openMenu, setOpenMenu] = useState<OpenMenu>(null)
  const [openModal, setOpenModal] = useState<ModalName>(reviewState === 'apply-progress' ? 'applyProgress' : null)
  const [drawerRow, setDrawerRow] = useState<ProductRow | null>(initialDrawerOpen || reviewState?.startsWith('drawer-') ? rows[0] : null)
  const [drawerTab, setDrawerTab] = useState<DrawerTab>(
    reviewState === 'drawer-rules' ? 'rules' : reviewState === 'drawer-misc' ? 'misc' : 'overview',
  )
  const [drawerMiscTab, setDrawerMiscTab] = useState<DrawerMiscTab>('orders')
  const [drawerTargetMargin, setDrawerTargetMargin] = useState('22')
  const [drawerPmin, setDrawerPmin] = useState('1850')
  const [drawerPmax, setDrawerPmax] = useState('2967')
  const [drawerRrc, setDrawerRrc] = useState('2537')
  const [drawerNightMedian, setDrawerNightMedian] = useState(true)
  const [drawerPromoBoost, setDrawerPromoBoost] = useState(false)
  const [drawerDetailCalcOpen, setDrawerDetailCalcOpen] = useState(false)
  const [drawerDiscount, setDrawerDiscount] = useState('30')
  const [drawerActionReason, setDrawerActionReason] = useState('')
  const [drawerManagerId, setDrawerManagerId] = useState<string | null>(null)
  const [commentDrafts, setCommentDrafts] = useState<Record<string, string>>({})
  const [comments, setComments] = useState<Record<string, SkuComment[]>>(() => initialComments)
  const [auditEvents, setAuditEvents] = useState<Record<string, SkuAuditEvent[]>>(() => initialAuditEvents)
  const [inlineReason, setInlineReason] = useState('')
  const [bulkReason, setBulkReason] = useState('')
  const [bulkActionDraft, setBulkActionDraft] = useState<BulkActionDraft>(null)
  const [bulkMenuOpen, setBulkMenuOpen] = useState(false)
  const [advancedOpen, setAdvancedOpen] = useState(reviewState === 'advanced')
  const [sidebarCollapsed, setSidebarCollapsed] = useState(reviewState === 'sidebar-collapsed')
  const [apiErrorOpen, setApiErrorOpen] = useState(reviewState === 'api-error')
  const [loadingOpen] = useState(reviewState === 'loading')
  const [toastOpen, setToastOpen] = useState(reviewState === 'toast')
  const [toastMessage, setToastMessage] = useState('XLSX будет сформирован по текущим фильтрам')

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    const legacyManagerRecordFor = (row: ProductRow) => {
      if (row.sku === 'HCBT_22') return null
      if (row.status === 'manual') return repricerManagers.find((item) => item.id === 'manager-maria-dudina') ?? null
      if (row.strategy === 'Ночная медиана') return repricerManagers.find((item) => item.id === 'manager-maria-f') ?? null
      if (row.margin < 0) return repricerManagers.find((item) => item.id === 'manager-maxim') ?? null
      return repricerManagers.find((item) => item.id === 'manager-irina') ?? null
    }
    const assignedManagerRecordFor = (row: ProductRow) => {
      const assignedId = Object.prototype.hasOwnProperty.call(managerAssignments, row.sku) ? managerAssignments[row.sku] : row.managerId ?? legacyManagerRecordFor(row)?.id ?? null
      return assignedId ? repricerManagers.find((item) => item.id === assignedId) ?? null : null
    }
    const result = rows.filter((row) => {
      const effectivePrice = priceOverrides[row.sku] ?? row.price
      if (status !== 'all' && row.status !== status) return false
      if (manager === 'mine' && assignedManagerRecordFor(row)?.id !== currentActor.id) return false
      if (manager === 'unassigned' && assignedManagerRecordFor(row) !== null) return false
      if (!['all', 'mine', 'unassigned'].includes(manager) && assignedManagerRecordFor(row)?.id !== manager) return false
      if (!garmentFilters.has(row.garment)) return false
      if (!colorFilters.has(row.color)) return false
      if (filterPreset === 'locomotive' && row.status !== 'auto') return false
      if (filterPreset === 'dead' && row.baskets > 5) return false
      if (filterPreset === 'liquidation' && row.strategy !== 'Ликвидация') return false
      if (filterPreset === 'noPmin' && row.margin >= 0 && effectivePrice >= 1500) return false
      if (advancedFilters.marginFrom && row.margin < Number(advancedFilters.marginFrom)) return false
      if (advancedFilters.marginTo && row.margin > Number(advancedFilters.marginTo)) return false
      if (advancedFilters.basketsFrom && row.baskets < Number(advancedFilters.basketsFrom)) return false
      if (advancedFilters.basketsTo && row.baskets > Number(advancedFilters.basketsTo)) return false
      if (advancedFilters.priceFrom && effectivePrice < Number(advancedFilters.priceFrom)) return false
      if (advancedFilters.priceTo && effectivePrice > Number(advancedFilters.priceTo)) return false
      if (advancedFilters.strategy !== 'Все' && row.strategy !== advancedFilters.strategy) return false
      if (advancedFilters.stock === 'Заканчивается (≤ 5)' && row.stock > 5) return false
      if (advancedFilters.stock === 'Низкий (≤ 20)' && row.stock > 20) return false
      if (advancedFilters.stock === 'Нормальный' && row.stock <= 20) return false
      const commentText = (comments[row.sku] ?? []).map((comment) => comment.text).join(' ')
      return !q || `${row.sku} ${row.name} ${row.wbId} ${commentText}`.toLowerCase().includes(q)
    })
    return result.toSorted((a, b) => {
      if (sortMode === 'baskets-desc') return b.baskets - a.baskets
      if (sortMode === 'margin-desc') return b.margin - a.margin
      if (sortMode === 'stock-asc') return a.stock - b.stock
      if (sortMode === 'sku-asc') return a.sku.localeCompare(b.sku, 'ru')
      return (priceOverrides[b.sku] ?? b.price) - (priceOverrides[a.sku] ?? a.price)
    })
  }, [advancedFilters, colorFilters, comments, filterPreset, garmentFilters, manager, managerAssignments, priceOverrides, query, sortMode, status])

  const selectedCount = selected.size
  const isEmpty = filtered.length === 0

  function toggleSelected(sku: string) {
    setSelected((current) => {
      const next = new Set(current)
      if (next.has(sku)) next.delete(sku)
      else next.add(sku)
      return next
    })
  }

  function toggleMenu(menu: Exclude<OpenMenu, null>) {
    setOpenMenu((current) => current === menu ? null : menu)
  }

  function showModal(name: Exclude<ModalName, null>) {
    setOpenMenu(null)
    setBulkMenuOpen(false)
    setOpenModal(name)
  }

  function notify(message: string) {
    setToastMessage(message)
    setToastOpen(true)
  }

  function legacyManagerFor(row: ProductRow) {
    if (row.sku === 'HCBT_22') return null
    if (row.status === 'manual') return repricerManagers.find((item) => item.id === 'manager-maria-dudina') ?? null
    if (row.strategy === 'Ночная медиана') return repricerManagers.find((item) => item.id === 'manager-maria-f') ?? null
    if (row.margin < 0) return repricerManagers.find((item) => item.id === 'manager-maxim') ?? null
    return repricerManagers.find((item) => item.id === 'manager-irina') ?? null
  }

  function managerRecordFor(row: ProductRow) {
    const assignedId = Object.prototype.hasOwnProperty.call(managerAssignments, row.sku) ? managerAssignments[row.sku] : row.managerId ?? legacyManagerFor(row)?.id ?? null
    return assignedId ? repricerManagers.find((item) => item.id === assignedId) ?? null : null
  }

  function managerNameFor(row: ProductRow) {
    return managerRecordFor(row)?.name ?? 'Без ответственного'
  }

  function commentsForSku(sku: string) {
    return comments[sku] ?? []
  }

  function auditForSku(sku: string) {
    return auditEvents[sku] ?? []
  }

  function latestComment(sku: string) {
    return commentsForSku(sku).at(-1)
  }

  function addAuditEvent(row: ProductRow, event: Omit<SkuAuditEvent, 'id' | 'sku' | 'createdAt'>) {
    setAuditEvents((current) => ({
      ...current,
      [row.sku]: [
        {
          ...event,
          id: `audit-${row.sku}-${Date.now()}`,
          sku: row.sku,
          createdAt: new Date().toISOString(),
        },
        ...(current[row.sku] ?? []),
      ],
    }))
  }

  function addSkuComment(row: ProductRow, text: string, reasonSource: 'comment' | 'action' = 'comment') {
    const value = text.trim()
    if (!value) {
      notify(reasonSource === 'action' ? 'Укажите причину изменения' : 'Введите комментарий')
      return false
    }
    const comment: SkuComment = {
      id: `comment-${row.sku}-${Date.now()}`,
      sku: row.sku,
      author: currentActor,
      createdAt: new Date().toISOString(),
      text: value,
    }
    setComments((current) => ({
      ...current,
      [row.sku]: [...(current[row.sku] ?? []), comment],
    }))
    if (reasonSource === 'comment') {
      addAuditEvent(row, {
        actor: currentActor,
        source: 'manager',
        scope: 'sku',
        action: 'Комментарий',
        reason: value,
        newValue: value,
      })
    }
    return true
  }

  function commitBulkAction() {
    if (!bulkActionDraft) return
    const reason = bulkReason.trim()
    if (!reason) {
      notify('Укажите причину массового действия')
      return
    }
    const selectedRows = rows.filter((row) => selected.has(row.sku))
    if (bulkActionDraft.action === 'assign_manager') {
      setManagerAssignments((current) => {
        const next = { ...current }
        selectedRows.forEach((row) => { next[row.sku] = bulkActionDraft.managerId ?? null })
        return next
      })
    }
    selectedRows.forEach((row) => {
      addAuditEvent(row, {
        actor: currentActor,
        source: 'bulk',
        scope: 'bulk',
        action: bulkActionDraft.title,
        reason,
        newValue: bulkActionDraft.newValue,
      })
    })
    setBulkReason('')
    setBulkActionDraft(null)
    setOpenModal(null)
    notify(`${bulkActionDraft.title}: причина сохранена для ${selectedRows.length} SKU`)
  }

  function closeFloatingSurfaces(event: MouseEvent<HTMLDivElement>) {
    const target = event.target as HTMLElement
    if (target.closest('.dd, .global-period, .modal, .drawer, .toast, .bulk-bar')) return
    setOpenMenu(null)
    setBulkMenuOpen(false)
  }

  function setPreset(nextPreset: FilterPreset) {
    setFilterPreset(nextPreset)
    if (nextPreset !== 'all') setStatus('all')
    if (nextPreset === 'all') setQuery('')
  }

  function setAdvancedValue(key: keyof typeof defaultAdvancedFilters, value: string) {
    setAdvancedFilters((current) => ({ ...current, [key]: value }))
  }

  function resetAdvancedFilters() {
    setAdvancedFilters(defaultAdvancedFilters)
    setGarmentFilters(new Set(defaultGarmentFilters))
    setColorFilters(new Set(defaultColorFilters))
    setAdvancedOpen(false)
    notify('Расширенные фильтры сброшены')
  }

  function toggleSetValue(setter: Dispatch<SetStateAction<Set<string>>>, value: string) {
    setter((current) => {
      const next = new Set(current)
      if (next.has(value)) next.delete(value)
      else next.add(value)
      return next
    })
  }

  function toggleColumn(column: string) {
    setVisibleColumns((current) => {
      const next = new Set(current)
      if (next.has(column)) next.delete(column)
      else next.add(column)
      return next
    })
    notify(`Колонка переключена: ${column}`)
  }

  function isVisible(column: string) {
    return visibleColumns.has(column)
  }

  function openDrawer(row: ProductRow) {
    setDrawerRow(row)
    setDrawerTab('overview')
    setDrawerMiscTab('orders')
    setDrawerActionReason('')
    setDrawerManagerId(managerRecordFor(row)?.id ?? null)
  }

  function startPriceEdit(row: ProductRow) {
    setEditingPriceSku(row.sku)
    setDraftPrice(String(priceOverrides[row.sku] ?? row.price))
    setInlineReason('')
  }

  function savePriceEdit(row: ProductRow) {
    const nextPrice = Number(draftPrice)
    if (!Number.isFinite(nextPrice) || nextPrice <= 0) {
      notify('Введите корректную цену')
      return
    }
    const reason = inlineReason.trim()
    if (!reason) {
      notify('Укажите причину изменения цены')
      return
    }
    const previousPrice = priceOverrides[row.sku] ?? row.price
    setPriceOverrides((current) => ({ ...current, [row.sku]: Math.round(nextPrice) }))
    addSkuComment(row, reason, 'action')
    addAuditEvent(row, {
      actor: currentActor,
      source: 'manager',
      scope: 'sku',
      action: 'Ручное изменение цены',
      reason,
      oldValue: rub(previousPrice),
      newValue: rub(Math.round(nextPrice)),
    })
    setEditingPriceSku(null)
    setInlineReason('')
    notify(`Цена ${row.sku} изменена вручную, причина сохранена`)
  }

  function applyDrawerChanges(row: ProductRow) {
    const reason = drawerActionReason.trim()
    if (!reason) {
      notify('Укажите причину изменения SKU')
      return
    }
    const previousManager = managerRecordFor(row)
    if ((previousManager?.id ?? null) !== drawerManagerId) {
      setManagerAssignments((current) => ({ ...current, [row.sku]: drawerManagerId }))
      const nextManager = drawerManagerId ? repricerManagers.find((item) => item.id === drawerManagerId) ?? null : null
      addAuditEvent(row, {
        actor: currentActor,
        source: 'manager',
        scope: 'sku',
        action: 'Смена ответственного',
        reason,
        oldValue: previousManager?.name ?? 'без ответственного',
        newValue: nextManager?.name ?? 'без ответственного',
      })
    }
    addSkuComment(row, reason, 'action')
    addAuditEvent(row, {
      actor: currentActor,
      source: 'manager',
      scope: 'sku',
      action: 'Изменения SKU',
      reason,
      oldValue: 'предыдущие настройки',
      newValue: `P_min ${drawerPmin} ₽, P_max ${drawerPmax} ₽, маржа ${drawerTargetMargin}%`,
    })
    notify(`SKU ${row.sku}: изменения сохранены, причина записана`)
    setDrawerActionReason('')
    setDrawerRow(null)
  }

  return (
    <div className="vella-root vella-html-preview" onMouseDown={closeFloatingSurfaces}>
      <aside className={`sidebar ${sidebarCollapsed ? 'collapsed' : ''}`}>
        <div className="logo">
          <img className="logo-full" src="/brand/satorna-logo-white.svg" alt="Satorna" />
          <img className="logo-icon-only" src="/brand/satorna-icon.svg" alt="" aria-hidden="true" />
        </div>

        <div className="nav-section">WB</div>
        <div className="nav-item active">
          <Icon name="activity" className="nav-icon" size={24} />
          <span className="nav-label">Репрайсер</span>
          <Icon name="chevronRight" className="chevron open" size={24} />
        </div>
        <div className="nav-sub nav-sub-open">
          {repricerNav.map((item) => (
            <div className={`nav-item ${item.label === activeSubtab ? 'nav-sub-active' : ''}`} key={item.label}>
              <Icon name={item.icon} className="nav-icon" size={24} />
              <span className="nav-label">{item.label}</span>
              {item.count && <span className={`nav-badge ${item.alert ? 'alert' : ''}`}>{item.count}</span>}
            </div>
          ))}
        </div>
        <div className="nav-item"><Icon name="barChart" className="nav-icon" size={24} /><span className="nav-label">Отчёты</span><Icon name="chevronRight" className="chevron" size={24} /></div>
        <div className="nav-item"><Icon name="star" className="nav-icon" size={24} /><span className="nav-label">Отзывы WB</span><span className="nav-badge soon">скоро</span></div>

        {sideNav.map((group) => (
          <div className="nav-group" key={group.section}>
            <div className="nav-section">{group.section}</div>
            {group.items.map((item) => (
              <div className="nav-item" key={item.label}>
                <Icon name={item.icon} className="nav-icon" size={24} />
                <span className="nav-label">{item.label}</span>
                {item.count && <span className={`nav-badge ${'alert' in item && item.alert ? 'alert' : ''} ${'warn' in item && item.warn ? 'warn' : ''} ${'soon' in item && item.soon ? 'soon' : ''}`}>{item.count}</span>}
              </div>
            ))}
          </div>
        ))}
        <button className="sidebar-edge" id="sidebarEdge" type="button" onClick={() => setSidebarCollapsed((value) => !value)} aria-label="Свернуть/развернуть меню">
          <span className="sidebar-edge-icon"><Icon name="chevronLeft" size={16} /></span>
        </button>
      </aside>

      <main className="main-area">
        <div className="topbar">
          <div className="nav-btns">
            <button className="nav-btn" type="button" aria-label="Назад" onClick={() => notify('История навигации: предыдущий экран недоступен в preview')}><Icon name="chevronLeft" size={18} /></button>
            <button className="nav-btn" type="button" aria-label="Вперёд" onClick={() => notify('История навигации: следующий экран недоступен в preview')}><Icon name="chevronRight" size={18} /></button>
          </div>
          <div className="breadcrumb"><span>WB</span><span className="sep">/</span><span className="current">Все товары</span></div>
          <div className="topbar-actions">
            <div className="global-period" id="globalPeriod">
              <div className="report-period" data-period-control>
                {['1 день', '7 дней', '14 дней', '30 дней'].map((item) => <button className={`period-btn ${activePeriod === item ? 'active' : ''}`} type="button" key={item} onClick={() => { setActivePeriod(item); notify(`Период переключен: ${item}`) }}>{item}</button>)}
                <span className="period-range-wrap">
                  <span className="period-custom-badge">Свой</span>
                  <input
                    className="period-range"
                    id="globalPeriodRange"
                    value={`${calendarStart.slice(0, 5)} — ${calendarEnd.slice(0, 5)}`}
                    readOnly
                    aria-label="Ручной диапазон"
                    onClick={() => toggleMenu('calendar')}
                  />
                </span>
              </div>
              <div className={`calendar-popover ${openMenu === 'calendar' ? 'open' : ''}`} id="calendarPopover" aria-hidden={openMenu !== 'calendar'}>
                <div className="calendar-head">
                  <div>
                    <div className="calendar-title">Период аналитики</div>
                    <div className="calendar-subtitle">Выберите быстрый период или диапазон дат</div>
                  </div>
                  <button className="calendar-close" type="button" onClick={() => setOpenMenu(null)} aria-label="Закрыть календарь">×</button>
                </div>
                <div className="calendar-body">
                  <div className="calendar-presets">
                    {['Сегодня', '7 дней', '14 дней', '30 дней', 'Свой период'].map((preset) => (
                      <button className={`calendar-preset ${preset === activePeriod ? 'active' : ''}`} type="button" key={preset} onClick={() => { setActivePeriod(preset); notify(`Выбран пресет календаря: ${preset}`) }}>{preset}</button>
                    ))}
                  </div>
                  <div className="calendar-main">
                    <div className="calendar-manual-row">
                      <div className="calendar-date-field"><label htmlFor="calendarStartInput">От</label><input className="calendar-date-input" id="calendarStartInput" value={calendarStart} onChange={(event) => setCalendarStart(event.target.value)} /></div>
                      <div className="calendar-date-field"><label htmlFor="calendarEndInput">До</label><input className="calendar-date-input" id="calendarEndInput" value={calendarEnd} onChange={(event) => setCalendarEnd(event.target.value)} /></div>
                    </div>
                    <div className="calendar-toolbar">
                      <div className="calendar-visible-range">{calendarOffset === 0 ? 'Апрель — Май 2026' : calendarOffset > 0 ? 'Май — Июнь 2026' : 'Март — Апрель 2026'}</div>
                      <div className="calendar-nav" aria-label="Переключение месяцев">
                        <button className="calendar-nav-btn" type="button" aria-label="Предыдущий месяц" onClick={() => { setCalendarOffset((value) => value - 1); notify('Календарь: предыдущий месяц') }}>‹</button>
                        <button className="calendar-nav-btn" type="button" aria-label="Следующий месяц" onClick={() => { setCalendarOffset((value) => value + 1); notify('Календарь: следующий месяц') }}>›</button>
                      </div>
                    </div>
                    <div className="calendar-months">
                      {['Апрель 2026', 'Май 2026'].map((month, monthIndex) => (
                        <div key={month}>
                          <div className="calendar-month-title"><span>{month}</span></div>
                          <div className="calendar-grid">
                            {['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'].map((day) => <div className="calendar-dow" key={day}>{day}</div>)}
                            {calendarDays.map((day) => {
                              const inRange = monthIndex === 0 ? day >= 25 && day <= 30 : day === 1
                              const start = monthIndex === 0 && day === 25
                              const end = monthIndex === 1 && day === 1
                              if (day > 31) {
                                return <button className="calendar-day" type="button" key={`${month}-${day}`} disabled aria-hidden="true" tabIndex={-1} />
                              }
                              return (
                                <button
                                  className={`calendar-day ${inRange ? 'in-range' : ''} ${start ? 'range-start' : ''} ${end ? 'range-end' : ''}`}
                                  type="button"
                                  key={`${month}-${day}`}
                                  aria-label={`${day} ${month}`}
                                  onClick={() => notify(`Выбрана дата: ${day} ${month}`)}
                                >
                                  {day}
                                </button>
                              )
                            })}
                          </div>
                        </div>
                      ))}
                    </div>
                    <div className="calendar-foot">
                      <div className="calendar-selection">Выбрано: <b>{calendarStart.slice(0, 5)} — {calendarEnd.slice(0, 5)}</b></div>
                      <div className="calendar-actions"><button className="btn btn-default btn-sm" type="button" onClick={() => setOpenMenu(null)}>Отмена</button><button className="btn btn-primary btn-sm" type="button" onClick={() => { setOpenMenu(null); notify(`Период применен: ${calendarStart.slice(0, 5)} — ${calendarEnd.slice(0, 5)}`) }}>Применить</button></div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
            <button className="icon-btn" aria-label="Ночная медиана" onClick={() => showModal('night')}><Icon name="moon" size={16} /></button>
            <div className={`dd ${openMenu === 'notifications' ? 'open' : ''}`} id="ddNotif">
              <button className="icon-btn has-badge" id="bellBtn" data-review="notifications" type="button" aria-label="Уведомления" onClick={() => toggleMenu('notifications')}>
                <Icon name="bell" size={16} />
                <span className="notif-badge" id="notifBadge">7</span>
              </button>
              <div className="notif-panel" id="notifPanel" aria-hidden={openMenu !== 'notifications'}>
                <div className="notif-head">
                  <div className="notif-head-left"><span className="notif-head-title">Уведомления</span><span className="notif-head-count">7 новых</span></div>
                  <button className="notif-head-markall" type="button" onClick={() => notify('Все уведомления отмечены прочитанными')}>Прочитать все</button>
                </div>
                <div className="notif-body">
                  {[
                    ['critical', 'Цена ниже P_min', 'HCBT_22 требует подтверждения отрицательной маржи', '2 мин'],
                    ['warning', 'Ликвидация', '8 SKU попали в кандидаты по низким корзинам', '18 мин'],
                    ['info', 'Экспорт готов', 'XLSX по репрайсеру сформирован', '1 ч'],
                  ].map(([severity, title, body, time]) => (
                    <div className="notif-row" key={title}>
                      <button className="notif-row-btn" type="button" onClick={() => notify(`Открыто уведомление: ${title}`)}>
                        <div className="notif-icon-wrap"><span className="notif-type-icon">!</span><span className={`notif-unread-dot ${severity}`} /></div>
                        <div className="notif-text"><div className="notif-title-row"><span className={`notif-title ${severity}`}>{title}</span><span className="notif-time">{time}</span></div><div className="notif-body-text">{body}</div></div>
                        <span className="notif-chevron">▾</span>
                      </button>
                    </div>
                  ))}
                </div>
                <div className="notif-foot"><span className="notif-detail-link">Все уведомления</span> · последние 7 дней</div>
              </div>
            </div>
            <div className={`dd q-menu ${openMenu === 'export' ? 'open' : ''}`} id="ddExport">
              <button className="btn btn-default" type="button" onClick={() => toggleMenu('export')}><Icon name="download" size={13} />Excel</button>
              <div className="dd-menu">
                <button className="dd-item" type="button" onClick={() => notify('Экспорт XLSX поставлен в очередь')}><Icon name="file" size={13} />Экспорт XLSX</button>
                <button className="dd-item" type="button" onClick={() => notify('Открыт mock-flow импорта XLSX')}><Icon name="plus" size={13} />Импорт XLSX для цен</button>
              </div>
            </div>
            <div className="user-chip"><div className="user-av">МФ</div><span className="user-name">Мария Ф.</span></div>
          </div>
        </div>

        <div className="subtabs">
          <div className="module-panel active">
            <button className={`subtab ${activeSubtab === 'Все товары' ? 'active' : ''}`} type="button" onClick={() => setActiveSubtab('Все товары')}><Icon name="box" size={14} />Все товары <span className="subtab-count">1 482</span></button>
            <button className={`subtab ${activeSubtab === 'Стратегии' ? 'active' : ''}`} type="button" onClick={() => { setActiveSubtab('Стратегии'); notify('В preview показана вкладка стратегий') }}><Icon name="grid" size={14} />Стратегии <span className="subtab-count">5</span></button>
            <button className={`subtab ${activeSubtab === 'История' ? 'active' : ''}`} type="button" onClick={() => { setActiveSubtab('История'); notify('В preview показана вкладка истории') }}><Icon name="clock" size={14} />История</button>
            <span className="subtab-divider" />
            <button className={`subtab ${activeSubtab === 'Ликвидация' ? 'active' : ''}`} type="button" onClick={() => { setActiveSubtab('Ликвидация'); notify('В preview показан переход к ликвидации') }}><Icon name="trendDown" size={14} />Ликвидация <span className="subtab-count danger">8</span></button>
            <button className={`subtab ${activeSubtab === 'Акции WB' ? 'active' : ''}`} type="button" onClick={() => { setActiveSubtab('Акции WB'); notify('В preview показана вкладка акций WB') }}><Icon name="percent" size={14} />Акции WB <span className="subtab-count warn">2!</span></button>
            <span className="subtab-divider" />
            <button className={`subtab ${activeSubtab === 'Правила' ? 'active' : ''}`} type="button" onClick={() => { setActiveSubtab('Правила'); notify('В preview показана вкладка правил') }}><Icon name="sliders" size={14} />Правила</button>
          </div>
        </div>

        <div className="tab-content active">
          <div className="stats repricer-kpis">
            {[
              ['Выручка за период', '633 288 ₽', '↑ +12.4% к прошлому периоду'],
              ['Средняя маржа %', '26.8%', '↑ +2.1 пп к прошлому периоду'],
              ['Маржа ₽', '161 589 ₽', 'после СПП, комиссии, логистики и хранения'],
              ['Цен изменено за период', '216', '↑ +18% к прошлому периоду'],
              ['Корзины за период', '472', '↑ активный спрос'],
              ['SKU в продаже', '1 478', '33 склада WB'],
              ['% участия в акциях', '44%', 'цены акции из XLSX'],
            ].map(([label, value, delta], index) => (
              <div className="stat" key={label}>
                <div className="stat-label">{label} <span className="stat-tip" tabIndex={0} data-tip={`${label}: показатель за выбранный период`}>i</span></div>
                <div className="stat-val">{value}</div>
                <div className={`stat-delta ${index === 2 || index === 5 || index === 6 ? 'neutral' : 'up'}`}>{delta}</div>
              </div>
            ))}
          </div>

          {activeSubtab === 'Все товары' ? (
            <>
          <div className="toolbar">
            <label className="search">
              <span aria-hidden="true">⌕</span>
              <input value={query} placeholder="Артикул или название..." onChange={(event) => setQuery(event.target.value)} />
            </label>
            <div className="chips">
              <button className={`chip ${status === 'all' && filterPreset === 'all' ? 'active' : ''}`} type="button" onClick={() => { setStatus('all'); setPreset('all') }}>Все <span className="chip-count">25</span></button>
              <button className={`chip ${filterPreset === 'locomotive' ? 'active' : ''}`} type="button" onClick={() => setPreset('locomotive')}>Локомотивы <span className="chip-count">15</span></button>
              <button className={`chip ${status === 'new' ? 'active' : ''}`} type="button" onClick={() => { setPreset('all'); setStatus('new') }}>Новинки <span className="chip-count">2</span></button>
              <button className={`chip ${filterPreset === 'dead' ? 'active' : ''}`} type="button" onClick={() => setPreset('dead')}>Неликвид <span className="chip-count">0</span></button>
              <button className={`chip ${filterPreset === 'liquidation' ? 'active' : ''}`} type="button" onClick={() => setPreset('liquidation')}>Ликвидация <span className="chip-count warm">4</span></button>
              <button className={`chip ${status === 'manual' ? 'active' : ''}`} type="button" onClick={() => { setPreset('all'); setStatus('manual') }}>Ручной <span className="chip-count">4</span></button>
              <button className={`chip ${filterPreset === 'noPmin' ? 'active' : ''}`} type="button" onClick={() => setPreset('noPmin')}>Без P_min <span className="chip-count danger">2</span></button>
            </div>
            <div className="toolbar-right">
              <select className="adv-select" aria-label="Менеджер" value={manager} onChange={(event) => { setManager(event.target.value); notify(`Менеджер: ${event.target.value}`) }}>
                {managerOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
              </select>
              <button className="view-btn view-btn-wide" type="button" aria-label="Расширенные фильтры" onClick={() => setAdvancedOpen((value) => !value)}><Icon name="filter" size={14} /></button>
              <div className={`dd q-menu ${openMenu === 'sort' ? 'open' : ''}`} id="ddSort">
                <button className="view-btn" type="button" aria-label="Сортировка" onClick={() => toggleMenu('sort')}><Icon name="sort" size={14} /></button>
                <div className="dd-menu">
                  <div className="dd-section">Сортировка</div>
                  {[
                    ['По корзинам ↓', 'baskets-desc'],
                    ['По марже ↓', 'margin-desc'],
                    ['По цене ↓', 'price-desc'],
                    ['По остатку ↑', 'stock-asc'],
                  ].map(([label, mode]) => <button className="dd-item" type="button" key={label} onClick={() => { setSortMode(mode as SortMode); setOpenMenu(null); notify(`Сортировка: ${label}`) }}>{label}</button>)}
                  <div className="dd-divider" />
                  <button className="dd-item" type="button" onClick={() => { setSortMode('sku-asc'); setOpenMenu(null); notify('Сортировка: по артикулу А–Я') }}>По артикулу А–Я</button>
                </div>
              </div>
              <div className={`dd q-menu ${openMenu === 'columns' ? 'open' : ''}`} id="ddCols">
                <button className="view-btn" type="button" aria-label="Колонки" onClick={() => toggleMenu('columns')}><Icon name="columns" size={14} /></button>
                <div className="dd-menu">
                  <div className="dd-section">Видимые колонки</div>
                  {['Артикул', 'WB-артикул', 'Статус', 'Ответственный', 'ABC и акция', 'Цена / СПП', 'Маржа', 'Комиссия WB', 'Корзины', 'Заказы', 'Остаток', 'Стратегия'].map((column) => (
                    <label className="dd-item" key={column}><input type="checkbox" checked={isVisible(column)} onChange={() => toggleColumn(column)} />{column}</label>
                  ))}
                  <div className="dd-divider" />
                  {['P_min / P_max', 'Реклама / ДРР / ROI', 'Логистика / хранение / себестоимость'].map((column) => (
                    <label className="dd-item" key={column}><input type="checkbox" checked={isVisible(column)} onChange={() => toggleColumn(column)} />{column}</label>
                  ))}
                </div>
              </div>
              <div className="tb-divider" />
              <div className="row-count">Показано <b>{filtered.length}</b> из 1 482</div>
              <div className="tb-divider" />
              <button className="btn btn-default" type="button" onClick={() => showModal('problemQueue')}>Проблемные SKU · 3</button>
              <button className="btn btn-default" type="button" onClick={() => setApiErrorOpen(true)}>API</button>
              <button className="btn btn-primary" type="button" onClick={() => showModal('apply')}>Применить цены · 47</button>
            </div>
          </div>

          <div className={`active-filters ${advancedOpen ? 'show' : ''}`} id="activeFilters">
            <span className="active-filter-label">Фильтры:</span>
            {advancedFilters.marginFrom && <span className="afilter">Маржа: от {advancedFilters.marginFrom}% <button type="button" onClick={() => setAdvancedValue('marginFrom', '')}>×</button></span>}
            {advancedFilters.basketsFrom && <span className="afilter">Корзины: от {advancedFilters.basketsFrom} <button type="button" onClick={() => setAdvancedValue('basketsFrom', '')}>×</button></span>}
            {!advancedFilters.marginFrom && !advancedFilters.basketsFrom && <span className="afilter">Активируйте поля ниже</span>}
            <button className="afilter-clear" type="button" onClick={resetAdvancedFilters}>Сбросить</button>
          </div>

          <div className={`adv-bar ${advancedOpen ? 'open' : ''}`} id="advBar">
            <div className="adv-grid">
              <div className="adv-field"><label>Маржа, %</label><div className="range-input"><input type="number" placeholder="от" value={advancedFilters.marginFrom} onChange={(event) => setAdvancedValue('marginFrom', event.target.value)} /><span>—</span><input type="number" placeholder="до" value={advancedFilters.marginTo} onChange={(event) => setAdvancedValue('marginTo', event.target.value)} /></div></div>
              <div className="adv-field"><label>Корзины за период</label><div className="range-input"><input type="number" placeholder="от" value={advancedFilters.basketsFrom} onChange={(event) => setAdvancedValue('basketsFrom', event.target.value)} /><span>—</span><input type="number" placeholder="до" value={advancedFilters.basketsTo} onChange={(event) => setAdvancedValue('basketsTo', event.target.value)} /></div></div>
              <div className="adv-field"><label>Цена, ₽</label><div className="range-input"><input type="number" placeholder="от" value={advancedFilters.priceFrom} onChange={(event) => setAdvancedValue('priceFrom', event.target.value)} /><span>—</span><input type="number" placeholder="до" value={advancedFilters.priceTo} onChange={(event) => setAdvancedValue('priceTo', event.target.value)} /></div></div>
              <div className="adv-field"><label>Тип одежды</label><div className="adv-checks">
                {[
                  ['tee', 'Футболка'],
                  ['hoodie', 'Худи'],
                  ['longsleeve', 'Лонгслив'],
                  ['shorts', 'Шорты'],
                ].map(([value, label]) => <button className={`adv-check ${garmentFilters.has(value) ? 'on' : ''}`} type="button" key={value} onClick={() => toggleSetValue(setGarmentFilters, value)}>{label}</button>)}
              </div></div>
              <div className="adv-field"><label>Пол / цвет</label><div className="adv-checks">
                {[
                  ['female', 'Жен.'],
                  ['male', 'Муж.'],
                  ['white', 'Белый'],
                  ['black', 'Чёрный'],
                ].map(([value, label]) => <button className={`adv-check ${colorFilters.has(value) ? 'on' : ''}`} type="button" key={value} onClick={() => toggleSetValue(setColorFilters, value)}>{label}</button>)}
              </div></div>
              <div className="adv-field"><label>Стратегия</label><select className="adv-select" value={advancedFilters.strategy} onChange={(event) => setAdvancedValue('strategy', event.target.value)}><option>Все</option><option>Агрессивный</option><option>Консервативный</option><option>Ночная медиана</option><option>Ликвидация</option></select></div>
              <div className="adv-field"><label>Остаток на складе</label><select className="adv-select" value={advancedFilters.stock} onChange={(event) => setAdvancedValue('stock', event.target.value)}><option>Любой</option><option>Заканчивается (≤ 5)</option><option>Низкий (≤ 20)</option><option>Нормальный</option></select></div>
            </div>
            <div className="adv-actions"><button className="link-action" type="button" onClick={() => notify('Сегмент сохранен в mock state')}>✓ Сохранить как сегмент</button><div><button className="btn btn-ghost btn-sm" type="button" onClick={resetAdvancedFilters}>Сбросить</button><button className="btn btn-primary btn-sm" type="button" onClick={() => notify(`Расширенные фильтры применены: ${filtered.length} SKU`) }>Применить</button></div></div>
          </div>

          <div className="table-wrap">
            {loadingOpen && (
              <div className="data-loading-state">
                <div className="vella-loader" />
                <div className="data-loading-copy">
                  <div className="data-loading-title">Загружаем данные WB</div>
                  <div className="data-loading-text">Подтягиваем данные из API. Экран обновится автоматически, когда источник ответит.</div>
                  <div className="data-loading-skeleton"><span className="skel skel-line skel-w-lg" /><span className="skel skel-line skel-w-md" /><span className="skel skel-line skel-w-sm" /></div>
                </div>
              </div>
            )}
            <table id="mainTable" className={isEmpty ? 'is-hidden' : ''}>
              <colgroup>
                <col style={{ width: 36 }} /><col className="col-sku" /><col style={{ width: 220 }} /><col style={{ width: 108 }} /><col className="col-stat" />
                <col style={{ width: 58 }} /><col style={{ width: 74 }} /><col className="col-price" /><col style={{ width: 110 }} /><col style={{ width: 70 }} />
                <col className="col-mg" /><col style={{ width: 72 }} /><col className="col-bsk" /><col className="col-ord" /><col style={{ width: 70 }} />
                <col style={{ width: 105 }} /><col style={{ width: 124 }} />
              </colgroup>
              <thead>
                <tr>
                  <th><button className="cb indet" type="button" aria-label="Выбрать все" onClick={() => setSelected(new Set(filtered.map((row) => row.sku)))} /></th>
                  {isVisible('Артикул') && <th>Фото / артикул</th>}
                  {isVisible('Комментарий') && <th><div className="th-tip tl">Комментарий <span className="tip-i">?</span><div className="tip-box">Заметки менеджера по SKU и причины ручных действий.</div></div></th>}
                  {isVisible('WB-артикул') && <th>WB-арт.</th>}
                  {isVisible('Статус') && <th><div className="th-tip tl">Статус товара <span className="tip-i">?</span><div className="tip-box">Статус аналитики SKU: локомотив, новинка, неликвид, ручной или ликвидация. Это не режим ценовой защиты.</div></div></th>}
                  {isVisible('Ответственный') && <th><div className="th-tip tl">Менеджер <span className="tip-i">?</span><div className="tip-box">Ответственный за SKU. Используется в отчётах и план-факте, но не влияет на цену.</div></div></th>}
                  {isVisible('ABC и акция') && <th><div className="th-tip tc">ABC <span className="tip-i">?</span><div className="tip-box">Две буквы: первая по продажам, вторая по чистой прибыли после всех расходов.</div></div></th>}
                  {isVisible('ABC и акция') && <th><div className="th-tip tc">Акция <span className="tip-i">?</span><div className="tip-box">Статус участия в акции WB по загруженному XLSX-файлу и ценам акции.</div></div></th>}
                  {isVisible('Цена / СПП') && <th className="num sorted">Цена до СПП ↓ <span className="sort-rank">1</span></th>}
                  {isVisible('Цена / СПП') && <th className="num">Средняя с СПП</th>}
                  {isVisible('Цена / СПП') && <th className="num">% СПП</th>}
                  {isVisible('Маржа') && <th className="num"><div className="th-tip tc">Маржа <span className="tip-i">?</span><div className="tip-box">Маржинальность = (Цена − себестоимость − комиссия WB − логистика) / Цена × 100%</div></div></th>}
                  {isVisible('Комиссия WB') && <th className="num">Комиссия</th>}
                  {isVisible('Корзины') && <th className="num"><div className="th-tip tc">Корзины <span className="tip-i">?</span><div className="tip-box">Добавлений в корзину за выбранный период. Это главный сигнал для изменения цены.</div></div></th>}
                  {isVisible('Заказы') && <th className="num">Заказы</th>}
                  {isVisible('Заказы') && <th className="num"><div className="th-tip tr">Выкуп <span className="tip-i">?</span><div className="tip-box">Процент выкупа за 30 дней. Влияет на расчёт логистики WB и реальную маржу.</div></div></th>}
                  {isVisible('Остаток') && <th className="num"><div className="th-tip tr">Остаток WB <span className="tip-i">?</span><div className="tip-box">Физический остаток на складах WB. Разрез по складам находится во вкладке Отчёты → Остатки.</div></div></th>}
                  {isVisible('Стратегия') && <th><div className="th-tip tr">Стратегия <span className="tip-i">?</span><div className="tip-box">Набор правил, по которым алгоритм меняет цену: агрессивный, консервативный, ночная медиана или ликвидация.</div></div></th>}
                </tr>
              </thead>
              <tbody>
                {filtered.map((row) => {
                  const isSelected = selected.has(row.sku)
                  return (
                    <tr className={isSelected ? 'sel' : ''} key={row.sku}>
                      <td><button className={`cb ${isSelected ? 'on' : ''}`} type="button" aria-label={`${isSelected ? 'Снять выделение' : 'Выбрать'} ${row.sku}`} onClick={() => toggleSelected(row.sku)}>{isSelected ? '✓' : ''}</button></td>
                      {isVisible('Артикул') && <td>
                        <div className="sku-wrap">
                          <ShirtThumb row={row} />
                          <div><div className="sku-row"><button className="sku" type="button" onClick={() => openDrawer(row)}>{row.sku}</button><span className="sku-wb">↗</span></div><div className="sku-size">{row.size}</div></div>
                        </div>
                      </td>}
                      {isVisible('Комментарий') && <td>
                        <button className={`report-comment-btn ${latestComment(row.sku) ? '' : 'report-comment-empty'}`} type="button" onClick={(event) => { event.stopPropagation(); openDrawer(row); setDrawerMiscTab('logs') }}>
                          {latestComment(row.sku) ? (
                            <>
                              <span className="report-comment-preview">{latestComment(row.sku)?.text}</span>
                              <span className="report-comment-meta">{latestComment(row.sku)?.author.name} · {shortDateTime(latestComment(row.sku)?.createdAt ?? '')} · {commentsForSku(row.sku).length}</span>
                            </>
                          ) : 'Добавить'}
                        </button>
                      </td>}
                      {isVisible('WB-артикул') && <td><span className="wb-link">{row.wbId}</span></td>}
                      {isVisible('Статус') && <td><StatusBadge status={row.status} /></td>}
                      {isVisible('Ответственный') && <td><span className={managerRecordFor(row) ? 'report-tag ok' : 'report-tag neutral'}>{managerNameFor(row)}</span></td>}
                      {isVisible('ABC и акция') && <td><span className={`abc-badge ${row.abc.toLowerCase()}`}>{row.abc}</span></td>}
                      {isVisible('ABC и акция') && <td><span className={`promo-dot ${row.promo ? 'yes' : 'no'}`} />{row.promo ? 'да' : 'нет'}</td>}
                      {isVisible('Цена / СПП') && <td className="num editable-price" onClick={() => startPriceEdit(row)}>
                        {editingPriceSku === row.sku ? (
                          <span className="inline-price-editor" onClick={(event) => event.stopPropagation()}>
                            <input value={draftPrice} onChange={(event) => setDraftPrice(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') savePriceEdit(row); if (event.key === 'Escape') setEditingPriceSku(null) }} autoFocus />
                            <input className="inline-reason-input" value={inlineReason} placeholder="Причина" onChange={(event) => setInlineReason(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') savePriceEdit(row); if (event.key === 'Escape') setEditingPriceSku(null) }} />
                            <button type="button" onClick={() => savePriceEdit(row)}>✓</button>
                          </span>
                        ) : (
                          <><strong>{rub(priceOverrides[row.sku] ?? row.price)}</strong><span className="sub">{priceOverrides[row.sku] ? 'ручн.' : 'без изм.'}</span></>
                        )}
                      </td>}
                      {isVisible('Цена / СПП') && <td className="num"><strong>{rub(row.avgPrice)}</strong><span className="sub">с СПП</span></td>}
                      {isVisible('Цена / СПП') && <td className="num"><strong>{row.spp}%</strong></td>}
                      {isVisible('Маржа') && <td className="num"><span className={row.margin < 0 ? 'metric-down' : 'metric-up'}>{row.margin > 0 ? '+' : ''}{row.margin}%</span><span className="sub">{row.marginRub > 0 ? '+' : ''}{row.marginRub} ₽</span></td>}
                      {isVisible('Комиссия WB') && <td className="num"><strong>{row.commission}%</strong></td>}
                      {isVisible('Корзины') && <td className="num">{row.baskets} ↑ <MiniBars /></td>}
                      {isVisible('Заказы') && <td className="num">{row.orders} ↑ <MiniBars /></td>}
                      {isVisible('Заказы') && <td className={`num buyout ${row.buyout < 75 ? 'warn' : ''}`}>{row.buyout}%</td>}
                      {isVisible('Остаток') && <td className="num"><strong>{row.stock}</strong><span className="sub">{rub(row.stockValue)}</span></td>}
                      {isVisible('Стратегия') && <td>{row.strategy ? <span className={`strategy-chip ${row.strategy === 'Ликвидация' ? 'liq' : row.strategy === 'Ночная медиана' ? 'night' : row.strategy === 'Консервативный' ? 'safe' : 'active'}`}><span className="dot dot-auto" />{row.strategy}</span> : <span className="strategy-empty">— нет —</span>}</td>}
                    </tr>
                  )
                })}
              </tbody>
            </table>
            {isEmpty && (
              <div className="empty show">
                <div className="empty-icon"><Icon name="box" size={24} /></div>
                <div className="empty-title">Нет SKU под текущие фильтры</div>
                <div className="empty-desc">Сбросьте поиск, статус или дополнительные фильтры, чтобы снова увидеть товары.</div>
              </div>
            )}
            {apiErrorOpen && (
              <div className="api-error show" id="apiErrorState">
                <div className="api-error-icon">×</div>
                <div className="api-error-title">WB API временно недоступен</div>
                <div className="api-error-desc">Не удалось получить актуальные корзины и цены. Алгоритм продолжает работать на последних данных.</div>
                <div className="api-error-meta">Последняя успешная синхронизация: <b>сегодня в 14:32</b> (3 минуты назад)</div>
                <div className="api-error-actions"><button className="btn btn-default btn-sm" type="button" onClick={() => setApiErrorOpen(false)}>Закрыть</button><button className="btn btn-primary btn-sm" type="button" onClick={() => setToastOpen(true)}>Повторить подключение</button></div>
              </div>
            )}
          </div>

          <div className="pagination">
            <span className="page-info">Показано <b>1–25</b> из <b>1 482</b></span>
            <div className="spacer" />
            <span>Строк на странице:</span>
            <select className="per-page-sel"><option>25</option><option>50</option></select>
            <div className="page-jump">{['‹', '1', '2', '3', '…', '60', '›'].map((page, index) => <button className={`page-btn ${page === '1' ? 'active' : ''}`} type="button" key={`${page}-${index}`} onClick={() => notify(`Пагинация: страница ${page}`)}>{page}</button>)}</div>
          </div>

          <div className={`bulk-bar ${selectedCount > 0 ? 'show' : ''}`}>
            <span className="bulk-count"><b>{selectedCount}</b> выделено</span>
            <div className={`dd q-menu ${bulkMenuOpen ? 'open' : ''}`} id="ddBulkTpl">
              <button className="bulk-btn" type="button" onClick={() => setBulkMenuOpen((value) => !value)}><Icon name="grid" size={13} />Применить стратегию ▾</button>
              <div className="dd-menu dark up">
                <div className="dd-section">Стратегии</div>
                {['Агрессивный', 'Консервативный', 'Запуск новинки', 'Ночная медиана'].map((label) => <button className="dd-item" type="button" key={label} onClick={() => { setBulkMenuOpen(false); setBulkActionDraft({ title: 'Массовая смена стратегии', action: 'strategy', newValue: label }); showModal('bulkAction') }}><span className="tpl-color-dot" />{label}</button>)}
                <div className="dd-divider" />
                <button className="dd-item" type="button" onClick={() => notify('Открыт mock-flow создания стратегии')}>+ Создать новый</button>
              </div>
            </div>
            <button className="bulk-btn" type="button" onClick={() => { setBulkActionDraft({ title: 'Массовое изменение P_min', action: 'p_min', newValue: '800 ₽' }); showModal('bulkPmin') }}>— Задать P_min</button>
            <button className="bulk-btn" type="button" onClick={() => { setBulkActionDraft({ title: 'Перевод в ручной режим', action: 'manual_mode', newValue: 'ручной режим' }); showModal('bulkAction') }}>◉ Перевести в ручной</button>
            <button className="bulk-btn" type="button" onClick={() => { setBulkActionDraft({ title: 'Назначить менеджера', action: 'assign_manager', newValue: 'Ирина', managerId: 'manager-irina' }); showModal('bulkAction') }}>Назначить менеджера</button>
            <button className="bulk-btn danger" type="button" onClick={() => { setBulkActionDraft({ title: 'Массовая ликвидация', action: 'liquidation', newValue: 'ликвидация' }); showModal('bulkLiq') }}>⌁ Ликвидировать</button>
            <button className="bulk-btn" type="button" onClick={() => notify(`Экспорт ${selectedCount} выбранных SKU поставлен в очередь`)}>↧ Экспорт</button>
            <button className="bulk-close" type="button" aria-label="Очистить выделение" onClick={() => setSelected(new Set())}>×</button>
          </div>
            </>
          ) : (
            <SecondaryTabContent tab={activeSubtab} notify={notify} showModal={showModal} />
          )}
        </div>
      </main>

      <div className={`drawer-overlay ${drawerRow ? 'open' : ''}`} onClick={() => setDrawerRow(null)} />
      <aside className={`drawer ${drawerRow ? 'open' : ''}`} aria-hidden={!drawerRow}>
        {drawerRow && (
          <>
            <div className="drawer-header">
              <div className="drawer-thumb-md"><ShirtThumb row={drawerRow} /></div>
              <div className="drawer-info">
                <div className="drawer-title">{drawerRow.name}</div>
                <div className="drawer-meta">
                  <span className="drawer-sku">{drawerRow.sku} · {drawerRow.size}</span>
                  <a className="drawer-link-btn" href={`https://www.wildberries.ru/catalog/${drawerRow.wbId}/detail.aspx`} target="_blank" rel="noreferrer">WB карточка ↗</a>
                </div>
                <div className="drawer-meta"><StatusBadge status={drawerRow.status} /><span className={`abc-badge ${drawerRow.abc.toLowerCase()}`}>{drawerRow.abc}</span><span className="brand-chip">Anomie</span></div>
                <div className="drawer-meta">
                  <span>Ответственный</span>
                  <select
                    className="adv-select"
                    value={drawerManagerId ?? managerRecordFor(drawerRow)?.id ?? 'unassigned'}
                    onChange={(event) => setDrawerManagerId(event.target.value === 'unassigned' ? null : event.target.value)}
                  >
                    <option value="unassigned">Без ответственного</option>
                    {repricerManagers.filter((item) => item.active).map((manager) => <option key={manager.id} value={manager.id}>{manager.name}</option>)}
                  </select>
                </div>
              </div>
              <button className="drawer-close" type="button" onClick={() => setDrawerRow(null)}>×</button>
            </div>
            <div className="drawer-tabs">
              <button className={`drawer-tab ${drawerTab === 'overview' ? 'active' : ''}`} type="button" onClick={() => setDrawerTab('overview')}>Обзор</button>
              <button className={`drawer-tab ${drawerTab === 'rules' ? 'active' : ''}`} type="button" onClick={() => setDrawerTab('rules')}>Правила</button>
              <button className={`drawer-tab ${drawerTab === 'misc' ? 'active' : ''}`} type="button" onClick={() => setDrawerTab('misc')}>Прочее</button>
            </div>
            <div className="drawer-body">
              {drawerTab === 'overview' && (
                <>
                  <div className="d-price-block">
                    <div className="d-price-row"><div><div className="d-price-label">Текущая цена до СПП</div><div className="d-price-big">{rub(priceOverrides[drawerRow.sku] ?? drawerRow.price)}</div><div id="dPriceHint" className="neutral">{priceOverrides[drawerRow.sku] ? '— ручная правка в preview' : '— цена не менялась за 24 ч'}</div></div><button className="d-price-edit" type="button" onClick={() => startPriceEdit(drawerRow)}>Изменить</button></div>
                    {editingPriceSku === drawerRow.sku && <div className="drawer-inline-price"><input value={draftPrice} onChange={(event) => setDraftPrice(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') savePriceEdit(drawerRow); if (event.key === 'Escape') setEditingPriceSku(null) }} autoFocus /><button className="btn btn-primary btn-sm" type="button" onClick={() => savePriceEdit(drawerRow)}>Сохранить</button><button className="btn btn-ghost btn-sm" type="button" onClick={() => setEditingPriceSku(null)}>Отмена</button></div>}
                    <div className="d-price-meta"><span>P_min <b>1 850 ₽</b></span><span>P_max <b>{rub(Math.round((priceOverrides[drawerRow.sku] ?? drawerRow.price) * 1.38))}</b></span><span>РРЦ <b>{rub((priceOverrides[drawerRow.sku] ?? drawerRow.price) + 387)}</b></span></div>
                  </div>
                  <div className="drawer-chip-row"><span className="brand-chip">H</span><span className="brand-chip anomie">Anomie</span><span className={`abc-badge ${drawerRow.abc.toLowerCase()}`}>{drawerRow.abc}</span><span className="user-av mini">{managerNameFor(drawerRow).slice(0, 2).toUpperCase()}</span><span>{managerNameFor(drawerRow)}</span><span className="wb-link">↗ {drawerRow.wbId}</span></div>
                  <DrawerSection title="Цены и маржа" rows={[
                    ['Цена после СПП', rub(drawerRow.avgPrice)],
                    ['Средняя цена за период с СПП', rub(drawerRow.avgPrice)],
                    ['СПП', `${drawerRow.spp}%`],
                    ['Себестоимость', '720 ₽'],
                    ['Маржа', `${drawerRow.margin > 0 ? '+' : ''}${drawerRow.margin}%`],
                    ['Маржа в ₽', `${drawerRow.marginRub > 0 ? '+' : ''}${drawerRow.marginRub} ₽`],
                    ['Комиссия WB', `${Math.round((priceOverrides[drawerRow.sku] ?? drawerRow.price) * drawerRow.commission / 100)} ₽ (${drawerRow.commission}%)`],
                    ['Логистика', '53 ₽'],
                    ['Хранение', '167 ₽/мес'],
                    ['Налоги', '6%'],
                    ['Акция WB', drawerRow.promo ? 'да' : 'нет'],
                    ['Стратегия', drawerRow.strategy || '— нет —'],
                  ]} />
                  <DrawerSection title="Спрос" rows={[
                    ['Корзины за период', `${drawerRow.baskets} ↑`],
                    ['Заказы за период', `${drawerRow.orders} шт`],
                    ['Показы за период', '488'],
                    ['CR (просмотр→заказ)', '4.1%'],
                    ['Выкуп %', `${drawerRow.buyout}%`],
                    ['Реклама за период', '1 056 ₽'],
                    ['ДРР', '2.5%'],
                    ['ROI рекламы', '+3 972%'],
                  ]} />
                  <DrawerSection title="Запас" rows={[
                    ['Остаток WB', `${drawerRow.stock} шт`],
                    ['Остаток WB в ₽', rub(drawerRow.stockValue)],
                    ['SKU в продаже', 'да'],
                    ['Мин допустимый', '5 шт'],
                    ['Дней до OOS', '~19 дн'],
                  ]} />
                  <section className="d-section">
                    <div className="d-section-title">WB-цены и калькулятор маржи <button className="help-btn" type="button" onClick={() => notify('Калькулятор показывает маржу после СПП, комиссии, логистики и хранения')}>?</button></div>
                    <div className="calc-block">
                      <div className="calc-slider-wrap">
                        <div className="calc-slider-head"><span>Цена селлера</span><b>{rub(priceOverrides[drawerRow.sku] ?? drawerRow.price)}</b></div>
                        <input
                          className="calc-slider"
                          type="range"
                          min={Math.max(400, drawerRow.price - 500)}
                          max={drawerRow.price + 800}
                          value={priceOverrides[drawerRow.sku] ?? drawerRow.price}
                          onChange={(event) => {
                            setPriceOverrides((current) => ({ ...current, [drawerRow.sku]: Number(event.target.value) }))
                          }}
                          aria-label="Новая цена продавца до СПП"
                        />
                        <div className="calc-slider-labels"><span>P_min: <b>{drawerPmin} ₽</b></span><span>P_max: <b>{drawerPmax} ₽</b></span></div>
                      </div>
                      <div className="calc-row"><span>СПП WB</span><b>{drawerRow.spp}%</b></div>
                      <div className="calc-row"><span>WB-кошелёк</span><b>3%</b></div>
                      <div className="calc-row"><span>Цена с СПП</span><b>{rub(Math.round((priceOverrides[drawerRow.sku] ?? drawerRow.price) * (1 - drawerRow.spp / 100)))}</b></div>
                      <div className="calc-row total"><span>Итоговая для покупателя</span><b>{rub(Math.round((priceOverrides[drawerRow.sku] ?? drawerRow.price) * (1 - drawerRow.spp / 100) * 0.97))}</b></div>
                      <div className="calc-row"><span>Новая маржа</span><b className={drawerRow.margin > 0 ? 'mg-high' : 'mg-low'}>{drawerRow.margin > 0 ? '+' : ''}{drawerRow.margin}% · {drawerRow.marginRub > 0 ? '+' : ''}{drawerRow.marginRub} ₽</b></div>
                      <button className="detail-calc-toggle" type="button" onClick={() => setDrawerDetailCalcOpen((value) => !value)}>› Подробный расчёт</button>
                      {drawerDetailCalcOpen && (
                        <table className="detail-calc-table">
                          <thead><tr><th>Показатель</th><th>Текущая</th><th>При новой цене</th></tr></thead>
                          <tbody>
                            <tr><td>Цена до СПП</td><td>{rub(drawerRow.price)}</td><td>{rub(priceOverrides[drawerRow.sku] ?? drawerRow.price)}</td></tr>
                            <tr><td>Комиссия WB</td><td>{drawerRow.commission}%</td><td>{drawerRow.commission}%</td></tr>
                            <tr><td>Логистика</td><td>53 ₽</td><td>53 ₽</td></tr>
                            <tr><td>Чистая маржа</td><td>{drawerRow.marginRub} ₽</td><td>{drawerRow.marginRub + ((priceOverrides[drawerRow.sku] ?? drawerRow.price) - drawerRow.price)} ₽</td></tr>
                          </tbody>
                        </table>
                      )}
                      <div className="calc-foot"><button className="btn btn-default btn-sm" type="button" onClick={() => setPriceOverrides((current) => { const next = { ...current }; delete next[drawerRow.sku]; return next })}>Сбросить</button><button className="btn btn-primary btn-sm" type="button" onClick={() => applyDrawerChanges(drawerRow)}>Применить</button></div>
                    </div>
                  </section>
                  <section className="d-section">
                    <div className="d-section-title">Анализ маржи при акции <button className="help-btn" type="button" onClick={() => notify('Анализ показывает, пробьёт ли скидка WB минимальную маржу')}>?</button></div>
                    <div className="calc-block">
                      <div className="promo-calc-input-row"><span>Скидка WB</span><input className="s-input" type="number" min="1" max="90" value={drawerDiscount} onChange={(event) => setDrawerDiscount(event.target.value)} /><span>%</span></div>
                      <div className={`margin-analysis-card ${Number(drawerDiscount) > 35 ? 'warn' : 'ok'}`}>
                        <b>{Number(drawerDiscount) > 35 ? 'Требуется подтверждение' : 'Маржа сохранится'}</b>
                        <span>После акции цена покупателя: {rub(Math.round((priceOverrides[drawerRow.sku] ?? drawerRow.price) * (1 - Number(drawerDiscount || 0) / 100)))} · P_min {drawerPmin} ₽</span>
                      </div>
                    </div>
                  </section>
                  <section className="chart-card">
                    <div className="chart-head"><div className="chart-title">Корзины за период</div><div className="chart-legend">X: дни · Y: корзины · Среднее: 24</div></div>
                    <svg className="chart-svg" viewBox="0 0 320 80" preserveAspectRatio="none"><path d="M0,55 L40,52 L80,48 L120,40 L160,42 L200,30 L240,32 L280,25 L320,15 L320,80 L0,80 Z" fill="rgba(59,130,246,.14)" /><polyline points="0,55 40,52 80,48 120,40 160,42 200,30 240,32 280,25 320,15" fill="none" stroke="#3B82F6" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /><line x1="0" y1="40" x2="320" y2="40" stroke="#E5E7EB" strokeWidth="1" strokeDasharray="3,3" /><circle cx="320" cy="15" r="3" fill="#3B82F6" /></svg>
                  </section>
                  <section className="chart-card">
                    <div className="chart-head"><div className="chart-title">Цена за 30 дней</div><div className="chart-legend">Min: {rub(drawerRow.price - 170)} · Max: {rub(drawerRow.price + 60)}</div></div>
                    <svg className="chart-svg" viewBox="0 0 320 80" preserveAspectRatio="none"><polyline points="0,50 30,48 60,52 90,40 120,38 150,45 180,30 210,25 240,28 270,20 300,18 320,12" fill="none" stroke="#10B981" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" /><line x1="0" y1="65" x2="320" y2="65" stroke="#FCA5A5" strokeWidth="1" strokeDasharray="3,3" /><circle cx="320" cy="12" r="3" fill="#10B981" /></svg>
                  </section>
                  <section className="d-section">
                    <div className="d-section-title">Эффект после изменения цены</div>
                    <div className="impact-grid"><div className="impact-cell"><span>Через 24 часа</span><b>корзины +14%</b><em>38 против 33 до изменения</em></div><div className="impact-cell"><span>Через 48 часов</span><b>заказы +9%</b><em>CR без просадки</em></div><div className="impact-cell"><span>Через 72 часа</span><b>маржа +2.1 пп</b><em>чистая +7 905 ₽</em></div></div>
                  </section>
                  <section className="d-section">
                    <div className="d-section-title">История цены (14 дней)</div>
                    <div className="history-list">
                      {[
                        ['сегодня', '+7.7%', `${rub(drawerRow.price - 65)} → ${rub(drawerRow.price)}`, 'корзины ↑ 38'],
                        ['24.04', '+3.2%', `${rub(drawerRow.price - 92)} → ${rub(drawerRow.price - 65)}`, 'корзины ↑ 32'],
                        ['23.04', '±0%', rub(drawerRow.price - 92), 'ночная медиана'],
                        ['22.04', '+2.5%', `${rub(drawerRow.price - 120)} → ${rub(drawerRow.price - 92)}`, 'алгоритм'],
                        ['21.04', '-2.4%', `${rub(drawerRow.price - 92)} → ${rub(drawerRow.price - 120)}`, 'корзины ↓'],
                      ].map(([time, delta, text, trigger]) => <div className="history-row" key={`${drawerRow.sku}-${time}`}><span className="h-time">{time}</span><span className={delta.startsWith('-') ? 'h-arrow dn' : delta.startsWith('±') ? 'h-arrow same' : 'h-arrow up'}>{delta}</span><span className="h-text">{text}</span><span className="h-trigger">{trigger}</span></div>)}
                    </div>
                  </section>
                </>
              )}
              {drawerTab === 'rules' && (
                <div className="drawer-rule-list">
                  <section className="d-section">
                    <div className="d-section-title">Почему алгоритм принял решение</div>
                    <div className="decision-list">
                      <div className="decision-item"><span className="decision-num">1</span><span>Корзины выше среднего прошлого периода</span><b>{drawerRow.baskets} / 29</b></div>
                      <div className="decision-item"><span className="decision-num">2</span><span>Маржа выше минимального порога стратегии</span><b>{drawerRow.margin}% / 20%</b></div>
                      <div className="decision-item"><span className="decision-num">3</span><span>Остаток WB позволяет удержать спрос без OOS</span><b>{drawerRow.stock} шт</b></div>
                      <div className="decision-item"><span className="decision-num">4</span><span>Новый шаг цены не пробивает P_min и суточный лимит</span><b>+5.8%</b></div>
                    </div>
                  </section>
                  <section className="d-section">
                    <div className="d-section-title">Применённая стратегия</div>
                    <div className="strategy-card">
                      <div><b>{drawerRow.strategy || 'Агрессивный'}</b><span>Маржа 20% · Шаг 6% каждые 1ч · корзины за период ≥ 25</span></div>
                      <button className="btn btn-default btn-sm" type="button" onClick={() => notify('Открыт выбор стратегии для SKU')}>Сменить</button>
                    </div>
                  </section>
                  <section className="d-section">
                    <div className="d-section-title">Override для этого SKU <button className="help-btn" type="button" onClick={() => notify('Override перебивает настройки стратегии только для выбранного SKU')}>?</button></div>
                    <div className="s-row"><div><div className="s-label">Целевая маржа</div><div className="s-desc">Перебивает значение из стратегии</div></div><div className="s-right"><input className="s-input" value={drawerTargetMargin} onChange={(event) => setDrawerTargetMargin(event.target.value)} /><span className="s-unit">%</span></div></div>
                    <div className="s-row"><div><div className="s-label">Шаг изменения</div><div className="s-desc">Максимальное изменение цены за цикл</div></div><div className="s-right"><input className="s-input" defaultValue="6" /><span className="s-unit">%</span></div></div>
                    <div className="s-row"><div><div className="s-label">Время шага</div><div className="s-desc">Как часто алгоритм может применять следующий шаг</div></div><select className="s-select" defaultValue="1"><option value="1">каждые 1 ч</option><option value="3">каждые 3 ч</option><option value="6">каждые 6 ч</option><option value="12">каждые 12 ч</option><option value="24">каждые 24 ч</option></select></div>
                    <div className="s-row stacked"><div><div className="s-label">P_min</div><div className="s-desc">Минимальная цена для алгоритма и защиты акции WB</div></div><div className="s-right"><input className="s-input" value={drawerPmin} onChange={(event) => setDrawerPmin(event.target.value)} /><span className="s-unit">₽</span></div>{Number(drawerPmin) < 657 && <div className="pmin-input-warn">P_min ниже точки безубыточности: 657 ₽</div>}</div>
                    <div className="s-row"><div><div className="s-label">P_max</div><div className="s-desc">Не повышать выше этого значения</div></div><div className="s-right"><input className="s-input" value={drawerPmax} onChange={(event) => setDrawerPmax(event.target.value)} /><span className="s-unit">₽</span></div></div>
                    <div className="s-row"><div><div className="s-label">РРЦ</div><div className="s-desc">Рекомендованная розничная цена бренда</div></div><div className="s-right"><input className="s-input" value={drawerRrc} onChange={(event) => setDrawerRrc(event.target.value)} /><span className="s-unit">₽</span></div></div>
                    <div className="s-row"><div><div className="s-label">Ночная медиана</div><div className="s-desc">Применять для этого SKU</div></div><label className="toggle"><input type="checkbox" checked={drawerNightMedian} onChange={(event) => setDrawerNightMedian(event.target.checked)} /><span className="toggle-track" /><span className="toggle-thumb" /></label></div>
                  </section>
                  <section className="d-section">
                    <div className="d-section-title">Себестоимость и расчёт <button className="help-btn" type="button" onClick={() => notify('Себестоимость берётся из карточки товара и влияет на P_min')}>?</button></div>
                    <div className="d-row"><span className="d-label">Себестоимость бланка</span><span className="d-val">380 ₽</span></div>
                    <div className="d-row"><span className="d-label">DTF-печать</span><span className="d-val">75 ₽</span></div>
                    <div className="d-row"><span className="d-label">Упаковка</span><span className="d-val">12 ₽</span></div>
                    <div className="d-row"><span className="d-label">Итого себестоимость</span><span className="d-val"><b>467 ₽</b></span></div>
                    <div className="d-row"><span className="d-label">Комиссия WB FBS</span><span className="d-val">{Math.round((priceOverrides[drawerRow.sku] ?? drawerRow.price) * drawerRow.commission / 100)} ₽ ({drawerRow.commission}%)</span></div>
                    <div className="d-row"><span className="d-label">Логистика</span><span className="d-val">53 ₽</span></div>
                    <div className="d-row"><span className="d-label">Маржа в ₽</span><span className="d-val mg-high">+{drawerRow.marginRub} ₽</span></div>
                  </section>
                  <section className="d-section">
                    <div className="d-section-title">Стратегия акций WB <button className="help-btn" type="button" onClick={() => notify('WB автоакции управляются через защиту min_price и заранее поднятую цену')}>?</button></div>
                    <div className="s-row"><div><div className="s-label">Повышать цену перед акциями</div><div className="s-desc">Компенсирует WB-скидку и защищает маржу</div></div><label className="toggle"><input type="checkbox" checked={drawerPromoBoost} onChange={(event) => setDrawerPromoBoost(event.target.checked)} /><span className="toggle-track" /><span className="toggle-thumb" /></label></div>
                    {drawerPromoBoost && <><div className="s-row"><div><div className="s-label">Уровень повышения</div><div className="s-desc">% от базовой цены</div></div><div className="s-right"><input className="s-input" defaultValue="25" /><span className="s-unit">%</span></div></div><div className="s-row"><div><div className="s-label">Начинать за</div><div className="s-desc">Плавный подъём цены до акции</div></div><div className="s-right"><input className="s-input" defaultValue="48" /><span className="s-unit">ч</span></div></div></>}
                  </section>
                </div>
              )}
              {drawerTab === 'misc' && (
                <div className="drawer-rule-list">
                  <div className="misc-subtabs">
                    <button className={`misc-stab ${drawerMiscTab === 'orders' ? 'active' : ''}`} type="button" onClick={() => setDrawerMiscTab('orders')}>Заказы</button>
                    <button className={`misc-stab ${drawerMiscTab === 'logs' ? 'active' : ''}`} type="button" onClick={() => setDrawerMiscTab('logs')}>Логи</button>
                  </div>
                  {drawerMiscTab === 'orders' ? (
                    <div className="orders-dashboard">
                      {[
                        ['Заказы сегодня', `${drawerRow.orders}`, '+12% к вчера'],
                        ['В работе', '8', 'до SLA 120 ч'],
                        ['На складе WB', `${drawerRow.stock} шт`, '33 склада'],
                        ['Возвраты', '3', 'выкуп ' + drawerRow.buyout + '%'],
                        ['Средний чек', rub(drawerRow.avgPrice), 'с СПП'],
                        ['Реклама', '1 056 ₽', 'ДРР 2.5%'],
                      ].map(([label, value, hint]) => <div className="orders-card" key={label}><span>{label}</span><b>{value}</b><em>{hint}</em></div>)}
                    </div>
                  ) : (
                    <div className="audit-dashboard">
                      <div className="audit-row"><span className="audit-dot price" /><div><b>Цена рассчитана</b><p>Мария Дудина · сегодня 14:32</p></div></div>
                      <div className="audit-row"><span className="audit-dot" /><div><b>Корзины обновлены</b><p>WB API · сегодня 14:28</p></div></div>
                      <div className="audit-row"><span className="audit-dot" /><div><b>Стратегия применена</b><p>Система · вчера 23:00</p></div></div>
                      <div className="audit-row"><span className="audit-dot price" /><div><b>P_min изменён</b><p>Мария Ф. · 15.04 · индексация себестоимости</p></div></div>
                    </div>
                  )}
                </div>
              )}
              <section className="d-section repricer-comments-section">
                <div className="d-section-title">История комментариев</div>
                <div className="event-list">
                  {commentsForSku(drawerRow.sku).length ? commentsForSku(drawerRow.sku).map((comment) => (
                    <div className="event-row" key={comment.id}>
                      <div className="event-row-title">{comment.author.name} · {shortDateTime(comment.createdAt)}</div>
                      <div className="event-row-meta">{comment.text}</div>
                    </div>
                  )) : (
                    <div className="event-row">
                      <div className="event-row-title">Комментариев пока нет</div>
                      <div className="event-row-meta">Добавьте заметку менеджера по этой позиции.</div>
                    </div>
                  )}
                </div>
                <div className="report-comment-form">
                  <textarea
                    className="report-comment-input"
                    value={commentDrafts[drawerRow.sku] ?? ''}
                    placeholder="Добавить комментарий менеджера..."
                    onChange={(event) => setCommentDrafts((current) => ({ ...current, [drawerRow.sku]: event.target.value }))}
                  />
                  <button className="btn btn-primary btn-sm" type="button" onClick={() => {
                    if (addSkuComment(drawerRow, commentDrafts[drawerRow.sku] ?? '')) {
                      setCommentDrafts((current) => ({ ...current, [drawerRow.sku]: '' }))
                      notify(`Комментарий ${drawerRow.sku} сохранён`)
                    }
                  }}>Добавить комментарий</button>
                </div>
              </section>
              <section className="d-section repricer-audit-section">
                <div className="d-section-title">Журнал действий</div>
                <div className="audit-dashboard">
                  {auditForSku(drawerRow.sku).map((event) => (
                    <div className="audit-row" key={event.id}>
                      <span className={`audit-dot ${event.source === 'system' ? '' : 'price'}`} />
                      <div>
                        <b>{event.action}</b>
                        <p>{event.actor.name} · {shortDateTime(event.createdAt)}{event.reason ? ` · ${event.reason}` : ''}</p>
                        {(event.oldValue || event.newValue) && <p>{event.oldValue ?? '—'} → {event.newValue ?? '—'}</p>}
                      </div>
                    </div>
                  ))}
                </div>
              </section>
              <section className="d-section repricer-action-reason">
                <div className="d-section-title">Причина изменения</div>
                <textarea
                  className="report-comment-input"
                  value={drawerActionReason}
                  placeholder="Обязательная причина для ручных изменений цены, правил и override..."
                  onChange={(event) => setDrawerActionReason(event.target.value)}
                />
              </section>
              <div className="d-actions-fixed">
                <button className="btn btn-default" type="button" onClick={() => setDrawerRow(null)}>Закрыть</button>
                <button className="btn btn-primary" type="button" onClick={() => applyDrawerChanges(drawerRow)}>Применить изменения</button>
              </div>
            </div>
          </>
        )}
      </aside>

      <div className={`modal-overlay ${openModal ? 'open' : ''}`} aria-hidden={!openModal}>
        {openModal === 'night' && (
          <div className="modal lg">
            <div className="modal-head"><div className="modal-icon brand"><Icon name="moon" size={22} /></div><h2>Ночная медиана</h2><p>Режим учитывает корзины и цены за 23:00–04:00, чтобы не реагировать на дневные шумы.</p></div>
            <div className="modal-body"><div className="seg-ctl">{['Авто', 'Только ночь', 'Отключить'].map((mode, index) => <button className={`seg-btn ${index === 0 ? 'active' : ''}`} type="button" key={mode} onClick={() => notify(`Режим ночной медианы: ${mode}`)}>{mode}</button>)}</div><div className="modal-summary"><div><span>SKU под режимом</span><b>36</b></div><div><span>Следующий пересчёт</span><b>23:00</b></div><div><span>Лимит шага</span><b>5%</b></div></div></div>
            <div className="modal-foot"><button className="btn btn-ghost" type="button" onClick={() => setOpenModal(null)}>Отмена</button><button className="btn btn-primary" type="button" onClick={() => setOpenModal(null)}>Сохранить</button></div>
          </div>
        )}
        {openModal === 'apply' && (
          <div className="modal lg">
            <div className="modal-head"><div className="modal-icon brand"><Icon name="download" size={22} /></div><h2>Применить новые цены к 47 SKU?</h2><p>Изменения отправятся в WB API одной транзакцией. Откатить можно через кнопку «Отменить» в течение 5 минут.</p></div>
            <div className="modal-body"><div className="modal-summary"><div><span>Повышение цены</span><b className="metric-up">31 SKU</b></div><div><span>Снижение цены</span><b className="metric-down">14 SKU</b></div><div><span>Перевод в ручной режим</span><b>2 SKU</b></div><div><span>Среднее изменение</span><b>+3.2%</b></div></div></div>
            <div className="modal-foot"><button className="btn btn-ghost" type="button" onClick={() => setOpenModal(null)}>Отмена</button><button className="btn btn-default" type="button" onClick={() => notify('Открыт список изменений цен')}>Просмотреть изменения</button><button className="btn btn-primary" type="button" onClick={() => showModal('applyProgress')}>Применить 47 цен</button></div>
          </div>
        )}
        {openModal === 'applyProgress' && (
          <div className="modal lg">
            <div className="modal-head">
              <div className="modal-icon brand">↻</div>
              <h2>Применение цен → WB API</h2>
              <p>Не закрывайте окно. Можно скрыть в фон — процесс продолжится.</p>
            </div>
            <div className="modal-body">
              <div className="progress-meta"><span>Применено: <b>12</b> из <b>47</b></span><span>26%</span></div>
              <div className="progress-bar"><div className="progress-bar-fill" style={{ width: '26%' }} /></div>
              <div className="progress-current"><span className="sku">FBCT_22</span> — отправка в WB API…</div>
              <div className="live-feed">
                {[
                  ['ok', 'FBBT_42', '847 ₽', '912 ₽', 'OK'],
                  ['ok', 'HCBT_17', '1 850 ₽', '1 890 ₽', 'OK'],
                  ['ok', 'FBCT_08', '850 ₽', '780 ₽', 'OK'],
                  ['err', 'LBBT_33', '', '', 'Rate limit — повтор через 30с'],
                  ['ok', 'FBBT_29', '620 ₽', '590 ₽', 'OK'],
                  ['ok', 'HBBT_11', '1 450 ₽', '1 490 ₽', 'OK'],
                  ['ok', 'LBCT_07', '980 ₽', '1 020 ₽', 'OK'],
                  ['ok', 'FBBT_18', '720 ₽', '740 ₽', 'OK'],
                ].map(([kind, sku, from, to, msg]) => (
                  <div className={`live-event ${kind}`} key={`${sku}-${msg}`}>
                    <span className="ico">{kind === 'ok' ? '✓' : '!'}</span>
                    <span className="sku">{sku}</span>
                    {from && <><span className="arrow">→</span><span>{from}</span><span className="arrow">→</span><span className="new">{to}</span></>}
                    <span className="msg">{msg}</span>
                  </div>
                ))}
              </div>
            </div>
            <div className="modal-foot"><button className="btn btn-ghost" type="button" onClick={() => { setOpenModal(null); notify('Применение продолжается в фоне') }}>Скрыть в фоне</button><button className="btn btn-danger" type="button" onClick={() => { setOpenModal(null); notify('Применение цен отменено') }}>Отменить применение</button></div>
          </div>
        )}
        {openModal === 'problemQueue' && (
          <div className="modal xl">
            <div className="modal-head"><div className="modal-icon warn">!</div><h2>SKU ниже порогов</h2><p>SKU, где цена ниже P_min, маржа ниже порога или нет данных для автоматического действия.</p></div>
            <div className="modal-body"><div className="problem-modal-summary"><div><span>Ниже P_min</span><b>2</b></div><div><span>Маржа &lt; 0</span><b>1</b></div><div><span>Нет данных</span><b>3</b></div><div><span>Ликвидация</span><b>8</b></div></div><table className="mini-modal-table"><tbody>{rows.slice(0, 5).map((row) => <tr key={row.sku}><td className="sku">{row.sku}</td><td>{row.name}</td><td className={row.margin < 0 ? 'metric-down' : 'metric-up'}>{row.margin > 0 ? '+' : ''}{row.margin}%</td><td><button className="btn btn-default btn-sm" type="button" onClick={() => { setOpenModal(null); openDrawer(row) }}>Открыть</button></td></tr>)}</tbody></table></div>
            <div className="modal-foot"><button className="btn btn-ghost" type="button" onClick={() => setOpenModal(null)}>Закрыть</button><button className="btn btn-primary" type="button" onClick={() => notify('Список SKU ниже порогов открыт')}>Открыть список</button></div>
          </div>
        )}
        {openModal === 'bulkPmin' && (
          <div className="modal">
            <div className="modal-head"><div className="modal-icon brand">—</div><h2>Установить P_min для {selectedCount} SKU</h2><p>Минимальная цена будет применена ко всем выделенным товарам.</p></div>
            <div className="modal-body"><label className="field-label">Новое значение P_min</label><div className="modal-input-row"><input className="modal-input" value="800" readOnly /><select className="modal-select" defaultValue="₽ (фикс)"><option>₽ (фикс)</option></select></div><label className="modal-check"><input type="checkbox" defaultChecked />Включить защиту от автоакций WB</label><label className="field-label">Причина массового действия</label><textarea className="report-comment-input" value={bulkReason} placeholder="Например: индексация себестоимости партии" onChange={(event) => setBulkReason(event.target.value)} /></div>
            <div className="modal-foot"><button className="btn btn-ghost" type="button" onClick={() => setOpenModal(null)}>Отмена</button><button className="btn btn-primary" type="button" onClick={commitBulkAction}>Применить</button></div>
          </div>
        )}
        {openModal === 'bulkLiq' && (
          <div className="modal lg">
            <div className="modal-head"><div className="modal-icon danger">!</div><h2>Перевести {selectedCount} SKU в ликвидацию?</h2><p>Цена будет снижаться плавно. SKU с отрицательной маржой требуют отдельного подтверждения.</p></div>
            <div className="modal-body"><div className="modal-summary"><div><span>Шаг снижения</span><b>5%</b></div><div><span>Периодичность</span><b>24 часа</b></div><div><span>Стоп-цена</span><b>P_min</b></div></div><label className="liq-acceptance"><input type="checkbox" defaultChecked /><span>Подтверждаю риск снижения маржи для выбранных SKU.</span></label><label className="field-label">Причина массового действия</label><textarea className="report-comment-input" value={bulkReason} placeholder="Например: слабый спрос и высокий остаток" onChange={(event) => setBulkReason(event.target.value)} /></div>
            <div className="modal-foot"><button className="btn btn-ghost" type="button" onClick={() => setOpenModal(null)}>Отмена</button><button className="btn btn-primary danger" type="button" onClick={commitBulkAction}>Запустить ликвидацию</button></div>
          </div>
        )}
        {openModal === 'bulkAction' && bulkActionDraft && (
          <div className="modal">
            <div className="modal-head"><div className="modal-icon brand">i</div><h2>{bulkActionDraft.title}</h2><p>{selectedCount} SKU получат одно массовое действие с общей причиной.</p></div>
            <div className="modal-body"><div className="modal-summary"><div><span>Действие</span><b>{bulkActionDraft.newValue}</b></div><div><span>SKU</span><b>{selectedCount}</b></div></div><label className="field-label">Причина массового действия</label><textarea className="report-comment-input" value={bulkReason} placeholder="Обязательная причина для audit-log" onChange={(event) => setBulkReason(event.target.value)} /></div>
            <div className="modal-foot"><button className="btn btn-ghost" type="button" onClick={() => setOpenModal(null)}>Отмена</button><button className="btn btn-primary" type="button" onClick={commitBulkAction}>Применить</button></div>
          </div>
        )}
      </div>

      <div className="toast-container">
        {toastOpen && (
          <div className="toast info show">
            <div className="toast-icon">i</div>
            <div className="toast-body"><div className="toast-title">{toastMessage}</div></div>
            <button className="toast-undo" type="button" onClick={() => setToastOpen(false)}>↶ Отменить</button>
          </div>
        )}
      </div>
    </div>
  )
}
