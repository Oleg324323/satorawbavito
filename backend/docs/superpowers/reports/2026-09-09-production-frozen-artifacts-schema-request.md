# T3 -> T1: следующие независимые Production persistence batches

Это schema request, не migration и не доказательство работающего production workflow.
Предыдущий request `8a8722f` (work items, assignment receipts/history) остаётся prerequisite.
T1 подтвердил, что его DDL ещё не READY. Названия ниже предлагаемые; exact migration,
shared ORM, grants и registration остаются у T1. КИЗ/matcher не входят в scope.

## Доказательства и границы

- Исходное задание этапов 5-8 требует immutable sheets, artifacts, archive/reprint,
  delivery intent/receipt и CAS; эти свойства нельзя обеспечить whole-JSON writer.
- `frontend/public/vella-production.html` на `58c32d83b9bce69ce1957e48e45b24001cc107c6`:
  `ordersPrintRowsFromGroups` потребляет готовые группы, но не определяет grouping policy;
  releaseTime `08:00` является display fallback, не доказательством календарного алгоритма.
- `backend/tests/test_orders_production_html.py` / `142cf46` исполняет реальные функции
  HTML на synthetic inputs: repeated quantities, source/date localStorage, last-writer-wins,
  неизвестный статус превращается в ready, failed send может выглядеть sent.
  Последние два свойства НЕ переносятся в canonical правила.
- `backend/app/avito/orders_picking_xlsx.py` существует; полный Production A4 renderer,
  WB fulfillment/sticker provider contract и legacy calendar/grouping source не восстановлены.
  Наличие HTML sticker markup не доказывает barcode/QR или quantity expansion parity.

Ниже только storage invariants из задания, без придуманной классификации, schedule,
weekend handling, cutoff, barcode или provider dispatch. DDL может оставаться dormant.

## Общие правила

PK BIGINT identity; org/account/membership/SKU совпадают с существующими INTEGER types.
TIMESTAMPTZ для receipt/publication timestamps, DB clock; DATE для Moscow business date.
Каждая relation ENABLE + FORCE organization RLS по transaction-local context.
Account-scope проверка сервисом не заменяет RLS. Ссылки work item и order item обязаны
включать одновременно org/account и concrete parent key. Не создавать копию Catalog.
Никакого CASCADE удаления истории, DELETE/TRUNCATE runtime grants или автоматического TTL.
Exact opaque TEXT keys без искусственного ограничения и raw unbounded Btree arbiter:
использовать принятую T1 account-serialized exact comparison стратегию, не digest-only identity.
Каждое изменение current row: version=old+1, immutable identity/created_at.

## Batch P2: batches / groups / sheet revisions

Предлагаемые relations:

1. `production_batches`: org/batch_id, version, business_date DATE, timezone с CHECK
   `Europe/Moscow`, schedule_rule_id/version, grouping_rule_id/version TEXT exact,
   created_by_membership FK(org,member), created_at/updated_at. Rule IDs обязательны,
   но это не разрешение назначить неизвестной legacy policy фиктивную версию.
2. `production_sheet_revisions`: org/sheet_revision_id, batch_id FK(org,batch),
   batch_version>=1, revision>=1, input_schema_version=1, input_checksum SHA256 hex,
   frozen account_scope sorted distinct INTEGER[], frozen business date/timezone/rules,
   row_count>=0, unit_count>=0, published_at. UNIQUE(org,batch,revision),
   UNIQUE(org,batch,input_checksum) только с exact semantic replay comparison в service.
   Append-only; header + exactly row_count rows sealed deferred at commit.
3. `production_sheet_rows`: org/sheet_revision_id/row_id, account_id, work_item_id,
   work_item_version>=1, order_id/order_item_id/source_item_version>=1,
   group_key TEXT exact, group_position>=1, row_position>=1, quantity INTEGER>0,
   payload_schema_version=1, frozen_payload JSONB object. PK/bounded unique
   (org,sheet,row_position), scoped FK work item, scoped FK order item, FK header.
   Каждая account входит в header.account_scope; row ordering/group ordering сохраняются.
   Frozen Catalog display values и assignment evidence находятся в payload, не в live join.
   Payload не содержит buyer contacts, credentials, arbitrary raw provider payload.

