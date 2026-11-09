import { readFileSync } from 'node:fs'
import { chromium } from 'playwright'
import { describe, expect, it } from 'vitest'

const html = readFileSync(new URL('../../../public/vella-production.html', import.meta.url), 'utf8')
type CompatibilityResult = {
  normalizedStatus: string; code: string; originalStatus: string; patchStatus: string
  retainedCode: string; isProblem: boolean; isReady: boolean; legacySelected: string
  newOptions: string[]; problem: string; desktopCopy: string; scopes: string; sticker: string
}

describe('shared Orders marking retirement compatibility', () => {
  it('removes new marking controls without promoting historical rows or erasing their codes', async () => {
    const browser = await chromium.launch({ headless: true })
    try {
      const page = await browser.newPage({ serviceWorkers: 'block' })
      const writes: string[] = []
      await page.route('**/*', async route => {
        const request = route.request()
        if (request.method() !== 'GET') writes.push(request.method())
        if (request.url() === 'http://satorna.test/orders' && request.method() === 'GET') {
          return route.fulfill({ contentType: 'text/html', body: html })
        }
        return route.abort()
      })
      await page.goto('http://satorna.test/orders', { waitUntil: 'domcontentloaded' })
      const result = await page.evaluate<CompatibilityResult>(`(() => {
        const original = { id: 'synthetic-legacy', jobNumber: 'JOB-1', source: 'wb',
          name: 'Synthetic shirt', size: 'M', color: 'white', sellerArticle: 'SKU-1',
          sticker: 'STICKER-1', barcode: 'BARCODE-1', honestSign: 'HISTORICAL-CODE',
          status: 'missing_honest_sign', quantity: 2 };
        const normalized = normalizeOrdersPickingRow(original, 0);
        ordersEditRowId = original.id;
        const table = document.createElement('table');
        table.id = 'compatibility-row';
        table.innerHTML = '<tbody>' + renderOrdersPrintRow(normalized) + '</tbody>';
        document.body.appendChild(table);
        const row = table.querySelector('tr');
        const patch = finalizeOrdersManualPatch(normalized, readOrdersRowEdit(row));
        const newStatus = document.createElement('div');
        newStatus.innerHTML = renderOrdersStatusSelect('ready');
        const form = document.createElement('div');
        form.id = 'compatibility-forms';
        form.innerHTML = renderOrdersManualSingleForm() + renderOrdersManualTableForm();
        document.body.appendChild(form);
        ensureDesktopFallback();
        openSettingsAccessDrawer('user-print-lead');
        return { normalizedStatus: normalized.status, code: normalized.honestSign,
          originalStatus: original.status, patchStatus: patch.status,
          retainedCode: ({ ...normalized, ...patch }).honestSign,
          isProblem: ordersMatchesQueueFilter(normalized, 'problem'),
          isReady: ordersMatchesQueueFilter(normalized, 'ready'),
          legacySelected: row.querySelector('[data-orders-edit-field="status"]').value,
          newOptions: Array.from(newStatus.querySelectorAll('option')).map(option => option.value),
          problem: ordersProblemCard(normalized),
          desktopCopy: document.getElementById('mobileFallbackCopy').textContent,
          scopes: document.getElementById('settingsAccessDrawerScopes').textContent,
          sticker: ordersStickerPrintMarkup({ ...original, sticker: null, barcode: null }, '58x40') };
      })()`)
      expect(result).toMatchObject({ normalizedStatus: 'missing_honest_sign', code: 'HISTORICAL-CODE',
        originalStatus: 'missing_honest_sign', patchStatus: 'missing_honest_sign', retainedCode: 'HISTORICAL-CODE',
        isProblem: true, isReady: false, legacySelected: 'missing_honest_sign' })
      expect(result.sticker).toContain('HISTORICAL-CODE')
      expect(result.sticker).toContain('SKU-1')
      expect(result.newOptions).not.toContain('missing_honest_sign')
      expect(result.problem).not.toContain('отдельного сервиса')
      expect(result.desktopCopy).not.toContain('КИЗ')
      expect(result.scopes).not.toContain('КИЗ')
      expect(result.scopes).toContain('Лист печати')
      expect(result.scopes).toContain('QR-возвраты')
      expect(await page.locator('#ordersPickingTable thead th').count()).toBe(15)
      const row = page.locator('#compatibility-row tbody tr')
      expect(await row.locator('td').count()).toBe(15)
      expect(await row.locator('[data-orders-edit-field="honestSign"]').count()).toBe(0)
      expect(await row.locator('[data-orders-edit-field="sticker"]').inputValue()).toBe('STICKER-1')
      expect(await row.locator('[data-orders-edit-field="barcode"]').inputValue()).toBe('BARCODE-1')
      const forms = page.locator('#compatibility-forms')
      expect(await forms.locator('[data-orders-manual-field="honestSign"], [data-orders-manual-table-field="honestSign"]').count()).toBe(0)
      expect(await forms.locator('[data-orders-manual-field="sticker"]').count()).toBe(1)
      expect(await forms.locator('thead th').count()).toBe(await forms.locator('tbody tr').first().locator('td').count())
      expect(writes).toEqual([])
    } finally { await browser.close() }
  }, 30_000)
})
