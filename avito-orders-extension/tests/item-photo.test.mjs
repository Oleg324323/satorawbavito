import assert from 'node:assert/strict'
import test from 'node:test'

globalThis.location = { href: 'https://www.avito.ru/orders' }
delete globalThis.SatornaAvitoItemPhoto

await import(`../src/item-photo.js?red=${Date.now()}`)

test('picks an Avito CDN image from srcset before placeholders', () => {
  const image = {
    currentSrc: 'data:image/svg+xml;base64,placeholder',
    src: 'blob:https://www.avito.ru/avatar',
    getAttribute: (name) => ({
      srcset: 'data:image/svg+xml;base64,a 1x, https://80.img.avito.st/image/1.jpg 2x',
    })[name] || '',
  }

  assert.equal(
    globalThis.SatornaAvitoItemPhoto.imageSource(image),
    'https://80.img.avito.st/image/1.jpg',
  )
})

test('extracts product photos from Avito profile order payloads', () => {
  const urls = globalThis.SatornaAvitoItemPhoto.extractPayloadImages({
    result: {
      order: {
        item: {
          id: '8226657890',
          title: 'Футболка ERD Mulholland Drive',
          imageUrl: 'https://70.img.avito.st/image/1.jpg',
          images: [
            { url: 'data:image/svg+xml;base64,bad' },
            { previewUrl: 'https://90.img.avito.st/image/2.jpg' },
          ],
        },
      },
    },
  })

  assert.deepEqual(urls, [
    'https://70.img.avito.st/image/1.jpg',
    'https://90.img.avito.st/image/2.jpg',
  ])
})

test('extracts escaped Avito image URLs from serialized payload text', () => {
  const urls = globalThis.SatornaAvitoItemPhoto.extractPayloadImages(
    '{"imageUrl":"https:\\/\\/70.img.avito.st\\/image\\/escaped.jpg"}',
  )

  assert.deepEqual(urls, ['https://70.img.avito.st/image/escaped.jpg'])
})
