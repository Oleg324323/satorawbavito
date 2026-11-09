# Orders and Production Stage 1 Handoff

**Date:** 2026-09-08
**Terminal:** 3 / Orders and Production
**Base:** `c88a474695569af905b294906ba604d61f3de62f`
**Branch:** `codex/arch-t3-operations`
**Scope:** local discovery, synthetic characterization, and schema request only

## 1. Outcome and hard boundary

Stage 1 inventories the locally available WB/Avito order paths and the evidence-only description of the separate production prototype. It extends the existing synthetic order fixtures and tests the already canonical pure contract in `app/modules/orders.py`.

This commit does **not** provide a common queue, Orders persistence, Production persistence, a renderer, a route, a collector, a provider action, a print action, or a frontend change. The production prototype and KIZ/Node matcher source are unavailable in the requested repository/worktree and accessible local Git history. They were not reconstructed from screenshots, HTML expectations, or discovery summaries.

No live database, GitHub, deploy target, printer, operational export, or marketplace write/action was used. During an attempted legacy regression run, the pre-existing XLSX test unexpectedly allowed listing enrichment to issue `GET https://api.avito.ru/core/v1/accounts/self` with its synthetic test credential; Avito rejected it with `403`. The run was stopped and no further external-capable legacy suite was executed. No business state was changed.

## 2. Evidence read

Fully read before this handoff:

- `/Users/bratishka/AGENTS.md` and `/Users/bratishka/.codex/memories/projects/index.md`;
- `CLAUDE.md`, repository `README.md`, applicable `AGENTS.md`, `SATORNA_ARCHITECTURE_HANDOFF.md`, and `SATORNA_SPEC_AUDIT.md`;
- `backend/docs/superpowers/reports/2026-09-08-empty-db-fix-and-architecture-remaining.md` at the base;
- `edc5439:backend/docs/satorna-orders-production-architecture-discovery-2026-09-08.md`;
- `6020d59:backend/docs/superpowers/reports/2026-09-08-kiz-pdf-pipeline-discovery.md`;
- the current Avito adapter/router/returns/XLSX paths, WB Statistics parsing, catalog/integration ORM and service, frontend serverless functions, legacy HTML, static Orders page, and related tests/fixtures.

The discovery commit is evidence only and was not cherry-picked. Current-source statements below are based on the actual `c88a474` tree, not only those reports.

## 3. Current component inventory

`Restart/concurrency` describes observed current behavior, not a target guarantee.

