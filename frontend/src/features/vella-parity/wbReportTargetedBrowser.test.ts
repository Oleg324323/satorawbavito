import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { build } from 'vite'
import react from '@vitejs/plugin-react'
import { expect, it } from 'vitest'

it('pins native report product cells and keeps Week diagnostics collapsed with one vertical workspace', async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({ configFile: false, envFile: false, root, logLevel: 'silent', plugins: [react()],
    define: { 'process.env.NODE_ENV': '"test"', 'import.meta.env.VITE_API_BASE_URL': '""' },
    resolve: { alias: { '@': path.join(root, 'src') } },
    build: { write: false, minify: false, lib: { entry: fileURLToPath(new URL('./__fixtures__/wbReportTargetedBrowser.tsx', import.meta.url)), formats: ['iife'], name: 'WbTargeted' } } })
  const chunk = (Array.isArray(result) ? result : [result]).flatMap(item => 'output' in item ? item.output : []).find(item => item.type === 'chunk' && item.isEntry)
  if (!chunk || chunk.type !== 'chunk') throw new Error('Missing report fixture')
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const page = await browser.newPage({ viewport: { width: 900, height: 600 }, serviceWorkers: 'block' })
    const errors: string[] = [], unexpected: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', route => {
      if (route.request().resourceType() === 'document') return route.fulfill({ contentType: 'text/html', body: '<div id="root"></div>' })
      unexpected.push(route.request().url()); return route.abort()
    })
    await page.goto('http://satorna.test/')
    await page.addScriptTag({ content: chunk.code })
    await page.locator('#tab-week tbody tr').first().waitFor()
    for (const tab of ['rnp', 'pnl', 'week']) {
      const table = page.locator(`#tab-${tab} .report-table-wrap`)
      const measure = () => table.evaluate(node => ({ left: node.getBoundingClientRect().left,
        cell: node.querySelector('tbody td')!.getBoundingClientRect().left,
        head: node.querySelector('thead th')!.getBoundingClientRect().left,
        sticky: getComputedStyle(node.querySelector('tbody td')!).position,
        headerBackground: getComputedStyle(node.querySelector('thead th')!).backgroundColor }))
      await table.evaluate(node => { node.scrollLeft = 650 })
      const middle = await measure()
      expect(middle.sticky).toBe('sticky')
      expect(Math.abs(middle.cell - middle.left)).toBeLessThan(2)
      expect(Math.abs(middle.head - middle.left)).toBeLessThan(2)
      expect(middle.headerBackground).not.toBe('rgba(0, 0, 0, 0)')
      await table.evaluate(node => { node.scrollLeft = node.scrollWidth })
      const end = await measure()
      expect(Math.abs(end.cell - end.left)).toBeLessThan(2)
      expect(await table.evaluate(node => Math.abs(node.scrollWidth - node.clientWidth - node.scrollLeft))).toBeLessThan(2)
    }
    expect(await page.locator('#tab-rnp tbody tr').first().innerText()).toContain('Продажи 39 − возвраты 5')
    const details = page.locator('#tab-week .week-source-status details')
    expect(await details.getAttribute('open')).toBeNull()
    expect(await page.getByText('Сравнение неполное:', { exact: false }).isVisible()).toBe(false)
    await details.locator('summary').focus(); await page.keyboard.press('Enter')
    expect(await details.getAttribute('open')).not.toBeNull()
    expect(await details.locator('section[aria-label="Выбранный период"] li').count()).toBe(1)
    expect(await details.locator('section[aria-label="Предыдущий период"] li').count()).toBe(1)
    await details.locator('summary').press('Enter')
    const week = page.locator('#tab-week')
    const dimensions = await week.evaluate(node => ({ page: node.scrollHeight > node.clientHeight,
      table: node.querySelector('.report-table-wrap')!.scrollHeight - node.querySelector('.report-table-wrap')!.clientHeight }))
    expect(dimensions.page).toBe(true); expect(dimensions.table).toBeLessThan(2)
    await week.evaluate(node => { node.scrollTop = node.scrollHeight })
    expect(await week.evaluate(node => node.scrollTop)).toBeGreaterThan(0)
    expect(await week.getByText('Без привязки к товару: Реклама 175 554 ₽').count()).toBe(1)
    const costDetails = page.locator('#tab-pnl .pnl-cost-gaps')
    await costDetails.locator('summary').click()
    expect(await costDetails.innerText()).toContain('WB-товара с записью себестоимости')
    expect(await costDetails.innerText()).toContain('2026-10-12')
    expect(errors).toEqual([]); expect(unexpected).toEqual([])
  } finally { await browser.close() }
}, 60_000)
