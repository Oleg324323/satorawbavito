import { describe, expect, it } from 'vitest'
import fs from 'node:fs'
import path from 'node:path'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { NotificationsToolbarIsland } from './VellaHtmlParityPage'

const sourceRoot = path.resolve(__dirname, '../..')
const paritySource = fs.readFileSync(path.join(sourceRoot, 'features/vella-parity/VellaHtmlParityPage.tsx'), 'utf8')
const settingsBackendSource = fs.readFileSync(path.join(sourceRoot, 'features/settings/backend.ts'), 'utf8')
const productionHtml = fs.readFileSync(path.resolve(sourceRoot, '../public/vella-production.html'), 'utf8')

describe('Avito live integration wiring', () => {
  it('renders refresh and read-all controls on the Avito notifications toolbar', () => {
    const html = renderToStaticMarkup(createElement(MemoryRouter, { initialEntries: ['/avito/notifications'] },
      createElement(NotificationsToolbarIsland)))
    expect(html).toContain('data-vella-island="notifications-toolbar"')
    expect(html).toContain('Прочитать всё')
    expect(html).toContain('Обновить данные')
    expect(html).toContain('data-tip="Обновить данные Авито"')
    expect(html).toContain('avito-action-btn is-refresh')
  })
  it('loads and saves Avito credentials from profile settings backend', () => {
    expect(settingsBackendSource).toContain('/api/v1/cabinet/avito-credentials')
    expect(settingsBackendSource).toContain('upsertCurrentUserAvitoCredentials')
    expect(paritySource).toContain('upsertCurrentUserAvitoCredentials')
  })

  it('keeps Avito repricer registered as a known legacy tab so the route cannot fall back to WB products', () => {
    expect(productionHtml).toContain("'avito-repricer': 'Репрайсер Авито'")
    expect(productionHtml).toContain("const AVITO_TABS = ['avito-overview', 'avito-inbox', 'avito-reviews', 'orders-avito', 'avito-listings', 'avito-repricer'")
    expect(productionHtml).toContain("'/avito/repricer': 'avito-repricer'")
  })

  it('renders Avito repricer as an actionable strategy table with settings in a modal', () => {
    const repricerSource = paritySource.slice(
      paritySource.indexOf('function AvitoRepricerToolbarIsland'),
      paritySource.indexOf('async function loadLiveAvitoStats'),
    )
    expect(paritySource).toContain('/api/v1/avito/repricer/items/${encodeURIComponent(itemId)}/strategy')
    expect(repricerSource).toContain('avito-strategy-picker')
    expect(repricerSource).toContain('avito-pending-card')
    expect(repricerSource).toContain('avito-row-photo')
    expect(repricerSource).toContain('price-history')
    expect(repricerSource).toContain('Посмотреть')
    expect(repricerSource).toContain('Стратегия')
    expect(repricerSource).toContain('Новая цена')
    expect(repricerSource).toContain('avito-title-cell')
    // Actual schedule-modal open/cancel behavior is covered in avitoSettingsModal.test.ts.
  })

  it('uses backend Avito stats data without static mock row arrays', () => {
    expect(paritySource).toContain('/api/v1/avito/stats')
    expect(paritySource).toContain('loadLiveAvitoStats')
    expect(paritySource).not.toContain('const AVITO_STATS_KPIS')
    expect(paritySource).not.toContain('const AVITO_STATS_DETAILS')
    expect(paritySource).not.toContain('const AVITO_STATS_ACCOUNT_ROWS')
    expect(paritySource).not.toContain('const AVITO_STATS_TOP_ROWS')
    expect(paritySource).not.toContain('const AVITO_STATS_LOW_ROWS')
  })

  it('lets Avito stats users choose a period and apply backend loading explicitly', () => {
    // Actual draft-input → Apply → scoped GET behavior is covered by
    // avitoStatsPeriodBrowser.test.ts, without relying on obsolete DOM IDs.
    expect(paritySource).toContain('applyAvitoStatsPeriod')
    expect(paritySource).toContain('forceRefresh')
    expect(paritySource).not.toContain('83 402')
    expect(paritySource).not.toContain('−19.6% к периоду')
  })

  it('uses backend Avito overview data without static overview mocks', () => {
    const overviewSource = paritySource.slice(
      paritySource.indexOf('function AvitoOverviewKpiStripIsland'),
      paritySource.indexOf('function defaultAvitoChatsState'),
    )
    expect(paritySource).toContain('/api/v1/avito/overview')
    expect(paritySource).toContain('loadLiveAvitoOverview')
    expect(paritySource).toContain('AvitoOverviewLiveState')
    expect(overviewSource).not.toContain('AVITO_OVERVIEW_KPIS')
    expect(overviewSource).not.toContain('AVITO_TOP_POSITIONS')
    expect(overviewSource).not.toContain('AVITO_OVERVIEW_EVENTS')
    expect(overviewSource).not.toContain('1 460')
    expect(overviewSource).not.toContain('3 658 ₽')
    expect(overviewSource).not.toContain('Баланс 320 ₽')
    expect(overviewSource).toContain('forceRefresh')
    expect(overviewSource).toContain('openAvitoOverviewItem')
    expect(overviewSource).toContain('avito-overview-item-modal')
    expect(overviewSource).not.toContain('window.openAvitoListingPopup?.(row.itemId)')
    expect(overviewSource).toContain('avitoOverviewDataForPeriod')
    // Actual loading, period isolation and empty/error rendering: avitoOverviewBrowser.test.ts.
    expect(overviewSource).toContain('Объявления по просмотрам и контактам за выбранный период.')
    expect(overviewSource).not.toContain('Объявления по просмотрам и контактам за 7 дней.')
  })

  it('scrubs the legacy Avito overview shell so stale fake periods cannot render under backend data', () => {
    const scrubSource = paritySource.slice(
      paritySource.indexOf('function scrubAvitoOverviewLegacyMocks'),
      paritySource.indexOf('function findDirectElement'),
    )
    const extractSource = paritySource.slice(
      paritySource.indexOf('function extractVellaRuntime'),
      paritySource.indexOf('const staticPnl'),
    )
    const overviewIslandSource = paritySource.slice(
      paritySource.indexOf('function AvitoOverviewIsland'),
      paritySource.indexOf('function defaultAvitoChatsState'),
    )
    expect(scrubSource).toContain('#tab-avito-overview')
    expect(scrubSource).toContain("tab.innerHTML = ''")
    expect(scrubSource).toContain('legacy-mocks-scrubbed')
    expect(scrubSource).toContain('avito-overview-report-shell')
    expect(scrubSource).toContain('avito-overview-period-control')
    expect(extractSource).toContain('scrubAvitoOverviewLegacyMocks(body)')
    expect(overviewIslandSource).toContain('scrubAvitoOverviewLegacyMocks(document.body)')
  })

  it('keeps Avito overview applied period wired to the backend request', () => {
    const islandSource = paritySource.slice(
      paritySource.indexOf('function AvitoOverviewIsland'),
      paritySource.indexOf('function defaultAvitoChatsState'),
    )
    // The current UI uses period tabs; actual clicks and request dates are
    // verified in avitoOverviewBrowser.test.ts, not obsolete date-input code.
    expect(islandSource).toContain('loadLiveAvitoOverview(accessToken, { dateFrom: state.dateFrom, dateTo: state.dateTo')
  })

  it('bridges legacy history navigation into React Router location updates', () => {
    expect(paritySource).toContain('installLegacyHistoryNavigationBridge')
    expect(paritySource).toContain('window.history.pushState = function pushState')
    expect(paritySource).toContain('window.history.replaceState = function replaceState')
    expect(paritySource).toContain("new PopStateEvent('popstate'")
    expect(paritySource).toContain('vella:history-navigation')
    expect(paritySource).toContain('installLegacyHistoryNavigationBridge()')
  })

  it('uses backend Avito listings data without static listing mock arrays', () => {
    expect(paritySource).toContain('/api/v1/avito/listings')
    expect(paritySource).toContain('loadLiveAvitoListings')
    // Actual labelled inputs → draft-only edits → Apply with exact GET dates
    // are covered by avitoListingsBrowser.test.ts; old DOM IDs are not required.
    expect(paritySource).not.toContain('const AVITO_LISTINGS_KPIS')
    expect(paritySource).not.toContain('const AVITO_LISTING_ROWS')
    expect(paritySource).not.toContain('const AVITO_LISTING_DETAILS')
  })

  it('closes Avito listings and stats detail panels through React state', () => {
    expect(paritySource).toContain('window.closeAvitoPopup =')
    expect(paritySource).toContain("window.__vellaSetAvitoListingsState?.({ selectedKey: '' })")
    expect(paritySource).toContain("window.__vellaSetAvitoStatsState?.({ selectedKey: '' })")
    // The unavailable daily-dynamics component deliberately renders nothing.
    // Detail close button/Escape are exercised by avitoListingsBrowser.test.ts.
    expect(paritySource).not.toContain('AVITO_DETAIL_TRENDS')
  })

  it('uses backend Avito chats data and actions without static inbox conversations', () => {
    const chatsLoaderSource = paritySource.slice(
      paritySource.indexOf('async function loadLiveAvitoChats'),
      paritySource.indexOf('async function loadLiveAvitoOrders'),
    )
    expect(paritySource).toContain('/api/v1/avito/chats')
    expect(paritySource).toContain('loadLiveAvitoChats')
    expect(paritySource).toContain('sendLiveAvitoChatMessage')
    expect(paritySource).toContain('markLiveAvitoChatRead')
    expect(paritySource).not.toContain('avitoChatsDateFrom')
    expect(paritySource).not.toContain('avitoChatsDateTo')
    expect(paritySource).not.toContain('applyAvitoChatsPeriod')
    expect(chatsLoaderSource).not.toContain('dateFrom')
    expect(chatsLoaderSource).not.toContain('dateTo')
    expect(paritySource).toContain('scrubAvitoInboxLegacyMocks')
    expect(paritySource).toContain("tab.innerHTML = ''")
    expect(paritySource).toContain('installAvitoConversationReactBridge()')
    expect(paritySource).not.toContain('const AVITO_INBOX_CONVERSATIONS')
    expect(paritySource).not.toContain('const AVITO_INBOX_CHIPS')
    expect(paritySource).not.toContain('Размер M указан в запросе')
  })

  it('renders Avito chat messages oldest first and newest at the bottom', () => {
    const helperSource = paritySource.slice(
      paritySource.indexOf('function avitoMessagesChronological'),
      paritySource.indexOf('function avitoChatsDiagnosticsLabel'),
    )
    const streamSource = paritySource.slice(
      paritySource.indexOf('function AvitoMessageStreamIsland'),
      paritySource.indexOf('function AvitoInboxShellIsland'),
    )
    expect(helperSource).toContain('new Date(left.createdAt || 0).getTime()')
    expect(helperSource).toContain('return leftTime - rightTime')
    expect(streamSource).toContain('avitoMessagesChronological')
    expect(streamSource).not.toContain('const messages = state.selectedChatId ? live.data?.messages[state.selectedChatId] ?? [] : []')
  })

  it('uses backend Avito orders data through a dedicated route island', () => {
    const ordersSource = paritySource.slice(
      paritySource.indexOf('async function loadLiveAvitoOrders'),
      paritySource.indexOf('function useAvitoStatsLiveState'),
    )
    expect(paritySource).toContain('/api/v1/avito/orders')
    expect(paritySource).toContain('loadLiveAvitoOrders')
    expect(paritySource).toContain('AvitoOrdersIsland')
    expect(paritySource).toContain("selector: '#tab-orders-print'")
    // avitoOrdersBrowser.test.ts proves the actual scoped read endpoint and
    // error/session rendering; obsolete provider/debug copy is not API wiring.
    expect(ordersSource).toContain("tab === 'orders-avito'")
    expect(ordersSource).toContain('Лист подбора Авито')
    expect(ordersSource).toContain('avitoOrderPickingRows')
    expect(ordersSource).toContain('.report-empty-note')
    expect(ordersSource).toContain('avitoOrdersBackendError')
    expect(ordersSource).toContain('htmlToReactFragment(sourceElement.innerHTML')
    expect(ordersSource).not.toContain('/api/v1/production/avito-orders/sync')
  })

  it('shows Avito API orders without requiring a browser extension', () => {
    const ordersIslandSource = paritySource.slice(
      paritySource.indexOf('function AvitoOrdersIsland'),
      paritySource.indexOf('function useAvitoStatsLiveState'),
    )
    expect(ordersIslandSource).toContain('orders-print-shell')
    expect(ordersIslandSource).not.toContain('Подключите расширение Avito Orders')
    expect(ordersIslandSource).toContain('Скопируйте токен')
    expect(ordersIslandSource).not.toContain('avito-orders-kpis')
    expect(ordersIslandSource).not.toContain('avito-browser-collector-note')
    expect(ordersIslandSource).not.toContain("['Заказы', formatAvitoInt(summary?.total ?? 0)")
    expect(ordersIslandSource).not.toContain("['Сумма', formatAvitoRubNullable(summary?.totalKopecks)")
  })

  it('uses backend Avito notifications data and actions on the Avito notifications route', () => {
    const notificationsSource = paritySource.slice(
      paritySource.indexOf('type AvitoNotificationsBackendResponse'),
      paritySource.indexOf('const EMPTY_WB_TOKEN_VIEW'),
    )
    expect(paritySource).toContain('/api/v1/avito/notifications')
    expect(paritySource).toContain('loadLiveAvitoNotifications')
    expect(paritySource).toContain('markLiveAvitoNotificationRead')
    expect(paritySource).toContain('markAllLiveAvitoNotificationsRead')
    expect(notificationsSource).toContain("tab === 'avito-notifications'")
    expect(notificationsSource).toContain('AVITO_NOTIFICATION_RULES.splice')
    expect(notificationsSource).toContain('AVITO_NOTIFICATION_CHANNELS.splice')
    expect(notificationsSource).toContain('refreshAvitoNotifications')
    expect(notificationsSource).not.toContain("id:'av-001'")
    expect(notificationsSource).not.toContain('Кошелёк Avito ниже порога')
  })

  it('uses backend Avito reviews data and answer actions through a React island', () => {
    const avitoReviewsIslandSource = paritySource.slice(
      paritySource.indexOf('function AvitoReviewsIsland'),
      paritySource.indexOf('const PRODUCT_SECTION_SPECS'),
    )
    expect(paritySource).toContain('/api/v1/avito/reviews')
    expect(paritySource).toContain('loadLiveAvitoReviews')
    expect(paritySource).toContain('generateLiveAvitoReviewDraft')
    expect(paritySource).toContain('sendLiveAvitoReviewAnswer')
    expect(paritySource).toContain('deleteLiveAvitoReviewAnswer')
    expect(paritySource).toContain('AvitoReviewsIsland')
    expect(paritySource).toContain('/api/v1/avito/reviews/${encodeURIComponent(review.reviewId)}/drafts/generate')
    expect(paritySource).toContain('AI-черновик')
    expect(paritySource).toContain('avito-reviews-shell')
    expect(paritySource).toContain('avito-review-drawer-overlay')
    expect(paritySource).toContain('avito-review-drawer')
    expect(paritySource).toContain('avito-reviews-table')
    expect(paritySource).toContain('avito-review-col-review')
    expect(paritySource).toContain('avito-review-col-product')
    expect(paritySource).toContain('avito-ai-settings-grid')
    expect(paritySource).toContain('avito-ai-prompt-setting')
    expect(paritySource).toContain('.avito-ai-prompt-setting textarea')
    expect(paritySource).toContain('AI черновики')
    expect(paritySource).toContain('Настройки AI')
    expect(paritySource).toContain('Режим автоответов')
    expect(paritySource).toContain('Низкие оценки')
    expect(avitoReviewsIslandSource).toContain('Тон Avito по умолчанию')
    expect(avitoReviewsIslandSource).toContain('avitoTone')
    expect(avitoReviewsIslandSource).not.toContain('anomieTone')
    expect(avitoReviewsIslandSource).not.toContain('blessTone')
    expect(paritySource).toContain('Одобрить')
    expect(paritySource).not.toContain("style={{ position: 'relative', inset: 'auto', width: 'auto'")
    expect(paritySource).toContain("selector: '#tab-avito-reviews'")
    expect(paritySource).not.toContain('AVITO_REVIEW_ROWS')
  })
})
