import type { ReviewNotificationList, ReviewNotificationVisible } from './canonicalReviewNotifications'
import type { ReviewNotificationFailure } from './canonicalReviewNotificationsClient'
import type { NotificationEvent } from './types'

/** Only server-confirmed personal receipts count as read. Discovery is not a receipt. */
export function canonicalNotificationItems(data: ReviewNotificationVisible | ReviewNotificationList): NotificationEvent[] {
  return data.items.filter((item) => !item.receipt?.value.dismissedAt).map(({ event, receipt }) => ({
    id: event.eventId, title: event.title, details: event.details, severity: event.severity,
    category: data.marketplace === 'avito' ? 'avito' : 'system',
    source: data.marketplace === 'avito' ? 'Отзывы Авито' : 'Отзывы WB', manager: '—',
    createdAt: event.occurredAt, readAt: receipt?.value.readAt ?? null,
    entityType: 'review', entityId: event.entityId,
    route: data.marketplace === 'avito' ? '/avito/reviews' : '/wb/reviews',
    blockedActions: event.kind === 'send_ambiguous' ? ['Повторная отправка до проверки результата']
      : event.kind === 'send_blocked' ? ['Отправка ответа'] : ['Отправка без подтверждения'],
  }))
}

export function canonicalNotificationError(failure: ReviewNotificationFailure): string {
  const messages: Record<ReviewNotificationFailure['state'], string> = {
    'invalid-request': 'Параметры запроса уведомлений не приняты.',
    unauthenticated: 'Сессия завершена. Войдите снова.',
    'no-access': 'Нет доступа к уведомлениям выбранного аккаунта.',
    'not-found': 'Уведомление больше недоступно. Обновите список.',
    conflict: 'Состояние уведомлений изменилось. Обновите список.',
    unavailable: 'Уведомления временно недоступны. Повторите чтение.',
    'invalid-response': 'Сервис вернул некорректный ответ. Повторите чтение.',
    stale: 'Аккаунт или сессия изменились. Обновите список.',
  }
  return messages[failure.state] + (failure.outcome === 'unknown'
    ? ' Результат отметки неизвестен; перечитайте состояние перед повтором.' : '')
}
