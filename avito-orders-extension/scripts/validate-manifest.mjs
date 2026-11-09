import { readFile } from 'node:fs/promises'
import { resolve } from 'node:path'

const manifest = JSON.parse(await readFile(resolve('manifest.json'), 'utf8'))

if (manifest.manifest_version !== 3) {
  throw new Error('manifest_version must be 3')
}

for (const field of ['name', 'version', 'background', 'action']) {
  if (!manifest[field]) {
    throw new Error(`manifest is missing ${field}`)
  }
}

console.log('Manifest OK')
