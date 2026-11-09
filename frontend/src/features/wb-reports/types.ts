export type ReportSourceType = 'operational' | 'financial'
export type FreshnessState = 'fresh' | 'partial' | 'pending_financial' | 'stale'
export type AlertSeverity = 'critical' | 'warning' | 'info'
export type KpiTone = 'good' | 'bad' | 'neutral' | 'warning'
export type ReportId = 'digest' | 'abc' | 'rnp' | 'pnl' | 'expenses' | 'ads' | 'stock' | 'week-over-week'
export type DatePreset = '1d' | '7d' | '14d' | '30d' | 'custom'
export type ReportGroupBy = 'sku' | 'manager' | 'brand' | 'category' | 'status' | 'warehouse'
export type ProductStatus = 'локомотив' | 'новинка' | 'неликвид' | 'средний' | 'хвост' | 'ликвидация'
export type PromotionStatus = 'yes' | 'no'
export type AbcLetter = 'A' | 'B' | 'C'
export type AbcCode = `${AbcLetter}${AbcLetter}`
export type ColumnVisibility = Record<string, boolean>
export type ManagerAssignmentSource = 'manual' | 'xlsx' | 'none'
export type ThresholdPreset = 'standard' | 'conservative' | 'aggressive'
export type ThresholdProfileStatus = 'active' | 'draft'
export type ThresholdAutomationAction =
  | 'raise_price'
  | 'lower_price'
  | 'rnp'
  | 'liquidation'
  | 'stop_ads'
  | 'alert'
  | 'audit'

export interface ThresholdShare {
  aPct: number
  bPct: number
  cPct: number
}

export interface ThresholdBand {
  goodMin?: number
  averageMin?: number
  thinMin?: number
  lossBelow?: number
  goodMax?: number
  warnMin?: number
  warnBelow?: number
  criticalBelow?: number
  badBelow?: number
}

export interface ThresholdProfile {
  id: string
  name: string
  preset: ThresholdPreset
  version: number
  status: ThresholdProfileStatus
  updatedAt: string
  updatedBy: {
    id: string
    name: string
    role: 'owner' | 'admin' | 'manager' | 'system'
  }
  abc: {
    salesShare: ThresholdShare
    netProfitShare: ThresholdShare
  }
  qualityBands: {
    ctrPct: ThresholdBand
    crPct: ThresholdBand
    cartToOrderPct: ThresholdBand
    buyoutPct: ThresholdBand
    marginPct: ThresholdBand
    drrPct: ThresholdBand
    roiPct: ThresholdBand
    daysToOos: ThresholdBand
    stockUnits: ThresholdBand
    localizationPct: ThresholdBand
  }
  automationMapping: Record<string, ThresholdAutomationAction>
}

export interface ThresholdPreviewRow {
  sku: string
  before: {
    abcCode: AbcCode
    status: string
    action: string
  }
  after: {
    abcCode: AbcCode
    status: string
    action: string
  }
  reason: string
}

export interface ThresholdPreview {
  affectedSkuCount: number
  abcChanges: number
  statusChanges: number
  automationImpact: {
    rnp: number
    liquidation: number
    alerts: number
    stopAds: number
  }
  sampleRows: ThresholdPreviewRow[]
  warnings: string[]
}

export interface DateRange {
  preset: DatePreset
  from: string
  to: string
}

export interface ReportMeta {
  id: ReportId
  title: string
  description: string
  sourceType: ReportSourceType
  freshnessState: FreshnessState
  lastUpdatedAt: string
}

export interface MetricDelta {
  value: number
  label: string
  tone: KpiTone
}

export interface ReportKpi {
  id: string
  label: string
  value: string
  hint?: string
  delta?: MetricDelta
}

export interface DigestAffectedItem {
  sku: string
  nmId: number | null
  warehouseName: string
  availableUnits: number | null
  wbStockUnits: number | null
  fromClientUnits: number | null
  toClientUnits: number | null
  reason: string
}

export interface FreshnessItem {
  id: string
  label: string
  sourceType: ReportSourceType
  state: FreshnessState
  updatedAt: string
  message: string
}

