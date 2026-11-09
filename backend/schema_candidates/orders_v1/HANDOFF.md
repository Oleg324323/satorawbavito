# Orders: готовый кандидат схемы для включения T1

Дата: 2026-09-09. Ветка: `codex/orders-schema-candidate`.
База T1: `5ba31a0`; контракт T3: `f4d6d55`.

**Статус: кандидат DDL проверен локально; в активный Alembic не включён.**
Владельцем номера миграции, общих прав runtime и интеграции остаётся T1.
ORM/repository/ingestion/API остаются T3. Общая очередь Orders ещё не готова.

## Что передаётся

- `upgrade.sql` — 11 таблиц, account-qualified Catalog anchors, FK, CHECK,
  FORCE RLS, защиты истории и сохранение полного содержимого строк снимка.
- `downgrade.sql` — блокировки всех новых таблиц, проверка пустоты без RLS-фильтра,
  отказ при наличии данных или невозможности проверить их без фильтра.
- `tests/test_orders_schema_candidate.py` — реальные PostgreSQL проверки.
  Тестовый Alembic wrapper создаётся только во временном каталоге.

| Таблицы | Назначение |
|---|---|
| order_sync_runs | Источник/версия адаптера, staging → complete/partial/failed, manifest metadata; terminal run неизменяем |
| marketplace_orders / marketplace_order_items | Устойчивая внешняя идентичность; текущая проекция с version; account-qualified ссылки на Catalog |
| order_observations | Неизменяемые нормализованные наблюдения; nullable source revision/effective time; привязка источника/адаптера к run |
| order_status_observations / order_lifecycle_events / order_deadlines | История статусов, событий и сроков с provenance, привязкой к наблюдению и item/order |
| order_sync_coverage / order_sync_memberships | История покрытия и состава загрузок, включая повторное использование ранее сохранённого observation |
| order_read_snapshots / order_read_snapshot_rows | Immutable header и замороженный payload каждой строки; порядок, версии, account scope, query checksum |

`marketplace_status_mappings` не создаётся: авторитетом остаётся существующий
versioned pure contract. Production work items, assignment audit, idempotency
receipts команд, sheets/print/KIZ также не создаются в этом Orders slice.

## Существенные договорённости для T3

1. Внешние IDs — точные TEXT: не превращать в числа, не обрезать пробелы.
   SQL-проверка покрывает набор whitespace Python `str.strip()` для Unicode.
   Нулевые количества и отсутствующую stable-line identity не заменять
   выдуманными единицами. Quarantine хранится в observation без item.
2. Replay unique: org/account/order/(optional item)/source/adapter/semantic checksum.
   Checksum обязан включать source revision и содержимое нормализованного факта
   по согласованной версии сериализации. Receipt time и порядок items не должны
   менять semantic checksum. Одинаковый provider event ID с новой/противоречивой
   ревизией сохраняется как новый факт; это ещё не разрешение обновить projection.
3. `normalized_evidence` и `row_payload` — versioned JSON objects, не raw payload.
   Схема проверяет форму object, но **не** полноту/редакцию полей. T3 обязан
   валидировать полный нормализованный контракт, сохранять stable-unit/line evidence,
   не включать buyer PII/секреты. `evidence_schema_version` и `payload_schema_version`
   сейчас равны 1. `{}` в SQL-тестах — только структурный fixture.
4. В `order_read_snapshot_rows.row_payload` сохраняется полная сериализация
   `OrderReadRow`, включая resolution, deadlines, blockers и наблюдение.
   Не собирать историческую страницу join-ом с изменяемыми текущими проекциями.
5. Header и все строки снимка вставляются в одной транзакции. Deferred checks
   проверяют точное число строк и позиции 1..N в финальном состоянии.
   После commit нельзя дописать строки, изменить header или payload.
   Row account должен входить в header scope; item observation обязан относиться
   к этому item, либо быть order-level observation данного заказа.
6. Org RLS — не account authorization. T3 проверяет membership/allowed accounts
   повторно на каждой странице и перед публикацией. Header account_coverage,
   состав account_scope, подпись/срок cursor и query checksum валидирует сервис.
7. Обновление текущей проекции требует `version = version + 1`; команда обязана
   дополнительно иметь `WHERE version = expected_version`. Триггер версии
   сам по себе не исключает blind overwrite. Source timestamps не дают
   автоматического разрешения регрессировать текущую проекцию.
8. SQL state/manifest checks не доказывают provider completeness, точное число
   наблюдений в manifest, допустимость перехода статуса или авторизацию публикации.
   Append observations/coverage/membership в terminal run сервис запрещает сам.
   Request period: оба bounds NULL либо оба заданы с to > from.
9. Lifecycle vocabulary кандидата: cancellation, return, partial_return,
   status_changed, reconciliation_required. Перед writer T3 должен закрепить
   отображение доменных событий в эти значения либо согласовать аддитивное
   расширение. Это не provider status mapping.
