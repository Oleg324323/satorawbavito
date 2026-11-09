# T2 account state persistence participant — 0075 source composition

**IMPLEMENTED / UNVERIFIED**, not a committed repository/service or activation.
Consumes actual T1 `9960548b47021880f3a511d09facd508c9c7270c`, migration0075,
and its full physical handoff. Imported ancestry includes other T1 source packages;
none is called verified because it merged. No schema/grant/flag/provider edits.

## Implemented domain slice

`app.modules.wb_repricing_state_postgres` exports `AccountStateScope`,
`AccountStateTransaction`, `StoredStateRevision`, `RepricingStateError`.
Constructor takes an already-authorized caller-owned active Session root, exact
org/account/SKU scope and explicit max_request_bytes. It requires clean physical
Engine-bound PostgreSQL READ COMMITTED, preserves the same Session/Connection/root,
sets exact account RLS context and serializes the canonical WB account before
domain state. It never commits, rolls back another root, accesses credentials or
performs provider I/O. Caller must rollback the complete root after any failure.

- `get_current(campaign_id=None)` reads assignment; exact campaign UUIDv4 selects
  liquidation. Full owner is mandatory, never UUID-only lookup.
- `history(limit, before_revision=None, campaign_id=None)` returns immutable,
  bounded descending revisions without locking historical rows for UPDATE.
- `replace(AssignmentChange|LiquidationChange)` resolves scoped command replay
  before current mapping or transition eligibility. Different canonical bytes/
  actor conflict; exact replay returns the historical receipt, no new audit.
- New writes lock actual offer mapping FOR SHARE, validate expected head version,
  retain exact NUMERIC money/version, use one DB-clock timestamp and fresh audit
  UUID, write immutable revision + initial/CAS head + reciprocal audit together.
- Liquidation creation also writes immutable campaign; future revisions use the
  existing pure lifecycle validator. Exact logical approval ID resolves only by
  org/account/SKU to0066 physical UUID, without article/SKU inference. Projection
  columns are copied to the head; active slot/deferred graph validation remains SQL.
- Reads reconstruct typed commands from SQL domain rows and compare full original
  canonical bytes/hash, revision ancestry and head timestamp/version. No JSON state
  storage, whole-object flush, File/memory fallback or formula changes.

## Deliberate authority boundary

T1 explicitly confirmed current `WB_SKU_OVERRIDE_*` permissions are override-only:
no accepted assignment/liquidation read/write or negative-margin-confirmation
permission mapping exists. This participant is **not an authentication boundary**.
An actor ID in a command is not live authorization. No public committed command
service/router/worker is registered; those mutations remain unavailable until
explicit policy and final live-guard composition are delivered. No arbitrary
permission callback, alias or default profile was invented. Org settings has no
writer here and remains SELECT-only by0075 runtime grants.

New/replaced non-null liquidation confirmation fails with
`state_confirmation_policy_required`. An unchanged previous confirmation may be
preserved as historical data; existing lifecycle invalidation still applies when
target/step changes. This is not fresh confirmation authority. Clear confirmation
is supported; new confirmation requires a future explicit authenticated command.

## Safe failure and commit ownership

Participant emits bounded state_invalid/conflict/persistence_failed/
mapping_unresolved/confirmation_policy_required. SQL uniqueness/serialization/
deadlock is conflict; other caught statement SQL failures are persistence failure,
without PostgreSQL DETAIL. Domain validation errors are existing fixed local codes.
Deferred constraints and physical commit happen in the future service's root:
that owner must translate commit errors safely and cannot report success from this
participant's returned value before commit. No persistence readiness claim yet.

## Pending centralized verification and remaining work

No imports, compile/lint, review/critic, tests or PG gates run for this source.
Pending actual runtime-role tests: two Sessions/CAS winner; different SKU changes
preserved; cross-org and same-org different account; replay after remapping/terminal;
key reuse changed payload; rollback and restart; explicit first unassign; full
revision/head/audit graph; mapped/unmapped SKU; campaign origin/partial active-slot;
terminal/paused transitions; confirmation gate/invalidation; logical+physical approval
alignment; very large NUMERIC/Decimal bytes; bounded history; stale roots/context;
no history UPDATE/no fallback. Existing pure codecs and pinned SQL vectors remain
acceptance prerequisites, not tests already run against0075.

Next authorized implementation after policy: live principal/account/mapping access,
committed service with final guard and safe deferred-commit failure mapping, then
central tests. Dated mapping/source/economics context, current writer fence,
assignment/liquidation cutover and org-wide settings policy remain separate gates.
Rollback removes dormant participant/report only; no stored rows were created.
