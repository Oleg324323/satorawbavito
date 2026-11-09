# Architecture integration acceptance

> Latest user scope supersedes older completion requirements below: deliver the finite architecture foundation first. Follow `2026-09-09-architecture-foundation-delivery.md`; original unfinished features and operational work are retained as P1/P2 backlog, not silently marked complete.

User scope: finish all assigned T1–T4 work, independently check and correct it,
audit omissions, then publish the verified result to `OMNIlos/satornawb:filipp`.
Publication is conditional on correctness. This checklist does not authorize
production rollout, real provider actions, or replacement of missing evidence.

## Current scope and integration checkpoint (supersedes historical gates below)

The user deferred the shared order assembly/printing module (Production).
Existing code/migrations and compatibility are preserved; new work items,
assignments, batches/calendar, sheets/labels, print archive and delivery work are
not current completion requirements. They remain deferred, not done. Orders
ingestion/storage/read/API remains active. KIZ and the separate matcher are
excluded; unrelated WB/Avito matching and historical compatibility remain.

Complete every remaining task supported by available requirements autonomously.
Record genuinely missing information/rules and their impact in one final list;
do not invent them or block unrelated implementation. Separate local acceptance
from unauthorized operational activation.

The consolidated frozen platform/consumer milestone now has root evidence:
17 main/router/wheel tests, 433 combined PostgreSQL tests, full frontend
430 PASS/0 FAIL/0 PENDING, both TypeScript projects and Vite build pass.
These are distinct gates, not a fabricated sum or full backend acceptance.
Details, exact inputs, failures and limits:
`../reports/2026-09-09-root-consolidated-milestone.md`.
Newer owner services outside those frozen inputs and full active-TZ acceptance
remain pending. Historical statuses below must not override this checkpoint.

## Execution

1. Accept immutable ready commits from each owner; verify prerequisites before
   releasing dependent repository/API work. Do not merge active working files.
2. Implement and verify each independent domain slice without waiting for
   unrelated external policy or source gaps.
3. Integrate owner commits on the existing `filipp` lineage, resolve overlaps,
   and repeat tests against the combined tree.
4. Independently review the complete requirement matrix and critical races.
5. Fetch `filipp` again, preserve intervening changes, rerun affected checks,
   and push normally only after acceptance. Verify the remote commit.

## Required acceptance evidence

| Area | Owner | Gate |
| --- | --- | --- |
| Credentials/context | T1 | Account-owned HTTP/worker binding, IDs-only queues, revoke/commit fencing, fail-closed credential resolution |
| Platform operation | T1 | Safe rotation/backfill/restore, migration bootstrap, runtime grants, installed packages, CI and bounded health checks |
| Repricer persistence | T2/T1 | Durable approval/attempt/audit, atomic CAS, two-session contention, dispatch/crash/replay matrix, no memory fallback |
| Settings/calculation | T2 | Versioned domain rows and immutable inputs, concurrent update preservation, exact-kopek characterization, single writer |
| Prices/stocks | T2 | Account/grain isolation, complete manifests, atomic publication, revisions/replay and partial-run preservation |
| Financial evidence | T2 | Proven KTR/source policy and explicit financial decisions; unapproved or missing evidence remains null with a blocker |
| Orders | T3/T1 | Actual schema/RLS, immutable observations, complete publication, revoke fencing, stable authorized snapshot pagination |
| Fulfillment | T3 | Proven source identity/readiness/deadlines; Statistics observations do not establish fulfillment eligibility |
| Production | T3/T1 | Work-item quantities, authenticated CAS/idempotency/audit, versioned grouping, immutable historical sheets |
| Production renderers | T3 | Separate required XLSX/A4/sticker parity, frozen inputs, checksums, restart/concurrency and authorized downloads |
| Reviews | T4/T1 | Persisted facts/policy/drafts/approvals, account isolation, atomic heads/audit, durable dispatch and ambiguous recovery |
| Notifications | T4/T1 | Transactional events/outbox, membership receipts/preferences, read-only GET, verified external delivery contracts |
| Frontend | T4 | Real typed API consumers, scoped queries, validation/error/409 UX, exclusive route ownership and feature gates |
| Release | All/root | Full backend/frontend suites, natural exit, typecheck/build, migration/restore and integrated concurrency checks |

## Evidence rules

- Owner test reports and independent verification are recorded separately with
  exact commit, command, outcome and limitations.
- A protocol, schema, serializer or fake-renderer test alone does not establish
  a working repository, mounted API, production delivery or artifact parity.
- Baseline failures require classification and correction where in scope;
  passing a selected subset is not the full release gate.
