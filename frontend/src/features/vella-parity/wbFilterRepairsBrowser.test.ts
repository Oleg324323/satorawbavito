import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { writeFile } from 'node:fs/promises'
import { beforeAll, afterAll, expect, it } from 'vitest'
import { chromium, type Browser, type Page, type Route } from 'playwright'
import { build } from 'vite'
import react from '@vitejs/plugin-react'

let browser: Browser, code: string, css: string
beforeAll(async () => {
  const root = fileURLToPath(new URL('../../../', import.meta.url))
  const result = await build({configFile:false, envFile:false, root, publicDir:false, logLevel:'silent', plugins:[{
    name:'wb-filter-repair-auth-fixture',enforce:'pre',transform(source,id){
      if(id.endsWith('/__fixtures__/reportLoadingBrowser.tsx'))return source.replace('cabinetMe: null',"cabinetMe: ({organization:{organizationId:9},user:{userId:'me',permissions:[]}} as AuthContextValue['cabinetMe'])")
    },
  },react()],
    define:{'process.env.NODE_ENV':'"test"','import.meta.env.VITE_API_BASE_URL':'""'}, resolve:{alias:{'@':path.join(root,'src')}},
    build:{write:false,minify:false,lib:{entry:fileURLToPath(new URL('./__fixtures__/reportLoadingBrowser.tsx',import.meta.url)),formats:['iife'],name:'WbFilterRepair'}}})
  const output=(Array.isArray(result)?result:[result]).flatMap(r=>'output' in r?r.output:[])
  const entry=output.find(r=>r.type==='chunk'&&r.isEntry)
  if(!entry||entry.type!=='chunk')throw new Error('Missing regression bundle')
  code=entry.code;css=output.filter(r=>r.type==='asset'&&r.fileName.endsWith('.css')).map(r=>r.type==='asset'?String(r.source):'').join('\n')
  browser=await chromium.launch({headless:true})
},60000)
afterAll(async()=>{await browser?.close()})

type Handler=(route: Route,url: URL)=>Promise<unknown>|undefined
async function open(routePath:string,handler:Handler){
  const page=await browser.newPage({viewport:{width:1600,height:1000},serviceWorkers:'block'}),requests:Array<{path:string;method:string;query:string}>=[],errors:string[]=[]
  page.setDefaultTimeout(10000);await page.clock.setFixedTime(new Date('2026-11-01T09:00:00Z'))
  page.on('pageerror',e=>errors.push(e.message))
  await page.route('**/*',async route=>{const req=route.request(),u=new URL(req.url())
    if(req.resourceType()==='document')return route.fulfill({contentType:'text/html',body:'<div id="root"></div>'})
    if(req.resourceType()==='image')return route.fulfill({status:204})
    if(u.hostname==='fonts.googleapis.com')return route.fulfill({body:''})
    requests.push({path:u.pathname,method:req.method(),query:u.search})
    const pending=handler(route,u);if(pending!==undefined){await pending;return}
    await route.fulfill({json:{items:[],data:[],total:0,state:'completed',running:false,steps:[]}})
  })
  await page.goto('http://127.0.0.1:4199'+routePath)
  await page.evaluate(()=>{const p={days:7,mode:'custom' as const,fromIso:'2026-10-05',toIso:'2026-10-11',label:'7 дней'};window.__vellaReportPeriods={'repricer-stats':p,digest:p,abc:p,pnl:p,rnp:p,ads:p,stock:p,week:p};window.__vellaProductsPeriodDays=p.days;window.__vellaProductsPeriodMode=p.mode;window.__vellaProductsPeriodFromIso=p.fromIso;window.__vellaProductsPeriodToIso=p.toIso})
  if(css)await page.addStyleTag({content:css});await page.addScriptTag({content:code})
  return{page,requests,errors}
}
const product=(i:number)=>({meta:{articleId:`REPAIR-${i}`,nmId:990070000+i,name:i===2?'Unknown product':'Футболка женская white',subject:i===2?'Unknown':'Футболка',brand:'Alpha',managerId:'me',status:'manual',currentPriceKopecks:20001,basketsLast7d:0,basketNorm:1},
  settings:{cogsKopecks:100,pMinKopecks:100,minMarginPct:15,wbCommissionPct:15},analytics:{financeState:'ok',settlementFormulaVersion:'wb-final-payout-cogs-tax-v1',settlementProfitKopecks:i===2?null:0,revenueKopecks:10000,basketsState:'ok',baskets:i===151?100:0,periodStatsState:'ok',reportOrdersUnits:0,ordersUnits:0,stockState:'ok',wbStockUnits:5}})
