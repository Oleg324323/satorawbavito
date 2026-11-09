# Repricer, Finance, and Sources: Stage 1 handoff

**Date:** 2026-09-08

**Base:** `c88a474695569af905b294906ba604d61f3de62f`

**Branch:** `codex/arch-t2-economics`
**Scope completed:** discovery, canonical identity bridge, and a pure repository contract only

## Status and hard boundary

This stage does **not** make approvals durable. It adds no ORM model, migration,
repository implementation, router/task wiring, feature flag, scheduler, provider
call, or formula change. The existing repricer remains the sole active writer and
keeps its current behavior.

The new contract is deliberately side-effect free. It defines how verified
canonical integer identities cross into the existing immutable approval kernel,
and the minimum scoped operations a future PostgreSQL repository must implement.
Stage 2 must not start until Terminal 1 supplies and the owner accepts the schema
commit described below.

## Evidence reviewed

- `SATORNA_ARCHITECTURE_HANDOFF.md`, `SATORNA_SPEC_AUDIT.md`, and
  `backend/docs/superpowers/reports/2026-09-08-empty-db-fix-and-architecture-remaining.md`.
- Evidence commit `5d7dde6`, report
  `backend/docs/superpowers/reports/2026-09-08-wb-repricer-module-global-state-audit.md`.
- `wb-prices-stocks-discovery-2026-09-03.md`,
  `wb-ktr-localization-source-discovery-2026-09-04.md`, and
  `satorna-economics-versions-discovery-2026-09-04.md`.
- WB BFF/Sprint B/Sprint C/execution/tasks, persistence, apply client, control-plane
  routes, Avito repricer/apply/tasks, canonical identity ORM, and focused tests.

No GitHub, production system, marketplace API, credential value, or real price
action was used.

## Price-writer inventory

| Path | Intent and approval | Provider request | Result/history/audit | Current failure mode |
|---|---|---|---|---|
| WB BFF single manual approval (`routers/wb_repricer_bff.py`) | Reads an org-wide pending row, creates/approves a Sprint B in-memory draft, then marks the JSON row `applying` | `apply_approved_draft` calls WB upload | Upload ID/job/result is written to memory, then pending JSON, goods cache, and changelog are updated separately | No atomic claim or account-scoped lookup; crash or concurrent whole-JSON flush can lose the claim/result or permit a repeated POST |
| WB BFF bulk approval | Repeats the same flow for selected org-wide pending IDs | One or more WB uploads through Sprint B | Per-item memory jobs followed by one whole-state patch/flush | Partial completion, stale read/write, and replay can diverge; no shared action-key owner |
| Sprint B direct routes (`routers/wb_repricer_sprint_b.py`) | Draft UUID is created and approved in module memory | Approved draft is posted by `run_price_apply`; failed/attention jobs expose retry | Job/result/history is in module memory; control-plane audit is a later independent write | Routes resolve by object UUID rather than org/account; restart loses state; audit cannot prove the provider effect; retry can POST again |
| WB strategy execution, manual BFF (`repricer_execution.py`) | Calculates a draft; can auto-approve/apply from options, otherwise upserts pending approvals | Auto-apply delegates to Sprint B/WB upload | Execution-run data and pending runtime JSON are separate | Inputs/settings and approval state are not transactionally frozen; pending is org-only |
| WB Celery/scheduler (`repricer_tasks.py`) | Hydrates org state, calculates, and may auto-approve/apply under existing gates | Same Sprint B/WB upload path | Finally flushes hydrated module state as one JSON value | Duplicate delivery and multiple workers have no durable claim; last whole-JSON flush wins |
| WB retry (`repricer_sprint_b.py`) | Reuses a failed/needs-attention memory job | Calls the WB POST again | Replaces memory result/history after the call | No durable attempt/dispatch marker or provider idempotency proof; ambiguous acceptance can be resent |
| Sprint C settings/assignment/night mode | Does not POST by itself; mutable versions, assignments, and night-mode policy feed execution | Indirectly enables the WB execution path | Module-memory diagnostics/settings | Restart resets policy state; concurrent changes overwrite; scope is not a durable account-owned fact |
| WB control-plane apply route | Non-dry-run is currently blocked | No real POST on the enabled route | Dry-run response/audit only | Must remain blocked; it is not a durable approval implementation |
| Avito manual approval (`routers/avito_repricer.py`) | Finds an org-wide pending recommendation and resolves account credentials | Direct item price update POST | Removes pending and appends history only after success | Account is payload metadata rather than lookup ownership; crash after acceptance causes replay |
| Avito scheduler (`repricer_tasks.py`) | Calculates candidates; under existing auto-apply gates posts directly, otherwise appends pending | Direct item price update POST | Appends history after the call | No durable intent, claim, attempt, dispatch marker, or shared manual/scheduler action key |

