/** Filtering only: never derive or replace financial amounts. */
export type PnlFilterProduct = {
  managerId?: string | null; managerName?: string | null; managerKnown?: boolean; brand?: string | null; status?: string | null
  promotion?: boolean | null; garment?: string | null; attrs?: string[]
  strategyId?: string | null; strategyName?: string | null; stockUnits?: number | null
  currentPriceKopecks?: number | null; pminKopecks?: number | null; changed24h?: boolean
  orders?: number | null; baskets?: number | null
}
export type PnlFilterMetadata = {
  marketplaceAccountId: number; dateFrom: string; dateTo: string
  products: Record<string, PnlFilterProduct>
}
export type PnlFilterRow = {
  nmId?: number | string | null; marginPct?: number | null
  articleId?: string | null; sku?: string | null; productName?: string | null
  label?: string | null; category?: string | null; comment?: string | null
}
export const pnlSegments = [['all', 'Все'], ['problem', 'SKU ниже порогов'], ['nopmin', 'Без P_min'],
  ['loss', 'Маржа ниже 0'], ['changes24h', 'Изменения 24ч'], ['illiquid', 'Неликвид']] as const
export const pnlStatuses = [['all', 'Все товары'], ['loko', 'Локомотивы'], ['new', 'Новинки'],
  ['illiquid', 'Неликвид'], ['liq', 'Ликвидация'], ['manual', 'Ручной режим'], ['nopmin', 'Без минимальной цены']] as const