const ids=(page:Page)=>page.locator('#mainTable tbody tr[data-sku]:visible').evaluateAll(ns=>ns.map(n=>(n as HTMLElement).dataset.sku))
async function diagnostic(page:Page,requests:unknown,errors:unknown){
  const data={url:page.url(),requests,errors,body:(await page.locator('body').innerText()).slice(0,5500),state:await page.evaluate(()=>({products:window.PRODUCTS?.length,cache:window.__vellaProductsCacheMeta,filters:window.__vellaProductsAdvancedSnapshot?.(),digest:window.__vellaDigestLiveDebug}))}
  return JSON.stringify(data)
}

it('Repricer sends applied advanced filters before pagination, reuses identical reads and preserves empty/clear',async()=>{
  const source=Array.from({length:151},(_,i)=>product(i+1))
  const {page,requests,errors}=await open('/wb/repricer',(route,u)=>{
    if(u.pathname!=='/api/v1/wb-repricer/sku')return
    const f=JSON.parse(u.searchParams.get('filters')||'{}'),p=Number(u.searchParams.get('page')||1)
    const selected=source.filter(r=>f.basketsFrom==null||r.analytics.baskets>=f.basketsFrom)
    return route.fulfill({json:{items:selected.slice((p-1)*150,p*150),total:selected.length,page:p,pageSize:150,summary:{skuCount:selected.length},cache:{pagesCached:2,totalCached:151,listTotalFiltered:selected.length,summaryScope:Object.keys(f).length?'filtered_skus':'catalog',serverFiltersApplied:true}}})
  })
  try{
    await page.locator('#mainTable tbody tr[data-sku]').first().waitFor()
    const trigger=page.locator('[data-vella-island="products-filter-chips"] button').filter({hasText:'Фильтры'}).first()
    await trigger.click();await page.locator('#advBasketsFrom').fill('100');await page.locator('#advBar').getByRole('button',{name:'Применить',exact:true}).click()
    await expect.poll(async()=>({ids:await ids(page),query:requests.filter(r=>r.path.endsWith('/sku')).at(-1)?.query,errors}),{timeout:10000}).toEqual({ids:['REPAIR-151'],query:expect.stringContaining('basketsFrom'),errors:[]})
    expect(new URLSearchParams(requests.filter(r=>r.path.endsWith('/sku')).at(-1)!.query).get('filters')).toContain('"basketsFrom":100')
    expect(await page.locator('#advBasketsFrom').inputValue()).toBe('100')
    const reads=requests.filter(r=>r.path.endsWith('/sku')).length
    await page.locator('#advBar').getByRole('button',{name:'Применить',exact:true}).click()
    await expect.poll(()=>ids(page)).toEqual(['REPAIR-151'])
    expect(requests.filter(r=>r.path.endsWith('/sku'))).toHaveLength(reads)
    await page.locator('#advBasketsFrom').fill('101');await page.locator('#advBar').getByRole('button',{name:'Применить',exact:true}).click()
    await expect.poll(async()=>({ids:await ids(page),query:requests.filter(r=>r.path.endsWith('/sku')).at(-1)?.query,errors}),{timeout:10000}).toEqual({ids:[],query:expect.stringContaining('101'),errors:[]})
    await page.locator('#advBar').getByRole('button',{name:'Очистить',exact:true}).click()
    await expect.poll(async()=>(await ids(page)).length).toBe(150)
    expect(errors).toEqual([])
  }catch(error){const text=await diagnostic(page,requests,errors);await writeFile(fileURLToPath(new URL('../../../../output/playwright/wb-filter-fix-main-diagnostic.json',import.meta.url)),text);throw error}finally{await page.close()}
},30000)

