import { describe, expect, it } from 'vitest'

import { selectScopedReportState } from './scopedReportState'

describe('scoped report rendering', () => {
  const loading = { status: 'loading' }
  const ready = { status: 'ready', rows: ['private report A'] }

  it('renders a completed report only in its request scope', () => {
    expect(selectScopedReportState('A', { scope: 'A', state: ready }, loading)).toBe(ready)
  })

  it('hides the previous report on the first render of another scope, before effects run', () => {
    expect(selectScopedReportState('B', { scope: 'A', state: ready }, loading)).toBe(loading)
    expect(selectScopedReportState('B', null, loading)).toBe(loading)
  })

  it('does not expose a late result or error from the previous scope', () => {
    expect(selectScopedReportState('B', { scope: 'A', state: ready }, loading)).toBe(loading)
    expect(selectScopedReportState('B', { scope: 'A', state: { status: 'error' } }, loading)).toBe(loading)
    expect(selectScopedReportState('B', { scope: 'B', state: ready }, loading)).toBe(ready)
  })
})
