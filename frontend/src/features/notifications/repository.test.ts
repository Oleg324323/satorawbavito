import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  DEFAULT_NOTIFICATION_FILTERS,
  filterNotifications,
  getNotificationManagers,
  getNotifications,
  getUnreadCount,
  markAllNotificationsRead,
  markNotificationRead,
} from './repository.js'

describe('notifications repository', () => {
  it('preserves the order alert without an invented marking prerequisite', () => {
    const alert = getNotifications().items.find((item) => item.id === 'orders-sla-001')
    expect(alert?.route).toBe('/orders')
    expect(alert?.severity).toBe('critical')
    expect(alert?.blockedActions).toEqual(['Автоперенос в конец очереди'])
  })
  // Fixtures describe this business day; the real filter must still use Date.now().
  beforeEach(() => {
    vi.spyOn(Date, 'now').mockReturnValue(Date.parse('2026-05-08T10:00:00+05:00'))
  })
  afterEach(() => vi.restoreAllMocks())

  it('returns a stable mixed notification feed', () => {
    const { items } = getNotifications()
    expect(items.length).toBeGreaterThan(5)
    expect(items.map((item) => item.category)).toEqual(
      expect.arrayContaining(['reports', 'prices', 'orders', 'avito', 'ai', 'system']),
    )
    expect(items[0]).toMatchObject({
      id: expect.any(String),
      title: expect.any(String),
      severity: expect.stringMatching(/critical|warning|info/),
      manager: expect.any(String),
    })
  })

  it('filters by period, category, severity, manager, read state and search', () => {
    const { items } = getNotifications()
    const filtered = filterNotifications(items, {
      ...DEFAULT_NOTIFICATION_FILTERS,
      period: '7d',
      category: 'reports',
      severity: 'critical',
      manager: 'Воробьева',
      read: 'unread',
      query: 'LBBT_03',
    })

    expect(filtered.map((item) => item.id)).toEqual(['abc-cc'])
  })

  it('excludes older items when the period is narrow', () => {
    const { items } = getNotifications()
    const filtered = filterNotifications(items, { ...DEFAULT_NOTIFICATION_FILTERS, period: '1d' })
    expect(filtered.map((item) => item.id)).toEqual([
      'orders-sla-001', 'price-pmin-001', 'report-export-ready-001', 'pnl-freshness-001',
    ])
  })

  it('includes the exact period boundary but excludes an item one millisecond older', () => {
    const template = getNotifications().items[0]
    const items = [
      { ...template, id: 'boundary', createdAt: '2026-05-01T05:00:00.000Z' },
      { ...template, id: 'older', createdAt: '2026-05-01T04:59:59.999Z' },
    ]
    expect(filterNotifications(items).map((item) => item.id)).toEqual(['boundary'])
  })

  it('expires fixture events when the clock advances beyond the selected period', () => {
    vi.mocked(Date.now).mockReturnValue(Date.parse('2026-05-16T10:00:00+05:00'))
    expect(filterNotifications(getNotifications().items)).toEqual([])
  })

  it('tracks unread count when one or all notifications are marked read', () => {
    const { items } = getNotifications()
    const unreadBefore = getUnreadCount(items)
    const oneRead = markNotificationRead(items, 'orders-sla-001', '2026-05-08T10:20:00.000+05:00')
    expect(getUnreadCount(oneRead)).toBe(unreadBefore - 1)
    expect(oneRead.find((item) => item.id === 'orders-sla-001')?.readAt).toBe('2026-05-08T10:20:00.000+05:00')

    const allRead = markAllNotificationsRead(oneRead, '2026-05-08T10:30:00.000+05:00')
    expect(getUnreadCount(allRead)).toBe(0)
  })

  it('returns sorted manager filter values', () => {
    const { items } = getNotifications()
    expect(getNotificationManagers(items)).toEqual(['Авито', 'Воробьева', 'Дудина', 'Отзывы', 'Отчёты', 'Производство', 'Система', 'Финансы'])
  })

  it('keeps generated report files in the notification feed', () => {
    const { items } = getNotifications()
    const reportExport = items.find((item) => item.id === 'report-export-ready-001')

    expect(reportExport).toMatchObject({
      category: 'reports',
      entityType: 'report',
      reportFile: {
        report: 'Реклама WB',
        format: 'XLS',
        fileName: 'wb-ads-25-04_01-05.xls',
        rows: 1482,
      },
    })
  })
})