| Component | Source path / revision | Route or method | Owner | Persistence | Restart / concurrency | Result format |
|---|---|---|---|---|---|---|
| Pure external identity/status contract | `backend/app/modules/orders.py` at `c88a474` | `ExternalOrderIdentity`, `ExternalOrderItemIdentity`, `map_avito_status`, `map_wb_statistics_status`, source-line key builders | Orders | None; frozen values only | Restart-independent; no shared state | Python frozen dataclasses/enums |
| WB Statistics order evidence | `backend/app/repricer_bff.py:1252`, `:1284`, `:2807` at `c88a474` | provider `GET /api/v1/supplier/orders`; `fetch_period_stats_aggregates` | Existing WB reports/repricer; future observation adapter belongs to Orders | Aggregated report/cache state, not order/item storage | Per-fetch aggregation; fallback unit keys can collapse/change; no complete-manifest publication | Python dict keyed by `nmId`, later JSON report payload |
| Avito Order Management adapter | `backend/app/avito/orders.py:53`, `:65`, `:80`, `:242`, `:282`, `:362`, `:488` at `c88a474` | provider `GET /order-management/1/orders` | Existing Avito integration; future observations belong to Orders | None in client; caller may cache response | Stateless call, but browser merge can match by listing ID/title or single-row position | Pydantic `AvitoOrdersFetchResult` / order rows |
| Avito orders list | `backend/app/routers/avito_orders.py:807` at `c88a474` | `GET /api/v1/avito/orders` | Legacy FastAPI Avito router; credential cutover owned by Terminal 1 | Mutable source-cache payload keyed by organization and query; may read browser snapshot | Survives process restart through source cache; GET can call provider and write cache; no account-scoped immutable snapshot/CAS | JSON dict with rows, total, source diagnostics, optional error |
| Avito browser observation | `backend/app/routers/avito_orders.py:742` at `c88a474` | `POST /api/v1/avito/orders/browser-snapshot` | Legacy FastAPI Avito router | Whole mutable JSON payload under org-scoped source key | Last writer wins for an organization; account is not part of the cache key | JSON acknowledgement plus snapshot metadata |
| Avito extension token | `backend/app/routers/avito_orders.py:709`, `:717` at `c88a474` | `GET .../extension-token`; `POST .../regenerate` | Legacy FastAPI Avito router / Integrations | Hashed token metadata in org-scoped source cache | Rotation is multi-write without a canonical account/CAS boundary | JSON status; regenerate response contains one-time token |
| Avito returns settings/manual run | `backend/app/routers/avito_orders.py:765`, `:779`, `:788`; `backend/app/avito/returns_tasks.py:111` at `c88a474` | `GET/PUT .../returns-sync`; `POST .../returns-sync/run`; Celery `sync_returns_for_org` | Legacy Avito integration | Settings/status source cache plus mutable `avito_return_items` | Task argument is organization only; first `on_return` page; duplicate dispatch protection is not a durable account/run lease | JSON settings/status/task ID |
| Avito return candidates | `backend/app/avito/returns_orm.py:12`; `backend/app/avito/returns_store.py:97`, `:211` at `c88a474` | repository upsert/list helpers | Existing Avito return helper; lifecycle facts should move to Orders | PostgreSQL mutable latest rows, unique `(organization_id, identity_key)`; nullable string `account_id` | Survives restart; row lookup/update is org-scoped and lacks canonical account FK, append-only lifecycle, CAS, and full-window retirement | Pydantic return candidates / counts |
| Avito picking XLSX | `backend/app/routers/avito_orders.py:908`; `backend/app/avito/orders_picking_xlsx.py:12`, `:63`, `:82` at `c88a474` | `GET /api/v1/avito/orders/picking-list.xlsx` | Legacy Avito router/render helper | None; bytes generated per request from live/cache rows | Route fetches up to 100 provider pages synchronously; no frozen revision, artifact row, checksum, or idempotency record | XLSX binary response; quantity expands to repeated rows |
| Source cache used by Avito paths | `backend/app/repricer_cache/orm.py:33`; `backend/app/repricer_cache/store.py:452`, `:735` at `c88a474` | `get_source_cache` / `save_source_cache` | Shared legacy cache infrastructure | PostgreSQL `wb_repricer_source_cache` plus eligible Redis entries, unique org/source key | Survives app restart; cache replacement is not Orders event history or account isolation | JSON payload plus cache metadata |
| Marketplace accounts | `backend/app/platform/integrations/orm.py:28` at `c88a474` | ORM | Integrations | PostgreSQL canonical accounts, unique org/marketplace/external account and org/account anchors | Durable; composite account anchor exists | SQLAlchemy ORM rows |
| Product/offer/SKU resolution | `backend/app/platform/catalog/orm.py:13`, `:42`, `:77`; `backend/app/platform/catalog/service.py:178` at `c88a474` | catalog service/ORM; no Orders route | Catalog | PostgreSQL Product/Offer/CatalogSku | Durable and tenant-filtered; WB resolver distinguishes a unique SKU from ambiguous product matches | Domain views/maps; Catalog stays owner |
| Static React Orders page | `frontend/src/features/orders/OrdersPrintListPage.tsx:22`, `:68`, `:76` at `c88a474` | UI `/orders` | Frontend | Hard-coded in bundle | Restart resets to same demo rows; no commands or concurrency | HTML UI |
| Legacy production UI contract | `frontend/public/vella-production.html:17398`, `:17438`, `:17482`, `:18485`, `:18755`, `:18922`, `:19132`, `:19146`, `:19197`, `:19249` at `c88a474` | expects `/api/v1/production/**` print-list, archive, row mutation, send, SKU, mapping, import, Avito sync | Frontend legacy prototype consumer; Terminal 4 owns future frontend | Manual rows, overrides, and delivery overrides in `localStorage` at `:17801-17857`; fallback payloads in source | Browser-local last write; state differs by browser and can report local delivery success after backend failure | HTML/JS JSON consumers and browser downloads |
| Avito XLSX frontend adapter | `frontend/src/features/vella-parity/avitoOrdersXlsx.ts:31`, `:43` at `c88a474` | calls `GET /api/v1/avito/orders/picking-list.xlsx` | Frontend | Browser object URL only | Download has no durable print/send meaning | XLSX browser download |
| Frontend serverless browser snapshot | `frontend/api/v1/avito/orders/browser-snapshot.ts:39` at `c88a474` | `POST/OPTIONS /api/v1/avito/orders/browser-snapshot` | Conflicting serverless owner; Terminal 4 owns proxy cleanup | None locally; proxies upstream | Filesystem route may win before rewrite; method-specific proxy contract | Parsed JSON or raw upstream text |
| Frontend serverless returns settings | `frontend/api/v1/avito/orders/returns-sync.ts:42` at `c88a474` | only `GET/OPTIONS /api/v1/avito/orders/returns-sync` | Conflicting serverless owner; Terminal 4 owns proxy cleanup | None locally; proxies upstream | Can return 405 for FastAPI-owned PUT/POST semantics | Parsed JSON or raw upstream text |
| Vercel rewrites | `frontend/vercel.json:6-15` at `c88a474` | `/api/v1/(.*)` to backend, then `/api/(.*)` to serverless | Terminal 4 | None | Routing order/filesystem resolution can produce different semantic owner by deployment | HTTP proxy/static HTML |

