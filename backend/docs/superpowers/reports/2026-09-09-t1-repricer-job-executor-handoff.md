# T1 immutable repricer job / receipt / executor handoff

Status: **IMPLEMENTED / UNVERIFIED**, source only. No tests, PostgreSQL/Redis,
imports, compile/lint checks, reviewer, provider call, operational grant/role,
keyring load, runtime flag, push or deployment ran in this implementation task.
This is not a provider-complete, activated or exactly-once worker.

First source commit: `fdf99d9c710b90c4d81a976271d5e406042c2138`
(`feat: add repricer job and upload receipt storage`). Separate schema follow-ups:
`d1e90a9233d5b46d548e78acff20abda271b64b6` (`fix: fence repricer job creation authority`)
and `40d178b4dbd38ad54ce427d756b864988f2ae2cc` (`fix: preserve repricer creation lock order`).
This handoff belongs to the runtime source commit (`feat: add explicit repricer executor authority`);
its exact resulting SHA is recorded after commit in
`.superpowers/sdd/2026-09-09-repricer-job-executor/task-1-report.md`.
The intervening controller documentation commit does not replace either source
commit. Schema follow-ups remain separate as required by the user's latest
source-sequencing instruction. No amend or rebase was used.

## Accepted inputs and corrections

Full source inputs: T2 worker amendment `3c99f1eddb4750faa2be0ce79806c2d6f26cbc7e`,
receipt request `470d41076f98009cd9fca60311092e5b3c648ffe`, and the complete
`2026-09-09-repricer-job-executor-design.md` specification. Existing0066 owns all
domain state, request canonicalization, CAS, attempt and audit transitions.
T2 participant signatures were inspected from `bd3bb60a0baadc8d7683c8efd8bf4eff444c9643`.
They are not imported, copied or registered by this package.

Source predecessor was sole0072; migration is exactly
`backend/alembic/versions/20260909_0073_repricer_job_executor.py`, revision
`20260909_0073`, down revision `20260909_0072`.

Controller explicitly resolved two missing predecessor details before runtime:

- Added a tenth allowed path, `app/platform/integrations/publication_guard.py`,
  for private exact-class repricer registration and closing installation.
  The former extension accepted only `UserOrdersPublicationHandle`.
- The canonical account `FOR UPDATE` lock serializes immutable job lookup.
  Job rows use plain scoped SELECT under that lock, without a job UPDATE grant
  or redundant advisory lock. Real approval then attempt locks follow.

Separate schema follow-ups add final deferred new-job principal/session/deadline
equality to0073, the two extra0066 columns described below, and an active credential
metadata lock before the approval lock in the direct job INSERT trigger. The
original schema commit remains unchanged; consume the complete listed chain.

## Exact imports and callable boundary

From `app.platform.integrations.repricer_job_contract`:

```python
RepricerJobLocator(organization_id, marketplace_account_id, job_id)
RepricerJobLocator.from_queue(payload)
RepricerJobLocator.queue_payload()
RepricerApprovalBinding(organization_id, marketplace_account_id, approval_row_id,
                       approval_id, action_key, request_checksum, canonical_request_bytes)
RepricerAuthorityPolicy(reference, version)
RepricerAuthorityMetadata(locator, external_account_id, credential_ref, credential_id,
                         generation, payload_schema_version, credential_expires_at,
                         authority_expires_at, policy)
RepricerExpectedState(approval, approval_version, attempt_id=None,
                     attempt_version=None, dispatch_key=None, claim_version=None)
RepricerUploadObservation(wb_upload_id, observed_at)
RepricerJobView
RepricerReceiptView
RepricerReadback
CommittedRepricerDispatch
RepricerJobError
RepricerReadbackRequired
InitiationAction
ClosingAction
```

