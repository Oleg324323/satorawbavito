# WB Published History To Canonical Orders

Date: 2026-09-10. T3 bounded source inspection/proposal requested by ROOT.
Pure decoder/planner implemented after ROOT scope approval; no SQL publication,
migration import, provider action or database access. Canonical writer still
requires the exact T1 authority/completeness contract.

## Exact Evidence

| Source | Fact |
| --- | --- |
| `1a4954d:backend/alembic/versions/20260910_0082_wb_history_staging.py:18` | Published page stores exact job/run/lease, credential generation and account incarnation, native input dateFrom, request checksum and EOF receipt. |
| Same migration, line 45 | Page rows retain ordinal, srid, nm_id, barcode, semantic/raw checksums, normalized observation and source revision. Unique srid is per page, not across inclusive pages. |
| Same migration, line 62 | Semantic index explicitly anticipates a future projector; dedup must preserve original page evidence. |
| `6b563d4:backend/app/wb_live/history_repository.py:94` | Staging validates exact normalized WB observation and stable unit identity; published page has at most 100000 rows, stage batches at most 1000. |
| Same file, `commit_history_page` | EOF receipt verifies contiguous ordinal/count, exact request and next dateFrom, and atomically seals staging/checkpoint under the history job guard. It does not write canonical Orders. |
| `6b563d4:backend/app/wb_live/auth.py:49` | `_WbReadJobGuard` explicitly authorizes only WB live repository roots. Persisted initiation login is evidence, not an active login lease. Current membership, scope, credential generation and incarnation remain guarded. |
| `6b563d4:backend/app/wb_live/repository.py:112` | `_guard` delegates to this read-job guard. It is not a canonical Orders publication capability. |
| `6b563d4:backend/app/wb_live/statistics_orders.py:153` | Existing normalizer sets srid as order/unit identity, nmId as external item ID, quantity one, explicit cancellation mapping, native lastChangeDate revision. |
| `5fd5ed1:backend/app/orders/publication_service.py:204` | Public manifest writer requires live `UserSessionPrincipal`, `sync:run`, actual credential/token authority and fresh/final guard validation. |
| Same file, line 277 | `_persist_orders_manifest` assumes an already-authorized physical root; private helper is not authority. |
| Same file, line 374 | Partial manifests append observations/events but skip current Orders item projection. |
| `5fd5ed1:backend/app/orders/job_publication.py:25` | Job publication requires the exact T1 UserOrdersJobs claim/request and a partial single page. An unrelated WB live job cannot be substituted. |
| `5fd5ed1:backend/app/platform/integrations/user_orders_job_contract.py:137` | Existing WB job request uses a calendar date, not native subday history dateFrom. Do not truncate timestamp to fit it. |
| `5fd5ed1:backend/app/orders/serialization.py:119` | Existing semantic checksum excludes only observed_at, allowing repeated inclusive-boundary observations fetched at different times to compare correctly. |

The history commits were inspected with local `git show`, not cherry-picked into
the T3 branch. ROOT owns integration of these commits and their prerequisites.

## Bounded Direction

1. A pure decoder/planner accepts a trusted captured page header, bounded ordinal
   range (at most 1000), and normalized stored rows. It validates exact scope,
   source/adapter/mapping versions, receipt boundary, ordinal range and canonical
   checksums. It does not read a database, infer authority, mutate state, or fetch.
2. A T1-owned publication capability selects the exact published page and chunk in
   a clean physical transaction, verifies current authority and immutable binding,
   and exposes only that trusted captured input to the T3 projector.
3. The projector reuses canonical Orders evidence/projection machinery under that
   capability. Its durable run identity includes the source page and deterministic
   chunk ordinal bounds. Retry returns the same run/result; changed input under
   that identity conflicts. No whole 100000-row materialization or long provider
   transaction is needed.
4. Prefer existing `order_sync_runs.source_run_key` and immutable run membership
   as replay receipts if T1 can prove exact page/chunk binding and discoverable
   progress with them. Add a new progress table only if that evidence demonstrates
   a missing transactional invariant; this proposal does not require new DDL.
5. Freeze an Orders read snapshot through an explicit existing guarded publisher,
   not GET. Keep source coverage partial/unknown and saved-view semantics explicit.
   T4's new selector can then discover it without assuming an unfiltered queue.

## Identity And Changes

Never manufacture unit identity: missing/blank srid is invalid, not printable.
Preserve org/account scope, leading-zero identifiers and native revision text.
nmId is product evidence; barcode is not a global line ID or automatic CatalogSku.
No buyer data is introduced by this bridge.

Inclusive-boundary repeated rows are identified by exact scoped srid plus the
existing semantic observation checksum. Retain every original staging ordinal
and source-row checksum; dedup only canonical semantic evidence, not source page
coverage. Different observed_at alone must not create a new semantic version.
Do not dedup by lastChangeDate alone or by raw payload hash alone.

Same identity and changed semantic evidence must remain an observation/revision
with explicit reconciliation, not overwrite/delete old evidence or silently
regress current state. Equal source time with different content is ambiguous;
older source time must not silently replace a newer projection. Existing complete
manifest publisher reconciles changed current facts rather than autonomously
advancing them; preserve that behavior in the first bridge. A separate proven WB
status-refresh policy would be another slice.

WB cancellation maps to cancelled only from explicit cancellation evidence.
Noncancelled Statistics data stays raw null, canonical null, mapping unmapped.
No sales isReturn conversion, FBS readiness, deadlines, labels, automatic work
creation, print/send, or inferred accepted/delivered status.

## T1 Decisions Required Before Writes