it('an existing period load publishes saved updates into the current filter without starting another collection',async()=>{
  let saved=false, starts=0
  const {page,requests,errors}=await open('/wb/repricer',(route,u)=>{
    if(u.pathname==='/api/v1/wb-repricer/sku'){
      const row=product(151);row.analytics.baskets=saved?120:100
      const f=JSON.parse(u.searchParams.get('filters')||'{}'),selected=f.basketsFrom>row.analytics.baskets?[]:[row]
      return route.fulfill({json:{items:selected,total:selected.length,summary:{skuCount:selected.length},cache:{pagesCached:1,totalCached:1,serverFiltersApplied:true,summaryScope:Object.keys(f).length?'filtered_skus':'catalog',
        periodStatsFetchedAt:'now',financeFetchedAt:'now',basketsFetchedAt:'now',adsFetchedAt:'now',financeFreshnessState:'fresh',adsDateFrom:'2026-10-05',adsDateTo:'2026-10-11',
        periodCoverage:{finance:{state:'ok'},orders:{state:'ok'},baskets:{state:saved?'ok':'partial'},ads:{state:'ok'}}}}})
    }
    if(u.pathname==='/api/v1/wb-repricer/sync/run'){starts++;return route.fulfill({json:{runId:'repair-existing-period',running:true}})}
    if(u.pathname==='/api/v1/wb-repricer/sync/status')return route.fulfill({json:{runId:'repair-existing-period',running:!saved,state:saved?'completed':'running',steps:[]}})
  })
  try{
    await expect.poll(()=>starts).toBe(1)
    await page.locator('[data-vella-island="products-filter-chips"] button').filter({hasText:'Фильтры'}).first().click()
    await page.locator('#advBasketsFrom').fill('110');await page.locator('#advBar').getByRole('button',{name:'Применить',exact:true}).click()
    await expect.poll(()=>ids(page)).toEqual([])
    saved=true
    await expect.poll(()=>ids(page),{timeout:10000}).toEqual(['REPAIR-151'])
    expect(starts).toBe(1)
    expect(new URLSearchParams(requests.filter(r=>r.path.endsWith('/sku')).at(-1)!.query).get('filters')).toContain('"basketsFrom":110')
    await page.locator('#searchTable').fill('REPAIR-151')
    await expect.poll(()=>new URLSearchParams(requests.filter(r=>r.path.endsWith('/sku')).at(-1)!.query).get('q')).toBe('REPAIR-151')
    expect(starts).toBe(1)
    expect(errors).toEqual([])
  }finally{await page.close()}
},30000)

