# T1 user/session publication guard handoff

Date: 2026-09-09. Task 2 on `codex/arch-t1-platform`; consumes paired fetch
`7bfb631d30426dd39a4f4dae7aab4754155b6a62`. No current consumer, writer, router,
schema, migration, permission profile or `infra/db.py` changes.

## Caller interface

All exports come from `app.platform.integrations.publication_guard`:

```python
UserSessionPrincipal(organization_id, user_id, membership_id, session_id)
ExpectedAccountBinding(marketplace_account_id, provider, external_account_id, credential_ref)
ExpectedCredential(marketplace_account_id, credential_id, kind, generation,
                   payload_schema_version, expires_at)
ExpectedIngestionToken(marketplace_account_id, token_id, scope, expires_at)
acquire_publication_guard(session, *, principal, required_permissions,
                          accounts, authorities) -> PublicationGuard
PublicationGuard.revalidate_before_write() -> datetime
```

Expectation classes are frozen, slotted and do not print their fields. IDs are
strict positive PostgreSQL INTEGER-compatible ints (generation is BIGINT), never
bool/coerced strings. User/session IDs are nonempty schema-bounded text, not UUIDs.
Credential/token UUIDs are UUID objects. Provider/kind/schema/expiry/ref metadata
are strict; `credential_ref=None` means exact NULL. Duplicate or contradictory
bindings fail closed; multiple distinct token UUIDs can be fenced for one account.

The trusted service captures principal from authenticated ActorContext plus the
exact canonical membership before fetch. The principal itself authenticates no
one. Permissions are nonempty frozensets from trusted service constants, never
client, HTTP or queue fields. Membership explicit permissions UNION the existing
`permissions_from_profile(membership.role)` decides access; stale ActorContext
permissions and the user's old profile cannot override live membership. Scope
supports all/selected/restricted; restricted account lists preserve positive int
and ASCII decimal-string IDs, including leading zeros. Malformed scope JSON denies.

Convert paired fetch `.binding` explicitly:

```python
binding = fetched.binding
identity = binding.credential_identity
account = ExpectedAccountBinding(
    binding.owner.marketplace_account_id, binding.owner.provider,
    binding.external_account_id, binding.credential_ref,
)
authority = ExpectedCredential(
    identity.marketplace_account_id, identity.credential_id,
    identity.credential_kind, identity.generation,
    identity.payload_schema_version, identity.expires_at,
)
# Provider work finishes before entering this transaction.
with Session(runtime_engine) as session, session.begin():
    guard = acquire_publication_guard(
        session, principal=principal, required_permissions=frozenset({"sync:run"}),
        accounts=(account,), authorities=(authority,),
    )
    lock_domain_run(session)
    guard.revalidate_before_write()
    write_domain_publication_and_audit(session)
    # Session.commit / context exit flushes and validates again automatically.
```

Every fetch credential/token must be declared by the consuming service. Empty
authorities expressly mean a trusted credential-independent operation, including
persisted `cabinet:read` or local `reviews:approve` work. The guard cannot infer an
unreported fetch. Orders must forbid omission on fetched publication paths and
use fixed `sync:run`; this is a caller obligation, not a new grants policy.
Guard idempotent replay returns and persisted cursor reads too: prior output is
not enduring authorization. Domain writes/audit must use this same root transaction.

## Transaction and error contract

Require a clean existing active root Session transaction on PostgreSQL READ
COMMITTED, no pending new/dirty/deleted objects, no SAVEPOINT, one guard per root.
The module creates no session, decrypts nothing and calls no resolver or provider.
Only explicit safe metadata columns are selected, avoiding stale identity-map data.

Locks are user SHARE, membership SHARE, login session SHARE, exact accounts UPDATE
ascending, exact credentials SHARE sorted(account, kind, UUID), tokens SHARE
sorted(account, UUID), then caller domain locks. Fresh user/org/activity,
membership/org/user/permission/scope, session/user/revoke/expiry and every exact
account and authority expectation are checked. No latest-row substitution.

