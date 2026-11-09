# Orders: domain binding amendment для T1

Основание: T3 `f4d6d554e784b10f9d25d8d4a2637f829502d3c7`, candidate
`09cf383ca7419b147ebb63a7b749a7ebacc99b73`, прочитанные `orders_v1/HANDOFF.md`
и `upgrade.sql`. Это принятие bounded domain binding, не принятие installed DDL.
Schema/ORM/shared grants/registration не менялись. Exact ready Alembic commit T1
нужно отдельно проверить до persistence. Предыдущие DB01-DB14 остаются NOT_RUN.

## 1. Реализованный storage serializer

`app/orders/serialization.py` экспортирует:

- `serialize_observation(OrderObservation)` -> JSON object с ключами
  `evidence_schema_version: 1`, `observation: {...}`.
- `observation_checksum(OrderObservation)` -> lowercase SHA-256 hex.
- `serialize_read_row(OrderReadRow)` -> JSON object с ключами
  `payload_schema_version: 1`, `row: {...}`.

Explicit allowlist полей; нет произвольного dict/raw payload/extra fields.
Только точные классы domain models, их tuples, int/bool/string/null и aware datetime.
Dataclass subclasses отклоняются, чтобы не протащить новые поля в формат v1.
Это структурная защита, НЕ PII-detector: upstream обязан гарантировать, что
raw_status/IDs/provenance содержат соответствующие значения, не buyer PII/secrets.
NUL и lone surrogate отклоняются typed error до PostgreSQL JSONB. Никакой silent
sanitization. Внешние IDs сохраняются строками; Unicode normalization не применяется.

Все даты: UTC ISO8601, ровно 6 цифр microseconds, offset `+00:00`.
Null keys сохраняются. Items сортируются по точному source_line_key, не listing ID
или transport position. Blockers/deadlines сохраняют переданный порядок.
Результат detached: изменение полученного dict не меняет frozen domain model.

### Checksum и replay

Из `serialize_observation` удалить ТОЛЬКО `observation.observed_at`, добавить
root `checksum_version: "orders-observation-v1"`. Сериализовать Python JSON:
`sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False`,
кодировать UTF-8, SHA-256. Версия embedded; DB evidence_schema_version = 1
должна совпадать с payload. Хешировать не JSONB textual rendering PostgreSQL.

В checksum входят identity/org/account/marketplace, source_kind, adapter_version,
opaque source_revision (включая null), effective_at (включая null), весь mapped
status/provenance, items со stable evidence/quantity, оба WB cancel booleans.
Не входят receipt time, DB IDs/run ID, ingested_at. Raw source_event_id пока нет
в domain model: первый writer сохраняет SQL source_event_id NULL. Не подменять
его order ID/revision. Добавление provider event identity потребует версии формата.

SQL replay identity для первого writer:
`org/account/order_id/source_kind/adapter_version/payload_checksum`,
`order_item_id IS NULL`. External order binding предварительно проверяется.
Новый run повторно использует observation через membership, а не меняет его
sync_run_id/observed_at. Receipt нового run хранится в membership.observed_at.
При unique conflict сравнить semantic bytes исключая receipt, а не только digest;
несовпадение означает integrity error, не успешный replay.

Новая/противоречивая revision -> новый факт; ordering revision не определён.
`compare_observations` остаётся authority для классификации сравнения, НЕ разрешением
публикации. Старый manifest checksum `orders-manifest-v1` не изменён: это другой
домен хеша с собственной сериализацией. Не использовать его как observation hash.

## 2. Форма и nullability

