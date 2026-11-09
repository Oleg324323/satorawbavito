# Satorna Avito Orders Collector

Chrome Manifest V3 extension for collecting Avito seller order details that are missing from the Avito Orders API.

## Local Install

1. Run `npm install`.
2. Run `npm run build`.
3. Open `chrome://extensions`.
4. Enable Developer mode.
5. Click Load unpacked.
6. Select `avito-orders-extension/dist`.
7. Open a logged-in Avito orders page.
8. Open `/avito/orders` in Satorna.
9. Click `Создать токен` or `Перегенерировать токен`.
10. Click «Скопировать код подключения». In extension Settings paste it into «Код подключения Satorna» and click «Подключить».
11. Choose what to collect: product photos, optional AI color/article extraction, and size from description or buyer-chat rules.
12. Click `Собрать всё`; the extension opens `https://www.avito.ru/orders` and collects operational orders, returns, chats, product photos and labels into Satorna. Optional retry tools are under diagnostics.

## Customer-chat size (0.2.9)

Select «Размер из чата с покупателем» in collection settings, then collect orders.
Only chats linked from operational order details are read (at most 50 recent
messages). The collector preserves message authors, IDs, times and exact
account/order/listing identity. It never scans the entire inbox or sends messages.
The backend validates the seller's size question or an explicit customer-initiated
selection (including «М оформляю»), and accepts the latest unambiguous buyer choice
using deterministic rules. Size parsing makes no AI requests and requires no API key.
The legacy `chat_ai` mode name is retained for compatibility with installed collectors;
confirmed evidence records `method=chat_rules`.

The normalized size and original answer are saved with provenance. Orders and
the existing XLSX Size column show confirmed values or a review reason; listing
sizes are never substituted in this mode. Unknown
message authors, missing dates, unavailable chat access, multiple-order/item
ambiguity require review rather than guesses.
Other collection modes and local/production backend configuration are unchanged.
Unexpected successful-HTTP chat payloads are reported as collection failures,
not as missing customer replies. Native `hasMore` marks incomplete history;
unverified older-page cursors are never guessed.

Default Satorna URL: `https://app.comcookie.store`. Legacy settings for retired production origins retain the existing migration. New codes always pin their exact origin, including the local port; they never use backend discovery or migrate to another server. Existing address/token pairs still work. Manual address and raw-token entry remain under «Дополнительные настройки», with an explicit address required.

## Connection code (0.2.21)

The website creates `satorna-connect-v1.<base64url(JSON)>` with `{ v: 1, origin, token }` immediately after the existing token-generation request. The clipboard and manual-copy field contain the same code. The full secret is not stored by the website and cannot be recovered from its displayed prefix. Base64URL is **not encryption**: treat the entire code as a password and never share it in logs or support messages.

The popup validates the envelope, exact origin and token before changing a saved connection. Remote destinations require HTTPS; HTTP is restricted to explicit loopback hosts. Unknown HTTPS origins require a separate destination confirmation before token transmission. Chrome host permission is requested only for that selected origin, using existing manifest permissions (not expanded).

