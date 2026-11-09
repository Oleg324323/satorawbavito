# T4 — Dormant Orders read wire boundary

## Delivered scope

Pure `canonicalOrders.ts` exports `buildCanonicalOrdersReadPath` and
`parseCanonicalOrdersPage`, inferred typed response and request/scope types.
No fetch, import from an active screen, feature flag, automatic pagination,
storage, status mapping, printable inference or legacy removal.

Source contract: T3 `da7e017`, owner confirmed current `ab272ee` does not change
HTTP shape/query semantics. Endpoint is GET `/api/v2/orders`. Query checksum is
an opaque prepublished-view selector, not a client-generated filter definition.
Future integration uses the shared `error.code` envelope, not standalone
test-app `detail.code`. HTTP registration/activation remains a separate gate.

The decoder preserves raw/canonical status, blockers, deadlines, coverage and
source versions. It validates exact requested organization/account scope,
optional snapshot, row membership, duplicate/mixed observations, coverage
consistency and required provider-specific evidence. Failure rejects the whole
page with fixed `CANONICAL_ORDERS_RESPONSE_INVALID`, without raw payload/details.
No fallback, omission, retry or fabricated rows. Strict unknown-field rejection
requires an explicit frontend update for future wire extensions.

Versions/snapshot are canonical positive INT64 decimal strings, never converted
to Number. Owner/catalog IDs and quantities follow SQL INT4. External IDs retain
leading zeros. Source line keys, mapping versions, cursor and HWM are opaque:
this boundary does not independently attest provider truth, recompute backend
status mappings or validate a cursor signature. It confers no authorization.

## Evidence

- Initial focused RED: missing module, followed by 36 focused PASS.
- Golden JSON serialized by actual T3 `OrdersReadPageResponse.model_dump_json`
  from synthetic backend domain fixtures, changing row version to `2**53+1`
  and snapshot to `2**63-1`. Recomputed backend JSON equals the committed frontend
  fixture. Python ran with empty environment and the existing all-network-denied
  sandbox; no DB/providers used. Fixture is synthetic, not customer data.
- Independent critic identified two P2s: Date.parse truncation of microsecond
  intervals and nullable mandatory WB evidence. Actual regression RED:
  50 PASS / 4 FAIL. Exact fractional comparison and WB evidence guards fix both.
  Microsecond comparisons normalize timezone seconds without rounding fractions.
- Tests cover WB/Avito, unknown statuses, malformed/extra fields, owner/snapshot
  isolation, duplicate/mixed items/coverage, lossless integer strings, empty
  complete/missing pages and precise interval ordering. URL builder tests prove
  repeated accounts, cursor escaping, bounds and mutually exclusive selectors.
- Full frontend before the final whitespace boundary correction: 345 passed /
  the same 21 failed / 0 pending, versus288/21; no new failed-test identities.
  Report `/tmp/satorna-t4-orders-wire-full.json`.
- Final boundary correction: Python exact-text permits leading BOM but rejects
  U+001C; JS trim differs. Actual backend constructor witness and frontend
  57 PASS / 2 FAIL RED verified the mismatch. Explicit Python whitespace edges
  fix it without normalizing IDs. Final Orders+ABC focused run: 115 passed,
  including59 Orders tests, no pending (`/tmp/satorna-t4-orders-wire-final-focused.json`).
  The broad run preceded only this correction; not claimed as final broad PASS.
- TypeScript and Vite build passed before that boundary correction; final
  TypeScript also exits0. Final independent critique: scoped PASS; reviewer
  exhaustively matched the edge-whitespace regex to local Python Unicode
  whitespace. No CodeRabbit. `git diff --check` passes.

## Remaining

No screen/client is activated. Future consumer needs approved default-off
organization/account rollout, authenticated request lifecycle/race fencing,
query registry, cursor/snapshot continuity and safe error UI. Orders/Production
capabilities, immutable history acceptance, print/export parity and single-writer
cutover remain their owners' gates. This is typed preparation, not a completed
Orders queue or production-readiness claim.
