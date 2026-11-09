import assert from 'node:assert/strict'
import test from 'node:test'

globalThis.location = { href: 'https://www.avito.ru/orders' }
delete globalThis.SatornaAvitoItemPhoto
await import(`../src/item-photo.js?red=${Date.now()}`)

test('extracts native clickstream chat IDs with a tilde inside the ID', async () => {
  await import('../src/page-state.js')
  const channel = 'u2i-n~syntheticBuyerItem'
  const redirect = `ru.avito://1/channel/show?channelId=${channel}&isMiniMessenger=true`
  const wrapped = `ru.avito://1/clickstream?id=order_chat&redirect=${encodeURIComponent(redirect)}`
  assert.deepEqual(globalThis.SatornaAvitoPageState.extractChannelIds({messagesDeeplink: wrapped}), [channel])
  assert.deepEqual(globalThis.SatornaAvitoPageState.extractChannelIds({channelId: channel}), [channel])
  assert.deepEqual(globalThis.SatornaAvitoPageState.extractChannelIds({link: 'channelId=u2i-n%7EsyntheticBuyerItem'}), [channel])
  assert.deepEqual(globalThis.SatornaAvitoPageState.extractChannelIds({link: 'channelId=u2i-longPrefix%7EsyntheticBuyerItem'}), ['u2i-longPrefix~syntheticBuyerItem'])
})

test('order profile retries transient failures once, but never retries access restrictions', async () => {
  await import('../src/page-state.js')
  for (const status of [503, 403, 429]) {
    let attempts = 0
    const result = await globalThis.SatornaAvitoPageState.loadCandidatesFromOrderResources(
      ['https://www.avito.ru/web/2/profile/order?referenceID=70000000532427280'], '',
      async (_url, options) => {
        assert.ok(options.signal)
        attempts += 1
        return attempts === 1 ? { ok: false, status } : { ok: true, status: 200, json: async () => ({ channelId: 'u2i-synthetic' }) }
      },
    )
    assert.equal(attempts, status === 503 ? 2 : 1)
    assert.equal(result.requests.at(-1).status, status === 503 ? 200 : status)
  }
})

test('extracts unique order buyer/seller binding and rejects conflicting actors', async () => {
  await import(`../src/page-state.js?binding=${Date.now()}`)
  const extract = globalThis.SatornaAvitoPageState.extractChatBinding
  assert.deepEqual(extract([{ order: { buyer: { id: 123 }, seller: { userId: 456 } } }]), { buyerId: '123', sellerId: '456' })
  assert.deepEqual(extract([{ buyerId: 123, sellerId: 456 }, { buyerId: 789 }]), { buyerId: null, sellerId: '456' })
  assert.deepEqual(extract([{ buyer: { deeplink: 'ru.avito://1/user/profile?userKey=buyer-public' } }]), { buyerId: 'buyer-public', sellerId: null })
  assert.deepEqual(extract([{ buyer: { deeplink: 'https://foreign.example/user/profile?userKey=buyer-public' } }]), { buyerId: null, sellerId: null })
})

test('current numeric account and hashed chat author are read only from native bootstrap', async () => {
  await import(`../src/page-state.js?session=${Date.now()}`)
  const root = (user, layout = {}) => ({ scripts: [{ textContent: `window.__preloadedState__ = ${JSON.stringify(JSON.stringify({ user, layout }))};` }] })
  const read = globalThis.SatornaAvitoPageState.readSessionIdentity
  assert.deepEqual(read(root({ id: 12345, hashedId: 'seller-public' })), { accountId: '12345', authorId: 'seller-public' })
  assert.equal(read(root({ id: 12345, hashedId: 'seller-public', isEmployee: true })), null)
  assert.equal(read(root({ id: 12345, hashedId: 'seller-public' }, { accountHierarchy: { isEmployeeMode: true } })), null)
  assert.equal(read(root({ id: 12345 })), null)
})

