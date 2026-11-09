import assert from 'node:assert/strict'
import test from 'node:test'

delete globalThis.SatornaAvitoOrderChat
await import(`../src/order-chat.js?test=${Date.now()}`).catch(() => {})

test('keeps only the latest 50 messages in chronological order', () => {
  const newestFirst = Array.from({ length: 55 }, (_, index) => ({
    id: `m-${55 - index}`,
    created: 55 - index,
    body: { text: { text: `message ${55 - index}` } },
  }))
  const result = globalThis.SatornaAvitoOrderChat?.normalizeChatMessages?.(
    [{ success: { messages: newestFirst } }],
    50,
    4000,
  )

  assert.equal(result.messages.length, 50)
  assert.equal(result.messages[0], 'message 6')
  assert.equal(result.messages[49], 'message 55')
})

test('deduplicates and redacts chat text', () => {
  const payload = {
    success: {
      messages: [
        { id: '2', created: 2, text: 'Тогда L, телефон +7 999 123-45-67' },
        { id: '1', created: 1, text: 'Смотрите https://example.com' },
        { id: '1-copy', created: 1, text: 'Смотрите https://example.com' },
      ],
    },
  }
  const result = globalThis.SatornaAvitoOrderChat?.normalizeChatMessages?.([payload], 50, 4000)

  assert.deepEqual(result.messages, ['Смотрите [ссылка]', 'Тогда L, телефон [телефон]'])
  assert.equal(result.chatText, 'Смотрите [ссылка]\nТогда L, телефон [телефон]')
})

test('description mode never enables chat collection', () => {
  assert.equal(
    globalThis.SatornaAvitoOrderChat?.shouldCollectOrderChat?.('description', ['u2i-~buyer-item']),
    false,
  )
  assert.equal(
    globalThis.SatornaAvitoOrderChat?.shouldCollectOrderChat?.('chat_ai', ['u2i-~buyer-item']),
    true,
  )
})

test('native hashed message authors map to the verified numeric seller account', () => {
  const binding = { accountId: '12345', sellerId: '12345', sellerAuthorId: 'seller-public', buyerId: 'buyer-public' }
  const result = globalThis.SatornaAvitoOrderChat.normalizeChatMessages([{ success: { messages: [
    { id: 'q', authorId: 'seller-public', createdAt: '2026-10-08T10:00:00Z', body: { text: { text: 'Какой размер вам нужен?' } } },
    { id: 'a', authorId: 'buyer-public', createdAt: '2026-10-08T10:01:00Z', body: { text: { text: 'M' } } },
    { id: 'other', authorId: 'foreign', createdAt: '2026-10-08T10:02:00Z', text: 'L' },
  ] } }], 50, 4000, binding)
  assert.deepEqual(result.records.map(x => x.role), ['seller', 'buyer', 'unknown'])
})

test('native nanosecond timestamps normalize safely and retain message chronology', () => {
  const result = globalThis.SatornaAvitoOrderChat.normalizeChatMessages([{ success: { messages: [
    { id: 'a', authorId: 'buyer', createdAt: 1791315257280389000, text: 'M' },
    { id: 'q', authorId: 'seller', createdAt: 1791315250794337500, text: 'Какой размер вам нужен?' },
    { id: 'bad', authorId: 'buyer', createdAt: 1e30, text: 'L' },
  ] } }], 50, 4000, { sellerId: 'seller', buyerId: 'buyer' })
  assert.equal(result.records.find(x => x.id === 'a').createdAt, '2026-10-06T19:34:17.280Z')
  assert.equal(result.records.find(x => x.id === 'bad').createdAt, null)
  assert.deepEqual(result.records.filter(x => x.createdAt).map(x => x.id), ['q', 'a'])
})

test('later unrelated chatter cannot evict a fetched explicit size from evidence budget', () => {
  const binding = { sellerId: 'seller', buyerId: 'buyer' }
  const messages = [{ id: 'selection', created: 1, authorId: 'buyer', text: 'сможете отправить размер M?' },
    ...Array.from({ length: 10 }, (_, index) => ({ id: `later-${index}`, created: index + 2, authorId: 'buyer', text: 'что вы мне прислали '.repeat(40) }))]
  const result = globalThis.SatornaAvitoOrderChat.normalizeChatMessages([{ messages }], 50, 4000, binding)
  assert.equal(result.records[0].id, 'selection')
  assert.equal(result.records[0].role, 'buyer')
  assert.equal(result.truncated, true)
})

