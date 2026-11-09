import { execFileSync } from 'node:child_process'
import { readFile, writeFile, mkdir, cp } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { zipTree } from './zip.mjs'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const originArgument = process.argv.find(value => value.startsWith('--origin='))?.slice(9)
let origin = null
if (originArgument) {
  const url = new URL(originArgument)
  if (url.protocol !== 'https:' || url.username || url.password || url.pathname !== '/' || url.search || url.hash) {
    throw new Error('Use an HTTPS origin without path, credentials or parameters')
  }
  origin = url.origin
}
for (const extension of ['avito-orders-extension', 'wb-prices-extension']) {
  execFileSync(process.execPath, [resolve(root, extension, 'scripts/build.mjs')], { stdio: 'inherit' })
}
if (origin) {
  const dist = resolve(root, 'avito-orders-extension/dist')
  const manifest = JSON.parse(await readFile(resolve(dist, 'manifest.json'), 'utf8'))
  const bridge = manifest.content_scripts.find(entry => entry.js.includes('src/satorna-bridge.js'))
  if (!bridge) throw new Error('Avito application bridge is missing')
  // Bind the application bridge to this exact installation, never all websites.
  bridge.matches = [`${origin}/*`, 'http://localhost/*', 'http://127.0.0.1/*']
  await writeFile(resolve(dist, 'manifest.json'), JSON.stringify(manifest, null, 2) + '\n')
  const connectionPath = resolve(dist, 'src/connection.js')
  const connection = await readFile(connectionPath, 'utf8')
  const marker = "defaultUrl: 'https://app.comcookie.store'"
  if (connection.split(marker).length !== 2) throw new Error('Unexpected Avito default URL contract')
  await writeFile(connectionPath, connection.replace(marker, `defaultUrl: ${JSON.stringify(origin)}`))
  const wbPath = resolve(root, 'wb-prices-extension/dist/src/connection.js')
  const wbConnection = await readFile(wbPath, 'utf8')
  const wbMarker = "const DEFAULT_SERVER_URL = 'https://api.elfprint-system.ru'"
  if (wbConnection.split(wbMarker).length !== 2) throw new Error('Unexpected WB default URL contract')
  // Leave LEGACY_SERVER_URL intact: old tokens must never migrate to a new
  // package default without an explicit new connection from the owner.
  await writeFile(wbPath, wbConnection.replace(wbMarker, `const DEFAULT_SERVER_URL = ${JSON.stringify(origin)}`))
}
for (const [extension, name] of [['avito-orders-extension', 'satorna-avito-orders-extension.zip'],
                               ['wb-prices-extension', 'satorna-wb-prices-extension.zip']]) {
  const destination = resolve(root, 'frontend/public/downloads', name)
  await zipTree(resolve(root, extension, 'dist'), destination)
  if (process.argv.includes('--to-dist')) {
    await mkdir(resolve(root, 'frontend/dist/downloads'), { recursive: true })
    await cp(destination, resolve(root, 'frontend/dist/downloads', name))
  }
}
console.log('Packaged WB and Avito extensions without credentials or local data')
