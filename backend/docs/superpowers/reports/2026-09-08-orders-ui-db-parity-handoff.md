# Orders: UI handoff, DB gates and legacy edge cases

Parent revision: `1afd5a2054f872b814b3cf3b2b6e00146cf87c40`.
Owner: Terminal 3. No runtime, schema, frontend or router changes in this slice.

## Verification scope

New characterization tests exercise existing code, not new functionality.
There is no claimed implementation RED/GREEN cycle or corrected legacy behavior.
The focused XLSX/returns suite passed 27 cases before formatting.
Combined offline regression after formatting: **187 passed, 2 dependency
deprecation warnings in 1.73s**, exit 0. Ruff/compileall/diff check exit 0.
There are 18 additional cases relative to the previous 169-case gate.
All payloads added here are synthetic. Python network audit guard is enabled.
No operational workbook is saved, no provider/production call or print occurs.

Run from `backend` using the existing project environment:

```sh
python -m pytest -p tests.orders_offline_plugin -q \
  tests/test_orders_contract.py tests/test_orders_stage1_characterization.py \
  tests/test_orders_xlsx_characterization.py tests/test_orders_offline_guard.py \
  tests/test_avito_orders.py tests/test_avito_returns.py \
  tests/test_orders_ingestion.py tests/test_orders_read_contracts.py \
  tests/test_orders_avito_adapter_characterization.py \
  tests/test_orders_returns_characterization.py
python -m ruff check tests/test_orders_xlsx_characterization.py \
  tests/test_orders_returns_characterization.py
python -m compileall -q tests/test_orders_xlsx_characterization.py \
  tests/test_orders_returns_characterization.py
```

Self-review checked that known unsafe legacy behavior is labelled BLOCKED,
wire fragments are not presented as mounted APIs, and DB scenarios are NOT_RUN.
The requested local preflight-critic skill file was absent; review was performed
directly against the implementation and tests. No full-backend suite claim.

## Parity findings

| Surface | Evidence | Gate |
|---|---|---|
| XLSX text fields | `test_all_provider_text_columns_are_inline_strings_without_external_links` | PASS: formula-looking strings remain inline strings in all populated columns; no formula/hyperlink/external-link parts |
| XLSX whitespace/Cyrillic | `test_control_prefixes_and_whitespace_remain_literal` | PASS at XML level; not a visual Excel layout claim |
| XLSX 1000 units | `test_thousand_units_have_contiguous_rows_and_complete_text` | PASS: exact expanded rows/text and dimension |
| XLSX forbidden controls | `test_legacy_renderer_emits_invalid_xml_for_forbidden_control_characters` | BLOCKED: NUL and vertical tab yield XML parse failure; canonical renderer must reject or explicitly sanitize under a versioned rule |
| Partial returns | `test_order_level_partial_return_marker_extracts_every_item_without_unit_evidence` | BLOCKED: order-level marker copies all items/quantities, not proof each unit returned |
| Unknown/cancel/dispute | status tests in `test_orders_returns_characterization.py` | PASS characterization: no candidate without return marker; arbitrary nonempty marker admits unknown status |
| Account separation | matcher account tests | BLOCKED: different accounts can match; same external order ID is excluded even across accounts |
| Allocation | zero-quantity/repeated-match test | BLOCKED: zero quantity can match; repeated matching does not reserve inventory |
| Tie determinism | equal-score limit test | BLOCKED: tied candidates are truncated in input order |

Implementation evidence: `app/avito/orders_picking_xlsx.py` functions `_cell`
and `_picking_rows`; `app/avito/returns.py` functions `extract_return_candidates`,
`_same_order`, `_score_match`, `match_return_candidates`. Test names above are
stable searchable evidence anchors. None of these legacy behaviors is approved
as canonical identity, authorization, return quantity or allocation authority.
Do not silently fix them while asserting unchanged renderer/matcher parity.

## Terminal 4 contract handoff