## 4. Missing-source recovery blockers

### B1. Production prototype and JSON repository

The requested tree has no `backend/app/production/` and no `backend/app/routers/production.py`. `git ls-tree` at the exact base and `git log --all --` for those paths return no files/commits. A local filesystem search under the user workspace also found no copy.

The evidence report at `edc5439:...orders-production-architecture-discovery...:157-184` describes a process-local repository, whole-file JSON replacement, mappings, print-list projection, XLSX/A4/sticker renderers, and audit behavior. That summary is not executable source and does not establish byte/layout parity.

**Recovery required:** the exact repository or immutable source bundle and revision containing `app/production/{repository,storage,contracts,print_list,export,mapping_import}.py`, `routers/production.py`, JSON schema/seed provenance, tests, and fixtures. Record source and dependency-lock checksums before characterization. Until recovery, automatic/manual mapping, batches/groups, frozen sheets, production XLSX/PDF/stickers, archive/reprint, delivery, audit, and prototype concurrency parity are `BLOCKED`.

### B2. KIZ/Node/PHP print dependency

The requested tree and accessible history contain no complete `wb-pdf-matcher`, `chzParser`, `wbParser`, `matcher`, `pdfBuilder`, Node lockfile/routes/tests, PHP pool implementation, or IndexedDB implementation. The evidence-only KIZ report requires complete recovery at `6020d59:...kiz-pdf-pipeline-discovery.md:499-507` and defines unavailability as a no-go at `:601-612`.

**Recovery required:** immutable matcher repository/source bundle, exact lockfile, route adapters, PHP mutation source, browser IndexedDB schema/version, parser/renderer tests and supported label fixtures. No parser or renderer may be inferred from screenshots, generated PDFs, HTML labels, or prose. No real KIZ/code/PDF may be used for recovery tests.

### B3. WB fulfillment authority

The only locally proven WB order source is Statistics `supplier/orders`. Evidence at `edc5439:...:109-121` and code at `backend/app/repricer_bff.py:2807-2912` establish analytics/dedup/cancellation use, not FBS fulfillment readiness, deadlines, supplies, or stickers.

**Recovery required:** a locally available WB fulfillment/orders API contract with source fields, pagination/completeness, stable unit/order-line identity, lifecycle/deadline semantics, and synthetic fixtures. Until then a non-cancelled WB Statistics row remains `raw_status`, `canonical_status = null`, `mapping_state = unmapped`; it cannot create printable/sendable work.

### 4.1 Unavailable component inventory

These rows are separated from current source so an evidence summary cannot be mistaken for an executable revision.