All metadata values have redacted repr and reject pickle. Approval/claim versions
accept only nonnegative exact Python int or finite integral Decimal and normalize
without a float/BIGINT conversion. Attempt versions are integer0..2. IDs are UUID4,
scope is positive INT4, hashes are lower-case64 hex, WB upload identity is positive
ASCII decimal TEXT without a digit limit. Original response validation belongs to
T2: this constructor intentionally rejects int/float/bool/coercible strings.
`RepricerApprovalBinding` preserves the exact canonical bytes and verifies SHA256;
the existing0066 row remains the canonical action/request owner.

From `app.platform.integrations.worker_identity`:

```python
ExecutorRoleIdentity(marketplace_executor_role, marketplace_api_runtime_role)
verify_executor_login(session, *, identity: ExecutorRoleIdentity) -> None
ExecutorIdentityDenied  # fixed message executor_identity_denied
```

From `app.platform.integrations.repricer_job_executor`:

```python
RepricerJobCommands(*, session_factory)
commands.create(*, authenticated_actor, approval, policy, authority_expires_at)
commands.readback(*, authenticated_actor, approval)

RepricerJobExecutor(*, executor_session_factory, identity, policy, credential_resolver)
executor.claim(*, locator, expected, participant)
executor.reserve(*, locator, expected, participant)
executor.resolve_fetch(*, locator, expected)
executor.mark_dispatch(*, locator, expected, resolved_credential, participant)
executor.before_provider_io(*, dispatch, resolved_credential)
executor.close_outcome(*, locator, expected, participant)
executor.publish_receipt(*, locator, expected, observation)
executor.readback(*, locator)
```

Create/readback return `RepricerJobView`; claim/reserve/close/readback return
`RepricerReadback`; receipt returns `RepricerReceiptView`; resolve_fetch returns
the existing `ResolvedCredentialForFetch` redacted secret+binding pair;
mark_dispatch returns `CommittedRepricerDispatch` only after physical commit;
before_provider_io returns None after a fresh successful root commits.

Queue is exactly org/account/job via locator: no permission, actor, session,
policy, deadline, capability, attempt, credential or callback serialization.
Participant is a trusted in-process `participant(session, handle)` callable,
never queue/request content. It must invoke the existing T2 `ApprovalTransaction`
in the supplied Session and derive its actor from the initiation handle only.
Its return value is ignored; T1 reloads the resulting durable row state before
the final guard. T1 does not implement any0066 UPDATE/CAS transition itself.

`RepricerInitiationHandle` exposes `.action`, `.expected`,
`.initiator_membership_id`, `require_participation(session, action)` and
`revalidate_before_write()`. Allowed fixed classes are CLAIM, RESERVE, DISPATCH,
FETCH, BEFORE_PROVIDER_IO. Preconditions respectively require pending/no attempt,
applying/no attempt, applying/reserved version0, applying/reserved version0, and
applying/dispatched version1 with marker. No latest version is substituted for
the caller's exact expected state. Claim/reserve/dispatch seal only their allowed
poststate, exact frozen origin, claimant, request/action and lifetime attempt.

`RepricerClosingHandle` is a separate class with `.action`, `.expected`,
`require_participation(session, action)` and `revalidate_before_write()`.
It has no initiating-principal/fetch/provider methods. ClosingAction is OUTCOME,
RECEIPT, READBACK; only OUTCOME can admit a trusted T2 participant. Allowed domain
close starts with applying plus reserved/dispatched and ends with the existing
terminal applied/failed/ambiguous pair. Reserved may only close to local failed;
the wrapper never invents a provider rejection or any outcome payload. Known
provider rejection remains a validated T2 result. Terminal re-entry conflicts;
readback is the duplicate-delivery ACK path. Ambiguous can only gain a receipt,
never reopen to applied or spawn another attempt.

## Physical roots, locks and expired authority

Every public command owns a new clean Session and Engine-bound PostgreSQL physical
READ COMMITTED root, excluding nested transactions, joined Connection roots and
AUTOCOMMIT. Factories returning already-active foreign sessions are rejected without
rolling back or closing that foreign transaction. Errors poison/roll back owned
roots. No usable external send authority is returned before physical commit.

