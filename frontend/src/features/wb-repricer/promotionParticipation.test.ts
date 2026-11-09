import { expect, test } from 'vitest'
import { mapLiveRepricerRowToParityProduct, type LiveRepricerSkuRow } from './liveParityData'
import { PromotionStatus } from './schemas'

const row: LiveRepricerSkuRow = {
  meta: { articleId: 'Synthetic-101', nmId: 101, name: 'Synthetic', status: 'manual', currentPriceKopecks: 150000, basketsLast7d: 1, basketNorm: 1 },
  settings: { cogsKopecks: 50000, logisticsKopecks: 5000, minMarginPct: 15, wbCommissionPct: 15 },
}

test.each([undefined, 'unknown'] as const)('preserves %s participation as unknown despite a promotion name', (status) => {
  const product = mapLiveRepricerRowToParityProduct({ ...row, analytics: { promotionStatus: status, promotionName: 'A name is not evidence' } }, 0)
  expect(product.promotionStatus).toBe('unknown')
  expect(product.promoActive).toBeNull()
  expect(product.promoState).toBe('Нет данных')
  expect(product.promoTooltip).not.toContain('Не участвует')
  expect(product.price).toBe(1500)
  expect(PromotionStatus.parse('unknown')).toBe('unknown')
})

test('preserves confirmed participation, its name and observation time', () => {
  const product = mapLiveRepricerRowToParityProduct({ ...row, analytics: { promotionStatus: 'yes', promotionName: 'Synthetic promo',
    promotionStatusReason: 'Подтверждено WB', promotionStatusObservedAt: '2026-11-02T10:00:00Z' } }, 0)
  expect(product.promoActive).toBe(true)
  expect(product.promoState).toBe('Synthetic promo')
  expect(product.promoTooltip).toContain('Подтверждено WB')
  expect(product.promoTooltip).toContain('МСК')
})

test('keeps negative evidence limited to the checked regular promotions', () => {
  const product = mapLiveRepricerRowToParityProduct({ ...row, analytics: { promotionStatus: 'no', promotionStatusScope: 'regular',
    promotionStatusReason: 'Не участвует в проверенных обычных акциях. Автоакции не проверены.' } }, 0)
  expect(product.promoActive).toBe(false)
  expect(product.promoState).toBe('Нет в обычных')
  expect(product.promoTooltip).toContain('Автоакции не проверены')
})