| Component | Source path / revision | Route or method | Owner | Persistence | Restart / concurrency | Result format |
|---|---|---|---|---|---|---|
| Production repository/storage | Expected `backend/app/production/{repository,storage}.py`; unavailable at base/all local refs | Unknown from executable source | Production | Evidence-only: process memory plus whole JSON replace | Evidence-only: restart reloads snapshot; independent processes can lose updates | Evidence-only JSON snapshot |
| Production contracts/print-list | Expected `backend/app/production/{contracts,print_list}.py`; unavailable | Evidence-only print-list read/generate | Production | Evidence-only dynamic projection, no frozen revision | Cannot characterize deterministic restart/concurrency behavior | Evidence-only JSON projection |
| Production mapping/import | Expected `backend/app/production/mapping_import.py` and router; unavailable | Evidence-only list/upsert/import | Catalog owns canonical mappings; Production owns versioned override action | Evidence-only whole snapshot | Cross-process overwrite risk; exact validation/mutation order unavailable | Evidence-only JSON/XLSX import result |
| Production XLSX/A4/sticker export | Expected `backend/app/production/export.py`; unavailable | Evidence-only download methods | Production artifacts | Evidence-only ephemeral response, no artifact record | No immutable retry/checksum proof | XLSX/PDF bytes, exact formats unavailable |
| Batches/groups/frozen sheets/archive | No source path recovered | Legacy HTML expects read/archive/row mutation; evidence router parity differs | Production | Not proven; evidence says groups are computed and sheets not frozen | Not characterizable without source | Expected JSON, exact envelope unavailable |
| Delivery/send/audit | No source path recovered beyond legacy HTML expectations | Evidence-only send/list audit | Production | Evidence says audit is snapshot data; UI also uses localStorage | Actor/CAS/receipt semantics unsafe or unavailable | Expected JSON, exact envelope unavailable |
| KIZ matcher/parser/renderer | Expected separate `wb-pdf-matcher`, Node/PHP/browser sources; unavailable | Exact CLI/routes unknown | Production dependency; PostgreSQL must own allocation | Evidence-only Map/file/IndexedDB/PHP state | No single writer, allocation, retry, or restart proof possible | PDF/parse trace exact contracts unavailable |

## 5. Route ownership conflicts

| Path | Backend owner/methods | Frontend/serverless owner/methods | Conflict and disposition |
|---|---|---|---|
| `/api/v1/avito/orders/browser-snapshot` | FastAPI POST at `backend/app/routers/avito_orders.py:742` | serverless POST/OPTIONS at `frontend/api/v1/avito/orders/browser-snapshot.ts:39` | Same semantic path has two owners/envelopes. Terminal 4 must reduce hosting to transparent proxy; no Terminal 3 edit. |
| `/api/v1/avito/orders/returns-sync` | FastAPI GET/PUT at `:765`, `:779` | serverless GET/OPTIONS only at `frontend/api/v1/avito/orders/returns-sync.ts:42` | Filesystem function can reject backend PUT with 405. Terminal 4 owns proxy/rewrite correction. |
| `/api/v1/avito/orders/returns-sync/run` | FastAPI POST at `:788` | covered only by broad rewrite, not the sibling function contract | Deployment routing must prove the POST reaches FastAPI. Terminal 4 owns proxy verification. |
| `/api/v1/production/**` | No router in the exact base | legacy HTML expects multiple methods/routes | Current consumer/provider contract is incomplete and prototype source is missing. Do not create compatibility handlers from HTML alone. |
| `/api/v2/orders/**`, `/api/v2/production/**` | Reserved for canonical FastAPI backend | none allowed except transparent proxy | Single semantic owner must be backend; Terminal 1 registers routers, Terminal 4 consumes typed contracts. |

## 6. Stage 1 synthetic fixtures

Files:

- `backend/tests/fixtures/orders/avito_orders_synthetic.json`;
- `backend/tests/fixtures/orders/wb_statistics_orders_synthetic.json`.

The pre-existing top-level contract cases are unchanged. A versioned `stage1` section adds:

- the same external order/item/unit IDs in two organizations;
- the same IDs in two marketplace accounts of organization `101`;
- Avito multi-item rows, repeated listing occurrences, and quantities `2` and `3`;
- leading-zero external order, listing, and WB `srid`-like unit IDs kept as strings;
- Avito missing stable line ID with explicit order/listing/occurrence fallback;
- WB missing stable unit ID that must raise rather than fabricate identity;
- unknown, return, and cancellation cases;
- a partial manifest where absence explicitly does not mean cancellation;
- original, reordered, and changed source revisions for replay characterization.

All IDs/text are explicitly synthetic. No buyer name, phone, address, recipient, token, secret, production-like identifier, or operational export is present.

## 7. Parity matrix

Status vocabulary is intentionally limited to `PASS`, `BLOCKED`, and `NOT_RUN`. `PASS` means only the specifically named Stage 1 contract evidence passed; it does not imply runtime queue parity.

