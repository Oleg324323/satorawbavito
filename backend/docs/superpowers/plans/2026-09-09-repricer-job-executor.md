# Repricer job and executor package

Spec: `backend/docs/superpowers/specs/2026-09-09-repricer-job-executor-design.md`.
Depends on completed source for the Orders job transaction-fence extension, not
Orders retry semantics. Candidate migration number is resolved only on dispatch
from actual single Alembic head; no reserved number or old-migration edits.

Global constraints: T1 worktree only, no domain rewrites, no provider/production/
working DB/Redis/real secrets/.env, no role provisioning or flags, no dependencies,
no push/deploy. User requires implementation before tests; no intermediate PG,
tests, review cycles or extra test writing instead of the package. Keep the pending
final verification honest. No fake worker/user, operational TTL or retry defaults.

## Task 1: Implement job, receipt and executor boundary together

Read spec fully. Read exact original T2 requests through git show:
3c99f1eddb4750faa2be0ce79806c2d6f26cbc7e:
backend/docs/superpowers/reports/2026-09-09-t2-repricer-worker-authority-amendment.md;
470d41076f98009cd9fca60311092e5b3c648ffe:
backend/docs/superpowers/reports/2026-09-09-t2-provider-upload-receipt-schema-request.md.
Preserve their immutable identities, prior-marker commitment, clock-domain and
lossless upload-ID rules. The new spec supplies the previously absent executor
identity boundary. No role is provisioned by this source-only implementation.

### Exact paths

- New additive migration under `backend/alembic/versions/`, name
  `YYYYMMDD_NNNN_repricer_job_executor.py` using actual next head at dispatch.
  Stop on head/collision disagreement, do not choose another revision silently.
- Modify `backend/ops/runtime-db-role.sql`, new objects only.
- Create `backend/ops/repricer-executor-grants.sql`, inert explicit-role grants.
- Modify `backend/app/config.py`, only optional nonsecret executor/API role names
  and fail-closed parsing; neither role is defaulted, no connection URL or credentials.
- Create `backend/app/platform/integrations/repricer_job_contract.py`.
- Create `backend/app/platform/integrations/repricer_job_store.py`.
- Create `backend/app/platform/integrations/repricer_job_executor.py`.
- Create `backend/app/platform/integrations/worker_identity.py`, only the concrete
  actual DB login verifier shared by price and subsequent Review workers, not a
  generic principal/permission registry. Role names are
  `marketplace_executor_role` and `marketplace_api_runtime_role`, default None.
- Modify `backend/app/platform/integrations/publication_guard.py`, only the exact
  Repricer initiation fence and separate exact closing-root installation described
  in the spec amendment below. Preserve the existing strict user entry point.
- Create `backend/docs/superpowers/reports/2026-09-09-t1-repricer-job-executor-handoff.md`.

No T2 domain/worker/adapter edits, broad publication guard rewrite, tests or other paths.
Consume the preceding implemented transaction-fence extension; if a signature is
missing, ask controller with the exact required capability before touching it.

Dispatch amendment: actual predecessor only admits the Orders handle, so the
controller explicitly authorizes the tenth guard path, not a fake Orders handle.
The immutable job is read under canonical account FOR UPDATE serialization;
no job UPDATE privilege or redundant advisory lock is granted merely for a lock.

### Code implementation

1. Strict typed/redacted job, policy/authority, receipt, expected-version/attempt,
   trusted-root handles and allowlisted errors. No request JSON/provider payload,
   arbitrary caller reason/actor/permission in the executor interface. Preserve
   scale-free numeric versions and exact decimal upload ID, no int(float).
2. Five relations exactly as spec: job, job authority, job-created audit, receipt,
   receipt audit. Implement actual composite FKs/ownership, both-context forced
   RLS, complete immutability, new-object-only narrowed ACLs, child lookup indexes
   and reciprocal deferred graph validation. Approval format/pending check for
   new job only; historical authorized exact replay remains possible. Deadline
   creation check uses actual DB clock and source session under proper locks.
