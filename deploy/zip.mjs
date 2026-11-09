import { readFile, readdir, mkdir, writeFile, rename } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'

// Deterministic, dependency-free ZIP; only the supplied built extension tree.
export async function zipTree(root, destination) {
  const local = [], central = []
  let offset = 0, count = 0
  function crc32(bytes) {
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
      if (!entry.isFile()) throw new Error('Extension archive contains a non-regular file')
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
  await archive(root)
  const directory = Buffer.concat(central), end = Buffer.alloc(22)
  end.writeUInt32LE(0x06054b50); end.writeUInt16LE(count, 8); end.writeUInt16LE(count, 10)
  end.writeUInt32LE(directory.length, 12); end.writeUInt32LE(offset, 16)
  await mkdir(dirname(destination), { recursive: true })
  await writeFile(destination + '.tmp', Buffer.concat([...local, directory, end]))
  await rename(destination + '.tmp', destination)
}
