import type { LiveWbPromotion } from './liveParityData'

export function promotionsSummary(promotions: LiveWbPromotion[]) {
  const current = promotions.filter((promo) => promo.status === 'active' || promo.status === 'upcoming')
  const participating = current.filter((promo) => Number(promo.participatingSkuCount) > 0)
  return {
    participating: participating.length,
    activeParticipation: participating.filter((promo) => promo.status === 'active').length,
    plannedParticipation: participating.filter((promo) => promo.status === 'upcoming').length,
    upcoming: current.filter((promo) => promo.status === 'upcoming').length,
    withoutThresholds: current.filter((promo) => !promo.excelLoaded).length,
  }
}
