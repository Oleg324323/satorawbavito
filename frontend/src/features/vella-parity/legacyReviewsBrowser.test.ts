import { readFileSync } from 'node:fs'
import { chromium } from 'playwright'
import { expect, it } from 'vitest'
import type { WbReviewPageRow } from '../wb-reviews/api'

const reviews: WbReviewPageRow[] = [
  {
    id: 'synthetic-medium-review', brand: 'Anomie studio', rating: 5, topic: 'качество',
    risk: 'medium', status: 'pending_review', buyer: 'Synthetic buyer A', sku: 'SYNTHETIC-A',
    nm: '900001', product: 'Synthetic medium-risk hoodie', size: 'M', age: '1 минуту назад',
    media: 'Без медиа', text: 'Синтетический отзыв: пять звезд, но качество не понравилось.',
    reason: 'Позитивная оценка, но негативный текст', draft: 'Синтетический черновик для проверки.',
    audit: ['Синтетическая запись · Только браузерная проверка'],
  },
  {
    id: 'synthetic-low-review', brand: 'Anomie studio', rating: 5, topic: 'качество',
    risk: 'low', status: 'scheduled', buyer: 'Synthetic buyer B', sku: 'SYNTHETIC-B',
    nm: '900002', product: 'Synthetic low-risk shirt', size: 'L', age: '2 минуты назад',
    media: 'Без медиа', text: 'Синтетический положительный отзыв.',
    reason: 'Синтетический низкий риск', draft: 'Синтетический запланированный ответ.',
    audit: ['Синтетическая запись · Отправка не выполняется'],
  },
]

it('renders empty legacy Reviews, then preserves drawer guards and settings with two synthetic rows', async () => {
  // Unmodified shell and real legacy renderer/guard; this is not canonical/backend authorization proof.
  const html = readFileSync(new URL('../../../public/vella-production.html', import.meta.url), 'utf8')
  const browser = await chromium.launch({ headless: true })
  try {
    const page = await browser.newPage({ serviceWorkers: 'block', viewport: { width: 1512, height: 982 } })
    const unexpected: string[] = [], errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', route => {
      const request = route.request(), url = new URL(request.url())
      if (request.method() === 'GET' && url.origin === 'http://satorna.test' && url.pathname === '/vella-production.html') {
        return route.fulfill({ contentType: 'text/html', body: html })
      }
      if (request.method() === 'GET' && request.resourceType() === 'image') return route.fulfill({ contentType: 'image/gif', body: Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64') })
      if (request.method() === 'GET' && url.origin === 'https://fonts.googleapis.com' && url.pathname === '/css2') return route.fulfill({ contentType: 'text/css', body: '' })
      unexpected.push(`${request.method()} ${url.pathname}`)
      return route.abort()
    })
    await page.goto('http://satorna.test/vella-production.html?tab=reviews', { waitUntil: 'domcontentloaded' })
    await page.locator('#tab-reviews.active').waitFor()
    await page.locator('#reviewsEmpty.show').waitFor()
    expect(await page.locator('#reviewsTableBody tr').count()).toBe(0)
    expect(await page.locator('#reviewsQueueList .review-queue-item').count()).toBe(0)
    expect(await page.locator('#reviewsKpiAuto').innerText()).toBe('0')
    await page.evaluate(rows => {
      // REVIEWS is a lexical const array. Mutate only its data, not its bindings or consumer functions.
      window.eval(`REVIEWS.push(...${JSON.stringify(rows)}); renderReviews();`)
    }, reviews)
    expect(await page.locator('#reviewsTableBody tr').count()).toBe(2)
    expect(await page.locator('#reviewsQueueList .review-queue-item').count()).toBe(1)
    expect(await page.locator('#reviewsKpiAuto').innerText()).toBe('2')
    expect(await page.locator('#reviewsKpiQueue').innerText()).toBe('1')
    expect(await page.locator('#tab-reviews .stats').innerText()).toContain('Готовые черновики')
    expect(await page.locator('#tab-reviews .stat-delta').count()).toBe(0)
    await page.locator('[data-review-filter="status"][data-value="scheduled"]').click()
    expect(await page.locator('#reviewsTableBody tr').count()).toBe(1)
    expect(await page.locator('#reviewsKpiQueue').innerText()).toBe('0')
    expect(await page.locator('#reviewsQueueList .review-queue-item').count()).toBe(0)
    expect(await page.locator('#reviewsKpiAuto').innerText()).toBe('1')
    await page.locator('[data-review-filter="status"][data-value="all"]').click()
    expect(await page.locator('#reviewsEmpty.show').count()).toBe(0)
    expect(await page.locator('#tab-reviews').innerText()).toContain('Anomie studio')
    await page.locator('#reviewsQueueList .review-queue-item').click()
    await page.locator('#reviewDrawer.open').waitFor()
    expect(await page.locator('#reviewDrawerTitle').innerText()).toBe('Synthetic medium-risk hoodie')
    expect(await page.locator('#reviewDrawer').innerText()).toContain('Позитивная оценка, но негативный текст')
    expect(await page.locator('#reviewApprovalGuard').innerText()).toContain('Нужна проверка менеджера')
    expect(await page.locator('#reviewApproveBtn').isDisabled()).toBe(true)
    expect(await page.locator('#reviewSendNowBtn').isDisabled()).toBe(true)
    await page.evaluate(() => window.eval("openReviewDrawer('synthetic-low-review')"))
    expect(await page.locator('#reviewDrawerTitle').innerText()).toBe('Synthetic low-risk shirt')
    expect(await page.locator('#reviewApprovalGuard').innerText()).toContain('Низкий риск, правила пройдены')
    expect(await page.locator('#reviewSendNowBtn').isDisabled()).toBe(false)
    await page.keyboard.press('Escape')
    await page.waitForFunction(() => !document.querySelector('#reviewDrawer')?.classList.contains('open'))
    await page.locator('#tab-reviews').getByText('Настройки', { exact: true }).click()
    await page.locator('#m-reviewSettings.open').waitFor()
    const settings = await page.locator('#m-reviewSettings').innerText()
    expect(settings).toContain('Только черновики')
    expect(settings).toContain('Bless T')
    expect(settings).toContain('на подтверждении')
    expect(unexpected).toEqual([])
    expect(errors).toEqual([])
  } finally { await browser.close() }
}, 30_000)