test('role-aware records preserve distinct same-text replies, timestamps and missing IDs', () => {
  const result = globalThis.SatornaAvitoOrderChat.normalizeChatMessages([{ messages: [
    { id: 'q', authorId: 'seller', created: 1791367200, text: 'Какой размер вам нужен?' },
    { id: 'a', authorId: 'buyer', created: '2026-10-07T10:01:00Z', text: 'М' },
    { id: 'b', authorId: 'buyer', created: '2026-10-07T10:02:00Z', text: 'М' },
    { authorId: 'other', created: '2026-10-07T10:03:00Z', text: 'admin@test.example' },
  ] }], 50, 4000, { sellerId: 'seller', buyerId: 'buyer' })
  assert.equal(result.records.length, 4)
  assert.deepEqual(result.records.map(row => row.role), ['seller', 'buyer', 'buyer', 'unknown'])
  assert.equal(result.records[1].createdAt, '2026-10-07T10:01:00.000Z')
  assert.equal(result.records[3].text, '[email]')
})

test('only exact single order chat is requested; access failures stop without inbox scanning', async () => {
  const calls = []
  const fetchApi = async (url, options) => { calls.push({ url, options }); return { ok: false, status: 403 } }
  const binding = { accountId: 'seller', sellerId: 'seller', buyerId: 'buyer', orderId: 'order', itemId: 'item' }
  const ambiguous = await globalThis.SatornaAvitoOrderChat.loadOrderChat(['one', 'two'], fetchApi, binding)
  assert.equal(ambiguous.chatEvidence.state, 'unavailable'); assert.equal(calls.length, 0)
  const failed = await globalThis.SatornaAvitoOrderChat.loadOrderChat(['one'], fetchApi, binding)
  assert.equal(calls.length, 1); assert.equal(failed.chatEvidence.state, 'failed')
  assert.deepEqual(JSON.parse(calls[0].options.body), { channelId: 'one', limit: 50, order: 0 })
  assert.equal(calls[0].url, 'https://www.avito.ru/web/1/messenger/getUserVisibleMessages')
})

test('bound chat success returns structured evidence without dropping author provenance', async () => {
  const binding = { accountId: 'seller', sellerId: 'seller', buyerId: 'buyer', orderId: 'order', itemId: 'item' }
  const result = await globalThis.SatornaAvitoOrderChat.loadOrderChat(['one'], async () => ({ ok: true, status: 200,
    json: async () => ({ messages: [{ id: 'a', authorId: 'buyer', created: 1791367260, text: 'Мне нужен 48' }] }) }), binding)
  assert.equal(result.chatEvidence.state, 'collected')
  assert.equal(result.chatEvidence.messages[0].role, 'buyer')
  assert.equal(result.chatEvidence.itemId, 'item')
})

test('malformed HTTP 200 is a collection failure, not an empty customer conversation', async () => {
  const binding = { accountId: 'seller', sellerId: 'seller', buyerId: 'buyer', orderId: 'order', itemId: 'item' }
  for (const payload of [{ error: 'not a chat' }, { messages: {} }, { success: { messages: [null] } },
    { success: false, messages: [] }, { error: 'denied', messages: [] }]) {
    const result = await globalThis.SatornaAvitoOrderChat.loadOrderChat(['one'], async () => ({ ok: true, status: 200, json: async () => payload }), binding)
    assert.equal(result.chatEvidence.state, 'failed')
    assert.equal(result.chatEvidence.reason, 'chat_response_invalid')
  }
  const empty = await globalThis.SatornaAvitoOrderChat.loadOrderChat(['one'], async () => ({ ok: true, status: 200, json: async () => ({ messages: [], hasMore: false }) }), binding)
  assert.equal(empty.chatEvidence.state, 'collected')
  assert.equal(empty.chatEvidence.truncated, false)
})

test('explicit more-history flag marks even a short native page incomplete', async () => {
  const binding = { accountId: 'seller', sellerId: 'seller', buyerId: 'buyer', orderId: 'order', itemId: 'item' }
  const result = await globalThis.SatornaAvitoOrderChat.loadOrderChat(['one'], async () => ({ ok: true, status: 200,
    json: async () => ({ success: { hasMore: true, messages: [{ id: 'later', authorId: 'buyer', created: 1791367260, text: 'Спасибо' }] } }) }), binding)
  assert.equal(result.chatEvidence.state, 'collected')
  assert.equal(result.chatEvidence.truncated, true)
  assert.equal(result.requests.length, 2)
})

