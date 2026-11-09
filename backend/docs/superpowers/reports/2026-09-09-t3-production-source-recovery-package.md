# T3: Single Production Source Recovery Package

LATEST SCOPE: user has DEFERRED Production assembly/printing. The missing-source
bundle and Production dependencies below are a resumption record, not an active
request or blocker for remaining platform acceptance. No more renderer/calendar
recovery, P1-P4 feature implementation or new print tests until resumed. Preserve
existing code/schema/history. The Orders-only bounded source policy was approved
by root and implemented in `f4c6a286030f15ba063980d10dc517a7ddad5da6`.
The original investigation/proposal below is retained as decision history, not
a current request to approve the same policy again.

## Current Active Orders Inputs

The implemented single-known-order refresh captures bindings/versions before
fetch, preserves valid rejected observations, promotes only status/time changes
under the explicit application policy, and keeps immutable reads and partial
coverage. It does not implement full-account sync or prove provider chronology.

Remaining inputs are distinct:

- A trusted credential-bound Orders client/job principal resolver and its caller
  contract. Supplying an arbitrary HTTP client is not proof of credential scope.
- Full-account traversal publication semantics, stronger event ordering or an
  approved replacement rule for changes beyond status. Partial pages never prove
  absence/cancellation; no status ranking is inferred.
- Stable line identity for repeated listings and a rule for enrolling old
  observations without the new non-status fingerprint. Existing rows are not
  silently backfilled with invented evidence.
- Exact UI filter semantics and the shared router/source-runner wiring. Existing
  `query_checksum` selects a prepublished immutable view, not a filter engine.
- WB fulfillment source contract. Statistics remains observation/cancellation
  evidence, never readiness, deadlines or sticker authority.
- Browser producer acceptance and token-only durable sink integration. The strict
  `avito-browser-evidence-v1` raw decoder is now implemented (see its dedicated
  contract report), always partial. It does not reconstruct missing legacy IDs,
  status, quantity, occurrence or capture time; T1 owns token-only commit authority.

Production source recovery below is deferred and is not part of this active list.

T4 confirmed no agreed canonical filter-engine contract exists. Its current
`frontend/src/features/orders/canonicalOrders.ts:90-102` emits only account scope,
opaque query checksum, limit and snapshot/cursor. Legacy Avito date/status/search
controls and decorative ready/problem chips are not a WB+Avito canonical rule for
time basis, status/readiness, or order-versus-item filtering. No filter engine is
invented from those controls. T1 likewise confirmed the trusted job-bound Orders
principal/client resolver is still a proposal, with implementation continuing in
its ownership. Shared route registration exists separately in root milestone
`587967a40c0d779e5271a709701282ffb97ae8a2`; consuming it requires the other domain
prerequisites, not an isolated import or a claim of source activation.

## Historical Investigation

Owner input request, not implementation or production authorization.
Checked against T3 `35ea34e77d80920b0d8779a6b888e6f1d8e7bad0` on 2026-09-09.
KIZ, Honest Sign allocation and the separate matcher are expressly excluded.
Do not send operational PDFs, customer JSON, codes, credentials or production data.

## Where Checked

- Exact original tree `c88a474695569af905b294906ba604d61f3de62f` and local Git refs:
  `git log --all -- backend/app/production backend/app/routers/production.py`
  again returned no commits. This is a claim about these exact paths, not proof
  that no differently named implementation exists anywhere.
- Existing source inventory:
  `2026-09-08-orders-production-stage1-handoff.md:34`, missing-path evidence at
  `:55`, route conflicts at `:88`; discovery `edc5439` was read as evidence only.
- Current `frontend/public/vella-production.html` includes browser print styling
  (120x75 and 58x40 at lines5309-5317), prototype selection/manual rows and local
  delivery state. `142cf46` characterizes available HTML. This is real partial
  source, not proof of the missing backend renderer or durable delivery contract.
- Existing `backend/app/avito/orders_picking_xlsx.py` is available and tested;
  it is not the missing unified Production exporter.
- Prior local search under `/Users/bratishka/Downloads/satornawb-main` and
  `/Users/bratishka/Documents/Codex` is recorded in the independent-preparation
  report. A fresh filename search for production Python, print-list, sticker and
  pdfBuilder sources returned only the known external fragment
  `/Users/bratishka/Documents/Codex/_remote_wb_pdf_2/pdfBuilder.ts` (plus unrelated
  scheduler state filenames, whose contents were not opened).
- `2026-09-08-orders-independent-preparation.md:151-165` records that fragment's
  checksum, missing dependency closure and 58x58 dimensions. It belongs to the
  excluded matcher path and does not establish ordinary 58x40 parity. No output
  PDFs/images or runtime state were inspected or exported for this request.

## One Required Input Bundle

