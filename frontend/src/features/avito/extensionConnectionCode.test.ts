import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import { expect, it } from 'vitest'
import { encodeAvitoExtensionConnectionCode } from './extensionConnectionCode'

const source = readFileSync(new URL('../../../../avito-orders-extension/src/connection.js', import.meta.url), 'utf8')

it.each(['https://app.comcookie.store', 'http://127.0.0.1:5177', 'http://localhost:58017', 'http://[::1]:5177'])
  ('website envelope interoperates with the actual extension decoder for %s', origin => {
    const token = 'sat_avito_SYNTHETIC_ONLY_abc-123_xyz'
    const code = encodeAvitoExtensionConnectionCode(origin, token)
    expect(code).toMatch(/^satorna-connect-v1\.[A-Za-z0-9_-]+$/)
    const context = vm.createContext({ URL, atob, TextDecoder })
    vm.runInContext(source, context)
    expect(context.SatornaConnection.decodeCode(code)).toEqual({ backendUrl: origin, accessToken: token })
    expect(JSON.parse(Buffer.from(code.split('.')[1], 'base64url').toString('utf8'))).toEqual({ v: 1, origin, token })
  })
