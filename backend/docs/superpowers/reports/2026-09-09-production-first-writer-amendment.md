# T3 -> T1: P1 creation / assignment witness / account RLS

Уточнение к exact request `8a8722f`, без implementation/DDL/activation.
Источник: исходный этап 4, `app/modules/production.py` AssignmentCommand и принятая
Orders identity/CAS семантика. Не добавляет source-refresh/unassign/print действия.

## Root-approved permission policy, 2026-09-09

Server-selected capabilities: `production:read` for work-item/history reads;
creation requires both `production:create` and `production:read`; assignment
requires both `production:assign` and `production:read`. Every replay requires
the same current capabilities as its originating operation, plus live user,
session, membership and exact account scope checks before exposing its receipt.
Idempotency key is never authority. Revalidation remains through commit.

T1 owns shared registry/guard changes. Do not auto-grant these keys to any profile,
including admin; preserve existing profile-plus-explicit-grants union and legacy
aliases. `sync:run`, `catalog:write`, `settings_editor` or the legacy `production`
profile alias are not substitutes. Synthetic explicit grants only in tests.
Actor comes from the authenticated session/membership, never body metadata.
Denial must not mutate business state or reveal stored receipt data. Audit must
distinguish creation, assignment and replay; worker authority remains separate.

The capability may cover future unassign, but this P1 operation/schema contract
still disallows it. No nullable-SKU update, source refresh, planning or background
user impersonation is introduced by recognition of these permissions. Routes and
runtime activation stay default-off. Owner policy resolves permission naming,
not pending exact DDL acceptance or source/provider completeness.

## Первый slice: ровно две операции

1. **Creation** из существующего accepted current Orders item: scoped org/account/order/
   order_item FK, source_item_version совпадает с текущей version Orders item,
   required_quantity совпадает с его positive quantity. Проверять под согласованными
   row locks и deferred commit witness, не только FK. Initial version=1,
   planned_quantity=0, remaining_quantity=required_quantity, catalog_sku_id=NULL,
   assignment receipt witness=NULL. Creation не означает printable/sendable и не
   выдаёт marketplace readiness. Automatic Catalog assignment не входит в P1;
   существующий Orders Catalog resolution остаётся отдельным evidence.
2. **Manual assignment** существующего work item: меняет только catalog_sku_id,
   version=OLD.version+1, updated_at и current assignment receipt witness.
   Все identity, source_item_version, required/planned quantities и created_at
   неизменны. Expected version и exact idempotency semantics из `8a8722f` сохраняются.
   Возвращается original receipt result, а не актуальный row при replay.

Source-refresh, unassign, planned-quantity allocation и quantity reconciliation
НЕ входят в первый writer. Любой UPDATE таких полей отклонять, а не угадывать
будущую semantics. Их добавление требует отдельного forward contract/DDL revision.
Source quantity/version может впоследствии измениться в Orders: старый work item
не перезаписывается автоматически и не объявляется актуальным. Service должен
явно блокировать production readiness при source-version mismatch.

## Physical receipt witness

Поддерживаю предложенный T1 технический current-assignment receipt witness и
captured OLD/NEW deferred check. Требуется доказать именно выполненный CAS:

- work item UPDATE без соответствующих receipt/history не может commit;
- receipt/history без соответствующего work item transition не может commit;
- exact OLD expected_version/SKU и NEW result version/SKU/quantities/source version
  совпадают с receipt request/result и history;
- actor membership, org/account/work-item target и reason совпадают;
- ровно один receipt/history на каждый assignment transition;
- replay не выполняет UPDATE и не создаёт новый witness/history;
- scope witness FK включает org/account/work_item и receipt key. Циклические FK
  допускают deferred validation, но не временно неполный committed state.

Имя технического поля/trigger и механизм captured transition выбирает T1.
Это не domain event bus и не разрешение расширить AssignmentCommand.
Initial creation без assignment receipt допустим только при точном initial shape.
Для нескольких transitions одной transaction witness должен доказать каждый
transition либо такой multi-command transaction должна явно отклоняться.
Сервис P1 выполняет один command в собственной guarded root transaction.

## Account RLS

Подтверждаю FORCE org+account RLS для всех трёх новых relations как совместимое
усиление request. Runtime consumer обязан использовать принятый helper
`set_marketplace_account_context` из `1ce8293` + обязательный `2408839` и отдельно
live authenticated guard. Org/account GUC не являются authorization.
Org-owned CatalogSku сохраняет org-scoped FK; не добавлять ему fake account owner.

Exact длинный idempotency key не ограничивать и не индексировать целиком Btree:
account-serialized RC equality + canonical BYTEA/JSONB comparison, checksum не identity.
BYTEA representation не должна менять accepted Unicode domain command, schema=1
checksum или replay comparison из `8a8722f`.

## Gates

Обязательны actual two-session CAS/idempotency races, independently проверенные
RLS и application scope, raw SQL forged receipt/CAS negatives, creation mismatch
quantity/version/account negatives, rollback all effects и nonempty downgrade refusal.
Нужен тест source change concurrently with creation: либо captured coherent binding,
либо retry/conflict, не mixed source quantity/version. Эти gates пока NOT_RUN:
exact Production migration не существует в T3.

Production command permission policy ещё не назначена владельцем: schema/consumer
может быть dormant, но публичный writer нельзя подключать под придуманным permission.
Не менялись code/ORM/migrations/config/пользовательские JSON/production.