it('statistics separates female/male and promotion unknown; invalid bounds preserve the last valid result',async()=>{
  const rows=['female','male','unknown'].map((gender,i)=>({articleId:`REPAIR-S${i}`,name:gender,brand:'Alpha',subject:'Футболка',promotionStatus:i===0?'yes':i===1?'no':null,
    metrics:{currentPriceKopecks:(i+1)*10000,marginPct:i===2?null:0,baskets:0,orders:0,stockUnits:5},priceProtection:{},sources:{},decision:{}}))
  const {page,errors}=await open('/wb/repricer/stats',(route,u)=>u.pathname==='/api/v1/wb-repricer/stats'?route.fulfill({json:{items:rows,total:3,summary:{skuCount:3},dateFrom:u.searchParams.get('dateFrom'),dateTo:u.searchParams.get('dateTo')}}):undefined)
  try{
    const body=page.locator('#repricerStatsBody'),panel=page.locator('#repricerStatsFilters'),trigger=page.locator('#repricerStatsFiltersTrigger')
    await body.locator('tr[data-article-id]').first().waitFor();await trigger.click()
    for(const value of ['female','white','black'])await panel.locator(`[data-stats-key="attrs"][data-stats-value="${value}"]`).click()
    await expect.poll(()=>body.locator('tr[data-article-id]:visible').getAttribute('data-article-id')).toBe('REPAIR-S1')
    await panel.locator('[data-stats-action="clear"]').click();await trigger.click();await panel.locator('[data-stats-key="promo"][data-stats-value="no"]').click()
    expect(await body.locator('tr[data-article-id]:visible').getAttribute('data-article-id')).toBe('REPAIR-S1')
    await panel.locator('[data-stats-action="clear"]').click();await trigger.click()
    await panel.getByLabel('Цена, ₽ от',{exact:true}).fill('200');await panel.getByLabel('Цена, ₽ от',{exact:true}).press('Tab')
    const before=await body.locator('tr[data-article-id]:visible').allTextContents()
    await panel.getByLabel('Цена, ₽ до',{exact:true}).fill('100');await panel.getByLabel('Цена, ₽ до',{exact:true}).press('Tab')
    expect(await panel.getByRole('alert').count()).toBe(1)
    expect(await panel.locator('[data-stats-action="apply"]').isDisabled()).toBe(true)
    expect(await body.locator('tr[data-article-id]:visible').allTextContents()).toEqual(before)
    await panel.getByLabel('Цена, ₽ до',{exact:true}).fill('200');await panel.getByLabel('Цена, ₽ до',{exact:true}).press('Tab')
    expect(await panel.getByRole('alert').count()).toBe(0)
    expect(await body.locator('tr[data-article-id]:visible').getAttribute('data-article-id')).toBe('REPAIR-S1')
    expect(errors).toEqual([])
  }finally{await page.close()}
},30000)

it('history keeps local source data, searches visible actions, and resets a three-page server query without stuck loading',async()=>{
  const source=Array.from({length:121},(_,i)=>({id:`repair-h${i}`,articleId:`REPAIR-H${i}`,skuName:`Product ${i}`,trigger:i%2?'manual':'algorithm',source:i%2?'manager':'system',timestamp:new Date(Date.parse('2026-10-31T12:00:00Z')+i*1000).toISOString(),oldPriceKopecks:10000,newPriceKopecks:11000,changePct:10,actor:{role:'system',name:'Synthetic'}}))
  const {page,requests,errors}=await open('/wb/repricer/changelog',(route,u)=>{
    if(!u.pathname.endsWith('/changelog'))return
    const selected=source.filter(r=>!u.searchParams.get('trigger')||r.trigger===u.searchParams.get('trigger')),p=Number(u.searchParams.get('page')||1)
    return route.fulfill({json:{items:selected.slice((p-1)*50,p*50),total:selected.length}})
  })
  try{
    const root=page.locator('#tab-history'),selects=root.locator('.hist-filters select'),items=root.locator('.hist-item'),more=root.getByRole('button',{name:'Загрузить ещё 50 событий',exact:true})
    await expect.poll(()=>items.count()).toBe(50)
    const count=requests.filter(r=>r.path.endsWith('/changelog')).length
    await selects.nth(1).selectOption('manager');await expect.poll(()=>items.count()).toBe(25)
    await selects.nth(1).selectOption('all');await expect.poll(()=>items.count()).toBe(50)
    expect(requests.filter(r=>r.path.endsWith('/changelog'))).toHaveLength(count)
    await root.getByPlaceholder('По артикулу, действию...').fill('вручную');await expect.poll(()=>items.count()).toBe(25)
    await root.getByPlaceholder('По артикулу, действию...').fill('');await more.click();await expect.poll(()=>items.count()).toBe(100)
    await more.click();await expect.poll(()=>items.count()).toBe(121)
    await selects.nth(0).selectOption('manual');await expect.poll(()=>items.count()).toBe(50)
    expect(await more.isEnabled()).toBe(true)
    await more.click();await expect.poll(()=>items.count()).toBe(60)
    expect(await more.isDisabled()).toBe(true)
    expect((await items.allTextContents()).every(t=>t.includes('вручную'))).toBe(true)
    expect(requests.filter(r=>r.query.includes('trigger=manual')).map(r=>new URLSearchParams(r.query).get('page'))).toEqual(['1','2'])
    expect(errors).toEqual([])
  }finally{await page.close()}
},30000)

