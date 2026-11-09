import assert from 'node:assert/strict'
import test from 'node:test'

await import('../src/return-details.js')
const { extract } = globalThis.SatornaAvitoReturnDetails
const missingStates = { returnPickupPlace: 'unavailable', returnPickupDeadline: 'unavailable', returnPickupCode: 'unavailable' }

test('native return pickup block provides unlabelled address and verbatim relative deadline', () => {
  const text = 'Заберите посылку в течение 7 календарных дней\nНазовите этот номер: 80510126542767\nВозврат: заберите заказ\nПочта России\n805 101 265 42767\nПримерный срок доставки 13 октября\nТестовая область, город, Тестовая ул., 3\nГрафик работы: вт — пт: 9:00–18:00'
  const result = extract(text)
  assert.equal(result.pickupPlace, 'Тестовая область, город, Тестовая ул., 3')
  assert.equal(result.pickupDeadline, 'Заберите посылку в течение 7 календарных дней')
  assert.equal(result.pickupCode, '80510126542767')
  assert.equal(extract(text.replace('Почта России', 'Покупатель')).pickupPlace, null)
  assert.equal(extract(text.replace('Возврат: заберите заказ', 'Заказ')).pickupPlace, null)
  assert.equal(extract(text + '\nСДЭК\nДругая ул., 4\nГрафик работы: ежедневно').pickupPlace, null)
})

test('return readiness and code need explicit return context', () => {
  assert.deepEqual(extract('Возврат можно забрать. Код получения возврата: 001 234 567'), {
    status: 'ready_for_pickup', pickupCode: '001 234 567', pickupPlace: null, pickupDeadline: null,
    fieldStates: { ...missingStates, returnPickupCode: 'collected' },
  })
  assert.deepEqual(extract('Возврат едет обратно. Трек P06516051994'), { status: 'in_transit', pickupCode: null, pickupPlace: null, pickupDeadline: null, fieldStates: missingStates })
  assert.deepEqual(extract('Заказ можно забрать. Номер отправления: 512 526 8946'), { status: null, pickupCode: null, pickupPlace: null, pickupDeadline: null, fieldStates: missingStates })
})

test('return instruction fields retain exact text and reject conflicting addresses', () => {
  const details = extract('Возврат можно забрать\nМесто получения возврата: Москва, ул. Тестовая, 2\nСрок получения: 12.10.2026\nКод возврата: 001234')
  assert.equal(details.pickupPlace, 'Москва, ул. Тестовая, 2')
  assert.equal(details.pickupDeadline, '12.10.2026')
  assert.equal(details.pickupCode, '001234')
  assert.equal(extract('Возврат\nМесто получения возврата: А\nМесто получения возврата: Б').pickupPlace, null)
  assert.equal(extract('Возврат получен').status, 'received')
})

test('negated, future and conflicting instructions are not terminal or guessed codes', () => {
  for (const text of ['Возврат не получен', 'Возврат ещё не забран', 'Когда возврат получен, проверьте товар', 'Возврат будет получен']) {
    assert.notEqual(extract(text).status, 'received')
  }
  assert.equal(extract('Возврат можно забрать. Возврат получен').status, null)
  assert.equal(extract('Код возврата: 00123\nКод возврата: 00456').pickupCode, null)
  assert.equal(extract('Код возврата: 00123\nКод возврата: 00123').pickupCode, '00123')
  assert.equal(extract('Код возврата: 00123\nКод возврата: 00456').fieldStates.returnPickupCode, 'ambiguous')
  assert.equal(extract('Возврат\nМесто получения возврата\nМосква\nСрок получения\n12.10.2026\nКод возврата\n001234').pickupCode, '001234')
})

test('an empty multiline field cannot consume the next field heading', () => {
  for (const following of ['Срок получения возврата\n12.10.2026', 'Срок получения возврата: 12.10.2026']) {
    const result = extract(`Возврат\nМесто получения возврата\n${following}`)
    assert.equal(result.pickupPlace, null)
    assert.equal(result.fieldStates.returnPickupPlace, 'unavailable')
    assert.equal(result.pickupDeadline, '12.10.2026')
  }
})
