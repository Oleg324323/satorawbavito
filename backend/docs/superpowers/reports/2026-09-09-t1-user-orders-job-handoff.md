# Local Orders job authority source handoff

2026-09-09. **IMPLEMENTED / UNVERIFIED**. This is source delivery, not final
integration acceptance or operational readiness. User sequencing explicitly
deferred all tests, PostgreSQL admission, intermediate reviews and new tests until
the complete source package. None was run or created during implementation.

The binding requirements are the full
`2026-09-09-t1-user-job-authority-proposal.md`, source amendment
`2f0422076ac0b365d2e97432b9ce57d6aa6b350f`, and controller decisions in
`../plans/2026-09-09-user-orders-job-authority.md`. Existing 0071 source remains
unchanged. Migration 0072 follows observed 0071; no operational registration is
introduced. Schema/runtime source commit:
`b274a5097abbe02f078ccabbea0355d68fa3d067`. This supersedes the briefly reported
`ce6b1ce` snapshot; consumers must not mix those snapshots. The amendment made
nullable SQL CHECK branches explicit, checked credential-ref text, audit time
ordering, and live/expired lease conditions. Further changes must be new commits.

## Source and callable boundary

All paths below are relative to `backend/`.

| Path | Responsibility |
| --- | --- |
| `alembic/versions/20260909_0072_user_orders_jobs.py` | Four new relations, codecs, state/history/witness constraints, dual FORCE RLS, empty-only downgrade |
| `app/platform/integrations/user_orders_job_contract.py` | Strict frozen redacted requests, policy, dependency, locator, committed claim and safe view |
| `app/platform/integrations/user_orders_job_store.py` | Private SQLAlchemy Core descriptors and scoped read/create/CAS/transition/audit operations; no commit/config/network |
| `app/platform/integrations/user_orders_jobs.py` | Trusted authenticated/worker boundaries, lifecycle, caller-root publication, resolution, closure and delivery |
| `app/platform/integrations/publication_guard.py` | Narrow exact-class Orders final transaction fence extension |
| `ops/runtime-db-role.sql` | New-object prerequisites and restricted grants after existing broad grants |

Imports from `app.platform.integrations.user_orders_job_contract`:

- `TrustedOrdersSourceBinding(provider, source_kind, adapter_version, mapping_version, source_contract_version)`.
  Trusted composition input, not client/queue data or an import path. It recognizes
  exactly WB supplier orders and Avito order-management selectors. Mapping versions
  remain `wb-statistics-status-v1` / `avito-order-status-v1`.
- `AvitoOrdersSourceRequest(date_from, statuses, limit, page)` requires explicit
  date-or-None, tuple of exact nonempty strings without comma, integer 1..20 limit,
  integer 1..BIGINT_MAX page. `WbOrdersSourceRequest(date_from)` requires a date.
  `datetime` is not silently coerced to a calendar date.
- `OrdersJobRequest(organization_id, marketplace_account_id, binding, source_request)`.
  Properties `canonical_bytes`, `checksum`; `from_bytes(value, *, trusted_binding)`
  rejects duplicate keys, unexpected fields, wrong bindings, noncanonical dates,
  booleans as integers, JSON constants and any byte-level re-encoding difference.
- `OrdersExecutionPolicy(reference, version, max_attempts, lease_seconds, retry_backoff_seconds)`.
  All values explicit. Backoffs are a tuple of exactly max_attempts minus one
  nonnegative integral seconds. Physical/resource bound max_attempts<=1000 is not
  an approved production attempt policy; every second value fits INTEGER.
- `OrdersCredentialDependency(organization_id, marketplace_account_id, provider,
  credential_id, credential_kind, generation, payload_schema_version, expires_at)`.
  Exactly one `wb_api` or `avito_oauth_access` dependency. This contains no payload.
- `OrdersJobLocator(organization_id, marketplace_account_id, job_id)`.
  `from_queue(payload)` rejects extra fields/noncanonical UUID strings;
  `queue_payload()` emits only the three locator fields.
- `ClaimedUserOrdersJob` includes locator, UUID attempt/token, exact job/attempt
  versions, lease/deadline and an in-process committed marker. The service mints
  it only after its own physical claim/renewal commit. It is not queue authority.
- `StoredOrdersJobView` exposes only job ID/state/version/attempt count/due/finish,
  safe reason and scoped result run/coverage. `delivery_result()` emits only
  `{"job_id": "<UUID>", "state": "<bounded state>"}`. Reprs are redacted.