export const pnlTypes = [['shirt', 'Футболка'], ['hoodie', 'Худи'], ['longsleeve', 'Лонгслив'], ['shorts', 'Шорты'], ['balaclava', 'Балаклава']] as const
export const pnlAttrs = [['female', 'Жен.'], ['male', 'Муж.'], ['white', 'Белый'], ['black', 'Чёрный']] as const
export const pnlRanges = [['margin', 'Маржа, %'], ['baskets', 'Корзины за период'], ['orders', 'Заказы за период'], ['price', 'Цена, ₽']] as const
export const pnlStocks = [['all', 'Любой'], ['ending', 'Заканчивается ≤5'], ['low', 'Низкий 6–20'], ['normal', 'Норма 21–100'], ['excess', 'Избыток >100']] as const
export type PnlRangeKey = typeof pnlRanges[number][0]
export type PnlFilters = {
  segment: string; manager: string; status: string; promo: string; brand: string; strategy: string; stock: string
  types: string[]; attrs: string[]; ranges: Record<PnlRangeKey, { from: string; to: string }>
}
export function defaultPnlFilters(): PnlFilters {
  return { segment: 'all', manager: 'all', status: 'all', promo: 'all', brand: 'all', strategy: 'all', stock: 'all',
    types: pnlTypes.map(([key]) => key), attrs: pnlAttrs.map(([key]) => key),
    ranges: { margin: { from: '', to: '' }, baskets: { from: '', to: '' }, orders: { from: '', to: '' }, price: { from: '', to: '' } } }
}
export function pnlActiveFilterCount(filters: PnlFilters) {
  return (['segment', 'manager', 'status', 'promo', 'brand', 'strategy', 'stock'] as const).filter(key => filters[key] !== 'all').length
    + Number(filters.types.length !== pnlTypes.length) + Number(filters.attrs.length !== pnlAttrs.length)
    + pnlRanges.filter(([key]) => Object.values(filters.ranges[key]).some(value => value.trim() !== '')).length
}
export function pnlRangeNumber(raw: string): number | null {
  if (!raw.trim()) return null
  const value = raw.trim().replace(',', '.')
  return /^-?(?:\d+(?:\.\d*)?|\.\d+)$/.test(value) && Number.isFinite(Number(value)) ? Number(value) : NaN
}
export function pnlFilterError(filters: PnlFilters) {
  for (const [key, title] of pnlRanges) {
    const from = pnlRangeNumber(filters.ranges[key].from), to = pnlRangeNumber(filters.ranges[key].to)
    if (Number.isNaN(from) || Number.isNaN(to)) return `${title}: введите корректное число.`
    if (key !== 'margin' && (from != null && from < 0 || to != null && to < 0)) return `${title}: значение не может быть отрицательным.`
    if (from != null && to != null && from > to) return `${title}: «от» не может быть больше «до».`
  }
  return ''
}
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value)
export function createPnlFilterMatcher(filters: PnlFilters, currentManagerId: string) {
  const active = pnlActiveFilterCount(filters)
  const bounds = pnlRanges.map(([key]) => [key, pnlRangeNumber(filters.ranges[key].from), pnlRangeNumber(filters.ranges[key].to)] as const)
  return (row: PnlFilterRow, product: PnlFilterProduct | undefined) => {
  if (!active) return true // Includes the account-level unallocated row unchanged.
  if (!row.nmId) return false // Never assign account-level charges to a product facet.
  const p = product ?? {}
  const noMin = finite(p.pminKopecks) && p.pminKopecks <= 0
  const loss = finite(row.marginPct) && row.marginPct < 0
  const illiquid = p.status === 'illiquid'
  const segment = filters.segment
  if (segment === 'nopmin' && !noMin || segment === 'loss' && !loss || segment === 'changes24h' && !p.changed24h
    || segment === 'illiquid' && !illiquid || segment === 'problem' && !(loss || noMin || finite(p.stockUnits) && p.stockUnits <= 20 || illiquid || ['liq', 'liquidation'].includes(p.status ?? ''))) return false
  const manager = p.managerId || 'unassigned'
  if (filters.manager !== 'all' && p.managerKnown === false) return false
  if (filters.manager !== 'all' && manager !== (filters.manager === 'mine' ? currentManagerId : filters.manager)) return false
  if (filters.status !== 'all' && !(filters.status === 'nopmin' ? noMin
    : filters.status === 'loko' ? ['loko', 'locomotive'].includes(p.status ?? '')
    : filters.status === 'new' ? ['new', 'warmup'].includes(p.status ?? '')
    : filters.status === 'liq' ? ['liq', 'liquidation'].includes(p.status ?? '') : p.status === filters.status)) return false
  if (filters.promo !== 'all' && (p.promotion == null || p.promotion !== (filters.promo === 'yes'))) return false
  if (filters.brand !== 'all' && p.brand !== filters.brand || filters.strategy !== 'all' && p.strategyId !== filters.strategy) return false
  if (filters.types.length !== pnlTypes.length && !filters.types.includes(p.garment ?? '')) return false
  if (filters.attrs.length !== pnlAttrs.length && !p.attrs?.some(attr => filters.attrs.includes(attr))) return false
  const stock = p.stockUnits
  if (filters.stock !== 'all' && (!finite(stock) || filters.stock === 'ending' && stock > 5
    || filters.stock === 'low' && (stock <= 5 || stock > 20) || filters.stock === 'normal' && (stock <= 20 || stock > 100)
    || filters.stock === 'excess' && stock <= 100)) return false
  for (const [key, from, to] of bounds) {
    if (from == null && to == null) continue
    const value = key === 'margin' ? row.marginPct : key === 'price' ? p.currentPriceKopecks : p[key]
    // Money stays in original kopecks; convert bounds, not rounded display text.
    const factor = key === 'price' ? 100 : 1
    if (!finite(value) || from != null && value < from * factor || to != null && value > to * factor) return false
  }
  return true
  }
}
export function matchesPnlFilters(row: PnlFilterRow, product: PnlFilterProduct | undefined, filters: PnlFilters, currentManagerId: string) {
  return createPnlFilterMatcher(filters, currentManagerId)(row, product)
}
export function selectPnlRows<T extends PnlFilterRow>(rows: T[], query: string, filters: PnlFilters, metadata: PnlFilterMetadata | null, currentManagerId: string) {
  const needle = query.trim().toLocaleLowerCase('ru-RU')
  const matches = createPnlFilterMatcher(filters, currentManagerId)
  return rows.filter(row => (!needle || [row.articleId, row.sku, row.productName, row.label, row.category, row.nmId, row.comment]
    .join(' ').toLocaleLowerCase('ru-RU').includes(needle)) && matches(row, metadata?.products[String(row.nmId)]))
}
