# User-session Publication Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide race-safe same-row fetch authority capture and a caller-transaction user/session publication guard for T3 and other trusted domain services.

**Architecture:** Pair decryption with safe identity evidence before fetch. After fetch, lock fresh user/membership/session/account/credential metadata through the caller's atomic publication commit. Preserve old consumers; user-only policy is explicit.

**Tech Stack:** Existing Python, SQLAlchemy Session/events, PostgreSQL READ COMMITTED, pytest; no dependencies/migrations.

**Spec:** `backend/docs/superpowers/specs/2026-09-09-publication-guard-design.md` (read fully). Root's explicit local architecture decision chooses fixed `sync:run` publication and `cabinet:read` persisted read; no grants/profile changes.

## Global Constraints

- Only T1 worktree `/Users/bratishka/Downloads/satornawb-main/.worktrees/arch-t1-platform`, branchcodex/arch-t1-platform. No otherworktree/domain source mutation, GitHub/push/deploy, production, .env/real credentials, network/providers/Redis, flags/key/host changes.
- No competing Orders/T2/T4 code. No schema revision, auth-router/session-helper behavior changes, permission registry expansion, worker principal, bearer-only auth, KIZ/matcher work.
- Use own backend/.venv. PostgreSQL only fresh random own disposable DB/runtime roles through authorized Unix `/tmp` maintenance postgres. Deny IP networking and secret-file reads in OS sandbox, scrub env/passfiles; no existing app data/role/server changes. Cleanup exact resources in finally with absence checks.
- User/membership/session/account and authority checks are fresh, safe metadata only. Existing permission union/profile aliases and restricted account-list compatibility preserved. Missing/malformed authority fails closed, never memory/file/legacy fallback.
- Lock user SHARE→membership SHARE→session SHARE→accounts UPDATE ascending→credential SHARE→token SHARE→domain locks. No provider I/O or separate session/decrypt under held publication locks. DB clock_timestamp after waits and final flush; caller owns root transaction.
- Preserve `resolve_marketplace_credential(owner, kind)->DecryptedCredential` and WB seller verification behavior. New interface does not switch current consumers.

## Task 1: Capture exact fetch evidence and fix WB binding lock order

**Files:**
- Modify `backend/app/platform/integrations/credential_store.py` only paired fetch API/types.
- Modify `backend/app/platform/integrations/wb_credentials.py` only locking branch of binding candidate/necessary metadata locator.
- Create `backend/tests/test_marketplace_credential_fetch.py`.
- Create `backend/tests/test_marketplace_credential_fetch_postgres.py`.
- Create `backend/docs/superpowers/reports/2026-09-09-t1-fetch-authority-handoff.md`.

**Consumes:** exact `MarketplaceAccountCredentialOwner`, `CredentialIdentity`, `DecryptedCredential`, credential store helpers and WB binding workflow. Read full store, wb_credentials and focused tests before changes.
**Produces:** `CredentialFetchBinding(owner, external_account_id, credential_ref, credential_identity)` safe frozen dataclass; slots/redacted/nonserializable `ResolvedCredentialForFetch` explicit `.secret`/`.binding`; `resolve_marketplace_credential_for_fetch(owner, kind)` selecting account+credential in ONE joined statement. New API requires connected account, decrypts exact selected row and closes short session before return. Never invoke old resolver then latest metadata. Original public resolver untouched.

- [ ] Step1 write focused RED for missing paired API, exact same-row identity, connected account, no legacy/helper calls, typed errors, repr/str/JSON/pickle/deepcopy/schema canaries.

```python
resolved = resolve_marketplace_credential_for_fetch(owner, 'wb_api')
assert resolved.binding.credential_identity.credential_id == seeded_id
assert resolved.binding.credential_identity.generation == 1
assert resolved.secret.reveal() == synthetic_payload
assert synthetic_marker not in repr(resolved)
with pytest.raises(TypeError):
    pickle.dumps(resolved)
```