Status: proposed wire binding, NOT an implemented HTTP/OpenAPI contract.
Authoritative implemented internal models are in `app/orders/contracts.py`,
`app/orders/ingestion.py`, `app/modules/orders.py`, `app/modules/production.py`.
T1 owns registration/auth integration; T4 owns frontend/serverless. Do not wire
the UI to an imaginary endpoint or generate competing frontend-owned schemas.

Suggested binding uses snake_case matching current internal model field names.
Choose aliases/envelope once during API integration, then test and generate from
backend OpenAPI. The examples below are fragments, not complete response models.

### Field semantics

| Field | Binding and client obligation |
|---|---|
| organization/account IDs | Scope comes from authenticated membership; client selection cannot grant access |
| external_order_id, external_item_id, source_line_key | Strings; preserve leading zeros; never parse as numbers or derive identity from title |
| item_identity | Includes parent scoped order; repeated listing occurrences remain separate rows |
| status.raw_status | Exact provider string; WB permits null; do not trim or alias |
| status.canonical_status | Nullable; null is unknown, not ready or accepted |
| status.mapping_state/version/evidence_source | Display/retain alongside raw status; version strings are opaque |
| resolution.state | resolved/unmapped/ambiguous/stale/manual_override are distinct; never infer from nonempty title |
| catalog_sku_id | Nullable; existing Catalog reference only, no new product identity |
| row_version | Positive integer, required for commands; do not increment optimistically as persisted truth |
| readiness_blockers | Explicit reasons; empty list alone is not print/send authorization |
| deadlines | Empty tuple/JSON array means absent evidence, not deadline zero; aware timestamps retain provenance |
| source_at/computed_at | Separate instants, not interchangeable; computed value requires rule ID/version |
| snapshot_id/high_water_mark | Opaque publication metadata, not provider page offsets |
| account_coverage | Entry for every authorized selected account, including missing evidence |
| next_cursor | Nullable opaque continuation token; null ends this retained snapshot only |

Unknown Avito status fragment:

```json
{
  "raw_status": "synthetic_unknown",
  "canonical_status": null,
  "mapping_state": "unmapped",
  "mapping_version": "avito-order-status-v1"
}
```

Assignment command example (internal field binding):

```json
{
  "work_item_id": 101,
  "expected_version": 3,
  "idempotency_key": "synthetic-assignment-001",
  "catalog_sku_id": 201,
  "reason": "Synthetic reviewed assignment"
}
```

No actor or organization field is accepted as command authority. Server obtains
actor from membership and validates org AND account across all Catalog links.
Scope and target precede lookup of a persisted idempotent result.

### Error and retry behavior

| Condition | API binding requirement | UI behavior |
|---|---|---|
| New key + stale expected version | HTTP 409 / `VERSION_CONFLICT` | Refetch current row; preserve user's draft; no automatic blind overwrite |
| Same scoped key + changed payload | HTTP 409 / `IDEMPOTENCY_CONFLICT` | Do not silently generate another key; reconcile original action |
| Same scoped key + same payload | Original durable result | Retry exact request/key after uncertain transport result |
| No permission/revoked account | Integration must define non-disclosing 403/404 convention | Remove action eligibility; no provider fallback |
| Invalid input | Integration must align validation envelope | Preserve field error; no status coercion |
| Expired/mismatched snapshot cursor | Integration must define explicit error code/status | Restart snapshot; never append new-snapshot rows to old list |

409 codes already exist as pure exceptions. HTTP envelopes/status mappings and
durable command result retrieval do not exist yet. Proposed envelope example:
`{"error":{"code":"VERSION_CONFLICT","message":"Order item changed"}}`.
Do not claim it is generated or mounted. Omit current versions/IDs for targets
the actor cannot access. Exact retry must still reauthorize the actor.

GET `/api/v2/orders` must read published state only: no fetch, business writes,
deadline recomputation from UI, or browser cache substitution. Authorization is
rechecked for each page. Filters/sort/account scope must be bound into cursor;
concurrent publication cannot change rows inside a retained snapshot.

