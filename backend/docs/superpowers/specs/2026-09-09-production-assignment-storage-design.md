# Production P1: создание work item и доказуемое назначение SKU

2026-09-09. Локальное архитектурное решение T1; schema/service activation не выполнены.
Authority: committed T3 `8a8722fb8ed03e2f45fdfb3e2db2173940170230`
`2026-09-09-production-workitems-schema-request.md`, уточнение
`0aa2c483d98d321dcf7e41ff2d921267f9f99965`
`2026-09-09-production-first-writer-amendment.md`, полный codec и тест
`72dfaccb9e6190234c025f3c96bcfef400ce43b0:app/modules/production.py`.
T1 прочитал исходные файлы из local Git, actual0062 Orders FK/guards и runtime grants.
Доменный код остаётся T3. Не копировать его в T1 и не включать router/writer.

## Выбор и граница

1. Только service CAS + append receipt проще, но прямой SQL может породить receipt
   без изменения work item или изменить SKU без истории. Не удовлетворяет amendment.
2. Выбрано: три requested relations, current receipt witness, captured OLD/NEW
   deferred validation и непрерывная уникальная цепочка result versions. Это
   доказывает mutation/receipt/history consistency, не аутентификацию оператора.
3. Отдельный event/outbox subsystem или ограничение транзакции через private xmin/
   caller GUC добавляют хранилище/скрытый протокол, не нужные первому writer.

Ровно две операции: initial creation из принятого current Orders item; manual
assignment, изменяющий только SKU/version/time/current witness. Source refresh,
unassign, quantities allocation/reconciliation, sheets/artifacts, print/send,
calendar, provider action и scheduler исключены. KIZ/matcher не возвращаются.
Production permission не назначена владельцем: dormant schema допускается, публичный
writer остаётся выключен. GUC и actor membership FK не являются разрешением.

## Физическая модель

Ровно `production_work_items`, `production_assignment_receipts`,
`production_assignment_history`. Существующие org/account/member/SKU — positive
INTEGER. Orders item/order/work item/receipt/history surrogate IDs и версии —
positive BIGINT, как в committed request; identity columns GENERATED ALWAYS.
Physical BIGINT limits не выдают за неограниченные Python int; command с отсутствующим
огромным target/version отклоняется сервисом, не округляется или преобразуется.
TIMESTAMPTZ finite; trusted DB clock после lock waits, без caller-body timestamps.
TEXT exact `COLLATE "C"`, без cap/hash-only identity. Нет CASCADE.

### production_work_items

- work_item_id PK; organization_id, marketplace_account_id, order_id, order_item_id;
  scoped FK `(org,account,order,item)` → existing marketplace_order_items target.
- source_item_version>=1, required_quantity INTEGER>0, planned_quantity INTEGER>=0
  DEFAULT0, stored generated remaining=required-planned, planned<=required.
- catalog_sku_id nullable, org-qualified Catalog FK; version>=1 DEFAULT1;
  created_at/updated_at finite DB timestamps; updated>=created.
- current_assignment_receipt_id nullable BIGINT: deferred FK
  `(org,account,work_item,current_receipt)` → receipt scoped unique target.
- UNIQUE(org,account,order_item_id); UNIQUE(org,account,work_item_id).
  Owner-leading bounded indexes and partial `(org,account)` WHERE remaining>0.

INSERT requires exact initial shape: version1, SKU NULL, planned0, current witness
NULL. Under canonical account lock, lock exact Orders item FOR SHARE, read its
current quantity/version together, require both equal supplied frozen values.
Deferred INSERT validation retains the captured initial binding and repeats exact
source comparison at commit. If the same transaction changes its source afterward,
creation fails; another transaction cannot change locked quantity/version until
commit. No predicate assumes `canonical_status='accepted'`: “accepted current item”
means canonical persisted source, not invented business readiness. The trusted
service separately decides which orders may enter production.

UPDATE permits only catalog_sku_id (required after initial version), version=old+1,
updated_at>=old.updated_at and a NEW distinct non-NULL current receipt witness.
Identity/source version/quantities/created_at immutable. Same SKU with a new valid
command still increments version, consistent with AssignmentCommand; no invented
no-op shortcut. No physical DELETE/TRUNCATE. Later Orders changes do not update
this snapshot; readiness checks must compare source versions in the service.

### production_assignment_receipts

- receipt_id identity PK; org/account/work_item scoped FK;
  UNIQUE(org,account,work_item,receipt_id), UNIQUE(org,account,work_item,result_version).
- idempotency_key exact nonempty TEXT C; no raw TEXT B-tree unique.
- request_schema_version=1; request_payload JSONB object exactly five command fields;
  canonical_request_bytes BYTEA NOT NULL; request_checksum lowercase64 SHA256 bytes.
