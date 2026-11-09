{
function normalizeSize(value) {
  return String(value || '')
    .trim()
    .replace(/[–—]/g, '-')
    .replace(/\s*-\s*/g, '-')
    .replace(/\s+/g, ' ')
    .replace(/[a-z]+/gi, (part) => part.toUpperCase())
}

function parseExplicitListingSize(text) {
  const value = String(text || '')
  const pattern = /(?:Размер(?:\s+одежды)?|Size)\s*:\s*((?:\d{2}(?:\s*[-–—]\s*\d{2})?)(?:\s*\((?:[2-5]?XL|XXL|XS|[SML])\))?|(?:[2-5]?XL|XXL|XS|[SML]))(?=\s|$|[,.<])/iu
  const match = value.match(pattern)
  return match?.[1] ? normalizeSize(match[1]) : null
}

globalThis.SatornaAvitoItemSize = { parseExplicitListingSize }
}