Runtime permission requests follow the [Chrome permissions API](https://developer.chrome.com/docs/extensions/reference/api/permissions) user-gesture flow. Browser setup tests use the real popup files with synthetic Chrome storage/permission APIs; they do not verify a native permission prompt or live production authentication.

Connection verification uses the existing read-only `GET /api/v1/avito/orders/browser-collection-context` with only the original bearer token, no cookies and no redirects. It does not start collection. Rejected credentials, unsafe addresses or failed verification do not replace a working connection. A 404 from an older server saves configuration with an explicit unverified label; the first normal request validates authentication. There is no alternate-server fallback.

## Local Satorna

1. Build this copy with `npm run build`. In Chrome extensions, load its `dist` folder unpacked. The installed Web Store copy is not modified by this build; disable it while testing to avoid two collectors on one page.
2. Open `http://127.0.0.1:5177/avito/orders`, sign in, and open «Настройки расширения». Create a local extension token (not the Avito API key).
3. Copy the connection code from that local page, paste it into extension Settings and click «Подключить». The code includes `http://127.0.0.1:5177`; no production address is substituted. For legacy manual setup expand «Дополнительные настройки», enter the local address and token, and save the connection. Changing the manual address clears the previous token in the form. Credentials stay in local extension storage, not Chrome sync.
4. Choose photo/size/color/article collection, then run collection in your authorized Avito browser. Reload Satorna Orders when collection reports success.

The local frontend proxies `/api` to local backend `127.0.0.1:58017`; both must be running. Local delivery never falls back to the hosted platform and does not follow redirects. A production token is not a local token. Existing photos/fields can only appear if the collector finds them and the order identifiers match; collection does not change marketplace order statuses.

## What It Sends

The extension posts to:

```text
POST /api/v1/avito/orders/browser-snapshot
```

Payload shape:

```json
{
  "capturedAt": "2026-08-06T10:00:00.000Z",
  "pageUrl": "https://www.avito.ru/profile/orders",
  "orders": [
    {
      "orderId": "ord_1",
      "marketplaceId": "123456",
      "trackNumber": "TRACK-1",
      "buyerName": "Иван",
      "items": [
        {
          "itemId": "8098482225",
          "title": "Худи черный размер M",
          "itemUrl": "https://www.avito.ru/...",
          "imageUrl": "https://70.img.avito.st/...",
          "sellerArticle": "BT-42",
          "size": "M",
          "color": "черный"
        }
      ]
    }
  ]
}
```

## Notes

The collector prefers stable `data-marker`, link, image, and visible text selectors. It uses list pagination and label-generation controls, never shipping, publishing, or price-changing actions.
# Сбор с продолжением (версия 0.2.7)

Обычный лист подбора сначала проверяет свежесть сохранённых наблюдений (10 минут).
Если данные устарели, запускается существующий сбор через расширение и затем
повторная проверка БД. Если актуальность не подтверждена, устаревший файл не
скачивается. Адрес сайта и адрес в настройках расширения должны совпадать.
Лист возвратов экспортирует сохранённые данные без запуска сбора и открытия вкладок.
Вкладка «Все активные» включает и заказы, найденные расширением, даже когда их
нет в ответе API; более новый завершённый статус не заменяется старым активным.

Детали обрабатываются пятью параллельными задачами, после каждой пачки результат сохраняется. Готовые поля повторно берутся из БД; подтверждённый вариант заказа не заменяется характеристиками объявления. Ограничение Авито прекращает новые задачи. Незагруженный список не считается пустым и не стирает предыдущие данные.

`npm run build` обновляет `dist` и ZIP `frontend/public/downloads/satorna-avito-orders-extension.zip`. Сборка frontend также упаковывает актуальное расширение. Установка нового ZIP требует обновить расширение в Chrome; backend с новыми маршрутами должен быть развёрнут отдельно.

Обновите распакованное расширение и нажмите «Сохранить фото объявлений».
Репрайсер должен быть хотя бы один раз загружен в Satorna: список объявлений берётся из его сохранённых данных.
Расширение получает только активные объявления без сохранённой фотографии. В служебной вкладке проходит страницы списка Авито Pro и собирает фотографии пачками (включая CSS-миниатюры), привязывая их по ID объявления. Ожидает готовность карточек и пагинации, а не загрузку всех ресурсов страницы. Изображения скачиваются и сохраняются пятью параллельными обработчиками. Только недостающие фото ищет в отдельных объявлениях, не более пяти одновременно. Для заказов и возвратов фото сохраняются независимо от активности объявления.
В БД сохраняются сами изображения и миниатюры; таблица читает их из Satorna, без повторного обхода Авито.
Повторный запуск пропускает сохранённые снимки и продолжает недостающие. Закрывать popup можно.
Отдельные объявления без фото пропускаются без остановки всего сбора. При остановке браузера или проверке безопасности сохранённое не теряется. Проверка безопасности остаётся в открытой вкладке для ручного прохождения; затем запустите сбор ещё раз.
Кнопка «Остановить сбор фото» завершает сбор после текущей операции.
Фотографии привязаны к организации, аккаунту и ID объявления, не к названию товара.