Database `clock_timestamp()` is sampled after all lock waits. The Session-specific
`before_commit` hook first verifies it is the last effective callback (including
class and instance listeners), checks context, flushes once, rejects any remaining
new/dirty/deleted ORM work, then performs complete fresh metadata/context/time
validation and again rejects pending ORM work. This prevents SQLAlchemy's later
commit flush loop from writing mutations left by `after_flush_postexec` or final
validation events after the guard's final check. Failure poisons the handle until
caller rollback. Nested transaction attempts also poison the root.

Trusted callbacks effectively before the guard are supported. Any callback after
it, even a claimed read-only observer, denies commit at guard entry. Effective
SQLAlchemy order determines this: class callbacks precede instance callbacks,
including class registrations made later. The check repeats on reused Sessions;
registration before a new acquisition does not move a callback before an already
retained guard. No callback collection is mutated during dispatch or drained in an
unbounded flush loop. Callers must use this supported Session protocol: raw DBAPI
or connection COMMIT, private-state manipulation and cursor/connection callbacks
executing SQL after checks are prohibited bypasses outside these guarantees. The
guard is not a sandbox for arbitrary application code.

Three constant callbacks are installed once per guarded Session. They hold no
captured Session/root state and never modify listeners during event dispatch.
Root completion/rollback invalidates the handle and clears active state; reused
Sessions/pool connections have no stale authorization and listeners do not grow.
Sessions that never acquire a guard receive no hooks. Both tenant marker and actual
PostgreSQL tenant/isolation settings are validated through commit.

`PublicationGuardError` exposes only `publication_context_invalid`,
`publication_access_denied`, `publication_binding_changed`,
`publication_authority_invalid`, `publication_expired`, or
`publication_persistence_failed`. Unknown codes normalize to the last code.
Acquisition, revalidation and final-flush SQLAlchemy failures are sanitized without
SQL/input/secret reflection. The caller still owns rollback and physical COMMIT
driver errors (outside the hook); do not reflect raw database exceptions in HTTP.

## Verification and limits

Missing-API RED was observed before implementation in both contract and real
PostgreSQL tests. Additional self-review RED reproduced malformed tenant markers,
leading-zero scope compatibility and multiple distinct same-account tokens; fixed
and rerun. Exact commands, counts and allocated-resource cleanup are in the local
task execution report `.superpowers/sdd/2026-09-09-publication-guard/task-2-report.md`.
Initial focused plus paired-fetch/crypto/store/WB binding run: **245 passed in
14.00s**, exit 0. Independent review then found the additional-flush callback gap
(I1), reproduced on actual PostgreSQL before the fix. The finalizer protocol above
includes that correction; exact follow-up verification is recorded in the report.
I1 regression RED: **3 failed in 2.73s**; covering guard GREEN: **161 passed in
11.02s**; final seven-file combined GREEN: **255 passed in 14.41s**, exit 0.
Ruff, compileall and diff checks pass; all follow-up disposable resources were
cleaned and verified absent. The inherited passfile warning remains unchanged.

PostgreSQL coverage includes both lock winner orders for user deactivation/org
change, membership revocation/permission/scope change, session revoke, account
disconnect/rebind/ref change, credential revoke/generation and token revoke.
Actual existing credential replace/revoke/reencrypt and ingestion rotate/revoke
writers run in both winner orders. Expiry is exercised during account, membership,
domain and final-flush lock waits, plus expiring Avito credentials and tokens.
Synthetic publication plus audit proofs commit together or leave zero rows;
deferred constraint failure also proves physical COMMIT rollback. Stale ORM,
exact UUID/generation/ref/expiry, permission union, tenant changes during flush,
listener reuse and credential-independent reads are covered.