Creation resolves membership freshly from authenticated `ActorContext`; actor's
permission claims are not reused. It acquires the existing live fixed `price:send`
guard in user→membership→session→account→credential order. Then it looks for exact
immutable origin/key/policy/deadline/account/credential replay before the pending
check for new rows. New creation atomically stores job+authority+audit with database
created time, explicit future deadline no later than originating session expiry.
Historical exact replay may succeed after terminal status or stored job deadline,
provided fresh authenticated live creation checks still pass. It never grants send.

Initiation verifies actual executor login, performs nonlocking scoped immutable
origin lookup, acquires the same live user guard, then immutable job lookup under
the account lock→approval FOR UPDATE→attempt FOR UPDATE. Current credential ID,
generation/schema, exact external account/ref, session/permission/account scope,
expected versions and claimant are checked after waits and at final commit.
The configured trusted policy reference/version must match captured policy.
No refreshed session or replacement credential extends the captured job deadline.

Closing/receipt/readback verify executor login then lock account→immutable job
lookup→approval→attempt. They acquire no later user/member/session/credential locks
and do not resurrect revoked user authority. Closing can preserve owned evidence
after originating user/session/credential revocation or authority expiry.

Private guard extension is exact `_register_repricer_fence(guard, fence)` for
`RepricerInitiationHandle`, exclusive with Orders; and exact
`_install_repricer_closing_guard(session, handle)` for `RepricerClosingHandle`.
The original final listener remains last, flushes pending ORM work, rejects
post-flush dirtiness, validates live auth and repricer state, rejects nested/ended
roots and poisons failure. The closing guard occupies the same private root state
without becoming a UserSessionPrincipal or public generic principal type.

T2 sequence: committed job/approval → guarded claim → guarded reserve → fetch the
exact paired credential outside publication locks → guarded marker commit →
consume in-process dispatch result with fresh before_provider_io → immediate
trusted T2 POST → new-root receipt/outcome. `CommittedRepricerDispatch` is consumed
once under an in-process lock before the final check; a failed check/crash/lost
result must not regenerate it from queue/readback. This is conservative process
coordination, not remote idempotency or durable external exactly-once.

Receipt's new root observes an already committed dispatch marker before any
receipt write. This wrapper accepts no transition participant, performs no
approval/attempt/cache write and retains marker identity even after version2
ambiguous. Same attempt+ID/binding returns the first row/audit and original times;
different ID is conflict. A new observation time on replay is ignored. Observed
adapter time and DB recorded time are different domains; no skew threshold or
ordering between those clocks is assumed. No receipt means unknown, not failed.

Any uncertain physical commit raises `RepricerReadbackRequired` with fixed code
READBACK_REQUIRED and the scoped locator, or exact approval binding for uncertain
creation. Use the matching readback method; never immediately make another POST.
A crash before receipt commit can still lose a known upload ID; the existing
marker then forces ambiguity/no resend. There is still a distributed revocation
gap between the final local check and provider I/O.

## Five relations, helpers and privileges

The exact DDL is in0073. Job fields are UUID4 job/approval/audit IDs, scope/provider,
action_key/request_checksum, fixed operation, immutable initiating user/session/
membership and created_at. One job per full-scope approval. Authority fields are
scope/job/provider, exact external account/ref, credential_id/kind/generation/
schema1/NULL expiry, explicit authority_expires_at, policy_reference/version.
Job audit has scope/job/approval/audit ID, fixed event/actor, initiating membership
and occurred_at=created_at.

Receipt fields are scope/provider/job/approval/attempt/receipt/audit IDs,
action/checksum/dispatch keys, exact wb_upload_id, observed_at and recorded_at.
Receipt audit contains full ownership references, fixed receipt event and
repricer_worker actor, NULL actor_membership_id, occurred_at=recorded_at.
No provider response/status/error blob or account-wide upload-ID uniqueness exists.

