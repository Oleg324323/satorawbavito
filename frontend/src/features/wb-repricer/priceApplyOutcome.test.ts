import { describe, expect, it } from 'vitest'
import { priceApplyOutcome, priceWasApplied } from './priceApplyOutcome'

describe('confirmed versus pending price state', () => {
  it.each(['sent', 'queued', 'processing', 'applying', 'accepted'])('does not publish %s as a new price', (state) => {
    expect(priceApplyOutcome({ job: { state } })).toBe('pending')
    expect(priceWasApplied({ job: { state } })).toBe(false)
  })
  it('requires explicit WB read-back proof', () => {
    expect(priceWasApplied({ job: { state: 'accepted', lastApplyResult: { priceConfirmed: true } } })).toBe(true)
    expect(priceWasApplied({ approval: { status: 'applied', priceConfirmed: true } })).toBe(true)
  })
  it('keeps local simulation distinct and classifies blocked and uncertain attempts', () => {
    expect(priceApplyOutcome({ job: { state: 'local_applied' } })).toBe('local')
    expect(priceApplyOutcome({ job: { state: 'blocked' } })).toBe('blocked')
    expect(priceApplyOutcome({ job: { state: 'needs_attention', lastApplyResult: { wbMutationSent: true } } })).toBe('pending')
    expect(priceApplyOutcome({ job: { state: 'failed' } })).toBe('failed')
    expect(priceApplyOutcome({ job: { state: 'needs_attention', lastApplyResult: { wbMutationSent: true, statusLabel: 'partial_errors', rowErrors: [{}] } } })).toBe('failed')
  })
})