Tests/app use only the worktree `backend/.venv`, scrubbed environment and the
existing OS sandbox denying IP networking and secret reads. Actual PostgreSQL is
only a fresh random disposable database/runtime role through the authorized local
Unix socket; migrations stop at 0061. Existing helper cleanup verifies exact absence.
The approved alternate Python is used for Ruff only. Existing PostgreSQL `/dev/null`
password-file warnings remain disclosed; output is not claimed pristine.

No production activation, provider/Redis/network work, current consumers, worker
or delegated-job principal, bearer-only authentication, access-token-expiry claim,
permission expansion, historical A→B→A binding epoch, whole application suite or
other writers' universal lock-order proof. Validation proves expiry at the final
database validation instant, not frozen wall-clock time until physical COMMIT.
Requested local preflight-critic was unavailable; an isolated self-review pass
covered spec/source/races/error/lifecycle checks. Controller independent review is
required before this handoff is accepted or wired by domain owners.

## Physical transaction admission follow-up — Task 3

The public API is unchanged. The supported caller is an **Engine-bound Session**
with an existing clean logical root and an active physical PostgreSQL root at
READ COMMITTED; the DBAPI driver's public `autocommit` must be exactly `False`.
All Connection-bound Sessions are rejected, including default
`conditional_savepoint`, `rollback_only`, `create_savepoint`, and `control_fully`.
A joined external root can otherwise survive Session completion and outlive the
final authorization check. The guard never assumes or transfers external ownership.

The shared private admission check runs before acquisition context SQL and on
every existing guard context check, including explicit revalidation and both
commit-finalizer validations. It rejects missing/inactive physical roots, active
Connection SAVEPOINTs, and missing/unknown/autocommit driver states with
`publication_context_invalid`; SQLAlchemy inspection errors retain sanitized
`publication_persistence_failed`. A later rejection poisons the handle through
the existing failure path until caller rollback. No repair SQL, global hooks,
private transaction maps, account-context helper invocation, permission changes,
consumer changes or altered lock/finalizer protocol were introduced.

Actual PostgreSQL RED against the accepted pre-follow-up guard: **10 failed,
1 passed, 123 deselected in 3.73s**, exit 1. All ten failures were `DID NOT RAISE
PublicationGuardError`: engine execution-options AUTOCOMMIT, constructor
AUTOCOMMIT, bound Connection AUTOCOMMIT, initial Connection SAVEPOINT, later
Connection SAVEPOINT at explicit validation/commit, and all four external join
modes. The autocommit setup uses a synthetic session-level tenant value to prove
old guard acceptance, rather than merely an eventual tenant mismatch. Ordinary
Engine-root publication was the passing positive control.

Focused GREEN: **24 passed, 161 deselected in 2.92s**, exit 0. All initial invalid
modes issue zero observed application SQL; the external owner's settings, row and
root remain untouched. A later SAVEPOINT denial rolls back both already-flushed
synthetic publication/audit proof rows, and closing the SAVEPOINT cannot revive
the poisoned guard. Driver-state/error contract tests additionally cover missing
or inactive roots, absent/unknown flags, non-bool 0/1 and safe exception rendering.

Final prescribed six-file suite (guard unit/PostgreSQL, paired fetch
unit/PostgreSQL, account context unit/PostgreSQL): **304 passed in 17.37s**, exit 0.
Ruff, compileall and diff checks pass. Every allocated disposable database and
runtime role was cleaned through tracked helper `finally` blocks and verified
absent, including RED runs. The existing `/dev/null` password-file warning remains.
Exact commands, natural exits and resource identifiers are in the local
`.superpowers/sdd/2026-09-09-publication-guard/task-3-report.md`.

This does not detect raw SAVEPOINTs opened and closed entirely between checks,
or sandbox arbitrary caller SQL/DBAPI commits or callbacks after final validation.
No new driver support, physical COMMIT error interception or absolute expiry-at-
COMMIT guarantee is claimed. Isolated self-review found no unresolved issue;
controller independent review is still required before follow-up acceptance.
