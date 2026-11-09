# WB History Per-Run Decision Evidence

2026-09-10. ROOT approved this bounded contract through coordination messages.
T1 owns migration 0085 and physical enforcement; T3 owns the new history
participant. This document is a schema request, not evidence of a deployed or
tested schema. Legacy Orders publication remains unchanged.

## Why It Is Required

`app/orders/publication_service.py::_event` writes `order_lifecycle_events` with
the scoped semantic key `orders-publication-v1:reconciliation_required`.
Events have no sync-run identity and may be reused. Joining historical events
to a new run cannot independently prove that run's reconciliation count.
An application-returned integer or copied audit JSON is not that proof either.

## Storage Contract

Extend the existing append-only `order_sync_memberships`; do not add a second
results table or change historical lifecycle meanings. New nullable fields:

| Field | Type | Meaning |
| --- | --- | --- |
| history_decision_version | SMALLINT | Exactly 1 for new history parent memberships |
| history_pre_order_version | BIGINT | Actual locked parent version, at least 1 |
| history_pre_sync_run_id | BIGINT | Actual prior current run, nullable |
| history_pre_observation_id | BIGINT | Actual prior current parent observation, nullable |
| history_outcome | TEXT | initial_projection, semantic_replay, or reconciliation_required |

The pre-run and pre-observation fields are either both NULL or both non-NULL.
Add scoped prior-run FK `(organization_id, marketplace_account_id, pre_run)`
and prior-parent-observation FK
`(organization_id, marketplace_account_id, order_id, pre_observation)`.
The existing partial unique index on `(organization_id,
marketplace_account_id, sync_run_id, order_id)` for parent memberships provides
exactly one decision per order in a chunk. Child and legacy memberships retain
all five NULL fields. No backfill or reinterpretation of historical runs.

Only exact WB source/adapter/mapping/contract from
`2026-09-10-wb-history-projection-consumer-contract.md` admits these fields.
Header identity must remain immutable; run is staging when evidence is inserted.

## DB-Derived Capture

A BEFORE INSERT membership trigger rejects caller-populated proof fields,
locks the scoped parent, and derives the five fields itself. It resolves the
parent's actual current-run parent membership and observation under that lock.
Missing, duplicate, malformed, wrong-source or wrong-scope current evidence
fails closed. An incoming observation already exists when the existing evidence
repository inserts its membership; it may legitimately be semantic-deduplicated.

Validate canonical codecs and checksums before comparison. Semantic equality
must match `app/orders/ingestion.py::_semantic_row`: exact identity/source and
all other normalized values, UTC effective instant, items sorted by source line
key, ignoring only observed_at. Do not compare arbitrary JSON minus one key or
introduce a second normalization rule. The T1 captured-row witness independently
binds the incoming semantic evidence to the selected page and ordinal.

| Locked prestate | Derived outcome |
| --- | --- |
| Current evidence semantically equals incoming | semantic_replay |
| Current evidence differs, including changed/older/tied/unknown chronology | reconciliation_required |
| No current; parent version 1; incoming observation originated in this new run; no other parent observations and no earlier parent memberships | initial_projection |
| Any other valid no-current state, including reused singleton history | reconciliation_required |

The conservative initial rule is an explicit tightening of the NEW dormant
history participant. Prior T3 draft allowed a historical singleton with no
current projection. It must now reconcile. The observation-origin-run equality
is exclusive to initial_projection: replay and reconciliation MUST still allow
deduplicated observations with old origin runs and old observed_at timestamps.

## Ordering And Final Witness

An immediate parent UPDATE admission guard must require the already DB-derived
initial membership witness before a history projection changes the parent.
Updating first and capturing that poststate as a fictional prestate must fail;
a deferred-only witness is insufficient. No new authority factory or caller
claim may stand in for the genuine T1 handle and current transaction guard.

The reciprocal deferred witness verifies, in the same physical transaction:

- initial_projection ends with current run equal to this run, parent version
  incremented exactly once, and the expected real child memberships/projections;
- semantic_replay and reconciliation_required leave parent current run/version
  equal to their captured prestate;
- every captured normalized row has exactly one matching parent decision;
- canonical run, scoped source/page/checksum proof, T1 receipt and progress agree;
- reconciliation_count equals the count of this run's parent decisions whose
  outcome is reconciliation_required, never a count of historical events.

For this WB source, order_count and item_count both count normalized incoming
rows/items. They do not count child memberships. Empty terminal chunks have
zero decisions/counts, page_count 1 and partial coverage. Exact run replay reads
the original immutable receipt and decisions; it never recalculates its result
from subsequently changed current projections.

Existing UPDATE/DELETE/TRUNCATE immutability and ENABLE/FORCE tenant RLS remain.
No consumer receives proof-update permission. T1 owns fixed search_path,
function invoker/ACL treatment and physical guard ordering. Downgrade must reject
existing decision/receipt evidence rather than erase it or restore stale writers.

## Acceptance Gates

T1/T3 actual migration/runtime tests must cover fresh initial, reused singleton,
earlier membership, divergent observation, semantic replay, changed/older/tie,
zero-row EOF, forged proof fields, parent-update-before-membership, wrong tenant
and same-org wrong account, missing receipt, mismatched count, rollback of all
writes, and exact replay after current state changes. Real guard/session and
concurrency evidence require the genuine T1 fixture and allocated local PG gate.

Current source-only evidence: seven new initial-admission tests first failed
with missing helper (exit 1); after the bounded helper and scoped provenance
query, the focused contract file passed 34 tests in 0.30s (exit 0). This proves
only pure admission behavior, not SQL capture, ordering, witness, RLS or runtime
integration. No fake handle, production, provider call, migration edit or external
action was used. Actual 0085 integration remains NOT_RUN.