Record missing-interface FAIL before implementation, then test actual SQL joined read (not only mocked consecutive queries) against disposable PostgreSQL; mutate/revoke/re-encrypt after return and prove old binding stays old, not relabelled.

- [ ] Step2 RED for actual legacy WB bind inversion: synthetic token/user/account and mocked seller adapter, two sessions deliberately order publisher userSHARE/account lock versus bind's acquisition. Use deterministic events and lock observation scoped only own test DB. Expect old account→joineduser lock path inconsistent with common order; never call real provider or leak token.

- [ ] Step3 implement minimal paired envelope and WB common order.

```python
# Locking branch only, after metadata-only owner locator:
user_query = select(LkUserRow.user_id, LkUserRow.organization_id, LkUserRow.is_active)
user_query = user_query.where(LkUserRow.user_id == expected_owner).with_for_update(read=True)
# After user validation: account FOR UPDATE; token query uses targeted lock:
token_query = token_query.with_for_update(of=LkUserWbTokenRow)
```

Refresh owner/org/active relationship after waits; token locator reassignment fails closed. Preserve seller fetch outside transaction and exact-token/current-binding comparisons. Explicit safe column projections where secret unnecessary; no user password/refresh hash reads. Resolved envelope delegates explicit secret only, refuses generic serialization/copy. Do not change existing safe error contract.

- [ ] Step4 GREEN focused new tests + existing crypto/store/WB resolver tests under offline sandbox. RealPG run uses own plan's ignored Unix-only profile and existing tracked disposable helpers. Compile changed paths, diff-check, record exact SQL/state/cleanup results and limits; no Ruff claim unless actually available in own environment.

```sh
.venv/bin/python -m pytest -q tests/test_marketplace_credential_fetch.py tests/test_marketplace_credential_fetch_postgres.py tests/test_marketplace_credential_crypto.py tests/test_marketplace_credential_store.py
.venv/bin/python -m compileall -q app/platform/integrations/credential_store.py app/platform/integrations/wb_credentials.py tests/test_marketplace_credential_fetch.py tests/test_marketplace_credential_fetch_postgres.py
git diff --check
```

- [ ] Step5 bounded commit `feat: capture marketplace fetch authority atomically`; handoff exact types/SQL semantics, no consumer cutover, independent controller review.

## Task 2: Enforce same-transaction publication authorization through commit

Finalizer acceptance amendment after independent I1: guard must verify it is the
last effective before_commit callback at entry (class and instance order); reject
later callbacks, including claimed read-only observers. Flush once and fail closed
if new/dirty/deleted work remains, then validate and again reject pending ORM work.
RealPG RED→GREEN must cover after_flush_postexec leaving a cached user inactive
mutation and a later before_commit callback mutation, zero committed proof/audit,
plus a trusted callback registered before guard as positive control. Do not mutate
dispatch listeners or implement unbounded flush draining. Caller cannot bypass
the Session protocol with raw COMMIT/private or SQL-executing connection events.

**Files:**
- Create `backend/app/platform/integrations/publication_guard.py`.
- Create `backend/tests/test_publication_guard.py`.
- Create `backend/tests/test_publication_guard_postgres.py`.
- Create `backend/docs/superpowers/reports/2026-09-09-t1-publication-guard-handoff.md`.
- `backend/app/infra/db.py` only if controller approves a concrete needed context assertion; default implement self-contained Session listeners in guard module.

**Consumes:** Task1 `CredentialFetchBinding`/`ResolvedCredentialForFetch`, current user/session/membership/account and credential/token metadata models, existing permission profile function. No own-session store call inside guard. Read exact spec interface/validation/error allowlists verbatim.
**Produces:** `UserSessionPrincipal`, `ExpectedAccountBinding`, `ExpectedCredential`, `ExpectedIngestionToken`, `PublicationGuardError`, `acquire_publication_guard(session, *, principal, required_permissions, accounts, authorities)->PublicationGuard`, `.revalidate_before_write()->datetime`.

