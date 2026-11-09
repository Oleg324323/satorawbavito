# T2 WB current-source persistence — actual 0077 composition

**IMPLEMENTED / UNVERIFIED**. Consumes T1 source
`b21f2166886ade7637b1b9594b5b28c1e38d3e80`, full physical handoff, exact existing
CollectionRequest, price/stock parsers and runtime column grants. No source fetch,
provider action, current writer, schema/grant or configuration change; no tests,
review, imports, compile/lint, PG/Redis execution. All final gates are deferred.

## Exact dormant participant

`app.modules.wb_current_sources_postgres.CurrentSourceTransaction(session, request)`
requires a caller-owned active clean Engine-bound physical PostgreSQL READ COMMITTED
root and exact typed CollectionRequest. Only the two closed parser/source pairs
are accepted. Scope is org/account/source/parser/request hash; account is serialized
before domain operations. Same root/Connection/isolation is checked before each SQL
statement. It sets account RLS context, not authentication. Caller must already
hold the full live source/account/credential guard through physical commit.

- `create(request_key=UUIDv4, started_at=aware_time, resolved_credential=...)`
  requires the actual existing paired wrapper. It copies binding metadata only,
  never reads/reveals the secret. Exact descriptor/credential generation/request
  intent is immutable. Scoped request replay compares all immutable fields and
  bytes, no replacement. New run omits run ID/sequence and uses DB RETURNING.
- `append_page(run_id, typed_page, page_no=..., http_status=...)` projects strict
  typed BIGINT presence/value fields without bool/float conversion. Stores page
  metadata + complete facts; exact replay compares page slot and all typed facts
  before returning no-write. Different number/offset intent conflicts. Unknown
  price sizes remain counts only; no invented ID or lost identity becomes complete.
  Transport request_id is deliberately omitted, not accepted as supposedly safe
  because a token might match its syntax. Source observed time remains NULL.
- `finalize(run_id, safe_error_code=None, publish_expected_version=None)` derives
  counts/manifest from persisted accepted pages/facts. A short last page plus fully
  identified price sizes and no source failure permits complete; unidentified
  products force partial/SOURCE_IDENTITY_UNRESOLVED. No pages becomes failed;
  accepted prefix becomes partial, never erased. Error vocabulary is closed.
- `publish_expected_version=None` seals history only. Integer0 requires absent
  head; positive N CASes exact head/version and newer admitted sequence. Finalize
  and head operation occur in the same physical root. The DB's xmin check remains
  authoritative. CAS/unique failure must roll back the caller's whole transaction,
  never savepoint-swallow and commit the terminal. No manual audit INSERT exists.
- Exact sealed terminal replay returns history without UPDATE/audit. A publish
  request against an already sealed run conflicts; historical runs cannot later
  be installed as current. DB finished_at/published_at/audit identity remain owned
  by the existing triggers, not supplied by this participant.

## Typed reads, no freshness fiction

`latest_attempt()` returns immutable CurrentRun for exact request context ordered
by ingest_sequence; `current()` separately returns current head version and its
complete run. `read_page(run_id,page_no=...)` returns immutable StoredSourcePage
with run identity/state/provenance, receipt/status/hash and typed rows. Stock uses
the original WarehouseStockObservation/StockCount; price uses StoredPriceProduct
with original presence fields, known sizes and explicit source/unresolved counts.
No unknown-size price row is fabricated, no CatalogSku is inferred, and no partial
new run is silently mixed with a previous current run. Original metadata carries
external account/ref and credential ID/generation for future live comparison.

No TTL/default freshness or repricer eligibility is attached to these reads.
Source receive time is not source-observed time or historical stock. Daily business
dates and corrections are not implemented against0077; the next actual daily
schema is required. Seller/club fields remain distinct, not buyer/SPP substitutes.

## Errors, ownership and acceptance gaps

Statement failures emit fixed source_invalid/conflict/persistence_failed, not raw
SQL DETAIL. Unique/deadlock/serialization conflicts force caller rollback. The
participant does not own final commit: the future service must safely translate
deferred graph/commit failures and return success only after physical commit.
Likewise dataclasses/actual paired wrapper are not permission to fetch/publish;
source worker/bootstrap/live authorization and transport-origin integration remain
T1/T2 dependencies. There is no global factory, File/memory fallback, scheduler or
external exactly-once claim. SQL consistency does not prove the raw checksum was
parsed into those typed facts; transport/decoder composition must prove that.

Final centralized gates: original10 pinned SQL/request/raw/manifest vectors;
actual runtime roles/no-context/cross-account RLS; immutable request binding and
generation changes; DB run/sequence ownership; exact page/fact replay and changed
slot; strict nullable grain/duplicate counts; unresolved/empty/large IDs and money;
page gap/overlap/terminal and receipt ordering; partial prefix; finalize/head atomic
rollback and two-session CAS; newer-first/old-late finishing; sealed replay cannot
publish; automatic reciprocal audit and downgrade; typed read isolation/restart;
service live authority/provider-free collection. None has run for this source.

Rollback removes the dormant participant/report; no persisted rows, operational
grants or flags were changed. Stage5 is not completed by this participant alone.
