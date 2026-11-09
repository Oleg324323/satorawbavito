# Session-bound Orders job authority implementation

Spec: `backend/docs/superpowers/reports/2026-09-09-t1-user-job-authority-proposal.md`.
The accepted source amendment is commit `2f0422076ac0b365d2e97432b9ce57d6aa6b350f`,
`backend/docs/superpowers/reports/2026-09-09-orders-job-source-binding.md`.
This plan implements the already accepted local architecture, not provider-complete
sync, operational execution policy, scheduler activation or price/message jobs.

## Global constraints

- Work only in the existing T1 worktree and branch; no push, deploy, production,
  working database/Redis, provider/network actions, real credentials or `.env` reads.
- T1 alone owns Alembic/runtime/shared guard. Do not edit T2/T3/T4 domain files,
  frontend/serverless, legacy migrations, plaintext consumers or operational flags.
- User changed sequencing: finish implementation first, then final testing. Do not
  run intermediate tests, databases, reviewers or diagnostics during this package.
  Existing evidence is retained; report new implementation as UNVERIFIED.
- No arbitrary worker principal, serialized user session, permission set, provider
  token, request body, lease/claim token or external error in queue/result/log/audit.
  Queue payload is exactly organization ID, account ID and job ID.
- No production policy defaults. All lifetime/lease/budget/backoff values are
  explicitly supplied by trusted application policy; missing policy fails closed.
- No general-purpose job framework, outbox, autonomous recovery schedule or
  provider registry activation. Preserve the commit-before-enqueue delivery gap.

## Controller decisions

The source proposal's four relation/state/audit matrices are binding. Current
head is `20260909_0071`; use `20260909_0072` only after independently checking the
actual single head and target absence. Stop on a collision, never renumber silently.

Use actual existing parent constraints or new-object integrity triggers; do not
alter old tables just to add redundant principal composite uniques. Parent user,
membership and session consistency must still be enforced, not merely mentioned.
All new relations enforce both canonical organization and account context RLS.
Provider comes from full canonical account FK, not a new caller-controlled GUC.

Public create/read/cancel/claim/recovery wrappers own a clean physical root through
an explicitly supplied Session factory. Publication participation alone receives
an existing clean caller-owned Session root. A returned claim is usable only after
its own claim commit. Existing UserSessionPrincipal may be constructed only from
authenticated ActorContext + freshly read membership at creation, or from the
persisted immutable job at worker entry; never from queue-supplied user fields.

The existing publication guard remains the final Session before_commit listener.
Add a narrow trusted transaction-fence registration mechanism to that guard for
the job guard: registrations are bound to the same root, finalized after flush and
live auth validation, poisoned on any failure, and cannot be added/replaced during
finalization. Preserve physical-root/AUTOCOMMIT/nested/listener-order/late-flush
protections. No new generic event bus or externally selectable callback names.

First remote read authority is exactly one encrypted `wb_api` or
`avito_oauth_access`, provider-specific. Do not silently substitute/refresh an
expired Avito access credential. An additional dependency needs a future trusted
source binding. The child relation still models exact metadata rather than secret.

## Task 1: Implement complete local Orders job authority package

Read this task first. Follow the referenced full spec and its source amendment,
including every lifecycle, audit, policy, replay, closure and delivery limitation.

### Scope and exact files

Create:

- `backend/alembic/versions/20260909_0072_user_orders_jobs.py`
- `backend/app/platform/integrations/user_orders_job_contract.py`
- `backend/app/platform/integrations/user_orders_job_store.py`
- `backend/app/platform/integrations/user_orders_jobs.py`
- `backend/docs/superpowers/reports/2026-09-09-t1-user-orders-job-handoff.md`

Modify only:

- `backend/app/platform/integrations/publication_guard.py`
- `backend/ops/runtime-db-role.sql` (new objects and prerequisites only)

Do not create tests in place of implementation. Record exact required final tests
in the handoff for the subsequent final validation phase. No test or DB process in
this task. No new dependencies, config defaults, router/task registration or domain
adapters. If another path is truly needed, give controller the concrete reason.

### Implementation

1. Read the spec fully, actual `publication_guard.py`, paired credential store,
   `control_plane/auth.py` ActorContext boundary, identity/cabinet/integration ORM,
   actual Orders0062/0064/0067 parent shape, runtime grants and applicable instructions.
   Resolve exact parent fields locally; never assume a proposal name is real SQL.
2. Implement strict immutable/redacted Python contracts for fixed Orders request,
   execution policy, metadata dependencies, safe stored status, committed claim and
   publication handle. Exact canonical ASCII JSON bytes + SHA256, duplicate-key and
   re-encoding validation; no bool-as-int, NUL/surrogate/lossy date/version coercion.
   Versions/source kinds are trusted service bindings, not caller-selected imports.
   Preserve every explicit source selector, both requested windows NULL and the
   exact `orders-job-v1:<job UUID>:<attempt UUID>` identity. Policy snapshots must
   contain finite explicit values and checked deadline/backoff arithmetic.
