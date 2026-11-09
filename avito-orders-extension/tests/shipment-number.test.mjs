import assert from 'node:assert/strict'
import test from 'node:test'

await import('../src/shipment-number.js')
const { extract } = globalThis.SatornaAvitoShipmentNumber

test('dispatch instruction survives bold line breaks and Avito nonbreaking spaces', () => {
  for (const separator of [' ', '\u00a0', '\u202f', '\u2009']) {
    assert.deepEqual(extract(`На месте назовите номер отправления:\n103${separator}286${separator}40390\nПосмотреть пункты приёма`), {
      number: '103 286 40390', state: 'confirmed',
    })
  }
  assert.equal(extract('Номер отправления: 001 234 5678\n89123456789').number, '001 234 5678')
  assert.equal(extract('номер отправления: 001 234 5678\nномер отправления: 0012345678').state, 'confirmed')
})

test('waits for delayed dispatch instruction, bounded without substituting tracking codes', () => {
  const { shouldWaitForInstruction } = globalThis.SatornaAvitoShipmentNumber
  assert.equal(shouldWaitForInstruction({ itemUrls: ['listing'], trackNumber: '12345' }, true, 100), true)
  assert.equal(shouldWaitForInstruction({}, true, 7999), true)
  assert.equal(shouldWaitForInstruction({}, true, 8000), false)
  assert.equal(shouldWaitForInstruction({}, false, 100), false)
  assert.equal(shouldWaitForInstruction({ shipmentNumber: '103 286 40390' }, true, 100), false)
  assert.equal(shouldWaitForInstruction({ shipmentNumberState: 'ambiguous' }, true, 100), false)
})

test('accepts only a labelled dispatch number in the order block', () => {
  assert.deepEqual(extract('Заказ 7000000048 Телефон 89123456789 Трек P06516051994 На месте назовите номер отправления: 001 526 8946'), {
    number: '001 526 8946', state: 'confirmed',
  })
  assert.deepEqual(extract('Заказ 7000000048 Трек P06516051994 Баркод 123456789'), { number: null, state: 'missing' })
  assert.deepEqual(extract('Номер отправления: AB-001 526 8946'), { number: 'AB-001 526 8946', state: 'confirmed' })
  assert.deepEqual(extract('Номер отправления: 00123 Номер отправления: 00987'), { number: null, state: 'ambiguous' })
})