3. Authenticated create/readback/replay wrapper owns physical root; ActorContext
   produces principal only via fresh membership lookup. Capture connected account
   and exact encrypted WB credential metadata, with live fixed price:send guard;
   no decrypt, fallback, automatically imported legacy jobs or updated origin.
4. Implement actual executor-login verifier using explicit configured executor
   and API role identities, actual session_user/current_user, role attributes,
   protected ownership and API membership checks. Fixed safe errors only. No
   configuration means denied, not API-role fallback. Connection factory is
   explicitly injected by trusted bootstrap, never reconstructed from queue.
5. Initiation resolves immutable scoped stored origin + authorities and exact
   approval request, acquires current user-session guard, then job/approval/attempt
   locks. Full versions/claimant/key/credential/deadline checks through final
   commit. Distinguish claim/reserve/dispatch action preconditions from closing.
   Queue is exactly org/account/job; no permission/session/capability serialized.
6. Separate state-closing handle requires executor login and scoped job/attempt/
   dispatch binding but cannot call initiation or fetch APIs. Permit only existing
   domain terminal outcome participation and exact receipt publication/readback;
   never reopen terminal approval or fabricate known provider rejection. Carry
   final root fencing around T2 participation, not copied domain transition code.
7. Receipt wrapper starts a new physical root, loads previously committed marker,
   locks canonical account→job→approval→attempt, verifies all keys and claimant,
   exact replay before immutable insertion+audit. Preserve observed and recorded
   timestamps; conflicting ID safely conflicts, ambiguous can retain late receipt.
   Never write approval/attempt or price cache as a side effect of receipt insert.
8. Explicit helper to resolve exact paired fetch credential only under valid
   initiation authority, outside publication locks, matching captured generation
   and account/ref. Closing never decrypts or borrows a replacement key/account.
   A passed check is not perpetual send authority: handoff requires fresh final
   guard immediately before provider I/O and retains distributed revocation gap.
9. Handle uncertain physical commits by scoped durable readback-required outcome,
   not new POST. Completed receipt/job replays do not authorize send. No broker,
   provider, Celery task or production handler registration is introduced.
10. Inert grants script validates already-provisioned roles and grants only exact
    columns/functions from spec. No password, CREATE/ALTER ROLE, default privileges,
    unrelated ACLs, plaintext-source/key/verifier SELECT or identity/permission/expiry UPDATE.
    Exact encrypted credential identity/AEAD/expiry SELECT is explicitly required
    for the existing initiation resolver; ciphertext is not a master key. Closing
    never invokes that resolver, and no independent crypto/client store is created.
    Narrow timestamp UPDATE exists solely to enable required PostgreSQL row locks;
    disclose its capability and activation approval requirement. Fail on missing
    column or unsupported role topology, never silently broaden grants.
11. Empty-only downgrade locks exact new tables and refuses any hidden history;
    explicit dependency drops, no CASCADE, old approvals/history preserved.

### Delivery

Publish exact imports/signatures, fixed action classes, columns/helpers/FKs/grants,
root and lock ownership, expiry/uncertain commit semantics, explicit executor
bootstrap requirements and safe errors. Mark IMPLEMENTED / UNVERIFIED; operational
executor identity/grants/policy and T2 original POST parser are activation inputs,
not implemented by setting an enum. Record concrete missing requirements instead
of claiming completion. No provider-complete or exactly-once assertion.

Keep final security/concurrency/RLS/migration/guard/transport cases from spec in
the handoff, do not execute them during implementation. Commit schema/grants as
`feat: add repricer job and upload receipt storage`, then runtime/config/handoff as
`feat: add explicit repricer executor authority`. Root final tests later, no push.

Source-delivery amendment: if implementation discovers a necessary0073/grants
addition after the schema SHA has been communicated, commit it separately as
`fix: fence repricer job creation authority` BEFORE the Python runtime commit.
The user's separate-schema-commit rule takes precedence over the planned two
commit count. Preserve original SHA; no amend/rebase. Handoff must include the
full original schema→schema follow-up→runtime chain, all UNVERIFIED.
