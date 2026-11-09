# Repricer immutable job, receipt and explicit executor boundary

Local implementation contract. Inputs read in full: T2 job/authority amendment
`3c99f1eddb4750faa2be0ce79806c2d6f26cbc7e` and receipt request
`470d41076f98009cd9fca60311092e5b3c648ffe`. Their exact documents are
`backend/docs/superpowers/reports/2026-09-09-t2-repricer-worker-authority-amendment.md`
and `backend/docs/superpowers/reports/2026-09-09-t2-provider-upload-receipt-schema-request.md`.
T2 confirms no additional accepted executor identity contract exists and accepts
the safety direction below, not a claim of code readiness.

## Scope and trust

No Orders lease/retry policy applies to price POST. Existing0066 owns the single
lifetime attempt, immutable action/request/dispatch keys, approval/attempt versions,
terminal outcomes and audit. This extension never reopens ambiguous, retries POST,
changes the provider parser or manufactures proof from a history query argument.
No production, providers, existing keys, database roles, deployments or flags are
used/enabled. Production executor role mapping and grants require infrastructure
owner configuration; code defaults deny missing configuration.

Distinguish authenticated creation from execution:

- Creation uses real authenticated ActorContext, freshly resolved membership and
  existing live user/session/account/credential guard with fixed `price:send`.
- A trusted executor is a dedicated actual PostgreSQL login identity. Verify
  `session_user=current_user=explicit configured executor role`, role properties
  LOGIN/NOSUPERUSER/NOCREATEDB/NOCREATEROLE/NOINHERIT/NOREPLICATION/NOBYPASSRLS,
  absence of ownership of protected relations, and configured API role is distinct
  and cannot assume that executor role through role membership. A GUC, SET ROLE,
  job ID, queue field, audit enum or arbitrary Python actor is not this proof.
- Initiation additionally reloads the job's immutable user/session origin and
  exact credential/account metadata, acquires the live `price:send` guard, then
  exact job/approval/attempt locks and CAS. No new session or refreshed generation.
- Closing requires executor identity and exact immutable job→approval→attempt→
  dispatch binding, but not that a revoked initiating user becomes valid again.
  It may append receipt or record an existing0066 allowed terminal outcome only.
  It cannot create/claim/reserve/mark dispatch, acquire fetch credentials, publish
  Catalog prices or convert uncertainty to rejection. Closure never grants a POST.

The concrete login verifier belongs to one shared
`app.platform.integrations.worker_identity` module so subsequent Review workers
do not copy authentication checks. It grants no domain action by itself.
Settings `marketplace_executor_role` and `marketplace_api_runtime_role` default
to None; their environment variable names use the corresponding `VELLA_` prefix.
The executor identity is configured in server code/settings, not supplied by an
HTTP request or queue. Connection secrets remain outside repository/logs/config
reports. Role names are nonsecret configuration but never reflected in safe errors.
The code does not create a usable executor connection from the API pool as fallback.
Deployment must explicitly provide the executor connection factory and two distinct
role names. Missing configuration or DB failure returns a fixed safe denial.

## Additive physical model

Five new relations, all organization/account/provider-owned and forced dual-context
RLS, restrictive scoped FKs, immutable rows, no delete/truncate, UUID4 identities:

1. `wb_repricing_jobs`: exact columns and one-job-per-approval invariant from3c99f1e.
   No job lifecycle/version/retry/lease: those facts already belong to0066.
2. `wb_repricing_job_authorities`: one exact immutable authority per scoped job,
   expected external account/ref, WB `wb_api` credential ID/generation/schema1,
   credential expiry NULL, finite explicit authority_expires_at, policy_reference
   and positive policy_version. This is metadata, never ciphertext/token/verifier.
   Deadline is captured under live guard and no later than originating login expiry;
   source/session refresh cannot extend it. Production policy is never defaulted.
3. `wb_repricing_job_audit`: fixed `repricer_job.created`, membership actor equals
   initiator, exact scoped job/approval, occurred_at=job.created_at. Reciprocal
   deferred witness; no ghost audit, orphan job or missing authority at commit.