| Needed source | What is missing | Required owner response / acceptance evidence |
|---|---|---|
| A4 print-sheet renderer | Executable renderer, fonts/templates, full dependency lock, selection/order/group/quantity handling and >48-row behavior | Immutable source bundle + revision/checksums and synthetic expected text/page fixtures. If the old renderer truncates, explicitly approve a separate rule fix; do not call that unchanged parity |
| Ordinary 120x75 stickers | Actual selected order/unit expansion, barcode/QR encoding and printer/PDF layout contract, not only browser CSS | Source/template revision and synthetic multi-item quantity>1 fixture with expected per-unit identity/code and physical page size |
| Ordinary 58x40 stickers | Same source closure for this format; existing 58x58 matcher fragment is not a substitute | Separate 58x40 fixture/layout contract and barcode verification expectations; preserve ordinary historical codes without KIZ allocation logic |
| Calendar, batches and grouping | Executable Europe/Moscow schedule/cutoff/weekend/coverage/overdue rules, deterministic group ordering and selected-item behavior | Exact source/rule revision and synthetic Friday/weekend/Monday, midnight, cutoff and coverage cases. No inferred UI thresholds or calendar defaults |
| Delivery and archive/reprint | Destination/operation definition, original request/response shape, receipt meaning, timeout/retry semantics, historical artifact selection | Source/contract + synthetic acceptance, rejection, timeout-after-possible-acceptance and retry fixtures. UI click/localStorage is not provider receipt; download is not print/send |

Prefer the original complete `app/production/{repository,storage,contracts,
print_list,export,mapping_import}.py`, router, tests and lockfile bundle if those
components were distributed together. Supply source/schema definitions only,
not the whole customer JSON store. Exact source paths may differ: record them.

If this source cannot be recovered, root needs a single explicit owner decision
authorizing a NEW specification for the missing behaviors and its acceptance
fixtures. That changes the target from preserving unknowable legacy parity to
implementing approved replacement rules. T3 cannot infer that authorization from
the request to continue locally. P1, Orders reads and existing Avito XLSX continue
independently; these missing inputs do not block all platform work.

## Avito Progression: Separate Source-Semantics Decision

The current confirmed path is fake normalized complete manifest -> guarded
append-only observations -> initial current projection -> immutable snapshot read.
Changed facts are persisted but remain reconciliation; this is NOT completed
revision-to-current-read behavior (`app/orders/publication_service.py:343-402`).

Available adapter evidence is insufficient to authorize automatic progression:

- `app/avito/orders.py:356-381` fetches one page per call, with page/limit and
  optional status/date filters, not a proven immutable provider snapshot.
- `:428-432` substitutes page length when total is missing/false-like.
  `tests/test_orders_avito_adapter_characterization.py:95-105` proves partial-page
  fetch and missing-total fallback; neither proves complete account coverage.
- `:450-451` parses created/updated timestamps but supplies no monotonic-version,
  tie or mutation-order guarantee. Local receipt time and opaque snapshot IDs
  cannot supply that guarantee.
- `app/orders/ingestion.py:287-320` explicitly describes `changed` as evidence,
  not authority to update a projection. Account locks serialize DB writers,
  not marketplace events fetched earlier outside the transaction.

Minimum missing authority: identify the Avito source endpoint/body and a documented
per-order ordered revision or approved snapshot-replacement rule; specify ties,
equal-version changed bodies, missing items and completeness across pagination.
Also identify stable order-line IDs or the limits of repeated-listing occurrence
identity under row reorder/change. No status ranking or received-at rule is assumed.

Once that authority exists, the bounded T3 path is one source only: fake pages ->
complete validated manifest -> append changed observation -> CAS parent/items in
the same publication transaction -> freeze/read the new revision; exact replay,
partial fetch, same-version conflict and older event tests must retain history
without silently regressing current. This uses existing repositories, not a new
generic source engine. Real provider execution is unnecessary for these local tests.

## Verification Boundary

This package is a read-only source investigation plus documentation. No schema,
provider, network, production, customer JSON, renderer output, print/export or
frontend mutation was performed. Paths/line references are tied to the stated T3
revision; T4's independent UI cleanup does not change the evidence authority.
P1 physical schema is now released (`d57c544` + `bb7a958`), not absent. Shared
permission READY and T3 service acceptance are distinct pending gates at this
checkpoint. Root consolidated integration follows ONE accepted consumer SHA,
not a claim that all eight stages are complete.

## Official Avito Documentation Follow-up