Import `UserOrdersJobs` from `app.platform.integrations.user_orders_jobs`.
Construct explicitly:

```python
service = UserOrdersJobs(
    session_factory=trusted_fresh_session_factory,
    trusted_sources=trusted_source_bindings_tuple,
    credential_resolver=trusted_paired_resolver,
)
```

`trusted_sources=()` is the default and leaves source-dependent create/claim/fetch/
publication unavailable. The default resolver is the existing paired
`resolve_marketplace_credential_for_fetch`; there is no fallback or refresh path.
Injection is trusted application wiring, not a public request field. No settings,
credentials, DB or network are accessed merely by constructing request contracts.

Actual public methods and ownership:

| Method (all named parameters keyword-only except indicated Session) | Result / ownership |
| --- | --- |
| `create(authenticated_actor, request, idempotency_key, execution_policy, authority_expires_at)` | Stored view after own clean root commit |
| `status(authenticated_actor, locator)` | Fresh cabinet:read and original principal/session; own root |
| `replay(authenticated_actor, request, idempotency_key, execution_policy, authority_expires_at)` | Exact original command read, no new attempt; own root |
| `cancel(authenticated_actor, locator, revoke=False)` | Original authenticated principal/session closure, terminal replay; own root |
| `claim(organization_id, marketplace_account_id, job_id)` | Committed claim, or bounded existing view for not-due/running/terminal delivery; own root |
| `renew(claim)` | New committed versions or expired terminal view if required lease exceeds deadline; own root |
| `fail_read(claim, safe_reason)` | Retry/final failure/expiry after full prior publication rollback; own root |
| `reclaim(organization_id, marketplace_account_id, job_id, expected_job_version, expected_attempt_version)` | Exact CAS of expired current attempt; no fetch; own root |
| `request_for_fetch(claim)` | Exact frozen persisted request after short guarded root closes |
| `resolve_fetch(claim)` | Existing `DecryptedCredential` secret wrapper only, after guarded root closes and exact paired-binding assertion |
| `acquire_publication_guard(session, *, claim)` | Caller-owned existing clean root, returns `UserOrdersPublicationHandle` |
| `publish(session, *, claim, participant)` | Trusted `participant(session, handle)` returns actual completed run ID; atomic completion participates in same caller root |
| `execute(locator)` | Always `SOURCE_CONTRACT_UNAVAILABLE`; no handler/fetch/claim registered |
| `close_denied(organization_id, marketplace_account_id, job_id)` | Proven denial closure only, content-free state result; own root |
| `readback(organization_id, marketplace_account_id, job_id)` | Content-free stored state only, never mints a claim or refetches; own root |
| `deliver_existing(authenticated_actor, locator, deliver)` | Authorized read commits before injected locator-only delivery |
| `create_then_deliver(authenticated_actor, request, idempotency_key, execution_policy, authority_expires_at, deliver)` | Create commit, authorized read commit, then locator delivery |
| `recover_delivery(organization_id, marketplace_account_id, limit, deliver)` | Explicit bounded one-account selection, fresh stored authority per send; no scheduling |

Function facades are `create_user_orders_job(service, *, authenticated_actor,
request, idempotency_key, execution_policy, authority_expires_at)`,
`claim_user_orders_job(service, *, organization_id, marketplace_account_id, job_id)`,
and `acquire_user_orders_job_publication_guard(session, *, service, claim)`.

The publication handle has read-only public `request`, `source_run_key`,
`account_binding`, `initiating_user_id`; `revalidate_before_write()` refreshes live
authorization and the exact job fence; `complete(*, result_sync_run_id)` validates
and seals the real completed Orders run. `initiating_user_id` is immutable audit
origin, not a new worker principal or role. Completion consumes the handle; a
second completion poisons the root. Handles cannot be pickled or reused in another
root. Caller must commit/rollback, and every participant failure requires rollback.

## Transaction and lifecycle invariants

Public wrappers require a factory returning an unused Engine-bound PostgreSQL
Session. Joined connections, AUTOCOMMIT, nested roots, dirty Sessions and non-RC
isolation fail closed. A factory returning a foreign active root is rejected
without taking ownership of it. No provider I/O occurs inside these roots.

Creation extracts the user/session from the real trusted `ActorContext` boundary
and locates canonical membership. It reads safe account/credential metadata, then
takes live user -> membership -> session -> account UPDATE -> credential SHARE
locks through the existing guard before scoped idempotency lookup. It captures no
decrypted data. Source, request bytes, session, account, credential and policy are
exact on create replay. Authority deadline is explicit and no later than current
session/credential expiry; no refresh/rotation updates persisted delegation.