4. `wb_repricing_upload_receipts`: exact470d410 fields plus required scoped job_id
   and created_audit_id. UNIQUE(org,account,attempt_id), not account/upload ID.
5. `wb_repricing_upload_receipt_audit`: fixed `provider_upload.receipt_observed`,
   actor_kind=`repricer_worker`, membership NULL, exact scoped job/approval/attempt/
   receipt and occurred_at=receipt.recorded_at. Reciprocal deferred creation witness.

Enforce actual parent identities and exact action/checksum/dispatch/claimant equality.
Do not add old-parent indexes or change0066. Parent principal consistency can use
existing FKs plus immediate/deferred new-object checks, not guessed nonexistent FKs.
Job creation requires a v1 pending approval only for a NEW job; exact authorized
existing job replay precedes that check and remains immutable in terminal states.
Job initiator must match the attempt claimant whenever the job drives execution or
receives evidence. Another user claiming the approval does not transfer the job.

All timestamps finite and representable by Python year1..9999. Upload ID is exact
positive ASCII decimal TEXT, no invented max/BIGINT/float coercion. Client observed_at
is not compared with DB recorded_at or dispatch time: distinct clock domains, no
invented skew tolerance. Replay same ID/binding retains original observed/recorded
times and audit; changed ID conflicts, never overwrite or resend.

Receipt SQL requires existing dispatch marker and exact keys even after terminal
ambiguous, without modifying approval/attempt. The service additionally proves the
marker was already committed by loading it in a separate clean physical root;
FKs and flush alone cannot prove that. No raw provider response/status/error fields.

## Runtime privilege separation

Migration narrows only new-object ACLs and exact helper signatures. API runtime
may SELECT/INSERT job, authority and job-audit through trusted creation, SELECT
receipt/audit, but cannot INSERT receipt or mutate history. No API executor identity.
Pure helper grants only where needed; no callable trigger functions or PUBLIC rights.

Supply a separate inert `backend/ops/repricer-executor-grants.sql` artifact, never
execute it in this implementation. It accepts already provisioned distinct role
names, verifies role attributes/ownership/membership, refuses unsuitable topology,
then transactionally grants only explicit required privileges, no role creation,
password, default privileges, global ACL changes or schema ownership. Existing API
and unrelated workloads are untouched. It grants no job/authority/job-audit INSERT.
Executor receipt INSERT is explicit;0066 mutation privileges are limited to columns
the existing participant changes. Necessary metadata row locks use UPDATE of a
non-authority timestamp column only where PostgreSQL requires UPDATE permission:
lk_users.updated_at, iam_memberships.updated_at, lk_sessions.last_seen_at,
marketplace_accounts.updated_at, marketplace_account_credentials.updated_at.
These grants do not include identity, status, permissions, expiry, generation,
credential payload or secret columns for UPDATE. If a required lock cannot use this narrow
existing column, report the exact missing privilege rather than grant a whole table.
Metadata SELECT is column-specific. The initiation worker legitimately resolves
credentials through the existing credential store; grant SELECT of that encrypted
row's exact required identity/AEAD/expiry columns (including ciphertext/nonce), not
plaintext source tables or ingestion verifiers. The store alone decrypts using the
existing externally mounted keyring and returns its redacted wrapper. No separate
credential service/secret broker or API-login fallback is invented. Closing methods
never invoke the resolver; schema/migration processes have no keyring. This permits
the trusted worker's normal credential-read capability, not a claim of safety after
worker/key compromise. Preserve actual locked metadata and resolver query shapes.

The timestamp UPDATE lock allowance is an explicit limited capability, not a claim
of read-only SQL. Infrastructure owner must approve that grant and isolated
connection custody before activation. Direct worker compromise remains a trusted
runtime compromise; role separation primarily prevents API/broker input alone from
claiming post-revocation closing authority. No cryptographic executor attestation is
claimed. Runtime database administrators remain outside the row-level threat model.