test('finds an Avito item hidden in React order-detail props', async () => {
  delete globalThis.SatornaAvitoPageState
  await import(`../src/page-state.js?red=${Date.now()}`).catch(() => {})

  const state = {
    children: {
      order: {
        marketplaceId: '70000000486515763',
        goods: [{
          avitoId: '8098482225',
          title: 'Лонгслив y2k opium archive anime',
          url: '/voronezh/odezhda_obuv_aksessuary/longsliv_y2k_opium_archive_anime_8098482225',
        }],
      },
    },
  }

  const candidates = globalThis.SatornaAvitoPageState?.findCandidates(
    state,
    'Лонгслив y2k opium archive anime',
  )

  assert.deepEqual(candidates, [{
    itemId: '8098482225',
    itemUrl: 'https://www.avito.ru/voronezh/odezhda_obuv_aksessuary/longsliv_y2k_opium_archive_anime_8098482225',
    title: 'Лонгслив y2k opium archive anime',
    score: 180,
  }])
})

test('builds a listing URL when React state exposes only the Avito item id', async () => {
  const candidates = globalThis.SatornaAvitoPageState.findCandidates(
    {
      order: {
        id: '70000000486519208',
        product: {
          itemId: '8226657890',
          title: 'Футболка ERD Mulholland Drive',
        },
      },
    },
    'Футболка ERD Mulholland Drive',
  )

  assert.equal(
    candidates[0]?.itemUrl,
    'https://www.avito.ru/items/8226657890',
  )
})

test('returns unique channel ids with the listing candidate', async () => {
  const fetchApi = async () => ({
    ok: true,
    status: 200,
    json: async () => ({
      channel: { channelId: 'u2i-~buyer-item' },
      duplicateAction: 'avito://chat?channelId=u2i-~buyer-item&isMiniMessenger=true',
      widgets: [{
        payload: 'action://open?itemId=8098284629',
        quantity: 1,
        title: 'Лонгслив y2k opium archive anime',
      }],
    }),
  })

  const loaded = await globalThis.SatornaAvitoPageState.loadCandidateForOrderPage(
    'https://www.avito.ru/orders/70000000486515763?source=orders_list',
    'Лонгслив y2k opium archive anime',
    fetchApi,
    'Asia/Yekaterinburg',
  )

  assert.deepEqual(loaded.channelIds, ['u2i-~buyer-item'])
})

test('keeps only Avito order and item resource URLs for runtime diagnostics', () => {
  const resources = globalThis.SatornaAvitoPageState?.networkResourceUrls?.({
    getEntriesByType: () => [
      { name: 'https://www.avito.ru/assets/app.js' },
      { name: 'https://www.avito.ru/web/1/order/70000000486519208' },
      { name: 'https://www.avito.ru/api/18/items/8226657890' },
      { name: 'https://70.img.avito.st/image.jpg' },
    ],
  })

  assert.deepEqual(resources, [
    'https://www.avito.ru/web/1/order/70000000486519208',
    'https://www.avito.ru/api/18/items/8226657890',
  ])
})

test('finds an Avito item in a non-React component state attached to a DOM node', () => {
  const componentNode = {
    __vueParentComponent: {
      props: {
        order: {
          item: {
            avitoId: '8345678901',
            title: 'Худи google search',
          },
        },
      },
    },
  }
  const root = {
    documentElement: componentNode,
    body: null,
    querySelectorAll: () => [],
  }

  const candidates = globalThis.SatornaAvitoPageState.collectFromDocument(root, 'Худи google search')

  assert.equal(candidates[0]?.itemId, '8345678901')
  assert.equal(
    candidates[0]?.itemUrl,
    'https://www.avito.ru/items/8345678901',
  )
})

test('loads the ordered Avito item from the profile order JSON resource', async () => {
  const fetchApi = async (url) => ({
    ok: true,
    status: 200,
    url,
    json: async () => ({
      result: {
        order: {
          id: '70000000486519208',
          item: {
            id: '8226657890',
            title: 'Футболка ERD Mulholland Drive',
            description: 'Размер: M\nЦвет: черный',
          },
        },
      },
    }),
  })

  const loaded = await globalThis.SatornaAvitoPageState?.loadCandidatesFromOrderResources?.(
    ['https://www.avito.ru/web/2/profile/order?referenceID=70000000486519208'],
    'Футболка ERD Mulholland Drive',
    fetchApi,
  )

  assert.equal(loaded?.candidates[0]?.itemId, '8226657890')
  assert.equal(
    loaded?.candidates[0]?.itemUrl,
    'https://www.avito.ru/items/8226657890',
  )
  assert.deepEqual(loaded?.requests, [{
    url: 'https://www.avito.ru/web/2/profile/order?referenceID=70000000486519208',
    status: 200,
    candidates: 1,
  }])
  assert.match(loaded?.resourceText || '', /Размер:\s*M/)
  assert.match(loaded?.resourceText || '', /Цвет:\s*черный/)
})

