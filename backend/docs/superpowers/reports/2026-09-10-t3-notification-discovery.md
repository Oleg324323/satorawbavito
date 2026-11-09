# T3: Discovery Review-уведомлений

Закрывает конкретный разрыв: существующий API требовал заранее известных event IDs.
Это список существующих account-scoped Review events 0074, не универсальный inbox
и не завершение всех исходных этапов Notifications/Orders/Production.

## Подключение

Factory `app.notification_list_http.make_review_notification_list_router` принимает
`service_dependency` и `cursor_codec_dependency`. Первый возвращает существующий
`ReviewNotificationService` с явным default-empty org/account/provider allowlist;
второй `NotificationListCursorCodec` с purpose-separated ключом минимум 32 bytes.
Main, default-off configuration и регистрация принадлежат ROOT/T1, не этому пакету.
Существующие `/visible`, `/capabilities`, `/receipts` не заменены.

GET `/api/v2/reviews/notifications`:
`marketplace_account_id` positive integer, `marketplace=wb|avito`, `limit` 1..100
(default50), optional opaque `cursor`. Чужие/повторные параметры отклоняются.
Получателя или список event IDs для discovery клиент не передаёт.

Ответ `NotificationListResponse`:

```text
schemaVersion = review-notification-list-v1
organizationId, marketplaceAccountId, marketplace
recipientMembershipId (определяет сервер)
eventIds[]
items[{ event: existing notification-event-v1, receipt: null | {value, version} }]
nextCursor: string | null
eventSetVersion: decimal string
capabilities: {canRead, canMarkRead, canDismiss}
```

`sourceVersion` и receipt `version` передаются decimal strings. Остальные поля
event/receipt не переопределяются, безопасный текст восстанавливается только из
проверенных canonical bytes/checksum. SQL0074 уже ограничивает события scope=account,
producer=reviews и тремя видами approval_required/send_blocked/send_ambiguous.
Org-wide/platform события не выдумываются. OpenAPI описывает фактический DTO.
Для пакетных действий по всей странице ROOT должен согласовать max_visible_ids
существующего receipt router с лимитом списка (100); request budget задаёт ROOT.

## Изоляция И Пагинация

Сервис повторно получает membership по реальному actor/session, проверяет
`reviews:read`, account allowlist, fresh permissions, account binding и RLS.
Capability true означает только те же полномочия собственных read/dismiss receipts,
которые использует существующий mark_visible, не право отправки ответа/сообщения.

Порядок: occurred_at DESC, event_id DESC, сравнение timestamp/UUID выполняет PG.
Один SQL snapshot читает bounded page, count неизменяемого append-only event set и
LEFT JOIN receipt только текущего membership. Cursor MAC связан с org/user/session/
membership/account/provider/limit и этим count. Добавление event меняет witness:
409 NOTIFICATION_CONFLICT, клиент начинает новую первую страницу. Нельзя выдавать
этот механизм за исторический immutable snapshot всех receipt состояний:
прочтение/скрытие отражается по текущему состоянию и не меняет порядок event IDs.
Фильтра по unread/dismissed нет, чтобы изменение receipt не разрушало пагинацию.

Source binding проверяется тремя пакетными запросами facts/observations/runs.
Нет отдельного запроса на каждое уведомление. Старый source после account rebind
fail-closed; историческая повторная привязка или скрытое relabel не выполняются.
GET не создаёт событий, receipts или preferences. Existing shared auth resolver
может обновлять session last_seen_at, это не бизнес-запись discovery service.

Индекс запрошен T1: `(organization_id,marketplace_account_id,marketplace,
occurred_at DESC,event_id DESC)` на notification_in_app_events. T3 не добавлял DDL.
COUNT каждой страницы может стать дорогим на большой истории: замеров такой
истории в этом пакете нет; speculative cache/revision table не добавлялись.

## Проверки

- Offline cursor/query/typed HTTP: 26 PASS, 2 warnings, 1.28 s, exit0.
- Actual PostgreSQL head0080 / runtime role: 6 PASS, 2 warnings, 6.67 s, exit0.
  Discovery, bounded page, live permission/account/org/logout denials, actual
  own receipt, new event conflict, typed HTTP и отсутствие business SQL writes.
- Дополнительная проверка двух live recipients: 1 PASS, 2 warnings, 6.24 s,
  exit0. Чужие read/dismiss timestamps не видны текущему получателю. Предыдущие
  шесть не повторялись. Оба owned DB/role набора удалены с проверкой отсутствия;
  после естественного завершения процессов слот явно освобождён ROOT.
- Ruff/compileall/diff: exit0 перед commit. Самостоятельный критический
  проход обнаружил необходимость отдельного negative recipient case; код
  полномочий не ослаблялся. Full suites/внешний review не запускались.

Команды из backend, approved Python3.11, safe_environment, explicit Unix-only
owned allocator и serialized slot ROOT:

```sh
python -m pytest -p no:cacheprovider -q tests/test_notification_list.py
python -m pytest -p no:cacheprovider -q -s tests/test_notification_list_postgres.py
python -m pytest -p no:cacheprovider -q -s tests/test_notification_list_postgres.py::test_discovered_receipts_never_include_other_live_recipient
python -m ruff check app/notification_list.py app/notification_list_http.py app/notification_service.py tests/test_notification_list.py tests/test_notification_list_postgres.py
python -m compileall -q app/notification_list.py app/notification_list_http.py app/notification_service.py tests/test_notification_list.py tests/test_notification_list_postgres.py
git diff --check
```

Дальше: ROOT соединяет factory/config/allowlist с T4 consumer. T1 отдельно владеет
preferences CAS/write. Затем T3 возвращается к Orders query/filters и Production.
КИЗ, реальные provider actions, production, operational export/print, push,
deploy и CodeRabbit в этом пакете не использовались.

## Timestamp Compatibility Follow-Up

T4 обнаружил несовместимость стандартного Pydantic datetime serializer с уже
существующим strict frontend parser: whole-second instant терял обязательные
шесть знаков дробной части. Для occurredAt/readAt/dismissedAt добавлен точный
UTC microsecond serializer; offset сохраняет instant, null остаётся null,
naive timestamps отклоняются. Frontend parser и canonical storage bytes не менялись.
Пять actual HTTP/model cases: 5 PASS / 26 deselected, 2 warnings, 0.62 s, exit0.
PG не повторялся: это изменение wire serialization, не storage/auth/query.