it('all six shared panels retry through real mouse and keyboard activation without duplicate requests',async()=>{
  for(const tab of ['abc','pnl','rnp','ads','stock','week'])for(const keyboard of [false,true]){
    let fail=true,calls=0
    const routeName=tab==='week'?'week-over-week':tab
    const {page,errors,requests}=await open(`/wb/reports/${routeName}`,(route,u)=>{
      if(u.pathname.endsWith('/filter-metadata')){calls++;const b=route.request().postDataJSON();return fail?route.fulfill({status:503,json:{error:{message:'Synthetic unavailable'}}}):route.fulfill({json:{marketplaceAccountId:12,dateFrom:b.dateFrom,dateTo:b.dateTo,products:{}}})}
      if(u.pathname.endsWith('/latest-cache'))return route.fulfill({json:{rows:[{nmId:990070001,articleId:'REPAIR-1',revenueKopecks:100}],meta:{freshnessState:'fresh',sourceType:tab==='pnl'?'financial':'operational'},cache:{fresh:true},filters:{dateRange:{from:'2026-10-05',to:'2026-10-11'}},kpis:[],reportJob:null}})
    })
    try{
      await expect.poll(async()=>({present:await page.locator(`#${tab}-filters-trigger`).count(),errors,requests:requests.filter(r=>r.path.endsWith('/latest-cache'))}),{timeout:10000}).toEqual({present:1,errors:[],requests:expect.any(Array)})
      await page.locator(`#${tab}-filters-trigger`).click();const panel=page.locator(`#${tab}-product-filters`)
      await panel.getByRole('alert').waitFor();expect(calls).toBe(1);fail=false
      const retry=panel.getByRole('button',{name:'Повторить загрузку фильтров',exact:true})
      if(keyboard){await retry.focus();await retry.press('Enter')}else await retry.click()
      await expect.poll(()=>panel.getByRole('button',{name:'Применить',exact:true}).isEnabled()).toBe(true)
      expect(calls).toBe(2);expect(await panel.getByRole('alert').count()).toBe(0);expect(errors).toEqual([])
    }catch(error){await writeFile(fileURLToPath(new URL('../../../../output/playwright/wb-filter-fix-retry-diagnostic.json',import.meta.url)),await diagnostic(page,requests,errors));throw error}finally{await page.close()}
  }
},90000)