export interface ReportAlert {
  id: string
  alertType: string
  severity: AlertSeverity
  entityType: 'sku' | 'report' | 'warehouse'
  entityId: string
  title: string
  details: string
  createdAt: string
  resolvedAt: string | null
  route?: string
}

export interface ChartPoint {
  label: string
  value: number
  compareValue?: number
  date?: string
  ordersKopecks?: number
  salesKopecks?: number
  returnsUnits?: number
  buyoutPct?: number | null
}

export interface ReportChartData {
  title: string
  valueLabel: string
  compareLabel?: string
  xAxisLabel?: string
  yAxisLabel?: string
  points: ChartPoint[]
  comparisonPoints?: ChartPoint[]
}

export type TableColumnKind = 'text' | 'number' | 'currency' | 'percent' | 'image' | 'wb-link' | 'abc' | 'promotion'

export interface TableColumn {
  key: string
  label: string
  align?: 'left' | 'right' | 'center'
  format?: TableColumnKind
  sticky?: boolean
  defaultVisible?: boolean
}

export interface CompositeMetricValue {
  units?: number
  unitsLabel?: string
  kopecks?: number
  percent?: number
  deltaPct?: number
  deltaLabel?: string
}

export interface PlanFactRow {
  owner: 'company' | 'manager'
  ownerId: string
  name: string
  planKopecks: number | null
  factKopecks: number | null
  completionPct: number | null
  forecastKopecks: number | null
  deltaPct: number | null
  needPerDayKopecks: number | null
  status: 'ok' | 'watch' | 'risk' | 'no_plan' | 'no_fact' | 'unallocated_costs'
  factFreshness: 'final' | 'preliminary' | 'stale'
  unallocatedCostKopecks?: number
  revenuePlanKopecks?: number | null
  revenueFactKopecks?: number | null
  metricLabel?: 'Выручка' | 'Маржа'
}

export interface AdsCampaignRow {
  campaignId: string
  campaignName: string
  campaignType: 'search' | 'catalog' | 'shelf' | 'media'
  attributionLevel: 'exact_sku' | 'campaign_sku' | 'campaign_only'
  attributionSource: 'wb_ads_nm' | 'campaign_product_list' | 'campaign_name_match' | 'manual_mapping' | 'unknown'
  attributionConfidencePct: number
  photoUrl: string
  productName: string
  category: string
  managerId?: string | null
  manager: string
  sku: string
  nmId: number
  observationDays: number
  impressions: number
  clicks: number
  ctrPct: number
  baskets: number
  orders: CompositeMetricValue
  sales: CompositeMetricValue
  adSpendKopecks: number
  drrPct: number
  minSpendMet: boolean
  hasOosInPeriod: boolean
  isNewSku: boolean
  isPromoOrLiquidation: boolean
  recommendation: 'keep' | 'draft_stop' | 'review'
  recommendationStatus: 'confirmed' | 'draft'
  recommendationReason: string
}

export interface StockWarehouseRow {
  sku: string
  nmId: number
  photoUrl: string
  productName: string
  warehouseName: string
  clusterName: string
  wbStockUnits: number
  fromClientUnits: number
  toClientUnits: number
  availableUnits: number
  ordersPerDay: number
  daysToOos: number
  ktrIndex: number
  localizationPct: number
  salesDistributionCoefficient: number
  logisticsPerUnitKopecks: number
  decision: 'норма' | 'держать' | 'дозагрузить' | 'draft'
  decisionStatus: 'confirmed' | 'draft'
}

export interface StockDailySnapshot {
  snapshotDate: string
  sku: string
  nmId: number
  warehouseName: string
  clusterName: string
  wbStockUnits: number
  fromClientUnits: number
  toClientUnits: number
  availableUnits: number
  wasOutOfStock: boolean
  source: 'wb_stocks_report'
  sourceUpdatedAt: string
}

export interface WeekOverWeekRow {
  sku: string
  photoUrl: string
  productName: string
  productStatus: ProductStatus
  abcCode: AbcCode
  orders: CompositeMetricValue
  sales: CompositeMetricValue
  baskets: CompositeMetricValue
  marginPct: CompositeMetricValue
  profit: CompositeMetricValue
  wasOutOfStock: boolean
  stockAvailability7d: boolean[]
  stockOutDays: number
  stockSnapshotCoveragePct: number
  stockSnapshotSource: string
  conclusion: string
}

