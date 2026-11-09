# Orders pure ingestion and read contracts

Branch: `codex/arch-t3-operations`. Parent: `3a30b16465ebb881917c328304d4b33f0a5dd736`.

## Implemented independent work

The existing `app/modules/orders.py` identity/status contract remains unchanged.
New `app/orders/ingestion.py` validates frozen normalized evidence and manifests.
New `app/orders/contracts.py` provides frozen read models. Pure Production
assignment preconditions live separately in `app/modules/production.py`.
These modules import no database, HTTP, framework, task or cache clients.
They are not registered or called by existing runtime paths.

### Ingestion input

- `ObservedOrderItem` requires strict positive quantity and a canonical item
  identity. The key must match explicit stable WB unit evidence or Avito stable
  line / order + listing + occurrence evidence using the existing builders.
- Duplicate listing IDs remain allowed with distinct explicit occurrences.
  Duplicate source line keys fail even if listing metadata differs.
- `OrderObservation` binds every item to the same organization/account/order.
  Source kind and mapping must match the marketplace. WB cancellation has
  separate strict boolean input evidence; cancellation is never inferred from
  the canonical output status. Avito raw status stays exact.
- Timestamps must be aware. Effective source time is optional and distinct
  from observed receipt time. Opaque revisions are never parsed/sorted.
- `OrderPage` is immutable and carries a normalized page number, source
  snapshot marker, terminal marker and observations.
- `OrderManifest` rejects duplicate pages/orders, source/account drift,
  mismatched totals and snapshot drift. Complete requires contiguous pages
  beginning at 1, one terminal page at the end, and exact declared order count.
  Empty complete coverage requires an explicit empty terminal page and total 0.
  Partial evidence cannot be promoted without those conditions.
- `checksum` is SHA-256 of versioned normalized semantic content, scope,
  pagination and coverage. Item order and row transport order within each page
  are canonicalized; receipt time is excluded and effective instants are UTC.
  Page membership and source snapshot remain part of the manifest checksum.
  This is a consistency/replay checksum, not an external order/item identity.

The source snapshot and terminal/total inputs are adapter claims. Passing this
validator does not independently prove provider snapshot completeness. A future
adapter must supply a reviewed source/pagination contract; the legacy Avito
`synced` flag and fallback `total` are explicitly insufficient. No arbitrary
source contract string in this model authorizes a real collector.

### Observation comparison

| Result | Meaning |
|---|---|
| `replay` | Same normalized semantic evidence, ignoring item arrival order/receipt time |
| `changed` | Different evidence with later effective time and no contradictory reused revision |
| `out_of_order` | Older effective time, retained as evidence |
| `reconciliation_required` | Equal/missing effective time, adapter change, or a reused revision with different payload |

Different order/account/source comparisons are validation errors. Even
`changed` is not permission to update a projection: monotonic provider authority,
account revalidation, optimistic locking and atomic publication remain future
repository obligations. There is no delete, absence-cancellation, publication
or synthetic fallback-item operation in this module.

### Read models

- `OrderReadRow` binds a specific item to its order observation, item row
  version, Catalog resolution, readiness blockers and optional deadlines.
  Multi-item orders can have different SKU resolution per row.
- `CatalogResolution` distinguishes resolved, unmapped, ambiguous, stale and
  manual override. It references existing catalog IDs; it creates no catalog
  entities and does not prove database FK ownership. Stale historical SKU
  linkage belongs in history, not the current resolved SKU field.
- `DeadlineEvidence` separates source and computed instants, requires rule ID
  and version for computed deadlines, validates timezone and retains provenance.
  It calculates no deadline or scheduling rule.
- `AccountCoverage` stores each account's source/version/snapshot, coverage
  state and optional paired half-open requested interval.
- `OrderReadPage` requires coverage for exactly its account scope and checks
  the aggregate state. It rejects out-of-scope/duplicate item rows, source drift
  and mixed observations for the same order in one page.
- Publication snapshot, high-water mark and next cursor are explicit strings.
  These models do not generate/sign cursors or prove membership in a retained
  snapshot. Empty readiness blockers do not authorize printing/sending.

These are internal Python contracts. HTTP aliases/envelopes, JSON/OpenAPI/TS
generation, authenticated account resolution, cursor expiration and retained
snapshot pagination need the schema/read API stage. Do not mount these directly
as a completed `/api/v2/orders` endpoint.

### Production command preconditions

