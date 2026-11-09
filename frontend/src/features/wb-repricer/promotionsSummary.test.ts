import { expect, it } from 'vitest'
import { promotionsSummary } from './promotionsSummary'
import type { LiveWbPromotion } from './liveParityData'

function promotion(id: number, status: string, participatingSkuCount = 1): LiveWbPromotion {
  return { id: String(id), name: `Promotion ${id}`, type: 'auto', status, participatingSkuCount }
}

it('counts actual participation separately from current and upcoming availability', () => {
  const rows = [
    ...Array.from({ length: 7 }, (_, i) => promotion(i, 'active')),
    promotion(20, 'upcoming'), promotion(21, 'upcoming'),
    promotion(22, 'active', 0), promotion(23, 'ended'),
  ]
  expect(promotionsSummary(rows)).toEqual({ participating: 9, activeParticipation: 7, plannedParticipation: 2, upcoming: 2, withoutThresholds: 10 })
})

it('does not substitute demo counts for an empty list or unknown participation', () => {
  expect(promotionsSummary([]).participating).toBe(0)
  const row = promotion(1, 'active')
  delete row.participatingSkuCount
  expect(promotionsSummary([row]).participating).toBe(0)
})