## PostgreSQL acceptance scenarios for T1 schema integration

All scenarios below are NOT_RUN. Use disposable local PostgreSQL, actual runtime
role without BYPASSRLS/superuser, two separate connections and explicit barriers.
SQLite, mocks, sequential calls and owner-role-only tests are not RLS/CAS proof.
Set transaction-local org context through the agreed T1 helper; assert it does
not leak after rollback/commit or connection reuse. Never use a live DB URL.

| ID | Setup/interleaving | Required assertions |
|---|---|---|
| DB01 tenant collision | Org A/account A1 and org B/account B1 share external IDs | Both persist; each runtime scope reads/writes only its own rows; direct SQL and repository both tested |
| DB02 same-org account collision | Org A/accounts A1,A2 share order/listing IDs | Composite links cannot cross accounts; FK negative insert/update and permission-filtered read tested |
| DB03 RLS default deny | Unset/wrong tenant context, including reused pooled connection | No unintended SELECT; INSERT/UPDATE/DELETE denied or affect zero as specified; ENABLE and FORCE policies verified |
| DB04 atomic publication | Writer A stages incomplete pages; reader B queries; A completes then commits | B sees previous publication until commit, then complete new snapshot, never staged/mixed rows |
| DB05 exact replay race | A/B ingest same manifest concurrently; barrier before publish | Single durable revision; no duplicate observations/events under agreed replay uniqueness; both callers get coherent receipt |
| DB06 changed source/out-of-order | Later evidence published, then older or contradictory reused revision arrives | Evidence retained; current projection not silently regressed; reconciliation explicit |
| DB07 partial fetch | Prior two orders; new fetch only one nonterminal page | Missing order/items remain; no inferred cancellation or false complete coverage |
| DB08 account revocation | Fetch ends; other transaction revokes/rebinds account before commit | Commit-time revalidation blocks unauthorized publication; no orphan observations published |
| DB09 assignment CAS | A/B read v3; different keys, both attempt v3 mutation after barrier | Exactly one commit; loser conflict; one assignment revision and matching audit; no lost update |
| DB10 idempotency race | A/B same scoped key with same payload; repeat with different payload | Same payload yields one durable receipt; changed payload conflicts; no cross-tenant receipt leakage |
| DB11 audit failure | Inject exception between mutation and audit before commit | Mutation, history and idempotency all rolled back; retry can succeed once |
| DB12 frozen sheet race | A reads work-item versions; B changes mapping/quantity before A freezes | Conflict/retry, not mixed versions; historical sheet unchanged by later Catalog edits |
| DB13 cancellation phases | Cancel before eligibility vs after frozen sheet; partial return of one item | Before-work eligibility removed; after-work exception recorded; historical artifact immutable; sibling item unaffected |
| DB14 cursor publication | Page1 at S1; publish S2 before page2; change account permissions | No S1/S2 mixing/duplicates/skips; permission rechecked; explicit expiration when S1 unavailable |

Each race must record connection IDs, barrier reached, results and final rows.
Use bounded timeouts and rollback/cleanup in finally. A test must fail if CAS
predicate, unique scope, RLS policy or publication transaction is removed.
KIZ allocation races additionally require recovered source and its own schema;
these cases do not claim KIZ coverage.

## Next gates and rollback

Tests and this handoff are the only changes. Revert this commit to remove them;
there are no data/schema/runtime effects. Keep known-defect characterization
separate from future canonical acceptance tests, which must expect safe behavior.
T1 schema unlocks DB01-DB14 and Orders persistence, not missing renderer sources.
Full prototype/matcher source unlocks scheduling/A4/sticker parity. Verified WB
fulfillment contract is independently required; Statistics remains observations.
T1 rechecked at `edd9d4b927252148b7e25d6933b9834a307d9493`: no
`order_sync_runs`, `marketplace_orders` or `marketplace_order_items` matches in
its local `backend/alembic/versions` directory. No schema integration attempted.