- actor_membership_id required org-qualified FK; result_version BIGINT>1;
  result_schema_version=1; result_payload JSONB object exactly **seven** fields from
  original request: work_item_id,version,catalog_sku_id,required_quantity,
  planned_quantity,remaining_quantity,source_item_version. No eighth field inferred.
- created_at finite trusted DB time. Immutable INSERT-only, no UPDATE/DELETE/TRUNCATE.

Exact `(org,account,work_item,idempotency_key)` uniqueness uses account-serialized
READ COMMITTED full C-collated equality, owner/workitem lookup index, fresh post-wait
SQL plus deferred final equality. No digest key; arbitrarily long valid text can
round-trip subject to PostgreSQL storage limits without index-entry failure.
Replay service checks target permission before exact key lookup, compares canonical
request bytes, returns the ORIGINAL receipt result even if work item advanced.
Same key changed body conflicts; no actor or previous receipt rewrite on replay.

### production_assignment_history

- assignment_event_id identity PK; org/account/work_item/receipt_id scoped FK;
  UNIQUE(org,account,receipt_id), UNIQUE(org,account,work_item,to_version).
- actor_membership_id org-qualified; event_kind exactly manual_assignment;
  from_version>=1, to_version=from+1; previous_catalog_sku_id nullable and new
  catalog_sku_id required, both org-qualified FKs; reason exact nonempty TEXT C;
  occurred_at finite DB time.
- Immutable INSERT-only; one exact history per receipt, no generic arbitrary JSON
  audit or separate event bus. No DELETE/TRUNCATE/UPDATE including privileged normal
  DML (migration superuser can alter schema, not an application threat guarantee).

## Canonical request and result contract

Five command fields retain exact Python int/text types. Key/reason must be nonempty,
without leading/trailing Python whitespace, NUL or unpaired surrogate. Interior
valid Unicode/control characters remain unchanged, no normalization/truncation.
Python strip edge set matches the approvals spec, not ASCII-only btrim.

Dedicated immutable fixed-signature helpers, schema qualified/search_path fixed:

```text
production_exact_text(text)->boolean
production_ascii_json_string(text)->text
production_assignment_bytes(bigint,bigint,integer,text,text)->bytea
# work_item_id, expected_version, catalog_sku_id, idempotency_key, reason
```

Emit exact compact ASCII JSON sorted keys:

```text
{"command":{"catalog_sku_id":I,"expected_version":V,"idempotency_key":S,"reason":S,"work_item_id":W},"schema_version":1}
```

Use lowercase Python ensure_ascii escapes, quote/backslash/short control escapes,
DEL and all non-ASCII escaped, supplementary codepoints UTF16 surrogate pairs.
No jsonb::text substitute; domain does not impose 4096 request cap here.
Byte equality against reconstructed typed values enforces lexical canonical bytes,
then SHA256 equality; parsed projection must match all exact keys/types and values.
Canonical request bytes require strict decimal integer tokens: no bool, quoted
numerics, fractional or exponent spelling, or null. Before casts the JSONB
projection validator rejects wrong types, retained noncanonical fractional forms
and out-of-range values, without rounding. JSONB can normalize an exponent token
such as 1e0 to 1 before a trigger sees it; the database cannot prove the original
lexical form of JSONB input. It proves exact typed projection equality against the
canonical BYTEA request and captured result values. Raw command lexical admission
remains the trusted decoder's obligation, not a property of JSONB or this schema.
Characterize normalization with real PostgreSQL tests and reject noncanonical
request BYTEA regardless of equivalent parsed JSONB. Preserve requested physical
BIGINT/INTEGER bounds. Result payload equals all seven values
from captured NEW, not supplied “success” metadata. Canonical bytes store metadata
commands, not credentials/provider body. Generic logs/errors never reflect reason/key.

T3 literal producer example at72dfacc is an independent fixed expected vector;
additional Python-standard-json inputs test escapes/Unicode/long text. Do not
claim T1 ran T3 producer merely because literal parity passes. Historical migration
helpers stay dedicated/frozen, not imports of mutable repricer/domain code.

## Mutation witness and no-ghost proof

Deferred work-item UPDATE trigger compares captured OLD/NEW with the receipt
identified by NEW.current witness and its unique history: expected=OLD.version,
result=NEW.version=OLD+1; old/new SKU, target/scope, all seven result values,
actor/reason and receipt/history/work-item command timestamp must agree exactly.
No metadata-only witness patch. All receipt/history triggers queue validation too.

Final per-workitem invariant under retained account lock:

- version1 means no receipts/history and NULL current witness; version>1 requires
  current witness at exactly final version and frozen result matching current row;
- receipt result versions are unique, within [2,current version], and count equals
  current version-1. Thus no missing or extra historical version can commit;
- every receipt has exactly one scoped matching history and vice versa; request
  expected=result_version-1; initial history previous SKU NULL; each later history
  previous SKU equals preceding receipt result SKU. Source/quantity constants agree;
- captured validators prove every actual transition; final count/unique/range and
  inverse history checks reject a ghost receipt even if its values resemble history.

This permits multiple valid transitions in one transaction without interpreting
all captured events as the final row. Service P1 nevertheless performs one command
per owned guarded root. Cost: final validation scans bounded-owner lifetime history;
no unsupported constant-time/high-throughput claim. Index by owner/workitem/version,
not request text. A later optimization needs equivalent no-ghost proof.

## Locking, RLS and privileges

Trusted service: live user→membership→login session→canonical account guard first,
then source Orders item (creation only), work item, receipt/history mutations.
Before any DML on new tables a VOLATILE BEFORE STATEMENT trigger validates strict
positive org+account context and READ COMMITTED, locks exact canonical account
FOR UPDATE before target tuple locks. Subsequent SQL does fresh exact lookup.
Both providers allowed according to canonical account; no fake WB-only Production.
Do not take a work-item tuple lock before account or introduce provider I/O under
locks. Source row locking precedes work-item allocation. Unsupported lock orders
may abort; don't claim generic arbitrary SQL deadlock freedom.

All three tables FORCE+ENABLE org AND account RLS, identical USING/WITH CHECK;
missing/noncanonical/out-of-INT4 context denies. Use accepted Engine-bound helper
2408839 and independent live guard8338ef7 in consumer; schema never authenticates.
Runtime nonowner/NOSUPERUSER/NOBYPASSRLS: workitems SELECT/INSERT/UPDATE, receipt/history
SELECT/INSERT; no DELETE/TRUNCATE/REFERENCES/TRIGGER/grant options/schema create or
SET ROLE owner. Dedicated identity sequences USAGE only, no UPDATE/setval.

Atomic upgrade narrows only NEW table/column/sequence/helper ACLs for every inherited
default grantee: intersect previous rights with allowed operations, PUBLIC gets
nothing; retain no grant options. No unrelated old/default ACL rewrite. Runtime
script applies same dedicated overrides after broad grants in its existing atomic
transaction. Trigger helpers SECURITY INVOKER, PUBLIC execution revoked, required
execution only to entitled grantees; no dynamic SQL mutation interface/SECURITY DEFINER.
New tables' FORCE RLS also included in runtime preflight.

## Migration, tests and rollback

New forward revision after actual sole head at implementation, explicitly selected
in its plan before dispatch. Never rewrite0062/0064/0065 or silently renumber.
Empty bootstrap without stamp, previous-head production-shaped synthetic preservation,
broad-default ACL after valid previous head, current runtime-script independent latest
fixture, graph future-successor regression, empty downgrade→upgrade required.

Downgrade fixed-order ACCESS EXCLUSIVE all3, genuine all-row visibility, refuse on
any data/visibility failure before DDL. Only then named cyclic constraints and own
objects in dependency order; no CASCADE, no deletion to satisfy guard. Rollback is
old dormant binary on additive schema, not destructive downgrade with history.

Actual runtime PostgreSQL tests: no-context/wrongorg/wrongaccount CRUD and FKs;
fresh source quantity/version coherence and concurrent source update; initial
shape violations; legal single/multiple transitions; missing/ghost/wrong-version/
wrong-actor/time/result/history/witness; no refresh/unassign/planning; physical
account wait before tuple mutation; two-session CAS/key races; long distinct/equal
keys; replay original frozen result and no write; injected audit/deferred/commit
failure all-or-nothing; broad ACL/PUBLIC/columns/sequences; nonempty/hidden downgrade.
Use existing random Unix-only disposable helpers, own venv, scrubbed env, network/
known-secret deny OS sandbox. No real data/credentials/services/providers, flags,
push/deploy, app DB, operational grants or policy decisions.

## Critical pass / remaining activation decisions

Checked: full TEXT index overflow, circular FK order, later-version replay, ghost
historical receipt, multi-transition OLD/NEW drift, source snapshot race, account
lock before tuple, JSON numeric canonicalization, sequence default privileges,
RLS-hidden downgrade. These are design/test obligations, not executed proofs.
Owner Production permission and domain readiness/source-refresh/print policy remain
unselected and do not block this dormant storage prerequisite. P2–P4 frozen artifact
request0aa2c48 is a separate future scope; no renderer/calendar semantics inferred.