Worker input is exactly org/account/job ID. The initial immutable locator read
learns the stored principal, then the same auth/account/credential order precedes
job UPDATE -> current attempt UPDATE -> Orders run/projection locks. The locked
delegation is compared against the unlocked locator snapshot. Fresh DB clock is
read after waits. Claims create UUID4 attempt and fencing token, one audited version
transition, and return usable authority only after physical commit.

Publication starts a fresh caller-owned root and repeats the same ordering. Domain
code creates a run only after validated snapshot/manifest exist. No run is created
at claim. Success checks account/org/source/adapter/mapping/source-contract,
historical external account/ref, both NULL normalized windows, exact
`orders-job-v1:<job UUID>:<attempt UUID>`, completed run state `complete` or `partial`,
and real `completed_at`. Complete coverage additionally requires complete manifest;
page count alone is never a success proof.

The existing guard remains the final Session before_commit listener. Its private
`_register_user_orders_fence(guard, fence)` accepts exactly one same-root
`UserOrdersPublicationHandle`, before finalization only. It cannot select callback
names or register arbitrary callables. Finalization flushes, rejects queued late
flush work, revalidates auth and then the exact job/attempt snapshot, token,
versions, lease/deadline and own expected success. Any failure poisons the root.
Listener-order, nested-transaction and physical-root protections are preserved in
source; compatibility still requires the final tests below.

Retry codes are `SOURCE_READ_UNAVAILABLE`, `SOURCE_READ_RATE_LIMITED`,
`LOCAL_TRANSIENT_FAILURE`. These apply only to this read-sync operation after
whole publication rollback. Unknown failure input becomes
`PERMANENT_SOURCE_FAILURE`. Exhaustion records `RETRY_BUDGET_EXHAUSTED`; expired
lease is abandoned with `LEASE_EXPIRED`, then retried only within finite budget
and deadline. Next due is the exact captured policy backoff indexed by the
completed attempt. Required lease/backoff beyond deadline expires the job.

Cancellation/revocation retain a running terminal attempt or no pointer if closed
from queued. Other closure reasons are `USER_CANCELLED`, `AUTHORITY_REVOKED`,
`AUTHORITY_EXPIRED`, `PERMISSION_DENIED`, `ACCOUNT_SCOPE_DENIED`,
`ACCOUNT_DISCONNECTED`, `BINDING_CHANGED`, `SOURCE_CONTRACT_UNAVAILABLE`.
`close_denied` accepts no reason or caller exception: it locks only job/attempt,
then derives the reason from safe current metadata reads without later auth/account
locks, or from the trusted configured-source absence. It cannot fetch/publish/
renew/rebind or expose command content. Failed live authority can remain denied
until explicit housekeeping runs; denial never grants continued execution.

Public error codes are `JOB_CONTRACT_INVALID`, `JOB_ACCESS_DENIED`, `JOB_NOT_FOUND`,
`JOB_CONFLICT`, `JOB_NOT_DUE`, `JOB_NOT_CLAIMABLE`, `JOB_FENCE_INVALID`,
`JOB_AUTHORITY_DENIED`, `JOB_PERSISTENCE_FAILED`, `READBACK_REQUIRED`,
`DELIVERY_UNCONFIRMED`, `SOURCE_CONTRACT_UNAVAILABLE`. Errors do not embed original
provider exceptions, request bytes, tokens, session IDs or external account data.

## Exact SQL objects

The migration is the authoritative column/constraint definition; there are no
changes to old parent tables or ORM metadata. New Core descriptors match it.

