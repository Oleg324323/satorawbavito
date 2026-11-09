# T3 -> T1: bounded Production work items / assignment DDL

Source authority: user Stage4 requirement, existing `app/modules/production.py`
AssignmentCommand/validate_assignment_preconditions, Orders0062; no legacy calendar,
renderer, automatic mapping, KIZ or print behavior inferred. Request only, no DDL
or ORM installed by T3. T1 owns naming/revision/shared ACL integration.

## Requested relations

All IDs BIGINT identity PK except existing INTEGER org/account/membership/SKU.
Timestamps TIMESTAMPTZ, server DB time; all foreign scope columns NOT NULL.
All relations ENABLE+FORCE org RLS using transaction-local app.organization_id;
account authorization additionally in service. No CASCADE deletes.

### production_work_items

Fields: work_item_id PK; organization_id; marketplace_account_id; order_id;
order_item_id; source_item_version BIGINT>=1; required_quantity INTEGER>0;
planned_quantity INTEGER>=0 default0; remaining_quantity GENERATED ALWAYS AS
`required_quantity-planned_quantity` STORED; catalog_sku_id INTEGER NULL;
version BIGINT>=1 default1; created_at,updated_at NOT NULL server defaults.

CHECK planned_quantity<=required_quantity. Meaning: required from accepted order
item quantity; planned assigned to production planning, NOT printed/sent/delivered.
remaining is unplanned quantity, not unfulfilled marketplace quantity. No completed
quantity invented. In this first assignment-only writer planned remains0. Later
batch allocation requires separately accepted schema/service before changing it.
Observed quantity decrease below planned must create reconciliation, not force
constraint by erasing historical plans or reducing required blindly.

FK(org,account,order,order_item) -> marketplace_order_items scoped unique from0062.
FK(org,catalog_sku_id) -> catalog_skus; SKU is org-owned, its offer/product provenance
must still be validated through exact account Catalog resolution when applicable.
UNIQUE(org,account,order_item_id): one work item per concrete item.
UNIQUE(org,account,work_item_id) for child FK. Immutable identity columns including
source item reference; any UPDATE version=old+1, created_at unchanged. source version
cannot regress. No physical DELETE/TRUNCATE. Runtime SELECT/INSERT/UPDATE only.
Index(org,account,work_item_id); partial index(org,account) WHERE remaining_quantity>0.
This table contains no implied marketplace/print readiness status.

### production_assignment_receipts

Fields: receipt_id PK; org/account/work_item_id; idempotency_key TEXT exact nonempty;
request_schema_version INTEGER=1; request_payload JSONB object; request_checksum
TEXT lowercase64hex; actor_membership_id INTEGER NOT NULL; result_version BIGINT>1;
result_schema_version INTEGER=1; result_payload JSONB object; created_at DB time.
UNIQUE(org,account,work_item_id,idempotency_key).
UNIQUE(org,account,work_item_id,receipt_id) for history binding.
Composite FK to work item and FK(org,actor_membership_id)->iam_memberships(org,id).
Append-only SELECT/INSERT; no UPDATE/DELETE/TRUNCATE including triggers.

Request payload v1 EXACT fields matching AssignmentCommand: work_item_id,
expected_version,catalog_sku_id,idempotency_key,reason. No actor/org body field.
Positive integer rules and exact nonempty text same as current pure command;
do not invent narrower text limits in SQL than domain accepts.
Amendment: pure AssignmentCommand now rejects embedded U+0000 and surrogate
codepoints U+D800..U+DFFF in key/reason BEFORE checksum or persistence. Error is
constant without input reflection. Valid Cyrillic/supplementary Unicode and
combining sequences remain byte-for-byte; no normalization or length cap.
TDD6 RED cases (no error previously) ->7 GREEN including valid Unicode case.
Checksum: SHA256 of UTF8 JSON object
`{"schema_version":1,"command":<five fields>}` sorted keys, ASCII escapes,
compact separators, no NaN. Org/account bound by receipt key/FK, not request body.
Compare canonical bytes on checksum conflict, not digest alone.
Result payload exact fields: work_item_id,version,catalog_sku_id,
required_quantity,planned_quantity,remaining_quantity,source_item_version.
All values frozen from successful post-CAS row; replay uses original result, not
current row. Schema versions are not optimistic lock versions.

### production_assignment_history

Fields: assignment_event_id PK; org/account/work_item_id/receipt_id;
actor_membership_id NOT NULL; event_kind TEXT CHECK='manual_assignment';
from_version BIGINT>=1; to_version BIGINT CHECK=from_version+1;
previous_catalog_sku_id NULL; catalog_sku_id NOT NULL; reason TEXT exact nonempty;
occurred_at NOT NULL DB time.
UNIQUE(org,account,work_item_id,to_version), UNIQUE(org,account,receipt_id).
Composite FK to receipt INCLUDING work_item; actor FK org-qualified; both SKU FKs
org-qualified. Append-only SELECT/INSERT; mutation/truncate denied by grants+triggers.
History is the domain assignment audit, not a separate generic event bus table.
Deferred receipt/history check requires exactly one matching event per receipt,
same actor/target/result_version/reason/SKU; supports atomic inserts in one tx.
No history row without receipt. Failure leaves neither receipt nor event nor CAS.

## Commands and transaction outcomes

`assign(command, authenticated membership, allowed account scope)` executes inside
caller-owned transaction and shared auth/session guard, not request-body actor.
Lock order follows T1 auth/account guard then target work item FOR UPDATE.
Validate target access BEFORE scoped receipt lookup; validate Catalog ownership.

1. Same scoped key + same canonical request: return original durable result, even
   if current work item advanced. No new audit/version or changed actor attribution.
2. Same key + different request: IDEMPOTENCY_CONFLICT/409, no overwrite or new receipt.
3. New key with stale expected_version: VERSION_CONFLICT/409, no receipt/history.
4. New valid command: CAS WHERE scope AND version=expected, version+1, set SKU;
   insert successful receipt + exactly one assignment-history audit in same tx.
5. Exception/crash before commit rolls back all3 effects. No pending receipt state
   or external side effect needed. Retry after uncertain commit returns stored result.

Source ingestion cannot overwrite a manual SKU silently. Automatic resolution,
manual unassign, batch planning, cancellation reconciliation, delivered state and
archive are separate commands, not accepted by this assignment-only interface.
Reason records the operator's exact text; logs/errors must not reflect it blindly.
Authenticated permission for Production assignment remains activation policy gap;
generic shared auth helper does not select permissions on behalf of domain.

## Acceptance and rollback

Two separate actual runtime-role sessions, barriers and bounded waits:
same v/different keys => one CAS winner/one409; same key/same body=>one receipt+event;
same key/different body=>one winner/one conflict. Audit failure rolls back CAS and
receipt. Cross-org and same-org cross-account negative reads/FKs; key reuse across
scopes allowed. Reauthorization before replay, actor from real membership; version
trigger alone not CAS proof. Existing DB09/10/11 refined by this exact contract.
RLS and application predicates tested independently. All currently NOT_RUN.

Downgrade: owner lock all3 relations and assert empty with unfiltered visibility;
nonempty refuses atomically, no CASCADE or DELETE. No scheduler/collector/writer
activation, policy defaults, archive TTL, external marketplace actions or KIZ.
Production batches/sheets/artifacts remain a later explicit DDL request.
