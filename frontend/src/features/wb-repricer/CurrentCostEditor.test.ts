import { expect, test } from 'vitest'
import { parseCostRubles } from './CurrentCostEditor'

test.each([['350', 35000], ['350,50', 35050], ['350.50', 35050], ['1 350,05', 135005], ['0', 0], ['0,01', 1]])('exact rubles %s', (input, expected) => {
  expect(parseCostRubles(input as string)).toBe(expected)
})
test.each(['', '-1', 'NaN', 'Infinity', '1e3', '1.001', '1,2,3', '1 2', '9007199254740992'])('rejects %s', input => {
  expect(() => parseCostRubles(input)).toThrow()
})
