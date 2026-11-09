{
function messageText(message) {
  const value = message?.body?.text?.text
    ?? message?.content?.text
    ?? message?.text
    ?? (typeof message?.body?.text === 'string' ? message.body.text : '')
  return typeof value === 'string' ? value.replace(/\s+/g, ' ').trim() : ''
}

function redactMessage(text) {
  return String(text || '')
    .replace(/https?:\/\/\S+/giu, '[ссылка]')
    .replace(/[\w.+-]+@[\w.-]+\.[a-z]+/giu, '[email]')
    .replace(/(?:\+?\d[\s()\-]*){7,}/gu, '[телефон]')
    .replace(/\s+/g, ' ')
    .trim()
}

function messageRole(message, binding) {
  const author = String(message.authorId ?? message.author_id ?? message.senderId ?? message.sender?.id ?? message.author?.id ?? '')
  const direction = String(message.direction || '').toLowerCase()
  let role = ['out', 'outgoing', 'sent'].includes(direction) || message.isOutgoing === true ? 'seller'
    : ['in', 'incoming', 'received'].includes(direction) || message.isOutgoing === false ? 'buyer' : 'unknown'
  if (author && binding?.sellerId && [String(binding.sellerId), String(binding.sellerAuthorId || '')].includes(author)) role = 'seller'
  else if (author && binding?.buyerId && author === String(binding.buyerId)) role = 'buyer'
  else if (author && binding?.sellerId && binding?.buyerId) role = 'unknown'
  return role
}

function chatPage(payload) {
  if (!payload || payload.success === false || payload.error) return null
  const page = payload?.success && typeof payload.success === 'object' ? payload.success
    : payload?.result && typeof payload.result === 'object' ? payload.result : payload
  if (page?.success === false || page?.error || !Array.isArray(page?.messages)
      || page.messages.some(message => !message || typeof message !== 'object' || Array.isArray(message))) return null
  return { messages: page.messages, hasMore: page.hasMore === true || payload?.hasMore === true }
}

function normalizeChatMessages(payloads, limit = 50, maxChars = 4000, binding = {}) {
  const rows = []
  for (const payload of payloads || []) {
    const messages = chatPage(payload)?.messages || []
    for (const message of messages) {
      const text = redactMessage(messageText(message))
      if (!text) continue
      const rawCreated = message?.created || message?.createdAt || message?.timestamp || 0
      const numericCreated = Number(rawCreated)
      // Native messenger createdAt is epoch nanoseconds, not milliseconds.
      const epochMs = Number.isFinite(numericCreated)
        ? (numericCreated > 1e17 ? numericCreated / 1e6 : numericCreated > 1e14 ? numericCreated / 1000 : numericCreated > 1e11 ? numericCreated : numericCreated * 1000)
        : Date.parse(String(rawCreated))
      rows.push({
        id: String(message?.id || ''),
        created: Number.isFinite(epochMs) && Math.abs(epochMs) <= 8.64e15 ? epochMs : 0,
        text,
        role: messageRole(message, binding),
        quoted: message.quoted === true || message.type === 'quote' || /^\s*>/.test(messageText(message)),
        orderId: message.orderId ? String(message.orderId) : null,
        itemId: message.itemId ? String(message.itemId) : null,
      })
    }
  }
  rows.sort((left, right) => left.created - right.created)
  let records = rows.filter((row, index, all) => !row.id || all.findIndex(value => value.id === row.id) === index).slice(-Math.max(1, limit))
  // Keep this guard even if the relevance/budget filter drops a long message.
  const messageTextTruncated = records.some(row => row.text.length > 1000)
  // Do not let later shipping chatter consume the evidence budget and erase
  // an older size choice already present in this response.
  const evidenceTruncated = records.reduce((sum, row) => sum + row.text.length, 0) > maxChars
  if (evidenceTruncated || records.length > 50) {
    const relevant = records.filter(row => /размер|(?:^|\s)(?:\d{2,3}\s*[-–—]\s*\d{2,3}|[XХ]{1,4}[SСLЛ]|[2-5][XХ][LЛ]|[SСMМLЛ]|\d{2,3})(?:\s|[?!.,]|$)/iu.test(row.text))
    if (relevant.length) records = relevant
  }
  records = records.slice(-50)
  while (records.length > 1 && records.reduce((sum, row) => sum + row.text.length, 0) > maxChars) records.shift()
  records = records.map(row => ({ id: row.id, role: row.role, text: row.text.slice(0, 1000), quoted: row.quoted,
    orderId: row.orderId, itemId: row.itemId,
    createdAt: row.created > 0 ? new Date(row.created).toISOString() : null }))
  const unique = rows.filter((row, index, all) => (
    all.findIndex((value) => (
      (value.id && row.id && value.id === row.id) || value.text === row.text
    )) === index
  ))
  let messages = unique.slice(-Math.max(1, limit)).map((row) => row.text)
  let truncated = unique.length > messages.length
  while (messages.length > 1 && messages.join('\n').length > maxChars) {
    messages.shift()
    truncated = true
  }
  let chatText = messages.join('\n')
  if (chatText.length > maxChars) {
    chatText = chatText.slice(-maxChars)
    truncated = true
  }
  return {
    messages,
    records,
    chatText: chatText || null,
    rawCount: rows.length,
    retainedCount: messages.length,
    messageTextTruncated,
    truncated: truncated || evidenceTruncated || messageTextTruncated,
  }
}

async function loadOrderChat(channelIds, fetchApi = globalThis.fetch, binding = {}) {
  const payloads = []
  const requests = []
  let invalidResponse = false
  let historyLimit = 50
  const channels = Array.from(new Set(channelIds || []))
  const evidence = { ...binding, channelId: channels.length === 1 ? channels[0] : null,
    capturedAt: new Date().toISOString(), state: 'unavailable', reason: null, messages: [] }
  if (channels.length !== 1 || !binding.accountId || !binding.orderId || !binding.itemId) {
    return { requests, rawCount: 0, retainedCount: 0, chatEvidence: { ...evidence, reason: channels.length !== 1 ? 'multiple_or_missing_channels' : 'chat_identity_missing' } }
  }
  for (const channelId of channels) {
    // The native endpoint supports a bounded larger window. Read it only when
    // Avito explicitly reports older messages; no cursor or inbox scan guessed.
    for (const pageLimit of [50, 200]) {
    for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const response = await fetchApi('https://www.avito.ru/web/1/messenger/getUserVisibleMessages', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ channelId, limit: pageLimit, order: 0 }),
        signal: AbortSignal.timeout(15000),
      })
      requests.push({ channelId, status: Number(response?.status || 0) })
      if (response?.ok) {
        const payload = await response.json()
        if (chatPage(payload)) { payloads.splice(0, payloads.length, payload); historyLimit = pageLimit }
        else invalidResponse = true
        break
      }
      if ([401, 403, 429, 439].includes(Number(response?.status || 0))) break
    } catch (error) {
      requests.push({ channelId, status: 0 })
    }
    if (attempt === 0) await new Promise(resolve => setTimeout(resolve, 500))
    }
    if (!payloads.length || !chatPage(payloads[0])?.hasMore
        || requests.at(-1)?.status !== 200 || invalidResponse) break
    }
  }
  const normalized = normalizeChatMessages(payloads, historyLimit, 4000, binding)
  return { ...normalized, requests, chatEvidence: { ...evidence,
    state: payloads.length ? normalized.messageTextTruncated ? 'unavailable' : 'collected' : 'failed',
    reason: payloads.length ? normalized.messageTextTruncated ? 'chat_message_incomplete' : null : invalidResponse ? 'chat_response_invalid' : 'chat_collection_failed',
    // More than the bounded window still requires review; never call it full history.
    truncated: normalized.truncated || normalized.rawCount >= historyLimit || payloads.some(payload => chatPage(payload)?.hasMore), messages: normalized.records } }
}

function shouldCollectOrderChat(mode, channelIds) {
  return mode === 'chat_ai' && Array.isArray(channelIds) && channelIds.length > 0
}

globalThis.SatornaAvitoOrderChat = {
  normalizeChatMessages,
  loadOrderChat,
  shouldCollectOrderChat,
}
}