| Domain -> SQL | Binding первого writer |
|---|---|
| OrderObservation -> order_observations | Одно полное order-level observation; order_item_id NULL, items внутри normalized_evidence |
| identity | Exact org/account/marketplace/external_order_id -> scoped order_id; не global external ID |
| items | Только validated stable line keys; quantity > 0, explicit occurrence; сохранять stable_order_line_id/stable_unit_id в evidence |
| source/effective/receipt | adapter/source/revision и timestamps копируются; NULL revision/effective допустим, receipt обязателен |
| status | Avito raw обязателен; WB raw может NULL; canonical NULL iff unmapped/ambiguous; mapping_version/evidence_source обязательны для каждого факта |
| mapping state | Current mappers выдают mapped/unmapped; ambiguous предусмотрен generic model/SQL, но не фабрикуется ingestion writer |
| WB | Оба boolean evidence обязательны; non-cancelled -> canonical NULL; никакой готовности/returned из sales |
| resolution | resolved требует product+SKU; manual_override допускает SKU-only; остальные без current SKU; offer требует product |
| resolution version | CatalogResolution.evidence_version -> resolution_version; не item row version |
| item row version | row_version -> marketplace_order_items.version; CAS отдельно от resolution version |
| deadlines | source_at -> source_deadline_at; computed_at -> computed_deadline_at; минимум один nonnull; computed требует rule_id/version; observed_at и timezone/provenance обязательны |

SQL nullable mapping_version текущей проекции НЕ разрешает writer публиковать
неверсионированный unknown status. SQL source_kind/adapter/run FK проверяет часть
binding; сервис также сверяет marketplace, run.mapping_version и source snapshot.
Avito browser source не меняет status mapping evidence_source, который у pure
mapping остаётся `avito-order-management`; это версия правил, не утверждение о
канале fetch. Реальный browser authority требует отдельного provenance review.

### Frozen OrderReadRow

row_payload хранит `serialize_read_row(row)` целиком: observation (включая receipt,
все items и их evidence), item_identity, row_version, resolution, readiness_blockers,
deadlines. Не title-only summary и не ссылки для join к mutable Catalog.
SQL row payload version = embedded payload_schema_version; SQL row_version =
row.row_version. SQL order/account/item IDs должны соответствовать вложенным
external identities; observation_id должен содержать то же normalized observation.
Нельзя serialize incoming replay receipt вместо сохранённого observation receipt:
repository строит snapshot из сохранённого факта. Иначе payload противоречит FK.
Historical pages не пересобираются из current rows, при чтении заново проверяются
permissions. Storage serializer не HTTP contract, не cursor signer и не decoder.

## 3. Lifecycle vocabulary

Принимаются SQL enum labels без добавления новых значений, но не автогенерация
событий по любому status. Поля evidence обязаны описывать причину и immutable
observation references; raw marketplace payload недопустим.

| event_kind | Authority/ограничение |
|---|---|
| cancellation | Явный mapped cancellation fact; не отсутствие order на partial page |
| status_changed | Различие принятой прежней и новой status evidence; initial observation само по себе не transition |
| reconciliation_required | Результат сравнения/конфликт evidence; не регрессировать current projection |
| return | Отдельное доказательство возврата; `on_return` означает returning, не факт received/returned |
| partial_return | Доказанные item/unit и возвращённое quantity; order-level return marker недостаточен |

Первый observation writer может сохранять status_observations без lifecycle rows.
Точная typed event evidence/source_event_key сериализация для return/partial_return
пока не определена: эти writers остаются off, SQL vocabulary не даёт полномочий.
Таким образом schema не блокируется отсутствием return collector, а неподтверждённая
история не фабрикуется. Terminal ambiguity не разрешается timestamp эвристикой.

## 4. Run, completeness и transaction protocol

1. Fetch/normalization вне DB transaction. Проверенный source contract обязателен:
   legacy synced/total fallback не доказывает terminal provider page.
2. Transaction-local tenant context + актуальная membership/account authorization.
   Lock account/auth guard, согласованный с revoke/rebind writer T1; простого SELECT
   непосредственно перед COMMIT недостаточно для конкурентной revocation.
3. Lock run `FOR UPDATE`, сверить scope/source/adapter/mapping/snapshot/request bounds.
   Любой append observation, membership, coverage допускается ТОЛЬКО staging.
   Каждый competing writer обязан брать тот же lock. Terminal complete/partial/failed
   -> никакого append/update; exact retry только читает сохранённый результат.
4. Identity lookup/create scoped org+account; validated items только. Existing replay
   observation можно связать с новым staging run, сверив source/adapter/account/order.