`AssignmentCommand` contains target work item, expected version, idempotency
key, SKU and reason, with strict validation. It accepts no actor/org claim.
`validate_assignment_preconditions` returns `new` or `replay` and uses typed
`VERSION_CONFLICT` / `IDEMPOTENCY_CONFLICT` exceptions for later HTTP 409 mapping.
Exact replay is checked before stale version. Different target/key passed as
the stored command is a validation error.

The caller must authenticate, authorize the exact tenant/account/work item,
retrieve a scoped stored command/result, and execute CAS + audit + idempotency
storage in one transaction. A pure `new` decision is not a write, lock or
concurrency proof; a pure `replay` decision is not a persisted receipt.

## Legacy adapter characterization

`tests/test_orders_avito_adapter_characterization.py` uses `httpx.MockTransport`
with synthetic records and no real credentials or network connection.

Confirmed behavior:

| Case | Observed legacy result | Canonical consequence |
|---|---|---|
| Repeated listing, quantity 2/3, leading zeros | Preserved as separate items/string IDs | Keep explicit line occurrence evidence |
| Unknown status | Raw text preserved | Canonical null/unmapped |
| Whitespace raw status | Preserved by adapter | Pure canonical mapper rejects it |
| Missing raw status | Adapter supplies `unknown` | Preserve missing-status provenance before normalization |
| One page with total 50 | One request, status `synced` | Not complete coverage |
| Missing total | Page length fallback | Not terminal/manifest evidence |
| Changed/reordered items | Order ID remains; row order/quantity change | No listing-only deduplication |
| Source line/occurrence fields | Not exposed in current normalized model | Ingestion must retain raw identity evidence before this lossy boundary |
| Quantity zero | Adapter substitutes one | Characterization only; separate rule change required |
| Browser repeated-listing merge | First match quantity applied to both positions | Do not use legacy merge as canonical item truth |

No legacy runtime behavior was silently corrected. Browser observations require
independent provenance and cannot override API rows through this merge path.

## Verification

TDD: initial ingestion/read-model tests failed on absent modules; deadline and
account coverage tests failed on absent models. Specific red tests additionally
demonstrated reused-revision acceptance, missing marketplace/source validation
for empty manifests, and fabricated line-key acceptance before their guards.
Focused new suite after corrections: **77 passed**.

Combined offline regression: **169 passed, 2 dependency deprecation warnings in
1.94s**, exit 0. Ruff and compileall exit 0. Import guard finds no HTTP,
SQLAlchemy, FastAPI or Celery imports in the new domain modules (rg exit 1,
no matches). No full-backend baseline or DB concurrency success is claimed.

Run from `backend` using an existing environment with project dependencies:

```sh
python -m pytest -p tests.orders_offline_plugin -q \
  tests/test_orders_contract.py tests/test_orders_stage1_characterization.py \
  tests/test_orders_xlsx_characterization.py tests/test_orders_offline_guard.py \
  tests/test_avito_orders.py tests/test_avito_returns.py \
  tests/test_orders_ingestion.py tests/test_orders_read_contracts.py \
  tests/test_orders_avito_adapter_characterization.py
python -m ruff check app/orders app/modules/production.py \
  tests/test_orders_ingestion.py tests/test_orders_read_contracts.py \
  tests/test_orders_avito_adapter_characterization.py
python -m compileall -q app/orders app/modules/production.py \
  tests/test_orders_ingestion.py tests/test_orders_read_contracts.py \
  tests/test_orders_avito_adapter_characterization.py
```

No network/provider/production/live-DB/print/export action was used in this slice.
Existing returns tests use their own disposable local SQLite fixtures.
RLS, durable replay, transactions and multi-session concurrency remain NOT_RUN.

## Remaining prerequisites

At the latest local check Terminal 1 was at `edd9d4b` and working on ingestion
tokens. Orders schema tables were still absent. The committed schema request
and its corrections are in the preceding Terminal 3 reports. Stage 2 PostgreSQL
implementation must wait for that schema and then prove real runtime-role RLS
and two-session publication behavior.

Local searches in Downloads/Documents and available Git paths still do not
recover the full production prototype or matcher project/lockfile/tests.
Previously found `_remote_wb_pdf_2` fragments remain read-only evidence.
Scheduling/grouping, A4/sticker parity and KIZ allocation cannot be implemented
from those fragments or screenshots. No approved WB fulfillment source contract
has appeared. WB Statistics remains observation-only.