export interface SkuComment {
  id: string
  sku: string
  author: string
  createdAt: string
  text: string
}

export interface ReportAuditEvent {
  id: string
  sku?: string
  actor: string
  actorType: 'system' | 'manager'
  createdAt: string
  text: string
}

export type FinancialConfirmationStatus = 'operational' | 'pending_financial' | 'final_financial'

export type TableValue =
  | string
  | number
  | boolean
  | CompositeMetricValue
  | boolean[]
  | DigestAffectedItem[]
  | null
  | undefined
export type TableRow = Record<string, TableValue>
export type DigestProblemRow = TableRow

export interface SkuAnalyticsSummary {
  abcCode: AbcCode
  productStatus: ProductStatus
  promotionStatus: PromotionStatus
  wbStockUnits: number
  buyoutPct: number
  baskets: number
  ordersUnits: number
  avgPriceWithSppKopecks: number
  marginPct: number
  marginKopecks: number
  wbCommissionPct: number
}

export interface SkuReportRow extends TableRow {
  sku: string
  nmId: number
  photoUrl: string
  productStatus: ProductStatus
  managerId?: string | null
  manager: string
  assignmentSource?: ManagerAssignmentSource
  assignedAt?: string | null
  brand: string
  category: string
  priceBeforeSppKopecks: number
  priceWithSppKopecks: number
  cogsKopecks?: number
  marginPct: number
  marginKopecks: number
  marginDeltaPct: number
  impressions: number
  clicks: number
  ctrPct: number
  baskets: number
  cartCrPct: number
  basketsDeltaPct: number
  ordersUnits: number
  ordersDeltaPct: number
  ordersKopecks: number
  salesUnits: number
  salesDeltaPct: number
  salesKopecks: number
  adSpendKopecks: number
  drrOrdersPct: number
  drrSalesPct: number
  netPerUnitKopecks: number
  netTotalKopecks: number
  logisticsCostPct: number
  logisticsDeltaPct: number
  commissionCostPct: number
  commissionDeltaPct: number
  storageCostPct: number
  storageDeltaPct: number
  wbStockUnits: number
  wbStockKopecks: number
  promotionStatus: PromotionStatus
  abcCode: AbcCode
  buyoutPct: number
}

export interface ReportResponse {
  meta: ReportMeta
  headline: string
  warning?: string
  financialConfirmationStatus?: FinancialConfirmationStatus
  comments?: SkuComment[]
  auditEvents?: ReportAuditEvent[]
  filters: {
    dateRange: DateRange
    groupBy?: ReportGroupBy
  }
  kpis: ReportKpi[]
  chart: ReportChartData
  columns: TableColumn[]
  rows: TableRow[]
}

export interface DigestResponse {
  availableBrands?: string[]
  brandScope?: string[]
  topMovers?: {
    status: 'ready' | 'partial' | 'unavailable'
    dateFrom: string; dateTo: string; comparisonFrom: string; comparisonTo: string
    rows: Array<{ nmId: number; sku: string; title: string; salesDelta: number | null; ordersDelta: number }>
  }
  meta: ReportMeta
  headline: string
  dateRange: DateRange
  kpis: ReportKpi[]
  planFactRows: PlanFactRow[]
  freshness: FreshnessItem[]
  alerts: ReportAlert[]
  charts: ReportChartData[]
  weeklyBalance?: ReportChartData
  periodCards?: Array<{
    id: string
    label: string
    openCount: number
    cartCount: number
    ordersUnits: number
    ordersKopecks: number
    salesUnits: number
    returnsUnits: number
    buyoutPct: number | null
    revenueKopecks: number
  }>
  problemRows: DigestProblemRow[]
  quickLinks: Array<{
    title: string
    description: string
    href: string
  }>
}

export interface ExportResponse {
  fileName: string
  rows: number
  exportAllowed: boolean
  allowedRoles: Array<'finance' | 'admin' | 'ads' | 'owner'>
  blockedReason?: string
  emptySourceNote?: string
}
