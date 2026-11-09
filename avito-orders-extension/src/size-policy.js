{
function applySizeEvidence(item, mode, explicitSize, chatText = null) {
  if (!item.sources || typeof item.sources !== 'object') item.sources = {}
  if (mode === 'chat_ai') {
    item.sizeMode = 'chat_ai'
    item.size = null
    delete item.sources.size
    item.descriptionSize = explicitSize || item.descriptionSize || null
    item.chatText = null
    return item
  }
  if (item.sizeMode === 'chat_ai') {
    item.size = null
    delete item.sources.size
    item.sizeState = null
    item.sizeReason = null
    item.sizeEvidence = {}
    item.sizeMode = mode
  }
  if (item.size && ['order_row', 'order_detail', 'chat_ai'].includes(item.sources.size)) return item
  delete item.sources.size
  if (mode === 'none') {
    item.size = null
    item.descriptionSize = null
    item.chatText = null
    return item
  }
  if (mode === 'description') {
    item.size = explicitSize || item.size || null
    delete item.descriptionSize
    delete item.chatText
    if (item.size) item.sources.size = 'description'
    return item
  }
  delete item.size
  item.descriptionSize = explicitSize || null
  item.chatText = chatText || null
  return item
}

globalThis.SatornaAvitoSizePolicy = { applySizeEvidence }
}
