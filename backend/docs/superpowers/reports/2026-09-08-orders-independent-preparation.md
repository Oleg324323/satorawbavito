# Orders preparation independent of schema

Branch: `codex/arch-t3-operations`. Parent: `4323821d2763291659b4ea1ba6ac5501c20b2109`.

## Executable characterization

Run the Orders selection with `-p tests.orders_offline_plugin`. The opt-in
plugin installs a Python audit hook before collection and rejects DNS resolution,
TCP connects and UDP sendto. It is process-local and does not change shared test
configuration. It is not an OS sandbox for native libraries or arbitrary child
processes; this selection invokes no provider CLI or native network client.
The separate subprocess test installs the same guard and proves DNS/TCP/UDP
rejection before I/O. No provider connection was made during this continuation.

The initial guarded run gave **78 passed / 2 failed**. Both failures were
pre-existing test-input defects:

- `test_avito_orders_picking_list_xlsx_matches_avito_order_rows` expected a color
  absent from its input and allowed live listing enrichment. Supply a local
  listing-cache row containing size/color/image metadata. The actual enrichment
  and XLSX rendering code still executes.
- `test_avito_orders_endpoint_ignores_blocked_cache_and_refetches` returned a
  blocked report payload for every cache key, including browser snapshot.
  Return no browser snapshot for that key and retain the blocked report for
  the requested report key. The actual cache-miss/refetch path still executes.

No runtime code was changed to make these tests pass. These two failure IDs
are intentionally repaired test cases, not unexplained baseline removals.

Added renderer tests inspect ZIP integrity and parsed worksheet XML, verifying:

- repeated listing rows and quantities 2/3 expand into exactly five rows;
- leading-zero order/listing IDs remain string cells;
- formula-like inputs remain literal inline strings without formula nodes;
- XML metacharacters and Cyrillic round-trip;
- 65 rows are complete, including the last row and nine expected headers;
- empty input has no phantom rows;
- rendering does not mutate source models.

Legacy quantity zero currently produces one output row. This behavior is
characterized explicitly and remains unchanged; canonical quantity eligibility
must handle it as a separately approved rule change. The workbook does not
carry canonical tenant/item identity or a frozen sheet version, so these tests
do not establish unified Production renderer parity.

Additional identity tests exercise all permutations of a three-item order,
quantity/title/color changes without changing line identity, missing explicit
occurrence rejection, and identical WB unit IDs across two accounts in one org
and across organizations. Stable occurrence assignment from a provider remains
an ingestion evidence requirement; array index is not inferred by these tests.

## Read contract proposal for Terminal 4

This is a reviewable wire-contract proposal, not a mounted API or generated
TypeScript artifact. Final naming must be reconciled with Terminal 1's schema
before implementing the HTTP boundary.

`GET /api/v2/orders` accepts selected account IDs, filters, page size and an
opaque cursor. Authenticate and permission-filter the account scope on every
page. Reject any explicitly requested unauthorized account; never silently
widen to all accounts. GET performs persisted reads only.

| Field | Type | Meaning |
|---|---|---|
| `snapshotId` | string | Immutable published snapshot identity, reused across all pages |
| `highWaterMark` | string | Opaque publication boundary, not a timestamp-only cursor |
| `publishedAt` | UTC ISO timestamp | Publication time distinct from provider effective time |
| `coverage` | per-account records | Source/version, requested bounds, observed bounds, complete/partial/missing state |
| `rows` | array | Canonical order/item views with scope and versions below |
| `nextCursor` | string or null | Bound to snapshot, authorized account/filter set and stable sort key |

Each row carries `organizationId`, `marketplaceAccountId`, marketplace,
external order ID, order item ID/source line key, raw/canonical status,
mapping state/version/evidence source, adapter/source version, `rowVersion`,
quantity and catalog resolution state. External IDs remain strings.
Readiness is a separate field with explicit blocker codes; a mapped marketplace
status does not establish printable/sendable readiness. WB Statistics alone
always leaves fulfillment readiness blocked.

Catalog resolution distinguishes `resolved`, `unmapped`, `ambiguous`, `stale`
and `manual_override`, with product/offer/SKU references and evidence version.
Deadline fields distinguish source instant, computed instant, rule/version,
timezone and observation provenance. Missing deadlines stay null.

Stable pagination must use retained immutable snapshot membership/row versions
or equivalent versioned observations. A high-water mark over mutable latest
rows alone is insufficient: an update between pages could hide/change a row.
Revalidate authorization even for a valid cursor. Expired/unavailable snapshots
return an explicit error requiring a fresh traversal, never silently switch to
the latest snapshot. Sorting needs a unique immutable tie-break within snapshot.

## Production command contract proposal

No actor, organization or permission assertion from request bodies is trusted.
Commands derive actor membership from authenticated context and scope the work
item through its order/account ownership.

