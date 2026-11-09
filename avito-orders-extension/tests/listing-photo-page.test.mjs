import test from 'node:test'
import assert from 'node:assert/strict'
import vm from 'node:vm'
import { readFileSync } from 'node:fs'
const photoSource = readFileSync(new URL('../src/item-photo.js', import.meta.url), 'utf8')
const pageSource = readFileSync(new URL('../src/listing-photo-page.js', import.meta.url), 'utf8')
const cdn = 'https://70.img.avito.st/product.jpg'
function page({ title = 'Товар', url = 'https://www.avito.ru/item_123', nodes = {}, one = {} } = {}) {
  const context = vm.createContext({ URL, location: new URL(url), document: {
    title, querySelector: selector => one[selector] || null,
    querySelectorAll: selector => nodes[selector] || [],
  } })
  vm.runInContext(photoSource, context)
  vm.runInContext(pageSource, context)
  return context.SatornaListingPhotoPage
}
test('extracts rendered gallery when raw HTML meta is absent', () => {
  const gallerySelector = '[data-marker="item-view/gallery"] img, [data-marker="image-frame"] img, [data-marker="item-view/image"] img, [data-marker="image-frame/image"]'
  const result = page({ nodes: { [gallerySelector]: [{ currentSrc: cdn }] } }).read(['123'], '123')
  assert.equal(result.photos['123'], cdn)
})
test('structured per-item images do not borrow another product image', () => {
  const selector = 'script[type="application/json"], script[type="application/ld+json"], script#__NEXT_DATA__'
  const result = page({ nodes: { [selector]: [{ textContent: JSON.stringify({ items: [
    { id: 123, recommendations: [{ id: 124, images: [cdn] }] },
    { id: 125, images: ['https://80.img.avito.st/other.jpg'] },
  ] }) }] } }).read(['123', '125'])
  assert.equal(result.photos['123'], undefined)
  assert.equal(result.photos['125'], 'https://80.img.avito.st/other.jpg')
})
test('wrong detail page and security page cannot produce product photos', () => {
  const meta = 'meta[property="og:image"], meta[name="twitter:image"]'
  assert.equal(page({ url: 'https://www.avito.ru/item_124', nodes: { [meta]: [{ content: cdn }] } }).read(['123'], '123').photos['123'], undefined)
  assert.equal(page({ title: 'Доступ ограничен: проверка безопасности' }).read(['123'], '123').blocked, true)
})

test('Avito Pro sibling CSS thumbnail is matched to its own ID, not the next row', () => {
  const bg={style:{backgroundImage:'url("//b00.img.avito.st/image/product")'},closest:()=>null}
  const link={href:'https://www.avito.ru/item_123',querySelectorAll:()=>[]}
  link.parentElement={querySelectorAll:s=>s==='a[href]'?[link]:s==='[style]'?[bg]:[],parentElement:null}
  const result=page({nodes:{'a[href]':[link]}}).read(['123','124'])
  assert.equal(result.photos['123'],'https://b00.img.avito.st/image/product')
  assert.equal(result.photos['124'],undefined)
})

test('pagination only clicks known list navigation, never a listing action', () => {
  let clicks=0
  const next={getAttribute:k=>k==='aria-label'?'Следующая страница':null,click:()=>clicks++}
  assert.equal(page({url:'https://www.avito.ru/profile/pro/items',nodes:{button:[next]}}).advance(),true)
  assert.equal(clicks,1)
  assert.equal(page({url:'https://www.avito.ru/item_123',nodes:{button:[next]}}).advance(),false)
  next.disabled=true
  assert.equal(page({url:'https://www.avito.ru/profile/pro/items',nodes:{button:[next]}}).advance(),false)
})
