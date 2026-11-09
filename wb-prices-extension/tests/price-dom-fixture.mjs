// Small DOM double: semantic class/tag matching, no browser or rendering jobs.
export function priceNode(className, text, visible = true, tag = 'span') {
  const node = { className, innerText: text, textContent: text, visible,
    getClientRects: () => visible ? [1] : [], querySelector: () => null }
  node.closest = selector => selector.split(',').some(part => {
    const s = part.trim()
    if (s.startsWith('.')) return className.split(' ').includes(s.slice(1))
    if (s === 'del' || s === 's') return s === tag
    if (s === '[class*="wallet"]') return className.includes('wallet')
    if (s === '[class*="Wallet"]') return className.includes('Wallet')
    return false
  }) ? node : null
  return node
}

export function priceBlock(nodes) {
  return { querySelectorAll: selector => nodes.filter(node => node.closest(selector)),
    getClientRects: () => [1] }
}
