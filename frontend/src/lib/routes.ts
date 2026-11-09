export const ROUTES = {
  root: '/',

  wb: {
    repricer: '/wb/repricer',
    repricerSku: (articleId: string) => `/wb/repricer/sku/${articleId}`,
    repricerSkuPattern: '/wb/repricer/sku/:articleId',
    promotions: '/wb/promotions',
    liquidation: '/wb/liquidation',
    reports: '/wb/reports',
    reportsMonitor: '/wb/reports/monitor',
    reportsRules: '/wb/reports/rules',
    reportsAbc: '/wb/reports/abc',
    reportsRnp: '/wb/reports/rnp',
    reportsPnl: '/wb/reports/pnl',
    reportsAds: '/wb/reports/ads',
    reportsStock: '/wb/reports/stock',
    reportsWeekOverWeek: '/wb/reports/week-over-week',
    templates: '/wb/templates',
    reviews: '/wb/reviews',
    algorithm: '/wb/algorithm',
  },

  avito: {
    overview: '/avito',
    chats: '/avito/chats',
    chatBot: '/avito/chat-bot',
    orders: '/avito/orders',
    ordersArchive: '/avito/orders/archive',
    listings: '/avito/listings',
    listingsManage: '/avito/listings/manage',
    listingsPhotoGen: '/avito/listings/photo-gen',
    repricer: '/avito/repricer',
    reviews: '/avito/reviews',
    stats: '/avito/stats',
    wallets: '/avito/wallets',
  },

  orders: {
    root: '/orders',
    archive: '/orders/archive',
    returns: '/orders/returns',
  },

  settings: '/settings',
  help: '/help',

  internal: {
    vellaComponents: '/internal/vella-components',
    vellaPreview: {
      repricer: '/internal/vella-preview/repricer',
      repricerSku: (articleId: string) => `/internal/vella-preview/repricer/sku/${articleId}`,
      changelog: '/internal/vella-preview/repricer/changelog',
      promotions: '/internal/vella-preview/promotions',
      liquidation: '/internal/vella-preview/liquidation',
      reports: '/internal/vella-preview/reports',
      reportsRules: '/internal/vella-preview/reports/rules',
    },
  },

  wiki: {
    root: '/wiki',
    wbRepricer: '/wiki/wb-repricer',
    liquidation: '/wiki/liquidation',
    promotions: '/wiki/promotions',
    algorithm: '/wiki/algorithm',
    templates: '/wiki/templates',
  },
} as const