| Capability | Status | Stage 1 evidence / blocker | Next gate |
|---|---|---|---|
| Tenant/account external order identity | PASS | Pure contract plus cross-org and two-account fixtures/tests | PostgreSQL composite FK/RLS tests after Terminal 1 schema |
| Avito source-line identity | PASS | Stable line wins; listing+explicit occurrence distinguishes repeated positions; leading zeros preserved | Confirm provider line-ID semantics during ingestion characterization |
| WB source-line identity | PASS | Proven `srid`-like ID accepted; missing stable ID raises | Recover WB fulfillment contract; Statistics remains evidence only |
| Exact Avito status mapping | PASS | All known statuses plus unknown raw/unmapped/null tested; no ready fallback | Persist mapping version and evidence append-only |
| WB cancellation mapping | PASS | Boolean/cancel-marker evidence maps cancellation; non-cancelled/unknown stays null/unmapped | Persist observation only; no readiness projection |
| List/filter/stable pagination | BLOCKED | Legacy Avito GET is live/cache and page-based; no combined queue or immutable high-water mark | Orders persistence plus cache-only `/api/v2/orders` snapshot contract |
| Complete/partial coverage | BLOCKED | Synthetic fail-closed rule is characterized, but no persisted manifest/publication exists | Atomic complete-manifest publication in Stage 2 DB tests |
| Automatic Catalog mapping | BLOCKED | Catalog primitives exist, but prototype mapping source/tests absent and Orders projection absent | Recover prototype; implement account→product→offer→SKU resolution states |
| Manual Catalog assignment | BLOCKED | Prototype source/audit/CAS absent | Production expected-version/idempotency/actor transaction |
| Quantities and physical-unit expansion | BLOCKED | Aggregate quantities characterized; existing Avito XLSX expands rows, but no durable work units/KIZ allocation parity | Production model plus recovered renderer; `N` distinct units/codes |
| Scheduling and weekend rules | BLOCKED | Only evidence summary exists; executable prototype rules absent | Recover source and freeze Europe/Moscow boundary tests |
| Batches and groups | BLOCKED | Prototype source and persisted grouping absent | Recovered characterization, then versioned deterministic rules |
| Selection | BLOCKED | Legacy HTML behavior exists but backend prototype source is absent | Recovered end-to-end selected-item characterization |
| Frozen sheets | BLOCKED | Current/evidence prototype dynamically projects latest records | Immutable row/version snapshot and conflict tests |
| XLSX production parity | BLOCKED | Legacy Avito picking XLSX is tested, but unified production renderer/source is absent | Recover production exporter; test values/types/formula safety/checksum |
| A4 PDF parity | BLOCKED | Renderer source absent; evidence says prototype truncates after 48 rows | Recover source; synthetic >48 row multipage/no-clipping tests |
| Sticker 120x75 parity | BLOCKED | Renderer/matcher source absent | Recover exact source, fixture and dimension/layout rules |
| Sticker 58x40 parity | BLOCKED | Renderer/matcher source absent; 58x40 vs 58x58 unresolved | Recover exact source and resolve layout with synthetic scanner tests |
| Archive/reprint | BLOCKED | HTML expects archive; prototype route/source parity is absent | Immutable sheet/artifact revision and historical reprint tests |
| Delivery | BLOCKED | Browser localStorage can claim status; no durable intent/receipt | Durable attempt/outbox, ambiguous timeout, receipt tests |
| Cancellations/returns | BLOCKED | Stage 1 mapping/no-absence rule passes, but current returns are mutable and full lifecycle/work reconciliation is absent | Append-only events plus partial-return work reconciliation |
| Audit | BLOCKED | Evidence prototype actor is caller payload and current return rows mutate | Authenticated membership actor plus append-only command/event audit |
| Restart durability | BLOCKED | Cache/return candidates survive, but no canonical order/work/sheet state | Kill/restart/replay tests over PostgreSQL projections |
| Concurrency | NOT_RUN | Stage 1 creates no schema/runtime state; sequential calls are not proof | Two real PostgreSQL sessions under runtime role in later stages |
| Archive/delivery/print external effects | NOT_RUN | Prohibited in this stage | Separately authorized synthetic/local and later operational gates |

## 8. Schema request for Terminal 1

This is a request, not a migration. Terminal 1 owns migration/config/router registration. Use existing `INTEGER` organization/account anchors, `BIGINT GENERATED ... AS IDENTITY` for new internal row IDs, `TIMESTAMPTZ` for instants, and strings for all external IDs so leading zeros survive. Prefer `VARCHAR` plus `CHECK` constraints over PostgreSQL enums so status vocabulary can evolve additively.

### 8.1 Required tenant tables

