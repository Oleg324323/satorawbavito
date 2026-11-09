import { expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import { encodeWbExtensionConnectionCode } from './extensionConnectionCode'

const token = `sat_wbp_${'a'.repeat(43)}`
it.each(['https://app.comcookie.store', 'http://127.0.0.1:5177', 'http://localhost:58017'])('pins a WB key to its exact issuing origin %s', origin => {
  const code = encodeWbExtensionConnectionCode(origin, token)
  const payload = JSON.parse(atob(code.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')))
  expect(payload).toEqual({ v: 1, origin, token })
  expect(code).toMatch(/^satorna-connect-v1\.[A-Za-z0-9_-]+$/)
})
it.each(['http://remote.test', 'https://user:pass@host.test', 'https://host.test/api', 'https://host.test/?key=secret', 'https://host.test/#key'])('rejects an unsafe origin %s', origin => {
  expect(() => encodeWbExtensionConnectionCode(origin, token)).toThrow()
})
it('rejects unrelated keys rather than wrapping them as WB extension access', () => {
  for (const wrong of ['sat_avito_fake', 'WB_API_SECRET', 'sat_wbp_prefix…']) expect(() => encodeWbExtensionConnectionCode('https://app.comcookie.store', wrong)).toThrow()
})

it('the actual WB extension accepts the platform-issued code without a server input', () => {
  const context = vm.createContext({ URL, atob, Uint8Array, TextDecoder })
  vm.runInContext(readFileSync(new URL('../../../../wb-prices-extension/src/connection.js', import.meta.url), 'utf8'), context)
  for (const origin of ['https://app.comcookie.store', 'http://127.0.0.1:5177', 'http://localhost:58017']) {
    const issued = encodeWbExtensionConnectionCode(origin, token)
    const parsed = context.WbPricesConnection.parseInput(issued, 'https://unrelated.example')
    expect(parsed.backendUrl).toBe(origin)
    expect(parsed.token).toBe(token)
  }
  const popup = readFileSync(new URL('../../../../wb-prices-extension/src/popup.html', import.meta.url), 'utf8')
  expect(popup.match(/<input\b/g)).toHaveLength(1)
  expect(popup).not.toMatch(/id="backend-url"|Адрес сервера Satorna/)
})