### Current WB race

```text
worker A: read org JSON v0 -> pending P -> mark applying in local copy
worker B: read org JSON v0 -> pending P -> mark applying in local copy
worker A: POST WB (accepted, upload U1)
worker B: POST WB (accepted, upload U2)
worker A: flush whole JSON based on v0, including result U1
worker B: flush whole JSON based on v0, overwriting with result U2

Observable outcome: two external effects, one surviving local result, incomplete audit.
```

### Required durable claim

```text
transaction A: UPDATE approval
               WHERE org/account/approval/status=pending/version=V
               -> one row, version V+1, audit row; commit; A may prepare attempt
transaction B: same UPDATE -> zero rows -> conflict -> no provider call

provider dispatch is allowed only for the committed winning attempt.
```

Exactly-once external effect is not claimed. Without provider idempotency, a crash
after possible provider acceptance is `ambiguous` and requires reconciliation by
upload ID/provider history; absence of proof is not permission to resend.

## Mutable state and persistence hazards

| State | Scope/persistence | Hazard | Required canonical owner |
|---|---|---|---|
| Sprint B drafts, jobs, price history | Process memory | Lost on restart; any worker sees only its own copy | Scoped approval and attempt rows plus append-only audit |
| Sprint C versions, assignments, night state, diagnostics | Process memory | Restart reset and replica divergence | Separate versioned algorithm settings, SKU overrides, assignments, and liquidation/night-domain rows; disposable diagnostics may remain cache-only |
| BFF/runtime pending approvals, recommendations, goods cache, logs | One mutable JSON document per organization | Whole-document lost updates; account collisions and cross-account reads | Approvals are normalized account-owned rows; catalog/price observations use their own canonical tables; diagnostics remain disposable |
| Runtime/algorithm persistence | PostgreSQL JSON, then local file, then process-memory fallback | File/memory success masks DB failure; replicas disagree; write-before-DB can expose state DB never committed | PostgreSQL is authoritative; an approval write fails explicitly when DB fails |
| Avito settings/pending/source cache/history | Predominantly org-only runtime JSON/cache | Two accounts in one org collide; accepted effects can be absent locally | Account-owned settings/actions/history; source snapshots carry account identity |

The current persistence code saves whole JSON payloads without a compare-and-swap
version and can preserve stale `pendingPriceApprovals` during flush. No new
unversioned catch-all JSON repository is requested.

## Canonical identity bridge added in this stage

`app/modules/wb_repricing_repository.py` preserves the existing
`PriceApprovalSnapshot` and its action-key/checksum rules. It introduces a narrow
boundary:

- organization, marketplace account, membership, and optional CatalogSku IDs must
  be verified canonical internal integers with `type(value) is int` and `value > 0`;
- `bool`, zero, negative numbers, floats, numeric strings, provider/external account
  strings, and unresolved legacy identities are rejected;
- accepted IDs become unpadded base-10 strings for the existing kernel;
- an imported snapshot must match organization, account, approval ID, and optional
  CatalogSku exactly; the same immutable object is returned, so its action key and
  request checksum are not recomputed or altered;
- unresolved identity raises a validation-class error containing only the safe
  blocker code `WB_REPRICER_APPROVAL_IDENTITY_UNRESOLVED` and the field name, never
  the rejected value;
- an authenticated actor is an internal membership in the same organization. The
  future service must still verify that membership's account scope and approve/send
  permission before constructing this value.

Legacy snapshots with noncanonical account or CatalogSku identity remain blocked.
This stage intentionally does not guess a mapping.

## Pure repository contract

The protocol exposes only:

```text
get(scope = organization/account/approval)
insert(scope, canonical catalog_sku_id, immutable snapshot)
claim(scope, expected_version, authenticated actor, now)
record_outcome(scope, attempt_id, expected_version, terminal outcome)
```