| Table | Required columns and types | Keys, checks, FKs, indexes |
|---|---|---|
| `order_sync_runs` | `sync_run_id BIGINT`, `organization_id INTEGER`, `marketplace_account_id INTEGER`, `marketplace VARCHAR(16)`, `source_run_key VARCHAR(256)`, `adapter_version VARCHAR(128)`, `mapping_version VARCHAR(128)`, `state VARCHAR(24)`, `manifest_state VARCHAR(16)`, `requested_from/to TIMESTAMPTZ NULL`, `source_high_water_mark TEXT NULL`, `page_count/item_count INTEGER`, `payload_checksum CHAR(64) NULL`, `started_at/completed_at TIMESTAMPTZ`, `error_code VARCHAR(128) NULL` | PK and unique `(org, sync_run_id)`; unique `(org, account, source_run_key)`; composite FK `(org, account, marketplace)` to `marketplace_accounts`; checks marketplace, state, manifest state, nonnegative counts/times; indexes `(org, account, started_at DESC)` and incomplete state |
| `marketplace_orders` | `order_id BIGINT`, tenant/account/marketplace, `external_order_id VARCHAR(256)`, optional display ID, source created/updated timestamps, latest raw/canonical/mapping fields, deadline summary, `version BIGINT`, `last_seen_sync_run_id`, created/updated timestamps | PK; unique `(org, account, external_order_id)` and anchor `(org, account, order_id)`; account and run FKs; `version >= 1`; indexes for queue `(org, account, canonical_status, updated_at, order_id)` and external lookup |
| `marketplace_order_items` | `order_item_id BIGINT`, tenant/account/order, `source_line_key VARCHAR(768) NOT NULL`, optional external item/listing ID, `occurrence_index INTEGER`, `quantity INTEGER`, optional product/offer/SKU refs, `resolution_state VARCHAR(32)`, `resolution_version VARCHAR(128)`, `version BIGINT`, source timestamps | PK; unique `(org, account, order_id, source_line_key)` and anchors `(org, account, order_item_id)` / `(org, account, order_id, order_item_id)`; composite order FK; `quantity > 0`, occurrence nonnegative, version positive; account-qualified Product/Offer refs and org-qualified CatalogSku ref; resolution-state check; queue/resolution indexes |
| `order_observations` | `observation_id BIGINT`, tenant/account/run/order, optional order item, `source_kind VARCHAR(64)`, optional `source_event_id VARCHAR(256)`, `source_revision VARCHAR(128)`, `payload_checksum CHAR(64)`, minimal redacted `normalized_evidence JSONB`, `source_effective_at TIMESTAMPTZ NULL`, `observed_at/ingested_at TIMESTAMPTZ` | Append-only PK/anchors; composite run/order/item FKs; unique source event when present and unique `(org, account, source_kind, payload_checksum)` replay key; timestamp/checksum checks; indexes by order/item and observed time |
| `order_status_observations` | `status_observation_id BIGINT`, tenant/account/order, optional item/observation, `raw_status VARCHAR(128) NULL`, `canonical_status VARCHAR(40) NULL`, `mapping_state VARCHAR(16)`, `mapping_version VARCHAR(128)`, `evidence_source VARCHAR(128)`, effective/observed timestamps | Append-only; exact raw status preserved; checks allowed canonical/mapping states, `raw_status IS NULL OR (raw_status <> '' AND raw_status = btrim(raw_status))`, and `mapped iff canonical non-null`; composite FKs; unique replay identity; timeline index `(org, account, order_id, effective_at, id)` |
| `order_lifecycle_events` | `lifecycle_event_id BIGINT`, tenant/account/order, optional item/observation, `source_event_key VARCHAR(256)`, `event_kind VARCHAR(32)`, `source_effective_at/observed_at TIMESTAMPTZ`, minimal `evidence JSONB` | Append-only; unique `(org, account, source_event_key)`; event-kind check; composite order/item/evidence FKs; timeline indexes; no cascade delete of order/item |
| `order_deadlines` | `deadline_id BIGINT`, tenant/account/order, optional item/observation, `deadline_kind VARCHAR(40)`, `source_deadline_at TIMESTAMPTZ NULL`, `computed_deadline_at TIMESTAMPTZ NULL`, `rule_id/rule_version VARCHAR(128) NULL`, `timezone VARCHAR(64)`, observed/effective timestamps | Append-only; exactly one/both source/computed values with provenance; computed value requires rule/version; timezone explicit; composite FKs; deadline queue index |
| `order_sync_coverage` | `coverage_id BIGINT`, tenant/account/run, `coverage_kind VARCHAR(32)`, requested bounds, cursor/page bounds, `is_complete BOOLEAN`, `manifest_checksum CHAR(64) NULL`, `completed_at TIMESTAMPTZ NULL` | One immutable coverage row/version per run/kind; complete requires checksum/completed time; partial cannot authorize absence reconciliation; composite run FK; index `(org, account, completed_at DESC)` |
| `order_sync_memberships` | `membership_id BIGINT`, tenant/account/run/order, optional item, `coverage_role VARCHAR(24)`, `observed_at TIMESTAMPTZ` | Surrogate PK; partial unique `(org, account, run, order) WHERE item IS NULL` and `(org, account, run, order, item) WHERE item IS NOT NULL`; composite FKs; membership indexes; only a completed full coverage run can drive missing-row reconciliation |
| `marketplace_status_mappings` | global/reference `mapping_id BIGINT`, marketplace/raw status/canonical nullable/state/version/evidence/effective range | Global reference, no `organization_id`; unique marketplace/raw/version; additive immutable versions; never overwrite historical mapping semantics |

