# T2 → T1: bounded operational orders history storage

Status: implementation request, not persistence acceptance. T1 owns DDL,
repository and authority. T2 owns transport/orchestration. Root owns registration
and the combined PostgreSQL gate. No real provider execution is authorized.

## Existing committed adapter and facts

`b5aa69d136d49d48f4d78f167e0e0481161330f9`:
`backend/app/wb_live/statistics_orders.py` and
`backend/tests/test_wb_live_statistics_orders.py` (35 focused tests passed).

Reuse `app.orders.ingestion.OrderObservation`,
`app.orders.serialization.serialize_observation` / `observation_checksum`,
existing order evidence/projection services and exact account ownership.
Do not create a parallel order domain or feed an entire history into
`OrderManifest`: that interface materializes all observations.

Source identity: `wb-statistics-supplier-orders`. Existing live sources
`content` / `prices` remain unchanged. This source is preliminary operational
history, not all orders, fulfillment readiness, settlement or final profit.

Official rules checked 2026-09-10:
<https://dev.wildberries.ru/docs/openapi/reports> (supplier/orders).
GET `/api/v1/supplier/orders`, query `dateFrom=<exact native value>&flag=0`.
No invented `dateTo`, offset, limit or timestamp increment. Only EMPTY is
terminal; roughly 80,000 rows is not a terminal condition. Cursor moves to the
last native `lastChangeDate`, inclusively. Same/nonadvancing cursor is a visible
blocker, never a guessed skip. Retention is 90 days; unpaid orders may be absent.
Unknown/base token interval is 10,800 seconds; no worker sleeps or JWT entitlement
inference. All source calls, including crash retries, share a persisted quota.

## Exact adapter output

`iter_orders_page(chunks, organization_id, marketplace_account_id, date_from,
observed_at)` emits:

- `HistoryOrderRow(observation: OrderObservation, nm_id: int,
  barcode: str | None, source_row_checksum: str)`.
- Exactly one final `OrdersPageEnd(next_date_from: str, terminal: bool,
  row_count: int, raw_checksum: str, request_checksum: str)` after valid array
  closure AND transport EOF. Every hash is lowercase SHA-256 hex.

Rows preceding the end marker are provisional. Adapter exceptions or abandoned
iteration do not authorize publication. Native srid is the account-scoped order
and one-unit identity. nmId is a product, not a size. No synthetic SKU identity.
Unknown noncancelled status stays unmapped. Year0001 cancellation sentinel is
not cancellation evidence. Source-naive time means Moscow; exact wire cursor
is retained separately from normalized timestamps.

`source_row_checksum` binds original JSON object bytes. It is provenance, not
entity identity. Canonical observation checksum excludes `observed_at`; reuse
it for semantic replay. Changes only in formatting must not create new orders.
Retain the raw checksum separately; never rewrite checksums to force equality.

## Accepted repository protocol

Accepted T1/root amendment: **only methods 1–3 are implemented in this package**.
`begin_history_page` returns a string UUID. Method 3 atomically marks the staged
page as published source evidence and advances source cursor; this is NOT
canonical Orders queue publication. T3 owns the bounded canonical projector
from committed pages, independently of fetching the next page. No whole-page
ORM upsert is authorized. History initialization requires an explicit validated
account-owned `dateFrom`; no automatic now-minus90-days start policy.

Use existing guarded `BatchLease` / `JobLocator` and `WbLiveError` safe codes.
All calls are exact org/account/job/run/lease scoped; no UUID-only lookup.

1. `begin_history_page(lease, *, request_checksum) -> page_id: str`.
   Requires a fetch-phase lease; binds its exact dateFrom, actor/authority,
   account incarnation and credential generation. An abandoned unsealed attempt
   is superseded, not resumed from an invented HTTP byte/row offset.
2. `stage_history_rows(lease, *, page_id, first_ordinal, rows) -> bool`.
   Ordinals start at zero. Immutable tuple, at most 1,000 typed rows per call.
   Validate all ownership, domain values and closed serialization before insert.
   Same ordinal + same semantic/source hashes replays; differing data conflicts.
   Each local transaction ends before requesting more provider bytes.
3. `commit_history_page(lease, *, page_id, end, next_due_at) -> bool`.
   Seal EOF only after exact staged count/contiguity, stored request hash,
   cursor progression and renewed publication-authority checks. Bound raw digest
   and terminal claim to this page. Atomically publish source evidence and
   advance its native cursor. Nonempty pages keep source coverage partial;
   empty EOF completes this preliminary source run. Canonical queue projection
   is neither performed nor implied by this transaction.

Existing `claim_batch`, `defer_batch`, `fail_batch`, `due_jobs` remain owners of
admission, retries and scheduling. Claim reserves the 10,800-second history
quota BEFORE HTTP, including crash paths without success/defer. T2 task locators
contain three IDs only; no projection capability is implied by these tasks.
Lease120s and task hard bound100s currently apply; no implicit lease extension.
Future T3 projection uses separate bounded progress over committed page evidence.
No proposed projection method is required or called by this package.

## Storage guarantees / schema request

- Page rows carry internal org/account composite foreign keys, job/run/page
  scope, request checksum, input cursor, EOF receipt and lifecycle state.
- Staging identity is page + ordinal; unique native srid within a page.
  Cross-page inclusive-boundary replay uses account/source/srid + semantic
  observation checksum; different source revisions remain evidence, not duplicates.
- Append-only receipts/audit bind lease, authority and published range; FORCE
  RLS and explicit worker grants. No raw token/provider exception or full raw
  provider payload is stored. Existing normalized observation schema is reused.
- Incomplete staging is never visible as published history. Published earlier
  pages may be visible with explicit partial/source coverage, never full-run ready.
- DB failure is explicit; no file/memory fallback. No deletion based on omission.
- Persist source progress: fetching/staged/published counts and
  safe error, next_due, last confirmed native cursor. UI/read ownership stays T3.
- Bounds: chunks64KiB, JSON row128KiB, native response128MiB, 100k rows hard
  safety cap (over-limit is an explicit error, not truncation). Do not accumulate
  complete pages or history in application memory. Staging batches <=1000.

## Acceptance and crash gates

Fixture-only T2 transport/orchestration first. Root-controlled actual PG gate:
two claimers; wrong org/account; revoked authority; credential replacement;
duplicate chunk; conflicting ordinal; inclusive page-boundary replay;
same-cursor blocker; DB rollback; restart after stage before EOF; after EOF
source publication before ACK; duplicate delivery. Prove no cursor advance or
visible page from unsealed staging, no double publication on replay, and quota
reservation surviving crash. A published page may be followed by another fetch
without waiting for canonical projection. That separate T3 acceptance must
prove bounded canonical projection/replay/restart and must not label this
source-only package as queue-ready. No price or messaging write is authorized.