Root explicitly authorized public documentation lookup after the local-only
checkpoint above. On 2026-09-09 an unauthenticated documentation GET succeeded:
[official catalog](https://www.avito.ru/developers/api-catalog/order-management/documentation)
and [official catalog OpenAPI payload](https://www.avito.ru/web/1/openapi/info/order-management).
The old developers.avito.ru catalog URL redirects to the former. The documentation
metadata URL on developers.avito.ru returned404; the current www.avito.ru URL
returned JSON. Web-reader extraction failed, so curl and a JSON parser read the
public specification, not any operational endpoint. No credentials/cookies were
supplied; no request was made to api.avito.ru or an account Orders endpoint.
Search-engine third-party/GitHub results were not opened or used as authority.

OpenAPI info.version is `1.0.0`. SHA256 of the exact UTF-8 `swagger` string from
the metadata response:
`dbc46528ad2a580e4ffd9b87c57a062b22454db1bbb5307ee521d18e32c9470a`.
No full specification, personal-data examples or generated contract was committed.

### Primary-source facts (not inferred guarantees)

- `paths./order-management/1/orders.get`: parameters `ids`, `statuses`,
  `dateFrom`, `page`, `limit`; limit maximum20. `dateFrom` filters creation time.
- `components.schemas.ordersInfo`: required `orders` and `hasMore`; no `total`
  property. Thus legacy total/page-length fallback is not authoritative coverage.
- `components.schemas.order`: required string `id`, `createdAt`, `updatedAt`,
  `items`, status and other data. `marketplaceId` is a nullable alternate order ID.
  No ordered revision, cursor, snapshot token or tie behavior is specified here.
- `components.schemas.item`: `avitoId` identifies the Avito product; optional `id`
  identifies the seller's product. Neither is documented as an immutable order-line
  ID. `count` is quantity. Repeated-listing positions lack a proven stable key.
- `updatedAt` describes update time only: no uniqueness, monotonicity, timestamp
  precision or ordering guarantee was found in this fetched specification.
- The eight enumerated status values match the existing pure contract. A status
  example elsewhere differs from that enum; it does not authorize an alias.
- `schedules` exposes nullable source deadlines, not factory calendar/group rules.

Absence here means not documented in this retrieved source, not proof that Avito
cannot supply a stronger guarantee elsewhere. `hasMore=false` terminates the
returned traversal; it does not promise an atomic account snapshot while orders
change between page requests. No new mapping or operational readiness is inferred.

### Original Bounded Policy Proposal (subsequently approved and implemented)

Name: single-known-order status observation replacement, not provider event order.
This is a proposed application freshness rule, explicitly weaker than guaranteed
provider chronology. It must have a separate source-contract version if approved.

1. Restrict to one already published Avito order with no Production work items.
   Use documented `ids` for that exact order, page1, limit20, no status/date filter.
   Capture expected current parent/item versions and immutable account binding
   before fetch; perform fetch outside the transaction. Local duplicate writers
   are controlled by guarded commit/CAS, not a claimed distributed source lock.
2. Admit only a successful body containing exactly that one order and strict
   boolean `hasMore=false`. Missing, duplicate, different IDs, more pages, errors
   and account drift append no promotable current replacement. They never delete
   an order or imply cancellation/account-wide absence.
3. Require item identities, occurrence assignments, quantities and all non-status
   normalized business fields to match the previously accepted observation.
   No repeated-listing source identity repair: unresolved or changed item shape
   reconciles. Catalog links are preserved, not recomputed from title or labels.
4. Status and source update time may differ. Exact semantic replay is idempotent;
   missing/invalid time, older time or equal time with changed facts reconcile.
   Strictly later time is accepted ONLY under this explicit application policy,
   not advertised as a proven Avito version. Unknown raw status remains unmapped
   with canonical null; no lifecycle rank or readiness fallback is introduced.
5. Under live authority/account locks, revalidate the captured projection versions
   and lack of Production work. Atomic append + CAS must preserve coherent parent,
   item membership and immutable run provenance. A source-only item version bump
   must be explicit and tested; no silent update of a P1 frozen source witness.
6. Single-order response coverage must never become account-complete coverage.
   Existing read assembly currently uses account manifests; before enabling this
   option it needs an explicit scoped coverage admission preserving prior account
   coverage and marking uncertain aggregate freshness. Do not pass this body as
   a normal complete account manifest just to reuse a successful code path.
7. Synthetic acceptance: initial fake source -> snapshot read -> changed status
   -> durable revision -> new read; original snapshot remains unchanged. Add exact
   replay, changed equal timestamp, older response, concurrent CAS, hasMore=true,
   missing order, changed/reordered repeated items, account rebind and pre-existing
   work-item cases. No real provider or physical print is needed.

Tradeoff: this unlocks a narrow local status-refresh chain but cannot guarantee
upstream freshness and deliberately does not progress quantity/line changes,
orders already in Production, full-account completeness or print eligibility.
Two same-body reads would not eliminate upstream eventual-consistency risk and
are not proposed as a false proof. An alternative is to keep changed facts in
reconciliation until Avito supplies stronger documented semantics; root should
choose explicitly. No generic engine, scheduler or extra cache is needed for
either choice. Orders storage/read/API remain independent. The newer deferral
pauses P1 and new XLSX work; it does not delete their existing implementations.