- Missing source code and financial decisions remain explicit unresolved gates.
  Do not reconstruct historical parity or choose business rules silently.
- Local test infrastructure uses disposable synthetic databases and fixtures;
  it must not inspect operational records or execute real marketplace actions.

## Initial independent evidence

- T1 `94381860a34632783fd1f75ab5aa51083f749597`: documented safe subset
  independently reproduced on a detached tree with network denied: 265 passed,
  six existing warnings, natural exit 0. This excludes actual PostgreSQL gates.
- T3 `f013dec2c0c17ba71e8905d334d41e829049d8c6` including serializer and
  strict decoder predecessors: 234 passed, two dependency warnings, Ruff passed
  on a detached tree. No blocking finding in the bounded independent review.
  Persistence/publication and HTTP renderer-error mapping remain separate work.

No whole-architecture acceptance or GitHub publication is claimed here.

## Scope correction and prerequisite verification

T3 relayed a newer direct user instruction excluding KIZ and the standalone
matcher from the requested product. Remove their isolated additions and exposed
placeholders only after checking dependencies; preserve core WB/Avito workflows,
Catalog resolution and Avito return matching. Historical evidence, backups and
operational data are not cleanup targets. Production itself remains in scope.

Orders schema `f9c7401d946063465e0576a689eb247c368eed73` independently passed
101 tests on fresh disposable PostgreSQL databases with restricted runtime roles,
including migration, RLS, contention, ACL and rollback checks (145.64 seconds,
natural exit 0). Network was denied except the local Unix PostgreSQL socket.
Independent DDL/ACL/Catalog review found no blocker. This releases the schema
prerequisite to T3; application authorization/publication and actual repository
and API acceptance are still required. Formatting-only followups through
`1514425516d5f24b53e5956912e93da1e2682ede` passed scoped Ruff independently.

## User-initiated Orders authorization decision

This is a new local implementation decision within the requested architecture
work, reviewed independently; it is not a claim about an earlier canonical
policy or permission to activate production.

- User-initiated synchronization/publication requires `sync:run`, following the
  existing manual Avito return-sync and control-plane sync-run permission.
  Orders service pins this requirement internally; HTTP input cannot choose it.
- Preserve existing profile permissions plus explicit membership grants without
  widening or silently narrowing profiles.
- Publication and replay returns require fresh authorization in the same
  transaction: user/session/membership, exact account scope and credential
  identity/generation. Authorization at enqueue time alone is insufficient.
- Persisted reads require `cabinet:read` and live user/session/membership/account
  authorization, including cursor access. Reading historical snapshots alone
  does not require decrypting or possessing an active marketplace credential.
- Worker delegation is a separate contract; no fabricated admin principal or
  implicit worker authorization is allowed.

## Combined integration checks so far

The integrated tree includes ready T1 through `1514425`, T2 through `6ee8485`,
T3 through `4374bb5`, and T4 through `1d0700d`. Ordinary merges preserved the
existing `filipp` lineage; no remote publication has occurred.

- Combined selected backend checks at `95aef72`: 888 passed, two dependency
  warnings, natural exit 0. OS network denial plus the Orders audit fence;
  synthetic fixtures only. This is not the full backend suite.
- Earlier combined attempt used the repricer-specific in-memory-only SQLite
  fence with an Avito test that intentionally creates a disposable temporary
  SQLite file. That harness mismatch failed once; using the appropriate
  network fence passed without changing product code or tests.
- Frontend at `a9e7601`: ten focused files, 96 tests passed; application and
  tooling TypeScript checks passed independently, with build-info outside the
  shared dependency directory.
- Independent complete bounded frontend review through `8d68560`: 86 focused
  tests passed, no blocking regression found. The owner's 27 remaining full
  frontend failures are still open and have not been waived.

Actual domain repositories, shared authorization fencing, subsequent schemas
and full release checks remain in progress.

## Open release blocker: long Orders identity keys

A later PostgreSQL 16.15 test found an uncovered limit in revision 0062: valid
4096-byte external order, item-line and source-run keys exceed the physical
B-tree index tuple limit (SQLSTATE 54000), including existing ON CONFLICT paths.
Rollback preserved all eleven Orders tables in the reproduced cases. Earlier
101 passing cases did not cover this boundary.

T1 owns an explicit migration amendment; T3 must adapt the corresponding
repository identity/replay operations. Preserve exact valid TEXT without an
invented length cap or treating a hash collision as identity. Check every raw
TEXT unique/index key in the new schema, not just the three reproduced ones.
Require real long-key/replay/concurrency/collision/rollback/downgrade evidence,
including supported transaction isolation. This defect must be closed before
full persistence acceptance and publication.