- [ ] Step1 focused RED for all missing API/contracts: textual IDs, positive canonical ints/bool rejection, UUID/kind/schema/expiry/ref strictness, duplicate/contradictory bindings, actual membership union, malformed scope fail-closed, no stale ActorContext authority. Empty authority only for explicitly credential-independent trusted calls; no principal-less or worker paths.

```python
with Session(runtime_engine) as session, session.begin():
    guard = acquire_publication_guard(session, principal=principal,
        required_permissions=frozenset({'sync:run'}), accounts=(binding,),
        authorities=(expected_credential,))
    lock_domain_run(session)
    guard.revalidate_before_write()
    write_synthetic_publication(session)
    # Context commit must perform final check, even with no caller recheck.
```

Reject no transaction, nested transaction, wrong dialect/isolation, pending mutations, tenant drift, second guard and reuse after root end. Assert no secret columns/old resolver/provider calls. Error/code/repr canaries include unknown attacker input. Test Session hooks affect only guarded root transaction and are cleaned up.

- [ ] Step2 actual PostgreSQL RED for both lock winner orders across user deactivate, membership revoke/permission/scope change, session revoke, account disconnect/rebind/ref invalidation, credential revoke/replacement/reencrypt and token revoke/rotation. Use explicit metadata fixture and actual owned writer where available, no domain implementation. Persist one synthetic publication/audit proof atomically; updater first denies all writes, publisher first holds updater until commit. Test stale identity map and exact old UUID/generation rejection, not latest-row substitution.

- [ ] Step3 implement clean caller transaction guard, explicit projections/locks, DB-time validation and before_commit finalizer.

```python
now = session.scalar(select(func.clock_timestamp()))
user = session.execute(user_metadata_query.with_for_update(read=True)).one_or_none()
# Revalidate every exact locked metadata expectation; no ORM cached attributes.
```

Install session-specific transaction-bound state/listeners; before_commit flush then revalidate fresh metadata/time/context, preventing omitted final-check commit. Do not mutate listener collection during dispatch or leak stale state into reused pooled/session transactions. Caller handles rollback; safe typed failures never reflect SQL/secret values. Reject nested/tenant manipulation while guard is active. Same-transaction metadata mutation is rechecked after flush.

- [ ] Step4 prove expiry during account/metadata/domain lock waits and during flush before commit with DB clock_timestamp versus transaction now. Barriers/events, not uncontrolled sleeps; local elapsed time only if actually testing expiry. Failure rolls back all synthetic rows, releases locks; no exception-to-success fallback. Test credential-independent persisted read with cabinet:read and revoked/absent credential still succeeds if live user/session/account permits it. Wrong tenant/account/provider denied.

- [ ] Step5 GREEN all guard/fetch + original credential suites, actualPG, compile and diffcheck. Record every command/env/count/exit and allocated-resource cleanup; exactly identify unproven worker/delegation/bearer-only/access-token-exp/ABA history semantics. No claim that validation-time expiry freezes wall clock until commit.

```sh
.venv/bin/python -m pytest -q tests/test_publication_guard.py tests/test_publication_guard_postgres.py tests/test_marketplace_credential_fetch.py tests/test_marketplace_credential_fetch_postgres.py tests/test_marketplace_credential_crypto.py tests/test_marketplace_credential_store.py
.venv/bin/python -m compileall -q app/platform/integrations/publication_guard.py tests/test_publication_guard.py tests/test_publication_guard_postgres.py
git diff --check
```

- [ ] Step6 bounded commit `feat: fence user publication with live account authority`; report exact imports/arguments/safeerrors/user-onlypermissions, consumer obligation to guard idempotent replay return and final writes; T3 wires its own service. Independent controller review and verification before ready handoff.

## Task 3: Reject unsupported physical transaction modes

**Files:**
- Modify `backend/app/platform/integrations/publication_guard.py` only physical transaction admission/context checks.
- Modify `backend/tests/test_publication_guard.py` only focused admission/safe-error controls.
- Modify `backend/tests/test_publication_guard_postgres.py` only actual driver/SAVEPOINT regression cases.
- Modify `backend/docs/superpowers/reports/2026-09-09-t1-publication-guard-handoff.md` append exact follow-up evidence and clarify supported protocol.