it('Sales Dynamics applies actual brands and ignores a late single-brand response after clearing or selecting another scope',async()=>{
  const rows=[{nmId:990070001,sku:'REPAIR-ALPHA',title:'Alpha item',salesDelta:1,ordersDelta:1},{nmId:990070002,sku:'REPAIR-BETA',title:'Beta item',salesDelta:2,ordersDelta:2}]
  let delayed:(()=>Promise<unknown>)|undefined
  const {page,requests,errors}=await open('/wb/reports/sales',(route,u)=>{
    if(!u.pathname.startsWith('/api/wb/reports/digest'))return
    const brands=u.searchParams.getAll('brand'),from=u.searchParams.get('from')!,to=u.searchParams.get('to')!
    const selected=rows.filter(r=>!brands.length||brands.includes(r.sku==='REPAIR-ALPHA'?'Alpha':'Beta'))
    const send=()=>route.fulfill({json:{availableBrands:['Alpha','Beta'],brandScope:brands.map(b=>b.toLowerCase()).sort(),meta:{freshnessState:'fresh',sourceType:'operational'},cache:{fresh:true},dateRange:{from,to},kpis:[],planFactRows:[],freshness:[],alerts:[],charts:[],problemRows:[],quickLinks:[],topMovers:{status:'ready',dateFrom:from,dateTo:to,comparisonFrom:'2026-09-28',comparisonTo:'2026-10-04',rows:selected}}})
    if(brands.length===1&&brands[0]==='Alpha'){return new Promise<void>(resolve=>{delayed=async()=>{await send().catch(()=>{});resolve()}})}
    return send()
  })
  try{
    await expect.poll(async()=>({present:await page.getByText('REPAIR-ALPHA',{exact:true}).count(),errors,requests:requests.filter(r=>r.path.includes('digest'))}),{timeout:10000}).toEqual({present:1,errors:[],requests:expect.any(Array)})
    const choose=async(value:string)=>{if(!await page.locator('#digestBrandFilter').evaluate(n=>n.classList.contains('open')))await page.locator('#digestBrandBtn').click();await page.locator(`#digestBrandFilter [data-brand-option="${value}"]`).click()}
    await choose('Alpha');await expect.poll(()=>Boolean(delayed)).toBe(true)
    await choose('Beta');await page.getByText('REPAIR-BETA',{exact:true}).waitFor()
    await choose('Alpha');await expect.poll(()=>page.locator('.sales-movers-table tbody tr[data-report-row]').count()).toBe(1)
    expect(await page.locator('.sales-movers-table tbody').innerText()).toContain('REPAIR-BETA')
    await page.locator('#globalPeriodRange').fill('2026-10-12');await page.locator('#globalPeriodRangeTo').fill('2026-10-18')
    await page.getByRole('button',{name:'Применить',exact:true}).first().click()
    await expect.poll(()=>page.evaluate(()=>window.__vellaDigestLiveReport?.dateRange?.from)).toBe('2026-10-12')
    await delayed?.();expect(await page.locator('.sales-movers-table tbody').innerText()).not.toContain('REPAIR-ALPHA')
    await choose('all');await expect.poll(()=>page.locator('.sales-movers-table tbody tr[data-report-row]').count()).toBe(2)
    expect(requests.filter(r=>r.method==='POST'&&/digest/.test(r.path))).toEqual([])
    expect(errors).toEqual([])
  }catch(error){await writeFile(fileURLToPath(new URL('../../../../output/playwright/wb-filter-fix-sales-diagnostic.json',import.meta.url)),await diagnostic(page,requests,errors));throw error}finally{await delayed?.();await page.close()}
},30000)

it('Sources type chips do not match unrelated action text and still intersect with search',async()=>{
  const {page,errors}=await open('/wb/sources',()=>undefined)
  try{
    const root=page.locator('#tab-sources'),chips=root.getByRole('group',{name:'Фильтры источников'})
    await root.getByText('Excel fallback',{exact:true}).waitFor();await chips.getByRole('button',{name:'1С',exact:true}).click()
    expect(await root.locator('tbody tr[data-report-row]:visible .source-open-btn .sku').allTextContents()).toEqual(['1С · Движение денежных средств','Правила статей ДДС'])
    await root.getByPlaceholder('Источник, отчёт или действие...').fill('Excel');expect(await root.locator('tbody tr[data-report-row]:visible').count()).toBe(0)
    await root.getByRole('button',{name:'Сбросить фильтры',exact:true}).click();expect(await root.locator('tbody tr[data-report-row]:visible').count()).toBe(9)
    expect(errors).toEqual([])
  }finally{await page.close()}
},30000)