**Authority:** Can the persisted read subscription explicitly authorize canonical
normalized Orders evidence and initial projection? If yes, T1 must provide a
dedicated typed guard/handle with operation scope, exact job/page/chunk binding,
current membership/account/credential generation/incarnation, retry fence and final
commit validation. If not, use a real authorized user/ingestion job intent. No fake
principal/session/token, extending private read guard in T3, or calling the private
persist helper under arbitrary RLS GUCs.

**Completeness:** Existing partial canonical manifests do not publish item
projections. A published page proves a complete captured response, not complete
provider/account history. A bounded chunk may be a complete local normalized
manifest ONLY under an explicit versioned source contract stating its exact local
scope and preserving partial source coverage. Do not label a chunk/provider page
as the full history to pass existing projection checks. Otherwise first slice is
observations only and cannot claim the Orders queue is populated.

**Closure:** Define exact checkpoint/run receipt equality, crash-before/after-commit
behavior, source rebind/credential rotation policy, and which component publishes
the read snapshot. No atomicity claim between separate uncoordinated transactions.

## Acceptance Plan

- Pure synthetic boundary duplicates, repeated fetch timestamps, exact leading
  zeroes, changed source payload, equal-time ties, older revision, missing stable
  unit, unknown status, cancellation and cross-account equal IDs.
- Actual publication tests after T1 authority: unpublished/partial staging denied;
  chunk gap/count/checksum mismatch denied; complete EOF evidence retained;
  exact replay one canonical run/result; changed payload conflict.
- Two physical sessions racing the same page/chunk; atomic run/projection/receipt
  and crash retry. Membership revoke, account rebind, credential rotate and stale
  generation deny before writes and at the final commit gate.
- Original staging immutable; partial fetch never cancels absent orders. Existing
  canonical changes reconcile instead of silently regressing. No readiness claim.
- Fresh local-role/RLS gate and bounded page/SQL query count; no full provider page
  materialization. Missing ownership/grants stay a T1 dependency, not broad grants.

All publication/DB/authority bridge tests remain NOT_RUN. No PG slot is occupied.

## Implemented Pure Slice

`app/orders/history_bridge.py` exports immutable `HistoryPageEvidence`,
`HistoryRowDecision`, `HistoryChunkPlan` and `decode_history_chunk`.
These dataclasses do not represent authenticated handles or write authority.
The exact row dictionary shape matches the nine selected staging evidence fields;
the future T1 participant must scope/lock/capture those rows itself.

Header capture includes exact org/account/job/page/run, credential generation and
account incarnation, request/raw checksum, native input/next dateFrom, row count,
terminal and publication time. Native dateFrom request semantics and actual stream
EOF authenticity remain the existing T1 history capture contract; pure validation
does not reconstruct the raw HTTP stream or independently authorize a header that
merely says published. Input checksum binds those captured values. Publication
time is normalized to a six-digit UTC instant so connection timezone cannot alter
the same chunk's input checksum.

Each chunk is a deterministic contiguous ordinal interval of up to 1000 rows,
with first ordinal a multiple of 1000. Exactly the expected number of rows must
be supplied; no gaps, silent trimming or duplicates within the page. Normalized
payloads are decoded through the existing strict Orders codec and limited to the
same 16384-character canonical envelope as staging. Stored native revision and
effective time must match the observation. Scope, srid/unit identity, nmId,
quantity one, mapping version/cancellation evidence, semantic hash and monotonic
in-chunk time are checked. All source ordinals/raw checksums remain in the plan.

Optional prior canonical observations are bounded to the incoming scoped IDs.
The existing `compare_observations` yields replay/changed/out_of_order/
reconciliation_required; no prior observation yields new. A changed comparison
does not authorize current projection. Semantic replay preserves observed source
evidence while allowing the later participant to avoid duplicate canonical facts.
Input checksum deliberately excludes prior comparison outcomes: evolving current
state cannot change the immutable captured chunk's identity on receipt replay.

`tests/test_orders_history_bridge.py` uses only explicit synthetic values. Initial
21 cases passed; the isolated timezone-normalization case passed separately.
Final focused gate: 22 PASS, 0.06s, exit 0. Scoped Ruff, compileall and diff check
passed; runtime import text guard returned no matches (rg exit 1 as expected).
Manual isolated critic pass verified that no SQL/authority handle is produced,
checksum excludes mutable prior decisions, and coverage stays partial. The
configured preflight-critic skill file is absent; no external review was used.
Coverage includes unknown
WB status, cancellation, inclusive replay with changed observation timestamp,
changed/newer/older/tied evidence, checksum/ordinal/scope/source tamper, missing
stable unit, leading-zero IDs, same external ID in another account, partial and
empty terminal page handling. Pure source imports no SQL/network/framework API.

## Approved Authority Direction

ROOT and T1 selected a separate explicit local projection intent under CURRENT
sync:run user/session/account/credential authority. The background integrations-
write history subscription is NOT extended. Request carries selected identifiers
and idempotency only; server captures completed immutable source/page scope. No
new TTL or execution policy is chosen in T3.

ROOT also approved a future narrow participant, leaving legacy
`_persist_orders_manifest` unchanged, plus a `read_assembly.py` branch ONLY for an
exact accepted source contract and immutable projection receipt. No generic
allow_partial boolean. New partial current rows retain partial coverage,
source_readiness_unproven and wb_fulfillment_source_missing. Production source
gate continues rejecting partial runs. A real WB fulfillment collector/contract
remains required for actual operational readiness. None of these SQL/runtime
amendments are implemented in this pure package.
