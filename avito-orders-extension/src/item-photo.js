if (!globalThis.__satornaAvitoItemPhotoLoaded) {
globalThis.__satornaAvitoItemPhotoLoaded = true

function absoluteUrl(value) {
  if (!value) return null
  const text = String(value)
    .replace(/\\u002F/g, '/')
    .replace(/\\\//g, '/')
    .replace(/&amp;/g, '&')
    .replace(/&quot;/g, '"')
  try {
    return new URL(text, globalThis.location?.href || 'https://www.avito.ru/').toString()
  } catch (_error) {
    return null
  }
}

function normalizeImageUrl(value) {
  const url = absoluteUrl(value)
  if (!url) return null
  try {
    const parsed = new URL(url)
    if (!/^https?:$/i.test(parsed.protocol)) return null
    const host = parsed.hostname.toLocaleLowerCase('en-US')
    const isAvitoImageHost = host === 'img.avito.ru' || host.endsWith('.img.avito.ru') || host.endsWith('.img.avito.st')
    if (!isAvitoImageHost) return null
    if (/\.(?:svg)(?:[?#]|$)/i.test(parsed.pathname)) return null
    return parsed.toString()
  } catch (_error) {
    return null
  }
}

function srcsetUrls(value) {
  return String(value || '')
    .split(',')
    .map((part) => part.trim().split(/\s+/)[0])
    .map(normalizeImageUrl)
    .filter(Boolean)
}

function imageSource(image) {
  const direct = [
    image?.currentSrc,
    image?.src,
    image?.getAttribute?.('src'),
    image?.getAttribute?.('data-src'),
    image?.getAttribute?.('data-url'),
  ].map(normalizeImageUrl).find(Boolean)
  if (direct) return direct

  const fromSrcset = [
    ...srcsetUrls(image?.getAttribute?.('srcset')),
    ...srcsetUrls(image?.getAttribute?.('data-srcset')),
  ]
  return fromSrcset[fromSrcset.length - 1] || null
}

function extractPayloadImages(root, limit = 10) {
  const result = []
  const seenObjects = new WeakSet()
  let visited = 0

  function add(value) {
    const url = normalizeImageUrl(value)
    if (url && !result.includes(url)) result.push(url)
  }

  function visit(value, depth) {
    if (result.length >= limit || value == null || depth > 14 || visited >= 12000) return
    if (typeof value === 'string') {
      add(value)
      const text = value.replace(/\\u002F/g, '/').replace(/\\\//g, '/').replace(/&amp;/g, '&').replace(/&quot;/g, '"')
      for (const match of text.matchAll(/https?:\/\/[^\s"'<>\\]+/giu)) add(match[0])
      return
    }
    if (typeof value !== 'object') return
    if (seenObjects.has(value)) return
    seenObjects.add(value)
    visited += 1

    if (Array.isArray(value)) {
      value.forEach((item) => visit(item, depth + 1))
      return
    }

    for (const key of ['imageUrl', 'image_url', 'photoUrl', 'photo_url', 'previewUrl', 'preview_url', 'url', 'src']) {
      if (key in value) add(value[key])
    }
    for (const key of ['image', 'photo', 'photos', 'images', 'pictures', 'gallery', 'sizes']) {
      if (key in value) visit(value[key], depth + 1)
    }
    for (const child of Object.values(value)) visit(child, depth + 1)
  }

  visit(root, 0)
  return result.slice(0, limit)
}

globalThis.SatornaAvitoItemPhoto = {
  imageSource,
  normalizeImageUrl,
  extractPayloadImages,
}
}