3. Add the four exact relations `user_orders_jobs`,
   `user_orders_job_authorities`, `user_orders_job_attempts`,
   `user_orders_job_audit`. Use actual parent FK shapes, scoped uniqueness,
   immutable delegation/history, typed request projection and canonical byte
   reconstruction, dual forced RLS, one current attempt, exact version CAS and
   deferred reciprocal transition/audit witnesses. Validate the complete matrix
   from the spec, not just enum membership. Every terminal state is immutable.
   Successful result must refer to the exact completed Orders run, source kind,
   adapter/mapping, historical account binding and per-attempt run key; never infer
   completeness from pages. Changes to a prior job version cannot reuse an audit.
   No fabricated run at claim. Match real run state/coverage column semantics.
4. Implement clean-root authenticated create + status/replay/cancel. Fresh live
   principal/account guard precedes exact scoped idempotency lookup; same command
   compares original session and immutable request/policy/binding. Another session
   cannot adopt a job. Replay is not a new attempt. Capture current exact credential
   metadata under the guard, no decrypt during creation. Deadline cannot exceed
   session/credential expiry; old session refresh cannot extend stored deadline.
5. Implement trusted queue-locator-only claim. Scoped immutable locator read learns
   stored principal, then acquire user→membership→session→account→credential guard,
   then job→attempt locks. Recompare locator under lock. Check state/version/budget,
   explicit policy, due time and fresh DB clock after waits. New UUID attempt/claim
   token, exact audited job transition and physical commit before returning claim.
6. Implement exact paired fetch resolution outside publication locks, asserting
   all persisted credential/account metadata against ResolvedCredentialForFetch.
   Return only its existing secret wrapper to trusted in-process caller; no provider
   call, fallback, newest-generation substitution or worker admin. Default handler
   dispatch remains unavailable without a trusted source implementation.
7. Implement caller-root publication guard and atomic success completion; hold
   job/attempt fencing through final flush/commit via the trusted guard extension.
   Validate exact token/current pointer/versions/lease/deadline, including own
   expected successful transition, reject reuse. Domain publication remains an
   injected trusted participant in the same transaction, never a fabricated row.
8. Implement lease renewal, safe read-failure retry, expired-lease reclaim, final
   failure and user cancellation/revocation according to the full spec matrix.
   A dedicated state-closing-only entrypoint after failed authority may lock
   job/attempt without taking lower-order auth locks; it can only record a proven
   allowlisted denial and cannot fetch, publish, renew, change bindings or expose
   content. Do not accept an arbitrary caller reason as proof of revocation.
9. Implement explicit commit-then-deliver locator helper and readback/recovery
   primitives. Duplicate running/terminal delivery does not refetch. Unknown or
   ambiguous commit becomes safe readback-required, not blind retry. No broker
   connection, scheduler or pretending no-outbox delivery is guaranteed.
10. Runtime grants are narrow and new-object only: no public execute, truncate,
    delete, role/default ACL reform or unrelated helper ownership changes. Pure
    helper execute may be granted where CHECKs require it; match exact signatures,
    never basename adoption of preexisting overloads. Empty-only downgrade locks
    fixed new tables and refuses hidden history, no CASCADE/historical data loss.

### Delivery and final checks (not run during implementation)

Record exact columns/types/FKs/indexes/functions, imports and call signatures,
transaction ownership/lock order, queue/result contract, safe codes, retry/closure
proof and remaining activation inputs in the handoff. Explicitly distinguish
implemented code from final integration acceptance. Preserve known no-outbox gap,
partial-source semantics, missing production policies and no provider handlers.

Final phase must cover request/SQL byte parity; wrong principal/org/account/session;
revocation in both lock orders; expiry at waits/flush; two-session claim/CAS/reclaim;
audit rollback/no-orphans; immutable terminal/history; generation change; exact result
run provenance; uncertain commit readback; no double fetch; wrapper/repr/canary;
both RLS USING/WITH CHECK; raw runtime mutation constraints; empty and populated
upgrade/downgrade guards; existing publication guard compatibility; broker/retry/
result secret absence with synthetic transport only. Disposable Unix PostgreSQL
only after separate final resource admission; no SQLite substitution or skips.

Finish source first. Report IMPLEMENTED / UNVERIFIED with exact remaining checks.
Commit schema/runtime as `feat: add session-bound Orders job storage`, then Python
service/guard/handoff as `feat: add trusted Orders job authority`. No intermediate
test/review loop. No operational enabling or scope expansion.