**Consumes:** existing accepted Task2 guard0900f8e+4860c53; the spec's physical
transaction follow-up. Account-helper implementation is read-only corroboration,
not a helper to invoke here (guard must not require an account selection context).
**Produces:** same public API/errors/lock/finalizer contract, with actual invalid
driver modes rejected before SQL and again through commit.

- [ ] Step1 write actual PostgreSQL RED for both autocommit entry configurations:

```python
autocommit_engine = engine.execution_options(isolation_level="AUTOCOMMIT")
with Session(autocommit_engine) as session, session.begin():
    with pytest.raises(PublicationGuardError, match="publication_context_invalid"):
        acquire_publication_guard(session, **trusted_synthetic_arguments)
```

Also test Engine construction isolation_level and bound Connection execution_options
paths without altering an existing engine/server globally. Existing fixture must
allocate only own disposable resources. Distinguish SQLAlchemy logical root from
actual driver autocommit; capture old guard acceptance as RED, not merely missing
test import. Parameter names above denote the existing test fixture's trusted
principal/accounts, not a new production function.

- [ ] Step2 RED initial Connection.begin_nested before acquisition, and active
Connection SAVEPOINT opened after acquisition before explicit revalidation or
Session.commit. Include ordinary real Session READ COMMITTED positive control.

Also reproduce the external-root join lifetime mismatch using a Connection whose
owner already called begin(), with Session default conditional_savepoint and
explicit rollback_only. Require Engine-bound Sessions using public get_bind type;
reject all Connection-bound Sessions before authority/context SQL, even join modes
that might transfer commit ownership. Negative tests leave external settings/data
and its root untouched; normal Engine-root lifecycle stays positive. No private
transaction maps or forced external commit/rollback.
Denied commit rolls back every synthetic publication/audit row. No application
context or authority query should execute on initial invalid admission; ordinary
driver connection initialization is not an application authority query.
- [ ] Step3 add minimal shared private admission check used at acquisition and
_context, preserving all old checks/ordering and safe errors:

```python
connection = session.connection()
root = connection.get_transaction()
if (root is None or not root.is_active or connection.in_nested_transaction()
        or getattr(connection.connection.dbapi_connection, "autocommit", None) is not False):
    raise PublicationGuardError("publication_context_invalid")
```

Actual SQLAlchemy inspection errors are sanitized by existing callers. Do not
repair mode, open a new transaction, commit/rollback caller work, install global
hooks or alter account context. Unknown driver-state failure is deliberate.
Existing unit doubles must faithfully represent an ordinary physical transaction;
new positive evidence must remain actual PostgreSQL, not double-only.
- [ ] Step4 GREEN focused regressions then all existing guard/fetch and account
context tests once:

```sh
.venv/bin/python -m pytest -q -s --tb=short tests/test_publication_guard.py tests/test_publication_guard_postgres.py tests/test_marketplace_credential_fetch.py tests/test_marketplace_credential_fetch_postgres.py tests/test_marketplace_account_context.py tests/test_marketplace_account_context_postgres.py
.venv/bin/python -m compileall -q app/platform/integrations/publication_guard.py tests/test_publication_guard.py tests/test_publication_guard_postgres.py
git diff --check
```

Use scrubbed own .venv and this plan's secret/IP-denying UnixPG sandbox; lint-only
approved Ruff interpreter. No new baseline/skip, external driver install, service
or credentials. Record exact natural exit, counts/cleanup, retained warnings,
redaction and invalid-mode failure before SQL. No claim that checks detect raw
SAVEPOINTs opened/closed entirely between checks or sandbox arbitrary caller SQL.
- [ ] Step5 self-review and commit `fix: require physical transactions for publication guard`;
independent task review before ready follow-up delivery directly to T2–T4.
