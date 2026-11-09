import { describe, expect, it } from 'vitest'
import { findNavItemByPath, isItemVisible, visibleNavigationForRole } from './navigation'

function visibleIdsFor(role: Parameters<typeof visibleNavigationForRole>[0]) {
  return visibleNavigationForRole(role).flatMap((group) =>
    group.items.flatMap((item) => [item.id, ...(item.children?.map((child) => child.id) ?? [])]),
  )
}

describe('navigation role visibility', () => {
  it('keeps repricer as the only Avito listing workspace', () => {
    expect(findNavItemByPath('/avito/listings')).toBeUndefined()
    expect(findNavItemByPath('/avito/repricer')?.id).toBe('avito-repricer')
  })
  it('keeps production orders and returns available without the removed marking workspace', () => {
    for (const role of ['admin', 'production'] as const) {
      const ids = visibleIdsFor(role)
      expect(ids).toContain('orders')
      expect(ids).toContain('orders-returns')
      expect(ids).not.toContain('orders-kiz')
    }
    expect(findNavItemByPath('/orders/kiz')).toBeUndefined()
  })
  it('keeps finance reports out of production navigation', () => {
    const pnl = findNavItemByPath('/wb/reports/pnl')
    const expenses = findNavItemByPath('/wb/reports/expenses')
    expect(pnl).toBeDefined()
    expect(expenses).toBeUndefined()
    expect(isItemVisible(pnl!, 'finance')).toBe(true)
    expect(isItemVisible(pnl!, 'production')).toBe(false)
  })

  it('shows the print list to production without exposing settings', () => {
    const ids = visibleIdsFor('production')
    expect(ids).toContain('orders')
    expect(ids).not.toContain('settings')
    expect(ids).not.toContain('wb-reports-pnl')
    expect(ids).not.toContain('wb-reports-expenses')
    expect(ids).not.toContain('wb-sources')
  })

  it('shows review workspaces to review role without finance-only reports', () => {
    const ids = visibleIdsFor('reviews')
    expect(ids).not.toContain('avito-overview')
    expect(ids).toContain('avito-notifications')
    expect(ids).toContain('wb-reviews')
    expect(ids).toContain('avito-reviews')
    expect(ids).not.toContain('wb-reports-pnl')
    expect(ids).not.toContain('wb-reports-expenses')
    expect(ids).not.toContain('avito-wallets')
  })

  it('exposes WB sources only to owner/admin and repricer stats to operational roles', () => {
    const sources = findNavItemByPath('/wb/sources')
    const stats = findNavItemByPath('/wb/repricer/stats')
    expect(sources?.id).toBe('wb-sources')
    expect(stats?.id).toBe('wb-repricer-stats')
    expect(isItemVisible(sources!, 'admin')).toBe(true)
    expect(isItemVisible(sources!, 'manager')).toBe(false)
    expect(isItemVisible(stats!, 'manager')).toBe(true)
  })

  it('does not expose the removed Avito overview', () => {
    expect(findNavItemByPath('/avito')).toBeUndefined()
    expect(findNavItemByPath('/avito/repricer')?.id).toBe('avito-repricer')
  })

  it('keeps Avito notifications as a separate Avito workspace', () => {
    const notifications = findNavItemByPath('/avito/notifications')
    expect(notifications?.id).toBe('avito-notifications')
    expect(isItemVisible(notifications!, 'production')).toBe(true)
    expect(findNavItemByPath('/notifications')?.id).toBe('notifications')
  })

  it('exposes full settings only to owner and admin', () => {
    const settings = findNavItemByPath('/settings')
    const access = findNavItemByPath('/settings/access')
    expect(settings?.badge).toBeUndefined()
    expect(isItemVisible(settings!, 'owner')).toBe(true)
    expect(isItemVisible(settings!, 'admin')).toBe(true)
    expect(isItemVisible(settings!, 'finance')).toBe(false)
    expect(isItemVisible(access!, 'manager')).toBe(false)
  })

  it('keeps Avito wallets finance-gated while ecom can use operational Avito areas', () => {
    const chats = findNavItemByPath('/avito/chats')
    const wallets = findNavItemByPath('/avito/wallets')
    const stats = findNavItemByPath('/avito/stats')
    expect(isItemVisible(chats!, 'ecom')).toBe(true)
    expect(isItemVisible(stats!, 'ads')).toBe(true)
    expect(isItemVisible(wallets!, 'ecom')).toBe(false)
    expect(isItemVisible(wallets!, 'finance')).toBe(true)
  })
})