10. Автоматическое resolution требует product + SKU; manual override допускает
    SKU-only. Остальные resolution states запрещают current SKU. Offer требует
    product того же аккаунта и той же пары product/offer. При чтении Catalog
    заново проверяется версия resolution; SKU ownership остаётся у Catalog.

## Интеграция T1

1. Проверить patch и свежий единственный Alembic head. При последней проверке T1
   HEAD был `daace4c`; после базового `5ba31a0` — тесты isolation и default-off
   repricer scheduler. Ни одного файла T1/T3 эта задача не изменяет.
2. Включить коммит кандидата через обычный review. Не запускать SQL против
   application/production DB как standalone shortcut.
3. Назначить новый revision поверх фактического head. Перенести DDL в одну
   owner-transaction migration; `op.execute(sa.text(sql))` сохраняет PostgreSQL
   `%ROWTYPE`/`format('%I',...)` без ошибочной интерпретации psycopg placeholders.
   Прямой `exec_driver_sql` с пустым mapping требует отдельно экранировать `%`.
4. Проверить существующие Catalog данные на нарушенные account/product пары:
   новый FK намеренно остановит migration при таких строках. Автоматически
   удалять/перепривязывать Catalog данные этот кандидат не пытается.
5. T1 отражает новые Catalog anchors в своём ORM и выделяет runtime grants:
   history SELECT/INSERT, текущим проекциям необходимый UPDATE, без DELETE/TRUNCATE,
   без ownership/BYPASSRLS/DDL/sequence mutation. Триггеры дополнительно защищают
   историю даже после ошибочного broad DML/TRUNCATE grant — это проверено.
   Сам SQL не выдаёт production runtime роли новых прав; inherited defaults
   нужно проверить при интеграции. Shared grant script не изменялся.
6. Повторить migration/runtime tests на итоговом revision и согласовать с T3
   точный контракт. Сохранить default-off readers/writers/collectors.
7. T3 после принятой миграции реализует свой ORM/repository и DB01–DB14 в их
   полной сервисной постановке. Полный production renderer/matcher и WB fulfillment
   authority остаются независимыми блокерами.

## Rollback

До записей: guarded downgrade в owner transaction, без CASCADE.
После записей: fence writers и сохранить данные; downgrade обязан отказать.
`SET LOCAL row_security=off` не даёт bypass: непривилегированный owner получает
ошибку вместо ложного «таблицы пусты». Проверки пустоты выполняет разрешённая
migration role с видимостью всех tenants. Catalog anchors удаляются только после
проверки всех новых таблиц и удаления зависимостей.

## Доказательства и пределы

Проверено: fresh empty → actual base head, SQL upgrade/empty downgrade/reupgrade;
полная цепочка с временным `orders_candidate_test` single head → downgrade до
`20260908_0061` → reupgrade. Активный каталог `alembic/versions` не изменён.

Runtime роль: LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION
NOBYPASSRLS. Проверены FORCE RLS/default deny, tenant/account collisions и FK,
nullable WB status/source revision, whitespace, Catalog pairing, immutable history,
nonempty downgrade и hidden-row downgrade, source/run mismatch, sealed snapshots,
две отдельные PostgreSQL sessions с barrier для replay uniqueness и projection CAS.
Эти гонки **не** доказывают готовую транзакционную публикацию, assignment/audit,
account revocation или сквозную идемпотентность Orders сервиса.

Системный SHMALL отказал в запуске нового PostgreSQL cluster: одинаково на
Homebrew Intel 16.15 и временном ARM 16.14. Для успешных тестов использованы новые
случайно именованные disposable DB на существующем локальном PostgreSQL 16.15,
подключение исключительно через Unix `/tmp` к maintenance DB `postgres`.
Таблицы application DB не читались/не менялись; сервер не перезапускался.
Каждый успешный/ошибочный setup очищает свои DB/role finalizer-ом.
Это database isolation, не отдельный server process. Fallback включается явно.

Финальный результат: **85 passed, 0 warnings** (39 candidate DB/DDL tests +
46 pure Orders contract tests). Ruff, compileall и staged diff check — exit 0.

Команда из backend worktree (использован уже установленный полный Python runtime):

```sh
ORDERS_TEST_USE_LOCAL_CLUSTER=1 \
  /Users/bratishka/Downloads/satornawb-main/.worktrees/arch-t1-platform/backend/.venv/bin/python \
  -m pytest -q tests/test_orders_schema_candidate.py tests/test_orders_contract.py
```

Независимый read-only агент проверил контракт и затем SQL. Исправлены замечания:
RLS-hidden downgrade, source/adapter FK, snapshot item/account links, период,
дописывание snapshot после commit. Нужный global preflight-critic SKILL.md
отсутствует; выполнен отдельный критический проход агентом по реальным файлам.
