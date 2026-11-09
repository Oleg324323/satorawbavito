import assert from 'node:assert/strict'
import { readFile, readdir } from 'node:fs/promises'
import { spawnSync } from 'node:child_process'

const manifest = JSON.parse(await readFile('manifest.json', 'utf8'))
assert.equal(manifest.manifest_version, 3)
assert.deepEqual(manifest.permissions, ['storage', 'alarms'])
assert.deepEqual(manifest.host_permissions, ['https://www.wildberries.ru/*', 'https://card.wb.ru/*', 'https://api.elfprint-system.ru/*'])
assert.equal(manifest.externally_connectable, undefined)
assert.equal(manifest.web_accessible_resources, undefined)
for (const script of manifest.content_scripts) {
  assert.equal(script.run_at, 'document_start')
  assert.equal(script.all_frames, false)
  for (const file of script.js) {
    if (file === 'src/page-observer.js') { await readFile('src/contract.js'); await readFile('src/observer.js') }
    else await readFile(file)
  }
}
for (const file of await readdir('src')) {
  if (!file.endsWith('.js')) continue
  const result = spawnSync(process.execPath, ['--check', `src/${file}`], { stdio: 'inherit' })
  if (result.status !== 0) process.exit(result.status || 1)
}
console.log('Manifest scope and JavaScript syntax OK')