Every lookup and mutation takes the composite organization/account/approval scope.
There is no UUID-only getter, provider call, apply method, retry method, global
container, serializer, clock singleton, ORM, or fallback. An outcome command binds
one attempt and requires a terminal apply outcome whose version is exactly
`expected_version + 1`.

This is a structural contract, **not a repository implementation**. PostgreSQL
compare-and-swap behavior, transactionality, RLS, restart behavior, and two-session
concurrency remain Stage 2 proof obligations.

## Schema request for Terminal 1

Current migration head observed on the base is `20260908_0061`. Terminal 1 owns all
schema, migration, shared resolver, and config work.

### `wb_repricer_price_approvals`

- Internal surrogate PK plus `organization_id`, `marketplace_account_id`, and
  textual `approval_id`. Do not force UUID: live legacy IDs include prefixed values.
- Composite FK `(organization_id, marketplace_account_id)` to the canonical account
  identity and optional composite FK `(organization_id, catalog_sku_id)` to
  `catalog_skus`; enforce the WB provider at the service/schema boundary supported
  by the existing account model.
- Immutable facts: `nm_id`, `article_id`, recommended price in kopecks, canonical
  request payload (or typed immutable fields sufficient to reproduce it),
  `request_checksum`, `action_key`, and `created_at`.
- Mutable lifecycle columns: status, optimistic version, claimed/decided membership,
  safe reason/error/result codes, WB upload ID, and timestamps.
- Unique `(organization_id, marketplace_account_id, approval_id)` and unique
  `(organization_id, marketplace_account_id, action_key)`.
- Checks: positive integer money and `nm_id`, `version >= 0`, lowercase 64-character
  SHA-256 checksum/action key, allowed lifecycle statuses, and result-metadata
  consistency matching the pure kernel.
- Composite actor FKs `(organization_id, membership_id)` to IAM membership where
  populated. Do not store free-form actor names.
- Partial claim scan index such as
  `(organization_id, marketplace_account_id, created_at, approval_id) WHERE status = 'pending'`.
  Add an `applying` reconciliation index keyed by account/update time if the accepted
  Stage 2 worker query needs it.

### `wb_repricer_price_apply_attempts`

- Scoped composite FK to the approval; immutable attempt ID/number and action key or
  dispatch key; unique per approval attempt and unique dispatch identity.
- Prepared/dispatch/terminal state and timestamps, expected approval version,
  request checksum, provider upload ID, result code, and allowlisted safe error code.
- Persist the intent, attempt, and dispatch marker before the network action. A
  retry is a new attempt only after an explicitly designed recovery transition;
  terminal/ambiguous approvals in this wave cannot be resent.
- Never persist raw exception text or credentials.

### `wb_repricer_price_approval_audit`

- Append-only scoped event rows with approval/attempt identity, before/after
  status/version, authenticated membership, timestamp, action/reason/result and
  filtered safe metadata.
- Claim or outcome transition and its audit event must commit in one transaction.
  No update/delete permission for the runtime writer.

### RLS and atomicity

- Enable and **FORCE RLS** on all three tables. Policies and runtime grants must
  require both organization and marketplace-account scope, using the shared
  resolver supplied by Terminal 1.
- Claim must be a single compare-and-swap statement, conceptually:

```sql
UPDATE ...
SET status = 'applying', version = version + 1, ...
WHERE organization_id = :org
  AND marketplace_account_id = :account
  AND approval_id = :approval
  AND status = 'pending'
  AND version = :expected_version
RETURNING ...;
```

- Zero returned rows is a typed conflict. It must commit no audit/attempt and must
  never call the provider.
- Outcome recording must scope by the same keys plus attempt and expected version;
  closed approvals are immutable.
- DB errors are explicit failures. There is no file/process-memory fallback.

### Backfill and downgrade guards

- Use a manifest with source counts and checksums. Re-running the same backfill must
  not create duplicates.
- Preserve exact legacy approval ID, status, kopeck price, request hash/checksum, and
  action key when those facts exist. Do not alter a checksum or synthesize a new key
  merely to make a row fit.
- Migrate only rows with an exact canonical organization/account mapping and enough
  immutable request evidence to validate the action key. Quarantine/report all
  unresolved rows under the safe blocker; do not guess account, CatalogSku, or actor.