test('keeps the product image from the profile order JSON candidate', async () => {
  const fetchApi = async (url) => ({
    ok: true,
    status: 200,
    url,
    json: async () => ({
      result: {
        order: {
          item: {
            id: '8226657890',
            title: 'Футболка ERD Mulholland Drive',
            imageUrl: 'https://70.img.avito.st/image/erd.jpg',
          },
        },
      },
    }),
  })

  const loaded = await globalThis.SatornaAvitoPageState?.loadCandidatesFromOrderResources?.(
    ['https://www.avito.ru/web/2/profile/order?referenceID=70000000486519208'],
    'Футболка ERD Mulholland Drive',
    fetchApi,
  )

  assert.equal(loaded?.candidates[0]?.imageUrl, 'https://70.img.avito.st/image/erd.jpg')
})

test('builds the profile order JSON URL when the performance entry is missing', () => {
  const resourceUrl = globalThis.SatornaAvitoPageState?.profileOrderResourceUrl?.(
    'https://www.avito.ru/orders/70000000486695926?source=orders_list',
    'Asia/Yekaterinburg',
  )

  assert.equal(
    resourceUrl,
    'https://www.avito.ru/web/2/profile/order?referenceID=70000000486695926&templateVersion=0&srcp=orders_list&location=Asia%2FYekaterinburg',
  )
})

test('rejects an unrelated Avito item id when its title does not match the order', () => {
  const candidates = globalThis.SatornaAvitoPageState?.findCandidates?.(
    {
      unrelatedListing: {
        id: '365024549',
        title: 'Bless T',
      },
    },
    'Лонгслив 1017 alyx 9sm',
  )

  assert.deepEqual(candidates, [])
})

test('binds itemId to the matching product block in the profile order payload', async () => {
  const fetchApi = async () => ({
    ok: true,
    status: 200,
    json: async () => ({
      unrelatedListing: {
        id: '365024549',
        title: 'Bless T',
      },
      widgets: [{
        payload: 'action://open?itemId=8226657890',
        quantity: 1,
        title: 'Футболка ERD Mulholland Drive',
      }],
    }),
  })

  const loaded = await globalThis.SatornaAvitoPageState?.loadCandidatesFromOrderResources?.(
    ['https://www.avito.ru/web/2/profile/order?referenceID=70000000486519208'],
    'Футболка ERD Mulholland Drive',
    fetchApi,
  )

  assert.equal(loaded?.candidates[0]?.itemId, '8226657890')
  assert.equal(loaded?.candidates[0]?.itemUrl, 'https://www.avito.ru/items/8226657890')
})

test('loads the listing candidate directly from an orders-list page', async () => {
  const requestedUrls = []
  const fetchApi = async (url) => {
    requestedUrls.push(url)
    return {
      ok: true,
      status: 200,
      json: async () => ({
        widgets: [{
          payload: 'action://open?itemId=8098284629',
          quantity: 1,
          title: 'Лонгслив y2k opium archive anime',
        }],
      }),
    }
  }

  const loaded = await globalThis.SatornaAvitoPageState?.loadCandidateForOrderPage?.(
    'https://www.avito.ru/orders/70000000486515763?source=orders_list',
    'Лонгслив y2k opium archive anime',
    fetchApi,
    'Asia/Yekaterinburg',
  )

  assert.deepEqual(requestedUrls, [
    'https://www.avito.ru/web/2/profile/order?referenceID=70000000486515763&templateVersion=0&srcp=orders_list&location=Asia%2FYekaterinburg',
  ])
  assert.equal(loaded?.candidate?.itemId, '8098284629')
  assert.equal(loaded?.candidate?.itemUrl, 'https://www.avito.ru/items/8098284629')
})