Manual assignment input: work-item ID, `expectedVersion` (positive integer),
`idempotencyKey` (nonempty exact string), target CatalogSku ID and mandatory
reason. The server returns the work-item ID, resulting version and immutable
action ID. Quantity commands follow the same concurrency envelope, but quantity
rules await recovered prototype characterization.

| Condition | Result |
|---|---|
| Same scoped key and normalized semantic payload | Return original committed result; do not repeat mutation/audit |
| Same scoped key and different payload | HTTP 409 `IDEMPOTENCY_CONFLICT` |
| New key and stale expected version | HTTP 409 `VERSION_CONFLICT` |
| Missing/ambiguous line or Catalog binding | Typed validation/readiness blocker; no printable work |
| Unauthorized scope | Access error without other-tenant row details |

Idempotency lookup must precede stale-version rejection for an exact authorized
replay. Scope key uniqueness by organization, account, command kind and target;
compare a canonical typed payload including expected version and reason.
CAS mutation, action audit and idempotent result commit together. Two concurrent
first requests require a DB unique/lock race test, not sequential calls.

These error names are proposed consumer codes; existing legacy envelopes are
not changed. Terminal 4 must keep conflict states visible and refetch the same
authorized context before issuing a new command; automatic overwrite retries
are not permitted.

## Schema request corrections for Terminal 1

The preceding Stage 1 report is a proposal and needs these refinements before
its migration is implemented:

- All referenced run/observation/status IDs require unique anchors containing
  organization AND account, not only `(org, id)`.
- Observation replay keys include source kind, external entity identity and
  source revision/checksum. A global payload-only checksum cannot deduplicate
  different orders whose minimal payloads happen to match.
- Provider event IDs may have multiple revisions: do not reject later source
  revisions with a unique event-ID-only constraint.
- Exact Python whitespace validation is broader than PostgreSQL `btrim`.
  Preserve application validation and explicitly test tab/newline/Unicode
  edge whitespace at the persistence boundary; do not silently trim.
- Unknown WB lifecycle evidence may have null raw status. Missing stable line
  ID belongs in quarantined observation evidence, not a fake item row.
- Snapshot pagination needs retained historical rows/membership; current rows
  plus a cursor are not enough. Choose the smallest schema that satisfies this
  before publishing the read API.
- Status mappings already live in the versioned pure contract. A new global
  mappings table is optional and requires demonstrated need; persist mapping
  version/evidence on observations without inventing a second mutable authority.
- An offer's product FK must include account, and an item's offer/product pair
  must be consistent. Independent org-only FKs do not prove that relationship.

## Partial matcher recovery correction

The prior report's blanket claim that no local `pdfBuilder`/`db` source existed
was too broad. Found local fragments (read-only, no execution):

| File | SHA-256 |
|---|---|
| `/Users/bratishka/Documents/Codex/_remote_wb_pdf_2/pdfBuilder.ts` | `608cba3dbef90fe0a7e3d7816fa7b596a8c190ce2cfec3b08b78c758b1c9dac5` |
| `/Users/bratishka/Documents/Codex/_remote_wb_pdf_2/db.ts` | `3eea3141156347b4cdb86c7cf69e7d3ff7602b956fe31233ba8a4a31c107a05e` |

`pdfBuilder.ts:11-21` imports unavailable types and `db`, specifies 58x58 mm,
and reads local fonts/PDFs at lines 165-169. `db.ts:3-9` imports HTTP/HTTPS and
other dependencies; lines 438, 463 and 470 expose availability/claim/consume.
The directory has no complete parsers/types/lockfile/routes/tests/assets tree.
Its operational-looking PDF/image outputs were neither opened nor exported.
Source revision and full dependency closure remain unknown. No fragment was
copied into the application or reconstructed into a renderer.

## Verification and remaining gates

Focused command from `backend`:

```sh
python -m pytest -p tests.orders_offline_plugin -q \
  tests/test_orders_contract.py tests/test_orders_stage1_characterization.py \
  tests/test_orders_xlsx_characterization.py tests/test_orders_offline_guard.py \
  tests/test_avito_orders.py tests/test_avito_returns.py
```

Production A4, 120x75, 58x40, frozen sheets and KIZ allocation remain BLOCKED
on source recovery and persistence. Real DB concurrency/RLS remain NOT_RUN
until Terminal 1 supplies Orders schema. The tested legacy Avito XLSX behavior
is PASS for the listed synthetic cases only. No shared routes, schema,
configuration, dependencies or operational data changed.

Final focused result: **92 passed, 2 dependency deprecation warnings**, exit 0.
Ruff check of all five changed/added Python files and compileall both exit 0.
This is a focused gate, not a full-backend baseline or DB concurrency claim.
Self-review checked that fixture repairs keep the actual cache/enrichment
branches, no expectation was weakened, and proposed contracts are not presented
as implemented APIs.
