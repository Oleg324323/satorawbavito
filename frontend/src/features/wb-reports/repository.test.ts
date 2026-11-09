import { describe, expect, it } from 'vitest'
import { buildReportContractSidecar } from './reportContracts'
import { AdsPerformanceResponseSchema, ExpenseImportCommitResponseSchema, ExpenseImportPreviewResponseSchema, ExpensesReportResponseSchema, PnlReportResponseSchema, RnpReportResponseSchema, SourceStatusResponseSchema } from './schemas'
import {
  ABC_COLUMNS,
  calculateLocalizationPct,
  getAbcReport,
  getAdsReport,
  getDigestReport,
  getExpensesReport,
  getExport,
  getExportForRole,
  getPnlReport,
  getRnpReport,
  getStockDailySnapshots,
  getStockReport,
  getSkuAnalyticsSummary,
  getWeekOverWeekReport,
  localizationCoefficients,
  normalizeDateRange,
} from './repository'

describe('wb reports repository', () => {
  it('builds digest from ABC, P&L, RNP, ads and stock metrics', () => {
    const digest = getDigestReport()
    expect(digest.kpis).toHaveLength(8)
    expect(digest.charts).toHaveLength(2)
    expect(digest.planFactRows.map((row) => row.name)).toEqual(['Компания', 'Котельникова', 'Воробьева', 'Дудина', 'Светлана', 'Ирина'])
    expect(digest.planFactRows[0]).toMatchObject({ owner: 'company', ownerId: 'company', status: 'unallocated_costs' })
    expect(digest.planFactRows.some((row) => row.status === 'no_plan')).toBe(true)
    expect(digest.quickLinks.map((link) => link.href)).toContain('/wb/reports/abc')
    expect(digest.freshness.some((item) => item.state === 'pending_financial')).toBe(true)
  })

  it('builds ABC report with Maria required columns and two-letter code', () => {
    const report = getAbcReport()
    expect(report.meta.id).toBe('abc')
    expect(ABC_COLUMNS.map((column) => column.key)).toEqual(
      expect.arrayContaining(['photoUrl', 'nmId', 'sku', 'abcCode', 'promotionStatus', 'drrSalesPct', 'wbStockUnits']),
    )
    expect(report.rows[0].abcCode).toMatch(/^[ABC]{2}$/)
    expect(report.columns.map((column) => column.key)).toEqual(expect.arrayContaining(['cogsKopecks', 'ordersComposite', 'salesComposite']))
    expect(report.columns.find((column) => column.key === 'cogsKopecks')?.label).toBe('Себестоимость')
    expect(report.rows[0].cogsKopecks).toEqual(expect.any(Number))
    expect(report.rows[0].ordersComposite).toMatchObject({ units: expect.any(Number), kopecks: expect.any(Number), deltaPct: expect.any(Number) })
    expect(report.rows[0].salesComposite).toMatchObject({ units: expect.any(Number), kopecks: expect.any(Number), deltaPct: expect.any(Number) })
  })

  it('validates 19.05 runtime contract sidecars for report repository outputs', () => {
    const range = normalizeDateRange({ preset: '7d' })
    const abc = getAbcReport(range)
    const ads = getAdsReport(range)
    const rnp = getRnpReport(range)
    const pnl = getPnlReport(range, 'financial')

    expect(() => buildReportContractSidecar('abc', abc, { dateRange: range, groupBy: 'sku' })).not.toThrow()
    expect(() => buildReportContractSidecar('ads', ads, { dateRange: range, groupBy: 'campaign' })).not.toThrow()
    expect(() => buildReportContractSidecar('rnp', rnp, { dateRange: range, groupBy: 'sku' })).not.toThrow()
    expect(() => buildReportContractSidecar('pnl', pnl, { dateRange: range, groupBy: 'sku', pnlSource: 'financial' })).not.toThrow()
  })

  it('keeps financial P&L non-final while source blockers are open', () => {
    const range = normalizeDateRange({ preset: '7d' })
    const sidecar = buildReportContractSidecar('pnl', getPnlReport(range, 'financial'), { dateRange: range, groupBy: 'sku', pnlSource: 'financial' })

    expect(sidecar.reportState).not.toBe('final')
    expect(sidecar.blockerIds).toEqual(expect.arrayContaining(['WB-12', 'WB-13', 'WB-23', 'WB-24']))
    expect(sidecar.totals.netProfitKopecks).toBeNull()
    expect(sidecar.totals.marginPct).toBeNull()
    expect(sidecar.rows.every((row) => row.netProfitKopecks === null && row.marginPct === null)).toBe(true)
    expect(PnlReportResponseSchema.safeParse({ ...sidecar, reportState: 'final' }).success).toBe(false)
  })

  it('builds expenses report as a finance-gated P&L input', () => {
    const report = getExpensesReport(normalizeDateRange({ preset: '7d' }))
    expect(report.meta.id).toBe('expenses')
    expect(report.meta.sourceType).toBe('financial')
    expect(report.warning).toContain('налоговая база/ставка')
    expect(report.warning).toContain('драйвера распределения')
    expect(report.columns.map((column) => column.key)).toEqual(expect.arrayContaining(['category', 'amountKopecks', 'sourceLabel', 'allocationBaseLabel', 'allocationCoverageLabel', 'approvalStatusLabel', 'owner', 'pending']))
    expect(report.columns.map((column) => column.key)).not.toContain('blockers')
    expect(report.rows.some((row) => String(row.sourceLabel).includes('1С'))).toBe(true)
    expect(report.rows.some((row) => String(row.approvalStatusLabel).includes('требует маппинга'))).toBe(true)
    expect(report.rows.find((row) => row.id === 'tax-vat')?.amountKopecks).toBeNull()
    expect(report.rows.find((row) => row.id === 'tax-vat')?.approvalStatusLabel).toBe('нужна ставка')
    expect(report.rows.some((row) => row.category === 'Упаковка')).toBe(true)
    expect(report.rows.some((row) => row.allocationCoverageLabel === '100% SKU')).toBe(true)
    expect(getExport('expenses').fileName).toContain('wb-expenses')
    expect(getExportForRole('expenses', 'finance').exportAllowed).toBe(true)
    expect(getExportForRole('expenses', 'manager')).toMatchObject({
      exportAllowed: false,
      blockedReason: expect.stringContaining('финансы или администратор'),
    })
  })

  it('validates expenses and source-status blocked envelopes', () => {
    const now = '2026-06-30T08:15:00.000Z'
    const period = { dateFrom: '2026-05-01', dateTo: '2026-05-19' }
    const evidence = [{ sourceId: 'one-c-finance', sourceType: 'one_c', sourceName: '1С · финансы и УУ', lastSyncedAt: now, freshnessTtlMinutes: 1440, fieldsUsed: ['amount', 'allocation'] }]
    const expenseRow = {
      expenseId: 'expense-operating-overhead',
      category: 'Операционные расходы',
      period,
      amountKopecks: null,
      allocationBase: 'unknown',
      sourceId: 'one-c-finance',
      sourceType: 'one_c',
      approvalStatus: 'blocked',
      ownerId: 'maksim',
      comment: 'Нужно подтверждение',
      blockerIds: ['WB-12', 'WB-13', 'WB-24'],
    }

    expect(ExpensesReportResponseSchema.safeParse({
      sourceStatus: 'blocked',
      confidence: 'blocked',
      blockerIds: ['WB-12', 'WB-13', 'WB-24'],
      sourceEvidence: evidence,
      calculatedAt: now,
      period,
      rows: [expenseRow],
      allowedAllocationBases: ['sku', 'brand', 'manager', 'marketplace', 'revenue', 'orders', 'units', 'stock_days', 'production_units', 'manual', 'unallocated'],
      defaultExpensePeriod: 'month',
      defaultAllocationBase: 'sku',
      templateCategories: ['Хранение', 'Упаковка'],
      temporaryAssumptions: ['1С live connector states are visible', 'Tax stays null until rate is configured'],
      importPolicy: 'preview -> approval -> commit',
    }).success).toBe(true)

    expect(ExpensesReportResponseSchema.safeParse({
      sourceStatus: 'blocked',
      confidence: 'blocked',
      blockerIds: ['WB-13', 'WB-24'],
      sourceEvidence: evidence,
      calculatedAt: now,
      period,
      rows: [{ ...expenseRow, allocationBase: 'sku' }],
      allowedAllocationBases: ['sku', 'brand', 'manager', 'marketplace', 'revenue', 'orders', 'units', 'stock_days', 'production_units', 'manual', 'unallocated'],
      defaultExpensePeriod: 'month',
      defaultAllocationBase: 'sku',
      templateCategories: ['Хранение'],
      temporaryAssumptions: ['SKU allocation confirmed'],
      importPolicy: 'preview -> approval -> commit',
    }).success).toBe(true)

    expect(ExpenseImportPreviewResponseSchema.safeParse({
      sourceStatus: 'blocked',
      confidence: 'blocked',
      blockerIds: ['WB-12', 'WB-13', 'WB-24'],
      sourceEvidence: evidence,
      calculatedAt: now,
      period,
      previewId: 'expense-preview-test',
      sourceName: '1С · финансы и УУ',
      validRows: 1,
      blockedRows: 1,
      rows: [expenseRow],
      requiredApproval: true,
      commitAllowed: false,
      nextAction: 'Ask Maksim to approve tax rate and unresolved mappings',
    }).success).toBe(true)

    expect(ExpenseImportCommitResponseSchema.safeParse({
      sourceStatus: 'blocked',
      confidence: 'blocked',
      blockerIds: ['WB-12', 'WB-13', 'WB-24'],
      sourceEvidence: evidence,
      calculatedAt: now,
      period,
      previewId: 'expense-preview-test',
      committed: false,
      committedRows: 0,
      auditEventId: null,
      nextAction: 'Approval and mapping are required before commit',
    }).success).toBe(true)

    expect(SourceStatusResponseSchema.safeParse({
      sourceStatus: 'blocked',
      confidence: 'blocked',
      blockerIds: ['WB-12', 'WB-13', 'WB-24'],
      sourceEvidence: evidence,
      calculatedAt: now,
      period,
      rows: [{
        sourceId: 'one-c-finance',
        label: '1С · финансы и УУ',
        surface: 'Расходы / P&L',
        sourceStatus: 'blocked',
        confidence: 'blocked',
        freshnessTtlMinutes: 1440,
        lastSyncedAt: now,
        nextRunAt: null,
        blockerIds: ['WB-13', 'WB-24'],
        evidenceRefs: ['docs/open-questions-current.md#WB-12'],
        owner: 'Максим',
      }],
      manualUploadAllowed: true,
      productionMetricsBlocked: true,
    }).success).toBe(true)
  })

  it('does not expose campaign-only spend as SKU-level ads attribution', () => {
    const range = normalizeDateRange({ preset: '7d' })
    const sidecar = buildReportContractSidecar('ads', getAdsReport(range), { dateRange: range, groupBy: 'sku' })

    expect(sidecar.groupBy).toBe('sku')
    expect(sidecar.rows.every((row) => row.attributionLevel !== 'campaign_only')).toBe(true)
    expect(AdsPerformanceResponseSchema.safeParse(sidecar).success).toBe(true)
  })

  it('keeps RNP ads source blockers visible at runtime boundary', () => {
    const range = normalizeDateRange({ preset: '7d' })
    const sidecar = buildReportContractSidecar('rnp', getRnpReport(range), { dateRange: range, groupBy: 'sku' })

    expect(sidecar.blockerIds).toContain('WB-02')
    expect(RnpReportResponseSchema.safeParse({ ...sidecar, adsSourceStatus: 'blocked', blockerIds: ['WB-11'] }).success).toBe(false)
  })

  it('builds ABC filtered summary from the current row set', () => {
    const range = normalizeDateRange({ preset: '7d' })
    const skuSidecar = buildReportContractSidecar('abc', getAbcReport(range, 'sku'), { dateRange: range, groupBy: 'sku' })
    const managerSidecar = buildReportContractSidecar('abc', getAbcReport(range, 'manager'), { dateRange: range, groupBy: 'manager' })

    expect(skuSidecar.filteredSummary.filterHash).not.toBe(managerSidecar.filteredSummary.filterHash)
    expect(skuSidecar.filteredSummary.skuCount).not.toBe(managerSidecar.filteredSummary.skuCount)
  })

  it('aggregates ABC by manager when groupBy is selected', () => {
    const report = getAbcReport(normalizeDateRange({ preset: '14d' }), 'manager')
    expect(report.filters.dateRange.preset).toBe('14d')
    expect(report.rows.length).toBeGreaterThan(1)
    expect(report.rows.map((row) => row.sku)).toContain('Без ответственного')
    expect(report.rows.find((row) => row.sku === 'Без ответственного')?.managerId).toBeNull()
    expect(report.columns.map((column) => column.key)).toContain('netTotalKopecks')
  })

  it('aggregates RNP by manager when groupBy is selected', () => {
    const report = getRnpReport(normalizeDateRange({ preset: '7d' }), 'manager')
    expect(report.filters.groupBy).toBe('manager')
    expect(report.rows.map((row) => row.sku)).toContain('Без ответственного')
    expect(report.rows.find((row) => row.sku === 'Котельникова')?.managerId).toBe('manager-kotelnikova')
  })

  it('keeps campaign-only ads unallocated for manager plan-fact', () => {
    const ads = getAdsReport()
    const digest = getDigestReport()
    const campaignOnlySpend = ads.rows
      .filter((row) => row.attributionLevel === 'campaign_only')
      .reduce((acc, row) => acc + Number(row.adSpendKopecks ?? 0), 0)
    const company = digest.planFactRows.find((row) => row.owner === 'company')
    const managerFact = digest.planFactRows
      .filter((row) => row.owner === 'manager')
      .reduce((acc, row) => acc + Number(row.factKopecks ?? 0), 0)
    expect(campaignOnlySpend).toBeGreaterThan(0)
    expect(company?.unallocatedCostKopecks).toBe(campaignOnlySpend)
    expect(managerFact).toBeLessThanOrEqual(Number(company?.factKopecks ?? 0))
  })

  it('prorates monthly manager plans for a custom period', () => {
    const digest = getDigestReport({ preset: 'custom', from: '2026-05-01', to: '2026-05-03' })
    expect(digest.dateRange).toEqual({ preset: 'custom', from: '2026-05-01', to: '2026-05-03' })
    const row = digest.planFactRows.find((item) => item.ownerId === 'manager-kotelnikova')
    expect(row?.planKopecks).toBe(Math.round(24000000 * 3 / 31))
    expect(row?.needPerDayKopecks).toEqual(expect.any(Number))
  })

  it('treats promo exclusion Excel as an empty/non-useful source note', () => {
    const exportInfo = getExportForRole('abc', 'admin')
    expect(exportInfo).toMatchObject({ fileName: expect.stringContaining('wb-abc'), rows: expect.any(Number), exportAllowed: true })
    expect(exportInfo.emptySourceNote).toContain('Товар уже участвует в акции')
  })

  it('marks financial P&L as pending until WB final report arrives', () => {
    const report = getPnlReport(normalizeDateRange({ preset: 'custom', from: '2026-05-01', to: '2026-05-03' }), 'financial')
    expect(report.meta.id).toBe('pnl')
    expect(report.filters.dateRange).toEqual({ preset: 'custom', from: '2026-05-01', to: '2026-05-03' })
    expect(report.meta.sourceType).toBe('financial')
    expect(report.meta.freshnessState).toBe('pending_financial')
    expect(report.financialConfirmationStatus).toBe('pending_financial')
    expect(report.columns.map((column) => column.key)).toEqual(expect.arrayContaining(['taxBaseKopecks', 'cogsKopecks']))
    expect(report.columns.map((column) => column.key)).not.toContain('financialConfirmationStatus')
    expect(report.warning).toBeTruthy()
  })

  it('builds SKU-first ads rows with draft stop recommendations', () => {
    const report = getAdsReport()
    expect(report.columns[0]?.key).toBe('photoUrl')
    expect(report.columns[1]?.key).toBe('sku')
    expect(report.columns[3]?.key).toBe('campaignName')
    expect(report.rows[0].campaignName).not.toMatch(/^(Поиск|Каталог|Полки|Медиа)\s*·/)
    expect(report.columns.map((column) => column.key)).toEqual(expect.arrayContaining(['campaignName', 'campaignType', 'attributionLevel', 'attributionConfidencePct', 'observationDays', 'photoUrl', 'sku', 'recommendationReason']))
    expect(report.rows[0].campaignType).toMatch(/search|catalog|shelf|media/)
    expect(report.rows[0].photoUrl).toEqual(expect.any(String))
    expect(report.rows.some((row) => row.recommendation === 'draft_stop')).toBe(true)
    expect(report.rows.some((row) => row.recommendation === 'review')).toBe(true)
    const draftStop = report.rows.find((row) => row.recommendation === 'draft_stop')
    expect(draftStop).toMatchObject({
      observationDays: expect.any(Number),
      minSpendMet: true,
      hasOosInPeriod: false,
      isNewSku: false,
      isPromoOrLiquidation: false,
      recommendationStatus: 'draft',
    })
    expect(Number(draftStop?.drrPct)).toBeGreaterThanOrEqual(25)
    expect(Number(draftStop?.attributionConfidencePct)).toBeGreaterThanOrEqual(70)
    expect(String(draftStop?.recommendationReason)).toContain('кандидат · черновик')
  })

  it('keeps RNP rows SKU-based without seeded manager comments', () => {
    const report = getRnpReport()
    expect(JSON.stringify(report.rows)).not.toContain('Склад Казань')
    expect(report.comments).toEqual([])
  })

  it('provides SKU photos and an empty comment collection for SKU-level reports', () => {
    const reports = [getRnpReport(), getPnlReport(), getStockReport(), getWeekOverWeekReport()]
    for (const report of reports) {
      expect(report.columns.map((column) => column.key)).toContain('photoUrl')
      expect(report.rows[0].photoUrl).toEqual(expect.any(String))
      expect(report.comments).toEqual([])
    }
  })

  it('builds stock warehouse rows with availability components and KTR', () => {
    const report = getStockReport()
    expect(report.columns.map((column) => column.key)).toEqual(
      expect.arrayContaining(['warehouseName', 'clusterName', 'wbStockUnits', 'fromClientUnits', 'toClientUnits', 'availableUnits', 'ktrIndex', 'localizationPct', 'salesDistributionCoefficient']),
    )
    const first = report.rows[0]
    expect(first.availableUnits).toBe(Number(first.wbStockUnits) + Number(first.fromClientUnits))
  })

  it('maps Maria localization table to KTR coefficients', () => {
    expect(calculateLocalizationPct(80, 100)).toBe(80)
    expect(localizationCoefficients(96)).toMatchObject({ territorialCoefficient: 0.5, salesDistributionCoefficient: 0 })
    expect(localizationCoefficients(58)).toMatchObject({ territorialCoefficient: 1.05, salesDistributionCoefficient: 2 })
    expect(localizationCoefficients(4)).toMatchObject({ territorialCoefficient: 2, salesDistributionCoefficient: 2.5 })
  })

  it('builds week-over-week rows with physical value plus percent dynamics and OOS history', () => {
    const report = getWeekOverWeekReport()
    expect(report.columns.map((column) => column.key)).toEqual(expect.arrayContaining(['orders', 'sales', 'wasOutOfStock', 'stockAvailability7d', 'stockOutDays', 'stockSnapshotCoveragePct']))
    expect(report.rows[0].orders).toMatchObject({ units: expect.any(Number), kopecks: expect.any(Number), deltaPct: expect.any(Number) })
    expect(report.rows.some((row) => row.wasOutOfStock === true)).toBe(true)
    expect(report.rows[0].stockAvailability7d).toHaveLength(7)
    expect(report.rows[0].stockOutDays).toBeGreaterThan(0)
    expect(report.rows[0].stockSnapshotCoveragePct).toBe(100)
  })

  it('stores daily stock snapshots for future backend persistence contract', () => {
    const snapshots = getStockDailySnapshots()
    expect(snapshots).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          snapshotDate: expect.stringMatching(/^\d{4}-\d{2}-\d{2}$/),
          sku: expect.any(String),
          nmId: expect.any(Number),
          warehouseName: expect.any(String),
          clusterName: expect.any(String),
          availableUnits: expect.any(Number),
          wasOutOfStock: expect.any(Boolean),
          source: 'wb_stocks_report',
        }),
      ]),
    )
    expect(snapshots.some((snapshot) => snapshot.wasOutOfStock)).toBe(true)
  })

  it('exposes SKU analytics summary for repricer', () => {
    const analytics = getSkuAnalyticsSummary('FBBT_42')
    expect(analytics?.abcCode).toBe('AA')
    expect(analytics?.promotionStatus).toBe('yes')
    expect(analytics?.ordersUnits).toBeGreaterThan(0)
  })
})
