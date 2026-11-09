import { describe, expect, it } from 'vitest'

import {
  adsCampaignStatusLabel,
  adsCampaignTypeLabel,
  adsDimensionValues,
  adsPaymentTypeLabel,
  buildAbcExportTable,
  buildAdsExportTable,
  digestPeriodRows,
  productsMetricTrend,
  productsBuyoutTrend,
  weekMetricPair,
  weekRowFilterData,
} from './VellaHtmlParityPage'

describe('WB release contracts', () => {
  it('shows the full WB funnel for each digest period', () => {
    expect(digestPeriodRows({
      openCount: 1_200,
      cartCount: 310,
      ordersUnits: 120,
      ordersKopecks: 5_000_000,
      salesUnits: 80,
      returnsUnits: 4,
      buyoutPct: 66.7,
      revenueKopecks: 3_200_000,
    })).toEqual(expect.arrayContaining([
      ['Открыли карточку', '1\u00a0200'],
      ['Добавили в корзину', '310'],
      ['Заказы, шт', '120 шт'],
    ]))
  })

  it('exports ABC sales units, cogs, storage and profit from backend rows', () => {
    const table = buildAbcExportTable([{
      sku: 'SKU-1',
      salesComposite: { units: 3, kopecks: 450_000 },
      cogsPerUnitKopecks: 70_000,
      storageCostPct: 4.2,
      netTotalKopecks: 125_000,
    }])

    expect(table.headers).toEqual(expect.arrayContaining(['Продажи, шт', 'Себестоимость за шт', 'Хранение, %', 'Чистая прибыль']))
    expect(table.headers).not.toContain('Показы')
    expect(table.headers).not.toContain('КТР / локализация')
    expect(table.rows[0]).toHaveLength(table.headers.length)
    expect(table.rows[0]).toEqual(expect.arrayContaining(['SKU-1', '3', '700 ₽', '4,2%', '1\u00a0250 ₽']))
  })

  it('exports and filters WB advertising by cabinet dimensions', () => {
    const row = {
      campaignId: 42,
      campaignName: 'Поиск футболок',
      campaignStatus: 9,
      campaignType: 9,
      bidType: 'manual',
      paymentType: 'cpc',
      fundingSource: 'Баланс',
      adSpendKopecks: 12_500,
    }

    expect(adsCampaignStatusLabel(row.campaignStatus)).toBe('Активна')
    expect(adsCampaignTypeLabel(row.campaignType)).toBe('Продвижение WB')
    expect(adsPaymentTypeLabel(row.paymentType)).toBe('CPC · за клики')
    expect(adsDimensionValues([row], 'bidType')).toEqual(['manual'])
    const exported = buildAdsExportTable([row])
    expect(exported.rows[0]).toEqual(expect.arrayContaining(['42', 'Активна', 125]))
    expect(exported.headers).not.toContain('РК / источник')
    expect(exported.headers).not.toContain('Тип РК')
    expect(exported.headers).not.toContain('Дней')
  })

  it('shows units and rubles together and makes week filters clickable', () => {
    expect(weekMetricPair({ units: 12, kopecks: 345_600 })).toEqual({ units: '12 шт', money: '3\u00a0456 ₽' })
    const filter = weekRowFilterData({
      sku: 'SKU-7',
      productName: 'Футболка',
      sales: { units: 12, kopecks: 345_600, deltaPct: 10 },
      marginPct: { percent: -2, deltaPct: -4 },
      conclusion: 'Рост продаж, маржа ниже порога',
    })
    expect(filter.search).toContain('SKU-7')
    expect(filter.tags).toContain('рост')
    expect(filter.tags).toContain('ниже порога')
    expect(filter.tags).toContain('маржа ниже')
  })

  it('uses green and red arrows only for a real previous-period comparison', () => {
    expect(productsMetricTrend([{ current: 15, previous: 10 }], 'current', 'previous')).toEqual({ className: 'up', label: '↑ +50% к прошлому периоду' })
    expect(productsMetricTrend([{ current: 5, previous: 10 }], 'current', 'previous')).toEqual({ className: 'down', label: '↓ −50% к прошлому периоду' })
    expect(productsMetricTrend([{ current: 5 }], 'current', 'previous')).toBeNull()
  })

  it('compares financial buyout quantities only with complete matching financial coverage', () => {
    const summary = { buyoutUnits: 6, buyoutComparison: {
      state: 'ok', currentState: 'ok', dateFrom: '2026-09-28', dateTo: '2026-10-04',
      units: 4, amountKopecks: 10000, source: 'finance.rrDate.retailAmount.netSalesUnits',
    } }
    expect(productsBuyoutTrend(summary)?.label).toContain('+50%')
    expect(productsBuyoutTrend({ ...summary, buyoutUnits: null })).toBeNull()
    expect(productsBuyoutTrend({ ...summary, buyoutComparison: { ...summary.buyoutComparison, currentState: 'partial' } })).toBeNull()
    expect(productsBuyoutTrend({ ...summary, buyoutComparison: { ...summary.buyoutComparison, state: 'no_data' } })).toBeNull()
    expect(productsBuyoutTrend({ ...summary, buyoutComparison: { ...summary.buyoutComparison, units: -4 } })).toBeNull()
    expect(productsBuyoutTrend({ ...summary, buyoutComparison: { ...summary.buyoutComparison, units: 0 } })?.label).toContain('рост с нуля')
    expect(productsBuyoutTrend({ buyoutUnits: 6 })).toBeNull()
  })
})