Actual restrictive FKs include existing account/org/WB,0066 approval and scoped
approval/attempt parents; new job→authority/job-audit reciprocal deferred witnesses;
receipt→receipt-audit reciprocal deferred witness; child audits→full job/approval/
attempt/receipt ownership. New child lookup indexes support principal, credential,
approval and attempt references. Deferred validation rejects wrong keys/claimant,
orphan/missing/ghost witnesses and unequal timestamps. New creation additionally
rechecks exact user/member/session ownership and deadline at commit.

Every new table has FORCE RLS requiring BOTH app.organization_id and
app.marketplace_account_id through existing repricer_context_id(text), immutable
INSERT-only triggers, and a TRUNCATE denial trigger. New pure helpers are
repricing_job_uuid(uuid), repricing_job_time(timestamptz),
repricing_job_text(text,integer); new trigger helpers are repricing_job_guard()
and repricing_job_witness(). Trigger functions have no direct runtime/PUBLIC
EXECUTE grant. Migration strips default nonowner grants on new objects only.
Empty-only downgrade locks exactly all five tables, sets row_security=off so
hidden history cannot be ignored, refuses any rows, and explicitly drops reciprocal
constraints/triggers/functions/tables without CASCADE or changing old history.

API script grants only new job/authority/job-audit SELECT+INSERT and receipt/audit
SELECT. No receipt INSERT, job UPDATE, history DELETE/TRUNCATE or executor identity
is conferred to the API by the extension. Existing unrelated script logic is unchanged.

Inert repricer-executor-grants.sql takes already provisioned executor_role and
api_runtime_role variables. It refuses missing/identical/privileged executor roles,
role-membership topology, protected ownership, absent forced RLS and existing broad
metadata/new-job writes. It does not CREATE/ALTER ROLE, load credentials, change
default privileges or touch unrelated ACLs. Required exact column/function names
must exist; a mismatch aborts the grant transaction rather than broadening it.

Metadata SELECT is explicit: user identity/org/active; membership identity/org/user/
active/role/permissions/scope/accounts; session identity/user/revoked/expiry; account
identity/org/provider/external/ref/status. Credential SELECT lists all columns of
the existing encrypted ORM row, including algorithm/key-version/AAD/schema/nonce/
ciphertext/generation/expiry/revocation/timestamps. It does not grant plaintext
source, password/refresh-token hashes, ingestion verifiers or keyring SELECT.

Timestamp UPDATE is an explicit limited capability for required row locks:
lk_users.updated_at, iam_memberships.updated_at, lk_sessions.last_seen_at,
marketplace_accounts.updated_at, marketplace_account_credentials.updated_at.
Infrastructure must approve this capability and isolated connection custody.
It grants no metadata identity/status/permission/expiry/generation/secret UPDATE.

Executor SELECT covers0066 and new evidence; INSERT covers0066 attempt/audit and
new receipt/audit, never job/origin creation.0066 approval UPDATE columns are
status,version,updated_at,claimed_by_membership_id,decided_by_membership_id,
reason_code,claimed_audit_id,safe_error_code,wb_upload_id,result_code,outcome_audit_id.
T2's unchanged `_mutable` always writes decided_by_membership_id/reason_code as
NULL for these operations, requiring those two columns; they are a limited domain
decision capability, not IAM identity UPDATE. The action fence excludes reject/block.
Attempt UPDATE columns are status,version,updated_at,dispatch_at,finished_at,
safe_error_code,wb_upload_id,result_code,dispatched_audit_id,outcome_audit_id.
Only the explicit pure0066 helpers needed by constraints and the three new pure
helpers receive EXECUTE. No trigger function grants or whole metadata-table grants.

## Bootstrap and remaining activation blockers

Settings `marketplace_executor_role` / `marketplace_api_runtime_role` default None;
environment names are `VELLA_MARKETPLACE_EXECUTOR_ROLE` /
`VELLA_MARKETPLACE_API_RUNTIME_ROLE`. Parsing rejects malformed names with a fixed
error. Actual session_user=current_user must equal the configured executor.
LOGIN/NOSUPERUSER/NOCREATEDB/NOCREATEROLE/NOINHERIT/NOREPLICATION/NOBYPASSRLS,
protected ownership and API membership isolation are verified on the executor
transaction's actual connection. GUC, SET ROLE, queue input and audit enum fail
to establish this identity. Missing configuration/DB errors deny safely.

