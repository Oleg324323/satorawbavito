import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { buildSync, transformSync } from 'esbuild'
import { chromium } from 'playwright'
import { expect, test } from 'vitest'

test('compact calendar real clicks, manual input and Apply preserve the exact range', async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = buildSync({ stdin: { contents: `
    import React, {useState} from 'react'; import {createRoot} from 'react-dom/client';
    import {WbPeriodCalendar} from './src/features/wb-repricer/WbPeriodCalendar';
    function Fixture() {
      const [applied,setApplied] = useState({fromIso:'2026-10-01',toIso:'2026-10-06',label:'01.10 — 06.10'});
      return <><button className="period-btn" data-vella-react-handlers="onclick" onClick={()=>setApplied({fromIso:'2026-10-03',toIso:'2026-10-09',label:'7 дней'})}>7 дней</button>
        <WbPeriodCalendar applied={applied} minIso="2026-07-12" maxIso="2026-10-09"
          onApply={(fromIso,toIso)=>{window.appliedRange={fromIso,toIso};setApplied({fromIso,toIso,label:fromIso+' — '+toIso})}} /></>
    }
    createRoot(document.getElementById('root')).render(<Fixture/>);
  `, resolveDir: root, loader: 'tsx' }, bundle: true, write: false, format: 'iife', platform: 'browser',
    jsx: 'automatic', alias: { '@': path.join(root,'src') },
    define: { 'process.env.NODE_ENV':'"production"', 'import.meta.env.VITE_API_BASE_URL':'""' }, minify: true })
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH })
  try {
    const page = await browser.newPage({viewport:{width:1100,height:700}})
    const errors: string[] = []; page.on('pageerror', error => errors.push(error.message))
    await page.clock.install({ time: new Date('2026-10-10T09:00:00Z') })
    await page.setContent('<div class="vella-html-parity-root" data-wb-period-owner="react"><div id="root"></div></div>')
    const source = readFileSync(path.join(root,'src/features/vella-parity/VellaHtmlParityPage.tsx'),'utf8')
    const css = source.slice(source.indexOf('.vella-html-parity-root .products-calendar-field {'),
      source.indexOf('.vella-html-parity-root #subtabsContext [data-context-control="live-status"] {', source.indexOf('.vella-html-parity-root .products-calendar-field {')))
    await page.addStyleTag({content:css})
    await page.addScriptTag({content:result.outputFiles[0].text})
    await page.getByRole('button',{name:'Календарь данных',exact:true}).waitFor()
    const legacy = readFileSync(path.join(root,'public/vella-production.html'),'utf8')
    const bindingsStart = legacy.indexOf("  document.querySelectorAll('.period-btn').forEach(btn => {", legacy.indexOf('function ensureReadOnlyControls(){'))
    const bindings = legacy.slice(bindingsStart, legacy.indexOf("  const abc = document.getElementById('tab-abc');", bindingsStart))
    await page.addScriptTag({ content: "function setGlobalPeriod(){throw new Error('Legacy period owner must not handle WB controls')} function openCalendarPopover(){throw new Error('Wrong calendar opened')}\n" + bindings })
    await page.getByRole('button',{name:'Календарь данных',exact:true}).click()
    await page.getByRole('dialog',{name:'Календарь данных WB'}).waitFor()
    await page.getByRole('button',{name:'2026-10-07',exact:true}).click()
    await page.getByRole('button',{name:'2026-10-09',exact:true}).click()
    expect(await page.getByRole('button',{name:'С 2026-10-07',exact:true}).count()).toBe(1)
    expect(await page.getByRole('button',{name:'По 2026-10-09',exact:true}).count()).toBe(1)
    expect(await page.evaluate(() => (window as any).appliedRange)).toBeUndefined()
    expect(await page.getByRole('button',{name:'2026-10-11',exact:true}).isDisabled()).toBe(true)
    // Start boundary can be edited explicitly after selecting a complete range.
    await page.getByRole('button',{name:'С 2026-10-07',exact:true}).click()
    await page.getByRole('button',{name:'2026-10-02',exact:true}).click()
    await page.getByRole('button',{name:'По 2026-10-09',exact:true}).click()
    await page.getByRole('button',{name:'2026-10-08',exact:true}).click()
    await page.getByRole('button',{name:'Применить',exact:true}).click()
    expect(await page.evaluate(() => (window as any).appliedRange)).toEqual({fromIso:'2026-10-02',toIso:'2026-10-08'})
    expect(await page.getByRole('dialog').count()).toBe(0)
    await page.getByRole('button',{name:'Календарь данных',exact:true}).click()
    expect(await page.getByRole('button',{name:'С 2026-10-02',exact:true}).count()).toBe(1)
    await page.keyboard.press('Escape')
    expect(await page.getByRole('dialog').count()).toBe(0)
    await page.getByRole('textbox',{name:'Начало периода аналитики WB',exact:true}).fill('07.10.2026')
    await page.getByRole('textbox',{name:'Конец периода аналитики WB',exact:true}).fill('09.10.2026')
    await page.getByRole('button',{name:'Применить',exact:true}).click()
    expect(await page.evaluate(() => (window as any).appliedRange)).toEqual({fromIso:'2026-10-07',toIso:'2026-10-09'})
    await page.getByRole('textbox',{name:'Конец периода аналитики WB',exact:true}).fill('2026-10-11')
    await page.getByRole('button',{name:'Применить',exact:true}).click()
    expect(await page.getByRole('alert').count()).toBe(1)
    expect(await page.evaluate(() => (window as any).appliedRange)).toEqual({fromIso:'2026-10-07',toIso:'2026-10-09'})
    await page.getByRole('button',{name:'7 дней',exact:true}).click()
    await expect.poll(() => page.getByRole('textbox',{name:'Начало периода аналитики WB',exact:true}).inputValue()).toBe('2026-10-03')
    await expect.poll(() => page.getByRole('textbox',{name:'Конец периода аналитики WB',exact:true}).inputValue()).toBe('2026-10-09')
    await page.getByRole('textbox',{name:'Конец периода аналитики WB',exact:true}).fill('2026-10-11')
    await page.getByRole('button',{name:'7 дней',exact:true}).click()
    await expect.poll(() => page.getByRole('textbox',{name:'Конец периода аналитики WB',exact:true}).inputValue()).toBe('2026-10-09')
    expect(errors).toEqual([])
    transformSync(source,{loader:'tsx',target:'es2022'})
  } finally { await browser.close() }
}, 20_000)