Deferred invariant: counts и sum(quantity) соответствуют header; все строки одной
group имеют одинаковый group_position; input checksum и row payload versions сравнивает
canonical service. Материальные FK не доказывают согласованность captured versions:
нужны shared auth guard, deterministic work-item locks и CAS versions в одной transaction.
Same inputs возвращают существующую revision; changed mapping/quantity создаёт новую,
а не обновляет старые payload. Пока allocation quantity не согласована с P1 планированием,
никаких updates planned_quantity и никаких printable/sendable claims.

Acceptance: два реальных runtime sessions с barrier, concurrent mapping/quantity change,
conflict/retry вместо mixed sheet, reordered request deterministic, same inputs replay,
Catalog change после commit не меняет historic sheet. Midnight/weekend/overdue acceptance
BLOCKED до evidence-backed rule; не подменять заранее выбранной библиотечной конвенцией.

## Batch P3: immutable artifacts и download receipts

`production_artifacts`: org/artifact_id, sheet_revision_id scoped FK, format CHECK
`xlsx|pdf_a4|sticker_120x75|sticker_58x40`, renderer_version/template_version TEXT,
selection_schema_version=1, exact frozen selection JSONB и selection_checksum,
content_checksum SHA256 hex, storage_ref TEXT, byte_count>0, row_count>=0,
page_count NULL для XLSX или positive для paginated formats, created_at DB clock.
Append-only; logical idempotency target (org,sheet,format,renderer/template,selection)
с exact payload comparison, не только hash. Account scope наследуется от sheet;
download revalidates все затронутые accounts и не вызывает provider/render live catalog.

Storage namespace: canonical org / sheet revision / artifact identity, сформированный
из internal IDs; external IDs/title не используются как filesystem path. Не принимать
arbitrary filesystem path/URL от request. Объект immutable после checksum verification;
внешний storage backend/retention/encryption policy пока не выбраны, не добавлять cache
или network upload. Нет table для фиктивного "printed" по факту скачивания.

Три отдельные renderer acceptance batches: XLSX (types, headers, Cyrillic, formula
injection, quantity expansion), A4 (48+, multipage, complete text, no clipping),
два sticker sizes (selected unit IDs, quantities, actual barcode/QR validation).
Каждый имеет отдельный rollback gate. До полного source recovery A4/sticker parity BLOCKED.

## Batch P4: archive и delivery reconciliation

Archive является append-only операторским событием со scoped target, actor membership,
expected_version, exact idempotency key/reason и DB time; historical sheet/artifact
не удаляются. Reprint ссылается на прежнюю immutable artifact revision, не строит
новый файл из current Catalog. Конкретный archive permission требует принятия владельца.

Delivery разделить на immutable intent и append-only attempts/receipts. Intent содержит
org/account-scoped target + frozen artifact selection, actor membership, idempotency key,
versioned payload/checksum и created_at; фиксируется ДО provider call. Attempt ссылается
на intent, имеет explicit outcome `pending|sent|ambiguous|failed` с receipt только при
доказанном acceptance; timeout после возможного acceptance не становится failed/retryable
автоматически. Raw response/buyer data не хранить без отдельного redaction contract.
Точные provider receipt semantics пока SOURCE BLOCKED: не вводить invented sent protocol.

Writer fence/cutover НЕ включается этими relations. После canonical writes rollback
не должен возвращать старый whole-JSON writer. Нужны checksum snapshot, reconciliation,
отсутствие in-flight writes и отдельный проверенный совместимый rollback.

## Downgrade и статус

Каждый batch имеет отдельный forward revision и downgrade guard: exclusive lock всех
его relations, unfiltered owner visibility и проверка пустоты. Nonempty -> atomic refusal,
без CASCADE/удаления/переноса записей обратно в JSON. Проверить RLS independently от
application predicates и runtime grants independently от owner tests.

Все реальные DB acceptance этой заявки **NOT_RUN**: схемы нет. Документ не меняет
routes/ORM/migrations/config/production/user JSON. Следующий разрешённый шаг T1:
точная schema/design для P1, затем согласованный минимальный P2; T3 пишет consumers
только против принятого exact committed contract, не создаёт параллельный DDL.
