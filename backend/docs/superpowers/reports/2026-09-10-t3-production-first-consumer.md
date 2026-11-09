# Dormant Production P1 Consumer

Date: 2026-09-10. T3 branch `codex/wb-live-t3-reads`.
Parent `6b774bcab013a31542f9937e37dc23a88aea31a5`.
Scope approved by ROOT: service and focused tests over the existing real 0069
schema, without DDL, router, bootstrap, shared config or grants.

## Implemented Boundary

`app/orders/production_service.py` implements three trusted, dormant entrypoints:

- `read_production_work_item(session, *, principal, account, work_item_id)`:
  returns a frozen `ProductionWorkItem` only after current source proof and commit.
- `create_production_work_item(session, *, principal, account, order_item_id,
  expected_source_item_version)`: returns `ProductionCreation(work_item_id,
  replayed)`. Identity is the existing org/account/order-item unique constraint.
  Initial quantities/version/SKU/witness use accepted database defaults; a replay
  does not reset or refresh stored work.
- `assign_production_work_item(session, *, principal, account, command)`:
  accepts the existing pure `AssignmentCommand`; returns
  `ProductionAssignment(result: AssignmentResult, replayed)` after commit.

These are internal Python contracts, not HTTP DTOs. Integers have not been exposed
as JavaScript numbers. Actor is the authenticated membership, never command data.
No public route or operator UI is activated by this package.

Every operation owns a clean PostgreSQL READ COMMITTED physical root through the
existing publication guard. Fixed shared `PRODUCTION_*_PERMISSIONS` require read
for writes, including replays. No profile receives automatic Production rights.
Exact account context and retained account lock precede work/source operations.

Source proof follows scoped Orders item, parent current run, complete manifest,
immutable account binding and current membership observation. It decodes and
checks canonical observation checksum, order/provider identity, source/adapter,
raw/canonical mapping and exact item identity/quantity. Expected source-item
version mismatch fails closed, including replay. It never fills old run binding
from current account metadata. Work quantity/source version are not refreshed.

Assignment locks current work, checks source BEFORE receipt lookup, and compares
full scoped idempotency text plus canonical bytes/checksum/request. Original
seven-field receipt result is returned before checking today's work CAS version.
Same key/different payload is `IDEMPOTENCY_CONFLICT`; stale version is
`VERSION_CONFLICT`. A new same-SKU command still creates a version transition.
CatalogSku is organization-owned and checked as such, not assigned a fake account.

Receipt, CAS update, history and audit share one transaction and one command
timestamp. Existing 0069 captured deferred witnesses prove the transition.
Replay adds a separate authenticated audit but no assignment receipt/history or
work update. Each write is immediately preceded by shared guard revalidation;
final commit validation still applies. SQL/domain errors are mapped outside the
original exception handler so returned safe errors do not retain private causes.

## Verification Evidence

Source-first implementation follows ROOT's current bounded execution policy;
no missing-import RED cycle is claimed. Initial local lint found a multiline SQL
quote typo, fixed before imports/tests; shared account-context helper was checked
against its keyword-only signature before the first run.

| Gate | Actual result |
| --- | --- |
| Pure ID/root admission | 6 PASS, 11 deselected, 1.91s, exit 0 at initial collection |
| First approved PG group | 11 PASS, 11 deselected, 13.10s, exit 0 |
| Remaining approved PG negatives | 5 PASS, 17 deselected, 10.03s, exit 0 |
| Existing pure codecs/permissions | 99 PASS, 0.42s, exit 0 |
| Scoped Ruff, compileall, diff check | PASS |

Alembic ScriptDirectory reports one current branch head `20260910_0080` and
confirms 0069 follows 0068. No migration imports were executed against a database
for this metadata check; ROOT's newer integration head is a separate lineage.

Actual PG coverage: create and create replay; initial quantities; read; first and
second assignment; original-result replay after newer assignment; changed key
payload and stale CAS; same-org other-account denial; foreign-org CatalogSku;
revoked assign right/session/account binding and changed source before replay;
read-only/admin-without-explicit-grants/create-without-read creation denials;
changed source before creation; final-auth rollback; injected history SQL failure
rolling back receipt/CAS/history with safe error context.

Four concurrency cases use distinct physical PostgreSQL PIDs and observe both
callers blocked while an independent transaction retains the account lock:
creation, same-key/same-payload, same-key/different-payload, different-key/same-CAS.
After releasing the holder, outcomes prove one identity/transition and exact
replay or conflict, not sequential-call concurrency claims.

Tests reuse the existing current-head Orders publication fixture with synthetic
explicit grants, inert synthetic credential metadata and the actual existing
`ops/runtime-db-role.sql`. All execution stays in the approved Unix-only sandbox
and exact owned allocator. Both PG processes naturally exited and teardown
asserted their exact owned database/roles absent. Slot released to ROOT for T2/T1.
No provider request, credential decryption, real data export, production, physical
print, KIZ/matcher, GitHub, push or deploy operation occurred.

## Runtime Dependency Inventory

T1 owns future least-privilege activation. This gate does NOT prove the final
`wb_live_api` role. Besides existing live-auth/account-lock grants, the consumer
needs scoped source SELECT and PostgreSQL row-lock privileges for source
items/parents/runs and CatalogSku; membership/observation SELECT; work items
SELECT/INSERT/UPDATE; receipt/history SELECT/INSERT; their identity sequence
USAGE; exact three production codec helper EXECUTE; existing ORM audit INSERT
and sequence access. Deferred witnesses also read receipt/history. No grants
were expanded in this package. Current work-item read takes a row lock, so the
service's read permission is not a promise of a SELECT-only database role.

## Remaining Gates And Rollback

This is not Production parity or print/send eligibility. Planning, automatic
assignment, unassign, source refresh/reconciliation, batches/groups/calendar,
frozen sheets, XLSX/PDF/stickers, archive/delivery and public HTTP remain separate.
WB Statistics never supplies fulfillment readiness. Current source bindings do
not add an A-to-B-to-A epoch guarantee absent from the accepted run contract.

Dedicated new-service tests for unbound historical runs, simultaneous source
change versus creation, raw-SQL forged witnesses, and final limited API-role
composition were NOT_RUN here. Existing T1 physical schema/RLS tests are prior
evidence, not freshly repeated service acceptance. Full suite, high-volume
history latency and network/provider behavior were not measured.

Rollback removes dormant consumer code only and preserves additive schema and
immutable history. Never delete receipts or reenable an obsolete whole-JSON
writer after canonical writes. Root/T1 must review and close remaining activation
gates before exposing an operator command route.

Focused commands from `backend`, under the existing scrubbed safe environment:

```sh
python -m pytest -p no:cacheprovider -q tests/test_production_service.py
python -m pytest -p no:cacheprovider -q tests/test_production_assignment_result.py tests/test_production_assignment_serialization.py tests/test_production_assignment_text.py tests/test_production_permissions.py
python -m ruff check app/orders/production_service.py tests/test_production_service.py
python -m compileall -q app/orders/production_service.py tests/test_production_service.py
git diff --check
```

PG selection requires coordinator admission plus `ORDERS_TEST_USE_LOCAL_CLUSTER=1`
and the existing `orders-tests.sb`; never substitute application/live DATABASE_URL.
Independent manual critic pass reviewed auth-before-receipt, source evidence,
physical commit/witness ordering, privacy, integer bounds and scope. The locally
configured preflight-critic skill file remains absent; no external review used.