| Relation | Exact columns (shared owner columns included below) |
| --- | --- |
| All four | `organization_id INTEGER`, `marketplace_account_id INTEGER`, `job_id UUID` |
| `user_orders_jobs` | `provider/operation_kind/required_permission TEXT COLLATE C`; `initiator_user_id/initiator_session_id VARCHAR(64)`, `initiator_membership_id INTEGER`; `idempotency_key UUID`; `request_schema_version SMALLINT`, `request_bytes BYTEA`, `request_checksum TEXT COLLATE C`; `source_kind/adapter_version/mapping_version/source_contract_version TEXT COLLATE C`; `source_date_from DATE`, `source_statuses TEXT[]`, `source_limit INTEGER`, `source_page BIGINT`; `requested_from/requested_to TIMESTAMPTZ` (both NULL only); `expected_external_account_id VARCHAR(128)`, `expected_credential_ref VARCHAR(255)`; `created_at/authority_expires_at TIMESTAMPTZ`; `policy_reference TEXT COLLATE C`, `policy_version/max_attempts/lease_seconds INTEGER`, `retry_backoff_seconds INTEGER[]`; `state TEXT COLLATE C`, `version BIGINT`, `attempt_count INTEGER`, `current_attempt_id UUID`, `next_attempt_at/completed_at TIMESTAMPTZ`, `safe_reason TEXT COLLATE C`, `result_sync_run_id BIGINT`, `result_coverage_state TEXT COLLATE C` |
| `user_orders_job_authorities` | `credential_id UUID`, `credential_kind/provider TEXT COLLATE C`, `generation BIGINT`, `payload_schema_version SMALLINT`, `expires_at TIMESTAMPTZ` |
| `user_orders_job_attempts` | `attempt_id/claimant_token UUID`, `attempt_number INTEGER`, `state TEXT COLLATE C`, `version/job_version_after BIGINT`, `claimed_at/lease_expires_at/finished_at TIMESTAMPTZ`, `safe_reason TEXT COLLATE C`, `result_sync_run_id BIGINT`, `result_coverage_state TEXT COLLATE C` |
| `user_orders_job_audit` | `event_id UUID`, `job_version_after BIGINT`, `event_kind TEXT COLLATE C`, `occurred_at TIMESTAMPTZ`, `actor_kind TEXT COLLATE C`, `actor_membership_id INTEGER`, `attempt_id UUID`, `before_state/after_state/reason TEXT COLLATE C`, `attempt_version_before/attempt_version_after BIGINT`, `attempt_state_before/attempt_state_after TEXT COLLATE C`, `attempt_count_after INTEGER`, `next_attempt_at/lease_expires_at TIMESTAMPTZ`, `result_sync_run_id BIGINT`, `result_coverage_state TEXT COLLATE C` |

FKs are restrictive, without CASCADE: full canonical account `(org,account,provider)`;
membership `(org,membership)` plus new-object insert consistency check with user
and login session; `lk_users(user_id)`; `lk_sessions(session_id)`; exact credential
ID plus new-object metadata owner/provider/kind/generation/schema/expiry comparison;
child `(org,account,job)`; current/audit attempt `(org,account,job,attempt)`;
success `(org,account,sync_run_id)` to actual Orders run parent. Existing parent
composite uniques are reused; no proposal-only parent constraint is assumed.
Later parent revocation/rebinding is a live guard denial, never an update to the
historical delegation. Database credentials still trust application authentication:
RLS is not cryptographic proof of who supplied a valid user/session identity.

Primary/scoped unique indexes cover job ID and `(org,account,job)`, scoped
membership/operation/idempotency UUID, authority scoped job/kind and job/credential,
attempt ID and scoped job/ID, job/number and job/token, event ID and scoped
job/version. Named `uq_user_orders_current` admits at most one claimed attempt per
job; `ix_user_orders_due` supports scoped queued due selection. Current attempt FK
is deferred. Event/version uniqueness prevents reuse of an audit for an earlier
job version. Additional typed audit witness columns correlate every captured job
and attempt transition, including retry/reclaim after clearing current pointer.

Pure function signatures: `public.user_orders_ascii(text)`,
`public.user_orders_text(text,integer)`,
`public.user_orders_request_bytes(public.user_orders_jobs)`.
ASCII JSON uses sorted keys, compact separators, explicit nulls, Unicode escape/
surrogate-pair encoding and exact SHA256. SQL reconstructs bytes from typed source
projection rather than trusting JSONB or caller checksum. All dates are explicit;
no provider timezone, half-open coverage or dateTo is invented.

Trigger function signatures: `public.user_orders_row_guard()` and
`public.user_orders_transition_witness()`. Each new table has `user_orders_guard`,
`user_orders_no_truncate`, and deferred `user_orders_witness`. Each has ENABLE +
FORCE RLS, policy `user_orders_scope`, both USING and WITH CHECK comparing canonical
organization and marketplace-account GUC strings. No caller provider GUC exists.

Runtime: SELECT/INSERT on all four, UPDATE only the explicit lifecycle columns on
jobs/attempts; no UPDATE on authority/audit, DELETE/TRUNCATE/public execute or
default-ACL/ownership changes. Only exact pure helper signatures get execute.
Upgrade creates new objects, rejects collisions and clears inherited new-object
grants. Downgrade locks the fixed four tables ACCESS EXCLUSIVE, sets local
row_security=off (refuses hidden RLS history), requires all four empty, then drops
only new constraints/functions/tables without CASCADE.

