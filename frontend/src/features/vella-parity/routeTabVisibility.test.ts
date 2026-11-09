import { describe, expect, it } from 'vitest'

import { enforceRouteTabVisibility } from './VellaHtmlParityPage'
import { routeStateFromPath } from '../vella-static/VellaStaticPage'

describe('enforceRouteTabVisibility', () => {
  it('hides WB tabs and keeps the Avito orders tab visible on /avito/orders', () => {
    const elements = new Map([
      ['tab-products', fakeTabElement('tab-products', true)],
      ['tab-work-status', fakeTabElement('tab-work-status', true)],
      ['tab-orders-print', fakeTabElement('tab-orders-print', false)],
    ])
    const root = {
      dataset: {} as Record<string, string>,
      querySelectorAll: (selector: string) => selector === '.tab-content' ? Array.from(elements.values()) : [],
    } as unknown as HTMLElement

    enforceRouteTabVisibility(root, 'orders-avito')

    expect(root.dataset.vellaActiveTab).toBe('orders-avito')
    expect(elements.get('tab-products')?.hidden).toBe(true)
    expect(elements.get('tab-products')?.classList.contains('active')).toBe(false)
    expect(elements.get('tab-work-status')?.hidden).toBe(true)
    expect(elements.get('tab-orders-print')?.hidden).toBe(false)
    expect(elements.get('tab-orders-print')?.classList.contains('active')).toBe(true)
  })

  it('keeps Avito privacy inside the platform route', () => {
    const privacy = fakeTabElement('tab-avito-privacy', false)
    const overview = fakeTabElement('tab-avito-overview', true)
    const root = {
      dataset: {} as Record<string, string>,
      querySelectorAll: () => [overview, privacy],
    } as unknown as HTMLElement

    const tab = routeStateFromPath('/avito/privacy', '')
    expect(tab).toBe('avito-privacy')
    if (typeof tab !== 'string') throw new Error('Expected a platform tab')
    enforceRouteTabVisibility(root, tab)
    expect(privacy.hidden).toBe(false)
    expect(overview.hidden).toBe(true)
  })
})

function fakeTabElement(id: string, active: boolean) {
  const classes = new Set(active ? ['tab-content', 'active'] : ['tab-content'])
  return {
    id,
    hidden: false,
    classList: {
      add: (value: string) => classes.add(value),
      remove: (value: string) => classes.delete(value),
      toggle: (value: string, force?: boolean) => {
        if (force === false) {
          classes.delete(value)
          return false
        }
        classes.add(value)
        return true
      },
      contains: (value: string) => classes.has(value),
    },
    dataset: {} as Record<string, string>,
  } as unknown as HTMLElement
}