## Service contract

Implementation-time interface resolution: the preceding guard has only an exact
Orders fence. Add private `_register_repricer_fence(guard, fence)` for the exact
initiation-handle class, same physical root, once before finalization and mutually
exclusive with an Orders fence. Existing final listener checks it after flush and
live user authorization; listener-last, no subsequent pending flush, nested/root
end/autocommit and caught-failure poisoning remain unchanged.
Closing is a distinct exact Repricer closing class, not a UserSessionPrincipal or
PublicationGuard subtype. Private `_install_repricer_closing_guard(session, handle)`
checks exact type and clean Engine-bound PostgreSQL RC physical root and tenant
context, occupies the same mutually exclusive guard state, and reuses the existing
listener installation. Its context/revalidation implements actual executor login,
scope, prior marker and permitted expected state, not live creator resurrection.
There is no public callback bus, duck-typed authentication or generic principal registry.

Immutable job lookup is plain SELECT under the canonical account FOR UPDATE lock;
the latter serializes all legitimate job operations. Then lock actual approval and
attempt rows. Do not grant job UPDATE or invent a mutable timestamp/advisory lock
solely to satisfy PostgreSQL FOR UPDATE privilege requirements. Job immutability
and FK/unique constraints remain database-enforced even for direct concurrent SQL.
References below to a job lock mean this explicit account-serialized immutable
lookup, not a falsely claimed row lock.

Provide typed/redacted immutable job and receipt metadata, allowlisted errors and
root-owned creation/readback/receipt operations. Callable names/signatures published
in implementation handoff, not guessed by T2 before source exists.

Creation: authenticated user→membership→session→account→credential locks; exact
replay; pending-only new-row check; job+authority+audit; live final validation; commit.
Worker initiation: actual executor identity→nonlocking scoped immutable locator→
live stored-user guard→job→approval→attempt; exact values and time checked after waits
and at physical commit via the existing root-bound guard fence. Bind fixed operation,
full logical/physical approval, request bytes/checksum/action, expected numeric
versions, claimant, attempt and dispatch key. No latest-version substitution.
Receipt/closing: actual executor identity→canonical account lock→job→approval→attempt,
all scoped; exact previous committed marker; permitted receipt replay first; immutable
insert+receipt audit atomically. Current initiator revocation does not erase this
owned evidence. No lower-order auth locks after job/attempt during state closing.

Authority handles are bound to the exact Session physical root and action class.
Closing handle must be incapable of being used as an initiation/fetch handle.
Helpers returning before commit return no usable external send authorization.
The T2 participant owns domain transitions; T1 must not copy or rewrite it. Expose
an exact trusted participation boundary, preserving T2's physical marker-before-I/O
sequence and final checks. No provider or task registration in this package.

Any uncertain local commit requires scoped durable readback, not immediate new POST.
Duplicate terminal delivery reads/ACKs known result and never reopens attempt.
Crash before receipt commit can lose known upload ID; durable dispatch then remains
ambiguous/no resend. Provider-history GET authority and explicit new credential
selection require a separately accepted live read contract; closing handle alone
does not supply it. Lossless original POST-response projection remains T2's
independent adapter responsibility, not repaired by receipt DDL.

## Final verification, after implementation

Real disposable PostgreSQL only, no intermediate test loop. Cover actual executor
login versus SET ROLE/GUC/shared API identity, missing policy/privileges, forced RLS,
immutable graph/no orphans, exact binding/claimant mismatch, deadline and revocation,
previous physical marker commit, same/different receipt races, late ambiguous
receipt, large exact upload ID and clock skew, safe errors/repr, transaction fencing,
failed/uncertain commit readback and zero provider calls on denied authority. Test
grants in exact synthetic roles and prove old ACLs unchanged. Empty-only downgrade
refuses any hidden new history; no CASCADE. Existing0066/0071/0072 compatibility and
whole worker composition remain final integration gates. Until then IMPLEMENTED /
UNVERIFIED is the only valid release status.
