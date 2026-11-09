{
const strong = source => ['order_row', 'order_detail', 'chat_ai'].includes(source)
function reuse(order, saved = []) {
  const id = order.orderId || order.marketplaceId
  const matches = saved.filter(row => id && id === (row.orderId || row.marketplaceId)
    && (!order.accountId || !row.accountId || order.accountId === row.accountId))
  if (matches.length !== 1) return order
  const old = matches[0]
  for (const field of ['accountId', 'shipmentNumber', 'shipmentNumberState', 'dropoffProvider', 'jobNumber', 'pageUrl']) {
    if (!order[field] && old[field]) order[field] = old[field]
  }
  for (const item of order.items || []) {
    const candidates = (old.items || []).filter(value => item.itemId && value.itemId === item.itemId && value.lineIndex === item.lineIndex)
    if (candidates.length !== 1) continue
    const cached = candidates[0]
    item.sources ||= {}
    for (const field of ['imageUrl', 'imageUrls', 'itemUrl', 'size', 'color', 'descriptionSize', 'sellerArticle']) {
      // A chat choice must be freshly revalidated in chat mode; it must not
      // masquerade as a description-derived value after switching modes.
      if (field === 'size' && cached.sources?.size === 'chat_ai') continue
      const upgrade = ['size', 'color'].includes(field) && !strong(item.sources[field]) && strong(cached.sources?.[field])
      if ((!item[field] || upgrade) && cached[field]) {
        item[field] = cached[field]
        if (cached.sources?.[field]) item.sources[field] = cached.sources[field]
      }
    }
  }
  return order
}
function complete(order) {
  return Boolean(order.shipmentNumber && order.items?.length && order.items.every(item =>
    item.itemId && item.imageUrl && item.size && item.color && strong(item.sources?.size) && strong(item.sources?.color)))
}
globalThis.SatornaOrderCache = { reuse, complete }
}