- A later synthetic backfill path must be separately characterized and tested before
  the legacy writer is fenced. It may not manufacture a successful historical
  provider outcome.
- Downgrade must refuse to drop or collapse rows once post-cutover approvals,
  attempts, or audit events exist. A reversible downgrade is acceptable only for an
  unused/empty schema or after an explicit verified export and owner decision.

## Stage 2 concurrency proof obligations

The implementation is not accepted until tests against PostgreSQL demonstrate:

1. Two independent DB sessions claim the same pending approval: one row/winner, one
   conflict, and exactly one fake-provider invocation.
2. Cross-organization and same-organization/cross-account access cannot read or
   mutate the row, including under RLS.
3. Stale versions, closed approvals, replay, and reuse of an action key with a
   different payload all conflict before provider dispatch.
4. Intent, claim/attempt, and audit roll back together on DB failure; there is no
   file/memory continuation.
5. Committed data survives process restart and duplicate delivery observes the
   existing attempt/result instead of POSTing again.
6. Crash points are classified explicitly:

```text
before intent                         -> no action; safe to create intent
after intent, before dispatch marker  -> resumable preparation; no provider proof
after dispatch/possible acceptance,
  before local result commit          -> ambiguous; reconcile, never blind resend
after result commit, before queue ACK -> replay reads terminal result; no POST
duplicate queue delivery              -> scoped claim/attempt conflict; no POST
```

## Prices, stocks, KTR, economics, and profit boundary

No source or finance implementation changed in Stage 1. The following facts remain
gates for later slices:

- Price and stock observations must become account-owned immutable runs with a
  complete manifest and pagination proof. Partial runs cannot become current;
  omitted values are not explicit zero; product/offer/size/warehouse grains cannot
  be mixed; `nmId` is not a size identity.
- Current buyer/SPP evidence is still incomplete. Historical SPP and Club/wallet
  price are not substitutes and cannot unblock the repricer.
- Today's stock cannot be backfilled as historical stock. Exact replay is distinct
  from an explained source revision, incomplete run, or unexplained change.
- The KTR local/all-orders source and grain remain unproven. `krp_table` is not
  `ktr_table`; missing evidence remains `NULL` plus a blocker, including range,
  denominator, warehouse, and cluster uncertainty.
- Versioned dated economics are canonical report inputs, but the repricer still
  uses legacy inputs. Moving state must not change formulas or kopeck results.
- `netProfitKopecks`, `profitClass`, and `abcCode` remain `NULL` until the financial
  owner approves expense composition, operational versus settlement semantics,
  recognition period, org-wide 1C OPEX ownership/allocation, missing-data and
  denominator rules, rounding, thresholds, ties, zero/negative behavior, formula
  version, and effective date. No historical account may be invented for org-wide
  OPEX and no cost is backdated from a later value.

## Preserved behavior and next authorized step

- All real-price, auto-apply, shadow, scheduler, and source flags are unchanged.
- Existing WB/Avito calls, routers, tasks, persistence, formulas, frontend, config,
  migration head, and handoff files are unchanged.
- No network or production mutation occurred.

## Stage 1 verification and baseline delta

TDD started with the new focused test failing during collection because
`app.modules.wb_repricing_repository` did not exist. After the minimal contract was
implemented:

```text
PYTHONDONTWRITEBYTECODE=1 <isolated-venv>/bin/python -m pytest -q \
  tests/test_wb_repricing_approval_domain.py \
  tests/test_wb_repricing_repository_contract.py
-> exit 0; 93 passed

<isolated-venv>/bin/python -m compileall -q \
  app/modules/wb_repricing.py app/modules/wb_repricing_repository.py \
  tests/test_wb_repricing_approval_domain.py \
  tests/test_wb_repricing_repository_contract.py
-> exit 0
```

The pre-change approval-domain baseline was 65 passing tests; this slice adds 28,
for 93 passing focused tests with no change to the original kernel. The architecture
handoff records 105 unrelated known failures in the broader historical suite. This
stage did not rerun or change that broad baseline and claims no delta for it.

The next authorized step is Stage 2 only after the Terminal 1 schema/resolver commit
is accepted. It must implement the real PostgreSQL repository and service, then run
the two-session persistence/concurrency/restart/rollback tests above. Until then,
do not call the repository durable, fence the legacy writer, increase replicas, or
alter scheduler concurrency.