## Delivery and limits that remain explicit

No outbox exists. A crash after durable creation and before broker send can strand
a queued job. Authenticated exact replay/redelivery and explicit bounded recovery
can resend existing locators; no scanner/beat is activated. `READBACK_REQUIRED`
means inspect durable state before any redelivery/refetch. A running claim with a
lost commit response yields no recoverable token; wait for expiry and use explicit
reclaim CAS. Succeeded readback acknowledges without fetch. This is not exactly-once
provider reading: crash/lease expiry can cause another read under a new attempt,
but old attempts cannot publish. No retry of provider POST/send/price operations
is authorized by these contracts.

Avito selectors describe one page. WB dateFrom has no inferred timezone or dateTo.
Both normalized windows remain NULL and mean no interval claimed. Partial run
success remains partial. Neither recognized source proves a provider-consistent
complete snapshot; no default provider handler, router/task registration,
rollout flag, broker connection, scheduler or autonomous recovery is installed.
Owner-approved durations, attempt/backoff limits, abuse/concurrency controls,
retention/deletion procedure and operational ownership remain activation inputs.
Production policies are not supplied by this package.

## Required final validation — NOT RUN

Controller owns the subsequent final phase. Admit only a disposable Unix-socket
PostgreSQL resource separately; no working DB, Redis, credentials, providers,
SQLite substitute, hidden skip or owner-role-only RLS result. No tests below are
claimed to exist yet. The final test author should add focused files such as
`tests/test_user_orders_job_contract.py`, `tests/test_user_orders_jobs_postgres.py`,
`tests/test_user_orders_jobs_transport.py` and run them with the existing guard and
paired credential tests after source is complete.

Required cases:

1. Python/SQL exact bytes + SHA256 parity across every selector, null, empty status
   tuple, Unicode/control/non-BMP text and dates. Reject duplicate keys, extra
   fields, bool IDs/page/limit/version/policy, NUL, isolated surrogates, alternate
   date encodings, source/version injection, lossy coercion and changed bytes.
2. Authenticated create, scoped same-key exact replay/status/cancel; wrong org,
   account, user, membership and another session; sync permission versus basic
   read; stale account/ref/current credential; session refresh cannot extend job.
3. Real two-session user/membership/session/account/credential revocation in both
   lock winner orders, credential generation change with same ID, expiry after
   auth/domain waits and final flush; state-closing proof cannot be caller-forged.
4. Claim physical commit before secret/queue use, duplicate queued/running/terminal
   deliveries, due/budget/deadline enforcement, two-session claim/version CAS,
   current-token fencing, explicit renewal and expired-lease reclaim, retry
   backoff/overflow/exhaustion, stale claim after cancellation or renewal.
5. Exact deferred audit/job/attempt reciprocal matrix, rollback on omitted/wrong
   audit, no orphan or skipped version, no reuse of prior audit, all terminal and
   authority history immutable; raw runtime SQL mutations cannot bypass matrix.
6. Actual T3 participant + real completed Orders run: wrong source/adapter/mapping/
   contract/run key/account binding/result owner, staging/failed/partial/complete
   semantics, changed attempt snapshot, concurrent prior token, final flush expiry,
   atomic run/projection/job/attempt/audit rollback and successful own final fence.
7. Simulated uncertain claim/success commit, state readback and no blind refetch;
   broker send failure/lost-enqueue recovery, queue/result/log/exception/repr/copy/
   serialization canaries contain no request/session/token/provider error/secret.
   Use only synthetic transport and paired credential wrappers.
8. Both RLS USING and WITH CHECK under nonowner NOSUPERUSER/NOBYPASSRLS runtime;
   explicit grants, wrong/missing contexts and cross-account FKs; migration fresh
   upgrade, collision rejection, empty downgrade/re-upgrade, populated and hidden
   history refusal; exact helper signatures and absent public execute.
9. Existing publication guard compatibility: credential-independent consumers,
   physical Engine root, AUTOCOMMIT, nested roots, wrong listener order, late flush,
   failed/reused/cross-root handle, attempted fence replacement during finalization.

Record exact commands, final source SHAs, all pass/fail evidence and any unresolved
defects in the controller's final report. This source handoff cannot substitute
for that acceptance.
