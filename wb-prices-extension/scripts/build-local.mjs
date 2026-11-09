import assert from 'node:assert/strict'
import { cp, mkdir, mkdtemp, readFile, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

// Separate, installable local artifact. Never overwrite production dist or an
// already-installed local folder unless --update validates it and backs it up.
// No credentials are included in this build.
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const destination = resolve(root, 'dist-local')
const productionApi = 'https://api.elfprint-system.ru/api/v1/wb/browser-prices'
const localApi = 'http://127.0.0.1:58017/api/v1/wb/browser-prices'
const productionOrigin = 'https://api.elfprint-system.ru'
const localOrigin = 'http://127.0.0.1:58017'
const manifest = JSON.parse(await readFile(resolve(root, 'manifest.json'), 'utf8'))
const background = await readFile(resolve(root, 'src/background.js'), 'utf8')
const connection = await readFile(resolve(root, 'src/connection.js'), 'utf8')
assert.equal(connection.split(`const DEFAULT_SERVER_URL = '${productionOrigin}'`).length, 2)
manifest.name = 'Satorna — цены WB (LOCAL)'
manifest.description = 'Локальная проверка цен WB: только Satorna на этом компьютере, API 127.0.0.1:58017.'
manifest.action.default_title = 'Satorna — цены WB (LOCAL)'
manifest.host_permissions = ['https://www.wildberries.ru/*', 'https://card.wb.ru/*', 'http://127.0.0.1/*']
manifest.optional_host_permissions = ['http://127.0.0.1/*', 'http://localhost/*']
if (process.argv.includes('--update')) {
  const installed = JSON.parse(await readFile(resolve(destination, 'manifest.json'), 'utf8'))
  const installedBackground = await readFile(resolve(destination, 'src/background.js'), 'utf8')
  assert.equal(installed.name, 'Satorna — цены WB (LOCAL)')
  assert.deepEqual(installed.host_permissions, manifest.host_permissions)
  if (!installedBackground.includes(`const API = '${localApi}'`)) {
    const installedConnection = await readFile(resolve(destination, 'src/connection.js'), 'utf8')
    assert.ok(installedConnection.includes(`const DEFAULT_SERVER_URL = '${localOrigin}'`)
      && installedConnection.includes('const ALLOW_LOCAL_SERVER = true'))
  }
  assert.ok(!installedBackground.includes(productionApi))
  const backups = resolve(root, 'output/artifact-backups')
  await mkdir(backups, { recursive: true })
  const backup = await mkdtemp(resolve(backups, `local-${installed.version}-`))
  await cp(destination, backup, { recursive: true })
  console.log(`Previous local artifact backed up: ${backup}`)
} else await mkdir(destination) // EEXIST prevents accidental replacement.
await cp(resolve(root, 'src'), resolve(destination, 'src'), { recursive: true, filter: source => !source.endsWith('/observer.js') })
await writeFile(resolve(destination, 'manifest.json'), `${JSON.stringify(manifest, null, 2)}\n`)
await writeFile(resolve(destination, 'src/connection.js'), connection
  .replace(`const DEFAULT_SERVER_URL = '${productionOrigin}'`, `const DEFAULT_SERVER_URL = '${localOrigin}'`)
  .replace(`const LEGACY_SERVER_URL = '${productionOrigin}'`, `const LEGACY_SERVER_URL = '${localOrigin}'`)
  .replace('const ALLOW_LOCAL_SERVER = false', 'const ALLOW_LOCAL_SERVER = true')
  .replace('const LOCAL_ONLY = false', 'const LOCAL_ONLY = true'))
const main = await Promise.all(['contract.js', 'observer.js'].map(file => readFile(resolve(root, 'src', file), 'utf8')))
await writeFile(resolve(destination, 'src/page-observer.js'), main.join('\n;\n'))
const popup = await readFile(resolve(root, 'src/popup.html'), 'utf8')
await writeFile(resolve(destination, 'src/popup.html'), popup
  .replace('<title>Satorna — цены WB</title>', '<title>Satorna — цены WB (LOCAL)</title>')
  .replace('<h1>Цены Wildberries</h1>', '<h1>Цены Wildberries · LOCAL</h1>')
  .replace('<p class="intro">Наблюдаемые цены покупателя из обычных страниц WB.</p>',
    '<p class="intro"><b>Локальная проверка.</b> Данные сохраняются только на этом компьютере: 127.0.0.1:58017. Используйте ключ из локальной Satorna, не рабочий ключ.</p>'))
await writeFile(resolve(destination, 'README.md'), `# Satorna — цены WB (LOCAL)\n
Готовая папка для «Загрузить распакованное расширение» в chrome://extensions.
Не удаляйте и не перемещайте эту папку после установки.

Приложение: http://127.0.0.1:5177
API расширения: ${localApi}
Используйте только отдельный ключ расширения из локального приложения.
Ключи и cookies в сборку не включены. Рабочий сервер Satorna не используется.
Расширение читает публичные цены WB и записывает наблюдения в локальную Satorna;
оно не изменяет цены WB. Кошелёк и отсутствующие размеры не выдумываются.
Необходимы работающий локальный сервер и разрешённый доступ расширения к WB.
Для остановки нажмите «Остановить». Для удаления доступа используйте
«Забыть ключ» в расширении и «Отключить» в локальной Satorna.
`)
for (const entry of manifest.content_scripts) for (const file of entry.js) await readFile(resolve(destination, file))
assert.ok(!(await readFile(resolve(destination, 'src/background.js'), 'utf8')).includes(productionApi))
console.log(`Built local-only extension: ${destination}`)