### 8.2 Catalog anchor dependency

Catalog remains the owner of Product/Offer/CatalogSku; Orders must not copy them. Before account-qualified item FKs can target internal product/offer IDs, coordinate additive catalog anchors:

- unique `(organization_id, marketplace_account_id, marketplace_product_id)` on `marketplace_products`;
- unique `(organization_id, marketplace_account_id, marketplace_offer_id)` on `marketplace_offers`;
- if an offer FK also includes product, unique `(organization_id, marketplace_account_id, marketplace_product_id, marketplace_offer_id)`.

The existing product external identity `(organization_id, marketplace_account_id, external_product_id)` is already unique. `CatalogSku` is organization-owned, so `(organization_id, catalog_sku_id)` remains the correct SKU FK. Every resolution query must constrain organization and account simultaneously; an org-only offer/product link is insufficient for two accounts in one organization.

### 8.3 RLS and runtime context

For every tenant table above:

```sql
ALTER TABLE <table> ENABLE ROW LEVEL SECURITY;
ALTER TABLE <table> FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation_<table> ON <table>
USING (
  organization_id = NULLIF(current_setting('app.organization_id', true), '')::integer
)
WITH CHECK (
  organization_id = NULLIF(current_setting('app.organization_id', true), '')::integer
);
```

The service must set transaction-local context through the existing `set_config('app.organization_id', ..., true)` pattern before any read/write. Provider fetch happens outside the DB transaction. Immediately before publication, the transaction revalidates `(organization_id, marketplace_account_id, marketplace, active status)` and takes the necessary account/run lock. Application predicates remain mandatory and do not replace RLS; RLS does not replace account predicates/composite FKs.

Append-only observation/status/lifecycle tables must grant the runtime role `SELECT, INSERT` but not `UPDATE, DELETE`; migration/maintenance roles are separate. If ownership privileges would bypass that grant model, add a database trigger that rejects runtime updates/deletes and test it under the runtime role.

Runtime-role integration tests must run without `BYPASSRLS` and prove: missing context sees/writes nothing; correct tenant succeeds; wrong tenant fails; same org/wrong account cannot bind an order/item; collectors/workers use transaction-local context; pooled connection reuse does not retain tenant context.

### 8.4 Publication, version, and time semantics

- Provider fetch and payload validation occur outside transactions.
- A run becomes `complete` only after all pages and a checksum-protected manifest are present.
- Run, coverage, memberships, observations/events, and current projections publish atomically in one short transaction.
- Exact replay with the same source identity/revision/checksum is a no-op and returns the prior run/result.
- Changed source evidence appends an observation and increments the affected projection version; reordering does not.
- Partial coverage appends evidence but cannot cancel/close missing orders.
- Source effective time, provider observed time, local ingestion time, and derived deadline time are separate `TIMESTAMPTZ` fields stored in UTC; rule timezone is explicit (`Europe/Moscow` when applicable).
- Out-of-order evidence is retained but may update the current projection only through a documented monotonic comparator (source revision/event ordering first, then effective/observed time with stable ID tie-break); a regression must emit conflict/reconciliation evidence, never silently replace newer state.
- Returns/cancellations append events and change eligibility/projection; they never physically delete orders/items/history.
- Unknown status always retains exact raw text, `mapping_state = unmapped`, and nullable canonical status.
- Missing stable WB line identity is stored only as quarantined observation evidence; it does not create a canonical item/work unit or synthetic source-line key.
- Raw provider payload/PII is not copied into the normalized observation by default. Any later raw retention needs a separate encrypted object-storage, access-log, and retention decision.

