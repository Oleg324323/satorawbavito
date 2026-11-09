import { cp, mkdir, rm, readFile, readdir, writeFile, rename } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const dist = resolve(root, 'dist')

await rm(dist, { recursive: true, force: true })
await mkdir(dist, { recursive: true })
await cp(resolve(root, 'manifest.json'), resolve(dist, 'manifest.json'))
await cp(resolve(root, 'README.md'), resolve(dist, 'README.md'))
await cp(resolve(root, 'src'), resolve(dist, 'src'), { recursive: true })

// Dependency-free ZIP so Vercel builds publish the SAME files as unpacked builds.
// Stored entries are intentional: this small extension does not need compression.
const local = [], central = []
let offset = 0, count = 0
const crc32 = bytes => {
  let crc = 0xffffffff
  for (const byte of bytes) {
    crc ^= byte
    for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0)
  }
  return (crc ^ 0xffffffff) >>> 0
}
async function archive(directory, prefix = '') {
  for (const entry of (await readdir(directory, { withFileTypes: true })).sort((a, b) => a.name.localeCompare(b.name))) {
    const name = prefix + entry.name
    if (entry.isDirectory()) { await archive(resolve(directory, entry.name), name + '/'); continue }
    const bytes = await readFile(resolve(directory, entry.name)), filename = Buffer.from(name)
    const header = Buffer.alloc(30), index = Buffer.alloc(46), checksum = crc32(bytes)
    header.writeUInt32LE(0x04034b50); header.writeUInt16LE(20, 4); header.writeUInt16LE(0x800, 6)
    header.writeUInt32LE(checksum, 14); header.writeUInt32LE(bytes.length, 18); header.writeUInt32LE(bytes.length, 22)
    header.writeUInt16LE(filename.length, 26)
    index.writeUInt32LE(0x02014b50); index.writeUInt16LE(20, 4); index.writeUInt16LE(20, 6); index.writeUInt16LE(0x800, 8)
    index.writeUInt32LE(checksum, 16); index.writeUInt32LE(bytes.length, 20); index.writeUInt32LE(bytes.length, 24)
    index.writeUInt16LE(filename.length, 28); index.writeUInt32LE(offset, 42)
    local.push(header, filename, bytes); central.push(index, filename)
    offset += header.length + filename.length + bytes.length; count++
  }
}
await archive(dist)
const directory = Buffer.concat(central), end = Buffer.alloc(22)
end.writeUInt32LE(0x06054b50); end.writeUInt16LE(count, 8); end.writeUInt16LE(count, 10)
end.writeUInt32LE(directory.length, 12); end.writeUInt32LE(offset, 16)
const download = resolve(root, '../frontend/public/downloads/satorna-avito-orders-extension.zip')
await mkdir(dirname(download), { recursive: true })
await writeFile(download + '.tmp', Buffer.concat([...local, directory, end]))
await rename(download + '.tmp', download)

console.log(`Built extension to ${dist}`)
