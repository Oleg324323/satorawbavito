// Test-only transport: actual worker + contract, all HTTP handled by TestClient.
// No browser, sockets, marketplace requests or installed extension are used.
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import readline from 'node:readline'
const lines = readline.createInterface({ input: process.stdin })[Symbol.asyncIterator]()
const receive = async () => JSON.parse((await lines.next()).value)
const emit = value => process.stdout.write(JSON.stringify(value) + '\n')
const initial = await receive()
let listener
const area = () => {
  const values = {}
  return { setAccessLevel: async () => {}, get: async () => structuredClone(values),
    set: async data => Object.assign(values, structuredClone(data)), remove: async key => { delete values[key] } }
}
const context = vm.createContext({ URL, URLSearchParams, Date, AbortSignal, setTimeout: () => 0, clearTimeout: () => {},
  chrome: { storage: { local: area(), session: area() }, permissions: { contains: async () => true },
    runtime: { id: 'test', onMessage: { addListener: fn => { listener = fn } }, onStartup: { addListener: () => {} } },
    alarms: { create: async () => {}, clear: async () => {}, onAlarm: { addListener: () => {} } },
    tabs: { onRemoved: { addListener: () => {} } } },
  fetch: async (url, options) => {
    emit({ kind: 'request', url, method: options.method || 'GET', headers: options.headers, body: options.body })
    const reply = await receive()
    return { ok: reply.status >= 200 && reply.status < 300, status: reply.status,
      headers: new Headers(reply.headers || {}), json: async () => reply.body }
  } })
context.importScripts = path => vm.runInContext(readFileSync(new URL(`../src/${path}`, import.meta.url), 'utf8'), context)
vm.runInContext(readFileSync(new URL('../src/background.js', import.meta.url), 'utf8'), context)
const command = value => new Promise(resolve => listener(value,
  { id: 'test', url: 'chrome-extension://test/src/popup.html' }, resolve))
const connected = await command({ type: 'connect', backendUrl: initial.backendUrl, token: initial.token })
if (!connected.ok) throw new Error('Synthetic connection did not succeed')
const collected = await command({ type: 'start' })
emit({ kind: 'done', connected: connected.ok, result: collected })
process.exit(collected.ok ? 0 : 1)