Production must provide a dedicated executor factory, approved two-role mapping,
reviewed finite policy/deadline, isolated connection custody and approved inert
grants. No operational role or policy was created here. An actual trusted worker
compromise remains a runtime/key-access compromise; there is no cryptographic
executor attestation and DB administrators are outside this row-level model.

The existing paired credential store opens its own get_session_factory() session.
It has no injected Session parameter. Controller accepted required explicit
credential_resolver injection with NO default/API fallback. Bootstrap must bind
that existing store factory to the dedicated executor pool/keyring; this wrapper's
identity verifier cannot attest the resolver's separately opened internal session.
Final integration must observe its actual SQL login. Missing that composition is
an activation blocker. No alternate query/decrypt service was created.

T2 must still compose its unchanged domain participant, preserve the explicit
physical marker-before-POST sequence and implement lossless ORIGINAL POST response
projection before optional polling. Existing `_as_int`/PriceApplyResponse is not
that proof. Dry-run/local_mock/history-query argument cannot create a trustworthy
receipt; no provider transport is registered by this package. Provider-history
GET/credential reselection requires a separate accepted live-read contract.

Safe service errors are exactly REPRICER_CONTRACT_INVALID, REPRICER_ACCESS_DENIED,
REPRICER_NOT_FOUND, REPRICER_CONFLICT, REPRICER_AUTHORITY_DENIED,
REPRICER_FENCE_INVALID, REPRICER_PERSISTENCE_FAILED and READBACK_REQUIRED.
Underlying SQL/provider/credential exception messages and role names are not
returned in service errors.

## Deferred final verification

Run only at the user's later final verification stage in disposable PostgreSQL:

- Actual dedicated login versus SET ROLE/GUC/API/shared identity, missing roles/
  policy/privileges, protected ownership and membership topology; safe errors/repr.
- Forced dual-context RLS and crossorg/same-org-other-account denial; all immutable
  UPDATE/DELETE/TRUNCATE paths; reciprocal graph, wrong origin/job/approval/attempt/
  claimant/action/hash/dispatch, ghost audit and atomic rollback.
- New pending/v1 job only; concurrent exact/conflicting creation; historical exact
  replay after terminal state; job creator A versus manual claimant B.
- Deadline with actual DB clock, wait expiry, source-session shortening/refresh,
  session/member/permission/account/credential revocation and generation change;
  no replacement credentials; zero fake provider calls when any check fails.
- Actual separate physical marker, send-check and receipt roots; no combined
  marker/POST/receipt transaction; failure/uncertain COMMIT and scoped durable
  readback; duplicate terminal ACK never sends; consumed marker not regenerated.
- Two-session same ID race gives one receipt/audit; different ID conflicts; exact
  large decimal ID; floats/bools/padded/exponent/zero rejected; clock skew preserved;
  late ambiguous receipt and restart; no price-cache/approval outcome mutation.
- Closing after originating revocation without lower-order user locks; no closing
  handle usable for initiation/fetch/GET; unknown result never becomes rejection.
- Exact synthetic grants, including narrow timestamp locks and existing resolver
  login/query shape; no unrelated old ACL changes, plaintext/verifier/key grants.
- Session/Engine/physical-root isolation, nested/autocommit/ended-root poisoning,
  last-listener invariant, flush/after-flush dirtiness, caught callback errors,
  old Orders and existing publication-guard compatibility.
- Empty-only downgrade including RLS-hidden history, explicit dependency drops,
  old0066/0071/0072 compatibility and full T2 worker/adapter integration.

None of these cases ran here. Final security/concurrency/RLS/migration/guard/
transport verification and operational bootstrap remain required.