5. Manifest.page_count = число validated pages; order_count = число разных orders;
   item_count = число validated item identities, НЕ сумма quantity. Один order-level
   membership на observation/order/run. Item-level memberships пока не создавать.
6. Complete требует contiguous 1..N, terminal ровно N, exact expected_order_count,
   consistent source snapshot и договор provider completeness. Partial не удаляет
   absent orders и не создаёт cancellations. Failed/partial не публикуют complete view.
7. Publication transaction атомарно сохраняет observations/memberships/coverage,
   CAS текущих проекций (`WHERE version=expected`, `version+1`), sealed read header
   со всеми payload rows и перевод run в terminal. Любой конфликт -> rollback всего
   publication; не оставлять header/coverage без строк. Historical staged evidence,
   если позже потребуется multi-transaction staging, не видна published read API.
8. Stable row positions определяются полным query sort + scoped unique item tie-breaker;
   header account_scope/coverage/query checksum совпадают с фильтрами. Schema snapshot
   count trigger не заменяет проверку manifest и permissions. Повторный page запрос
   сверяет authorization заново, snapshot/cursor scope не расширяет права.

## 5. Конкретные замечания T1 / acceptance gates

- Candidate защищает mutation terminal run, но НЕ append дочерних relations.
  Принято как service obligation только при едином lock protocol выше; DB test
  должен реально соревновать append с terminal transition (DB04/DB05 extension).
- Candidate разрешает `{}` и произвольные normalized JSON objects. Writer использует
  serializer v1; SQL structural tests не считаются domain acceptance.
- Candidate поддерживает item-level observation FKs, но выбранный первый writer
  order-level. Item-level deadlines/events требуют отдельного typed evidence binding;
  нельзя указывать item_id при order-level observation там, где composite FK запрещает.
- Quarantine payload не существует в current pure contract. `items=()` означает
  отсутствие validated items, НЕ доказательство причины quarantine. Не кодировать
  malformed raw rows как успешные пустые orders; rejected/quarantine ingestion пока
  blocked до redacted typed schema. Missing stable WB ID остаётся validation error.
- Candidate nonempty snapshot account_scope vs domain empty authorized scope:
  пустой scope возвращать без persisted header, без выдуманного account ID.
- SQL deadline uniqueness `(org, account, observation, kind)` требует одного факта
  на kind. Не молча перезаписывать несколько item-specific deadlines одной order
  observation; это отдельный contract gap до включения deadline writer.
- Account revoke/rebind serialization должен согласовать T1: shared tenant helper
  сам по себе authorization не предоставляет. DB08 обязателен после exact schema.
- Production commands/CAS audit/receipts/sheets/KIZ отсутствуют в candidate.
  Orders schema не означает, что DB09-DB13 production portions уже можно выполнить.

## Verification / rollback

TDD: initial test collection failed `ModuleNotFoundError: app.orders.serialization`
(exit 2); после реализации 18 serializer cases и весь focused offline regression:
205 passed, 2 прежних dependency warnings. Ruff нашёл import ordering в тесте,
исправлено до commit. SQL/DB execution в этой задаче не выполняется.
Независимый self-review: проверены все serialized fields, nullable WB evidence,
receipt/replay различие, UTC normalization, subclass/raw-dict rejection и отсутствие
runtime wiring. Ни completeness authority, ни DB concurrency не заявляются.

Команды из backend:

```sh
python -m pytest -p tests.orders_offline_plugin -q tests/test_orders_serialization.py \
  tests/test_orders_contract.py tests/test_orders_stage1_characterization.py \
  tests/test_orders_xlsx_characterization.py tests/test_orders_offline_guard.py \
  tests/test_avito_orders.py tests/test_avito_returns.py tests/test_orders_ingestion.py \
  tests/test_orders_read_contracts.py tests/test_orders_avito_adapter_characterization.py \
  tests/test_orders_returns_characterization.py
python -m ruff check app/orders/serialization.py tests/test_orders_serialization.py
python -m compileall -q app/orders/serialization.py tests/test_orders_serialization.py
```
Rollback нового serializer не затрагивает runtime, поскольку wiring отсутствует;
после будущих persisted v1 payloads decoder/version support нельзя просто удалить.