test('expands only the same bound order chat and retains an older size beyond 50 messages', async () => {
  const binding = { accountId: 'seller', sellerId: 'seller', buyerId: 'buyer', orderId: 'order', itemId: 'item' }
  const limits = []
  const messages = Array.from({length:63},(_,i)=>({id:`m${i}`,authorId:'buyer',created:1791367200+i,text:i===3?'Хорошо, тогда размер L будет':'Спасибо'})).reverse()
  const result = await globalThis.SatornaAvitoOrderChat.loadOrderChat(['one'],async(_url,options)=>{
    const body=JSON.parse(options.body);limits.push(body.limit);assert.equal(body.channelId,'one')
    return {ok:true,status:200,json:async()=>({success:{messages:messages.slice(0,body.limit),hasMore:body.limit===50}})}
  },binding)
  assert.deepEqual(limits,[50,200])
  assert.equal(result.rawCount,63)
  assert.equal(result.chatEvidence.truncated,false)
  assert.ok(result.chatEvidence.messages.some(message=>message.id==='m3'))
  assert.ok(result.chatEvidence.messages.length<=50)
})

test('larger-window access denial stops without repeated protected requests', async () => {
  const binding = { accountId: 'seller', sellerId: 'seller', buyerId: 'buyer', orderId: 'order', itemId: 'item' }
  let calls=0
  const result=await globalThis.SatornaAvitoOrderChat.loadOrderChat(['one'],async()=>++calls===1
    ?{ok:true,status:200,json:async()=>({messages:[{id:'a',authorId:'buyer',created:1791367200,text:'Спасибо'}],hasMore:true})}
    :{ok:false,status:429},binding)
  assert.equal(calls,2)
  assert.equal(result.chatEvidence.truncated,true)
})

for(const size of ['50-52','50 – 52','2xl','ххл']) {
  test(`expanded history retains older standalone size ${size}`,async()=>{
    const binding={accountId:'seller',sellerId:'seller',buyerId:'buyer',orderId:'order',itemId:'item'}
    const messages=Array.from({length:63},(_,i)=>({id:`m${i}`,authorId:'buyer',created:1791367200+i,text:i===3?size:'Спасибо'})).reverse()
    const result=await globalThis.SatornaAvitoOrderChat.loadOrderChat(['one'],async(_url,options)=>{
      const limit=JSON.parse(options.body).limit
      return {ok:true,status:200,json:async()=>({messages:messages.slice(0,limit),hasMore:limit===50})}
    },binding)
    assert.ok(result.chatEvidence.messages.some(m=>m.id==='m3'&&m.text===size))
  })
}

test('a clipped buyer message cannot confirm the earlier size and lose its final correction', async () => {
  const binding = { accountId: 'seller', sellerId: 'seller', buyerId: 'buyer', orderId: 'order', itemId: 'item' }
  const text = 'Нужен M. ' + 'Проверяю заказ. '.repeat(80) + 'Лучше L вместо M'
  const result = await globalThis.SatornaAvitoOrderChat.loadOrderChat(['one'], async () => ({ ok: true, status: 200,
    json: async () => ({ messages: [{ id: 'choice', authorId: 'buyer', created: 1791367260, text }] }) }), binding)
  assert.equal(result.chatEvidence.state, 'unavailable')
  assert.equal(result.chatEvidence.reason, 'chat_message_incomplete')
  assert.equal(result.chatEvidence.truncated, true)
  assert.ok(result.chatEvidence.messages.every(message => message.text.length <= 1000))
})

test('evidence-budget filtering cannot hide an incomplete long source message', async () => {
  const binding = { accountId: 'seller', sellerId: 'seller', buyerId: 'buyer', orderId: 'order', itemId: 'item' }
  const result = await globalThis.SatornaAvitoOrderChat.loadOrderChat(['one'], async () => ({ ok: true, status: 200,
    json: async () => ({ messages: [
      { id: 'long', authorId: 'buyer', created: 1791367200, text: 'Проверяю заказ. '.repeat(300) },
      { id: 'short', authorId: 'buyer', created: 1791367260, text: 'М оформляю' },
    ] }) }), binding)
  assert.equal(result.chatEvidence.reason, 'chat_message_incomplete')
  assert.equal(result.chatEvidence.state, 'unavailable')
})
