import test from 'node:test'
import assert from 'node:assert/strict'
import vm from 'node:vm'
import { readFileSync } from 'node:fs'
import '../src/return-details.js'

const source = readFileSync(new URL('../src/content.js', import.meta.url), 'utf8')
const context = vm.createContext({ SatornaAvitoReturnDetails: globalThis.SatornaAvitoReturnDetails })
vm.runInContext(source.slice(source.indexOf('function deliveryService('), source.indexOf('function itemUrl(')), context)

test('operational status is not inferred from a generic return mention', () => {
  for (const text of ['Завершён. Возможен возврат', 'Заказ отменён. Возврат средств', 'Возврат не оформлен']) assert.notEqual(context.statusFromText(text), 'on_return')
  for (const text of ['Подтвердите возврат', 'На возврате', 'Возврат: заберите заказ']) assert.equal(context.statusFromText(text), 'on_return')
  assert.equal(context.statusFromText('Ждут отправки'), 'ready_to_ship')
  assert.equal(context.statusFromText('В пути'), 'in_transit')
  for (const text of ['Ждёт выдачи покупателю', 'Ждет выдачи покупателю', 'Доставлен в пункт выдачи', 'Едет к покупателю']) assert.equal(context.statusFromText(text), 'in_transit')
  assert.equal(context.statusFromText('Выдан покупателю'), 'delivered')
  assert.equal(context.statusFromText('Выдан покупателю. В пути'), 'delivered')
  assert.notEqual(context.statusFromText('Ещё не выдан покупателю. Ждёт выдачи покупателю'), 'delivered')
  assert.equal(context.statusFromText('Отправьте заказ\nКогда заказ будет выдан покупателю, получите оплату'), 'ready_to_ship')
  assert.equal(context.statusFromText('Отправьте заказ\nВыдан покупателю'), 'ready_to_ship')
  assert.equal(context.statusFromText('Ждёт выдачи покупателю\nВыдан покупателю'), 'in_transit')
  assert.equal(context.statusFromText('Заказ ещё не завершён. Отправьте заказ'), 'ready_to_ship')
  assert.equal(context.statusFromText('Возврат: заберите заказ\nЗаказ получен'), 'on_return')
  assert.equal(context.statusFromText('Ждут подтверждения'), 'awaiting_confirmation')
})

test('native inbound return variants remain operational, not outgoing or terminal', () => {
  for (const text of ['Возврат: едет к вам', 'Возврат: едет к продавцу', 'Возврат: едет обратно', 'Возврат — в пути', 'Возврат: едет к вам\nСДЭК\nВыдан покупателю']) {
    assert.equal(context.statusFromText(text), 'on_return')
    assert.equal(globalThis.SatornaAvitoReturnDetails.extract(text).status, 'in_transit')
  }
  for (const text of ['Если возврат: едет к вам', 'После доставки возврат: едет к вам']) assert.notEqual(context.statusFromText(text), 'on_return')
  assert.equal(context.statusFromText('Возврат получен\nВозврат: едет к вам'), 'closed')
  assert.equal(context.statusFromText('Заказ отменён\nВозврат: едет к вам'), 'canceled')
})

test('actual row collector preserves status lines while normalizing product text', () => {
  const rowContext = vm.createContext({
    SatornaAvitoReturnDetails: globalThis.SatornaAvitoReturnDetails,
    SatornaAvitoShipmentNumber: { extract: () => ({ number: null, state: 'missing' }) },
    itemTitle: () => 'Synthetic', cleanFallbackTitle: () => 'Synthetic', itemUrl: () => null,
    extractAvitoItemIdFromRow: () => '000456', buildAvitoItemUrl: () => 'https://www.avito.ru/item_000456',
    itemIdFromUrl: () => '000456', orderIdFromLink: () => '000123',
    orderDetailsLink: () => null, trackNumber: () => null, parseKopecks: () => 0,
    absoluteUrl: value => value || null,
    location: { pathname: '/orders', href: 'https://www.avito.ru/orders' },
  })
  vm.runInContext(source.slice(source.indexOf('function textOf('), source.indexOf('function first('))
    +source.slice(source.indexOf('function deliveryService('), source.indexOf('function itemUrl('))
    +source.slice(source.indexOf('function collectOrder('), source.indexOf('function dedupeOrders(')), rowContext)
  for (const [text, expected] of [
    ['Возврат: едет к вам\nСДЭК\nВыдан покупателю', 'on_return'],
    ['Возврат получен\nСДЭК\nВозврат: едет к вам', 'closed'],
    ['Отправьте заказ\nВыдан покупателю', 'ready_to_ship'],
  ]) assert.equal(rowContext.collectOrder({ innerText: text }, { photoMode: 'none' }).status, expected)
})

test('dropoff provider requires an outbound instruction, never a return address', () => {
  for (const [text, expected] of [
    ['До 11 октября отнесите посылку в пункт Яндекс Доставки', 'Яндекс Доставка'],
    ['Сдайте посылку в пункт приема СДЭК', 'СДЭК'],
    ['Отнесите посылку в пункт приема Почта России', 'Почта России'],
    ['Отнесите посылку в отделение Почты России', 'Почта России'],
    ['Пункт приема\nСДЭК', 'СДЭК'],
  ]) assert.equal(context.dropoffProvider(text), expected)
  assert.equal(context.dropoffProvider('Место возврата: Яндекс Доставка'), null)
  assert.equal(context.dropoffProvider('Покупатель выбрал СДЭК'), null)
})

test('actual return instruction code stays text and needs return context', () => {
  const extract = globalThis.SatornaAvitoReturnDetails.extract
  const value = extract('Возврат: заберите заказ\nМесто возврата: Москва, Тестовая 2\nДо 15 октября включительно\nНазовите этот номер: 80510126542767')
  assert.equal(value.pickupCode, '80510126542767')
  assert.equal(value.pickupPlace, 'Москва, Тестовая 2')
  assert.equal(value.pickupDeadline, '15 октября')
  assert.equal(extract('Возврат\nНазовите этот номер: 000123').pickupCode, '000123')
  assert.equal(extract('Отправьте заказ\nНазовите этот номер: 000123').pickupCode, null)
  assert.equal(extract('Возврат\nЗаберите посылку в течение 7 календарных дней').pickupDeadline, null)
})