### 8.5 Downgrade guards

- Make the first schema migration additive and leave all writers/readers disabled by default.
- Downgrade may drop a newly created table only when it is empty; otherwise raise with an explicit data-preservation blocker.
- Never cascade-drop Catalog/Integrations anchors or Orders history.
- Do not remove a mapping version referenced by observations/sheets.
- Do not collapse append-only observations/events into current projections during downgrade.
- After any canonical write cutover, rollback means fence canonical writers and use a verified read path; it must not reactivate the legacy whole-JSON writer over newer canonical state.
- One Alembic head, upgrade-from-empty, upgrade-from-current, downgrade-on-empty, and downgrade-refuses-nonempty tests are required from Terminal 1.

## 9. Compatibility and concurrency test request

After the Terminal 1 schema commit, Stage 2 may begin with Avito only and WB observation-only ingestion. Required tests before a route or Production consumer:

1. Two organizations and two accounts in one organization can store identical external order/item IDs without collision.
2. Same account/order/source-line replay is idempotent; changed evidence produces exactly one new observation and projection version.
3. Two real PostgreSQL sessions concurrently publish the same run: one logical run/projection wins without duplicate observations or lost state.
4. A deactivated/rebound account between fetch and commit fails publication.
5. Complete pagination publishes one manifest atomically; injected page failure leaves no complete run/current partial projection.
6. Partial fetch never cancels an absent order.
7. Older status/deadline evidence is retained but cannot silently regress a newer current projection.
8. Unknown Avito and non-cancelled WB Statistics statuses remain raw/unmapped/null after persistence round-trip.
9. Missing WB stable unit evidence cannot create an item/work unit.
10. Forced-RLS runtime role proves missing/correct/wrong tenant and same-org wrong-account behavior in separate sessions.

Sequential service calls do not count as concurrency proof.

## 10. Verification record

TDD RED before fixture implementation:

```text
pytest -q tests/test_orders_stage1_characterization.py
10 failed in 0.05s
expected failure: KeyError: 'stage1'
exit 1
```

Focused GREEN after fixture implementation:

```text
pytest -q tests/test_orders_stage1_characterization.py tests/test_orders_contract.py
56 passed in 0.05s
exit 0
```

Broader legacy characterization attempt:

```text
pytest -q tests/test_avito_orders.py -x
1 failed, 11 passed, 2 warnings in 8.96s
failure: test_avito_orders_picking_list_xlsx_matches_avito_order_rows
assertion: KeyError: 'F6'
captured cause: pre-existing listing enrichment attempted
GET https://api.avito.ru/core/v1/accounts/self and received 403
exit 1
```

An earlier attempt with the minimal temporary verify environment stopped during collection because that environment lacked `cryptography` (`exit 2`). Re-running with an existing complete local virtualenv reached the legacy test above. This is not caused by the Stage 1 fixture/test changes, but the legacy suite is not claimed green and must be made hermetic by its owner before reuse in this scope.

Final local checks before staging:

```text
pytest -q tests/test_orders_contract.py tests/test_orders_stage1_characterization.py
56 passed in 0.04s
exit 0

python -m compileall -q app/modules/orders.py tests/test_orders_contract.py \
  tests/test_orders_stage1_characterization.py
exit 0

python -m ruff check tests/test_orders_stage1_characterization.py
All checks passed!
exit 0

python -m json.tool tests/fixtures/orders/avito_orders_synthetic.json
python -m json.tool tests/fixtures/orders/wb_statistics_orders_synthetic.json
both exit 0

git diff --check
exit 0

rg -n "httpx|requests|sqlalchemy|fastapi|celery" \
  backend/tests/test_orders_stage1_characterization.py backend/app/modules/orders.py
no matches; expected exit 1
```

## 11. Next permitted stage

Wait for Terminal 1's reviewed schema commit. Then Stage 2 may implement `app/orders/` persistence and ingestion only:

- Avito complete-manifest ingestion can proceed against synthetic provider fixtures;
- WB Statistics can append identity/cancellation observations but cannot publish fulfillment readiness, deadlines, stickers, or printable work;
- no `/api/v2/orders` read route until cache-only stable snapshot/pagination semantics are proven;
- no Production work items until Orders item identity/resolution is durable;
- no batches/sheets/artifacts/delivery/KIZ work until the production prototype and matcher source blockers above are resolved and characterized.

`app/modules/orders.py` remains the pure canonical contract and must not be converted into a package. Future persistence belongs in `app/orders/`.
