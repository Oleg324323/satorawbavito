# User-initiated Orders job authority: bounded architecture proposal

2026-09-09, proposal only under coordinator's explicit mandate. No code, migrations, revision allocation, DB/network/provider/queue/env access, Git changes, other-worktree reads or subagents. Only this scratch report is written. No outbox, broker abstraction, secrets storage, autonomous scheduler, KIZ, renderer/calendar, price/send worker or production defaults are proposed.

## Recommendation and evidence

Use **one persisted, session-bound delegation per exact single-account Orders request**. It records the authenticated user/session/membership, service-fixed `sync:run`, exact account/fetch-authority metadata, immutable request and explicit execution-policy snapshot. A queue message merely locates that job. A trusted worker loads the row and revalidates its stored principal through the specified user-session guard (not yet installed) before any fetch attempt and before atomic publication. A job grants no role and never outlives its persisted authorization deadline or a revoked session/account/credential. Another user's current session cannot silently replace its initiator.

Considered alternatives: serializing ActorContext/permissions into Celery cannot prove freshness and allows no durable job revocation; detached service-account delegation needs an additional independent principal policy. The session-bound option directly satisfies current user-initiated work without fabricating admin authority. Detached/system scheduling remains disabled.

Sources read:

- `1406686:backend/docs/superpowers/specs/2026-09-09-publication-guard-design.md`: root already assigns Orders synchronization/publication `sync:run`, persisted read `cabinet:read`; user→membership→login-session→account→credential/token locks, exact metadata, READ COMMITTED and final commit checks. Current spec deliberately excludes worker delegation; this proposal extends it via a trusted persisted job, not a new principal label.
- Current `app/routers/avito_orders.py:788–803` checks `sync:run` then enqueues only org/scenario/force; authenticated user/session/account authority is lost. It is a legacy route, not a ready canonical job-creation owner.
- Current `app/avito/returns_tasks.py:24–25,110–176,176–194` uses org-based credentials/cache and an all-org scheduler, including a legacy default org fallback. None is a positive canonical execution principal.
- Current `app/canonical_shadow_tasks.py:68–118,173–204` uses org allowlist, org Redis lock and Celery retry. These are scheduling mechanisms, not persisted account/user delegation or durable fencing.
- Current `app/infra/celery_app.py:17–98`: JSON serialization/accept-content and beat/task routing exist. They do not authenticate a job, retain a user session or replace DB CAS; do not inherit existing periodic schedules into this proposal.
- Exact T3 `8a8722f:app/orders/ingestion.py:37–46,159–210` defines WB statistics, Avito order-management/browser source identities and typed manifests; exact Orders0062 defines immutable run/source/version/window identities. These are ingestion/result contracts, **not an exact enqueue request or callable adapter registry**.

## Narrow operation boundary

Proposed first operation literal is `orders.sync.v1`, fixed permission `sync:run`, one explicit org/account/provider, read-only provider acquisition followed by canonical Orders publication. Initial selectable source kinds must be explicitly bound by T3 to approved read adapters: `wb-statistics-supplier-orders` or `avito-order-management`. The existing domain recognizes both, but that recognition alone does not register a job handler or authorize a production fetch. Exclude `avito-browser` from this initial fetch job: browser ingestion needs an authenticated upload/frozen input identity, not a made-up remote fetch operation.

Proposed exact request v1 fields are:

`schemaVersion=1, operationKind=orders.sync.v1, organizationId, marketplaceAccountId, provider, sourceKind, adapterVersion, mappingVersion, sourceContractVersion, sourceRequest, requestedFrom, requestedTo`.

Versions and source kind come from a trusted, versioned service dispatch table, not an arbitrary client import path. T3 exact response `2f0422076ac0b365d2e97432b9ce57d6aa6b350f:backend/docs/superpowers/reports/2026-09-09-orders-job-source-binding.md` supplies the following binding, replacing the earlier unresolved source-request proposal:

- Avito `sourceRequest`: explicit `dateFrom` null or ISO calendar date; `statuses` array of exact nonempty strings without embedded comma/trim/aliases; integer `limit`1..20 and `page`>=1. No defaults. Null date and empty statuses omit the corresponding remote parameter. A nonnull date becomes UTC-midnight Unix seconds. This is one page acquisition, not proven complete synchronization.
- WB `sourceRequest`: exactly `dateFrom` ISO calendar date, rendered `YYYY-MM-DDT00:00:00` without offset. No inferred timezone, dateTo or flag.
- All selectors enter canonical request bytes and exact replay equality. `requestedFrom`/`requestedTo` are both null in this initial recognized-source contract: no normalized coverage interval is claimed. Both-null does not claim all-history. Nonnull normalized windows require another reviewed source contract proving timestamp field, timezone, inclusivity, pagination and completeness; guessed local filtering is forbidden.

These source kinds are recognized domain evidence, not registered complete-sync handlers. No inspected provider source proves an immutable complete snapshot/terminal manifest. Real `orders.sync.v1` handler activation therefore remains fail-closed. Local storage/claim/revocation tests may use explicitly synthetic source fixtures. No free payload JSON, scenario/force switch, callable name, URL, queue-provided permission or credentials.

Canonical bytes: ASCII-safe UTF-8 JSON, sorted keys, compact separators, ensure_ascii=true, allow_nan=false; fixed six-digit UTC microseconds with Z and explicit nulls. Store bytea and SHA256, compare exact bytes on replay. Integer IDs are canonical positive integers; strings must be PostgreSQL-representable Unicode scalar text without NUL. New job-key domain may use canonical UUIDv4 client idempotency keys, avoiding the unrelated unbounded legacy text-key issue. This is a new bounded interface, not a change to Orders/source serializers.

`source_snapshot` and manifest bytes are outputs learned by the adapter. They are **not** fabricated at enqueue or claim. The exact accepted run key is `orders-job-v1:<canonical lowercase job UUID>:<canonical lowercase attempt UUID>`, additionally scoped by org/account/source. Create the run only after a validated normalized snapshot and manifest are available, within publication transaction after user/account/job/attempt locks and before domain run/projection operations. Failed/no-evidence fetch closes the attempt without a run. Same-attempt changed snapshot/request conflicts; terminal runs never reopen.

## Minimal proposed relations

Names are proposal names; T1 owns SQL types/constraint implementation and eventual revision ordering. IDs below are server UUID4 except existing canonical INTEGER org/account/membership and textual user/session IDs. All timestamps TIMESTAMPTZ, all scoped FKs restrictive, no cascade/history deletion. All relations have ENABLE+FORCE org RLS and explicit account predicates; runtime NOSUPERUSER/NOBYPASSRLS. Exact account authorization remains in the service guard. No table contains a token, verifier, secret, ciphertext, cookie, provider payload or auth-token string.

### 1. user_orders_jobs — immutable delegation + current lifecycle

Immutable columns:

- job_id, organization_id, marketplace_account_id, provider; unique(org,account,job_id), account/provider composite FK.
- operation_kind CHECK orders.sync.v1; required_permission CHECK sync:run. Registry constant determines them; clients/queue cannot set them.
- initiator_user_id, initiator_membership_id, initiator_session_id, all required. Composite org/membership/user proof, login session→same user proof; physical FK target extension or a deferred equivalent is T1's choice. User/session strings are not integer worker IDs.
- idempotency_key UUID; unique(org,account,initiator_membership_id,operation_kind,idempotency_key). A different initiator may create its own independent intent; another session cannot take over an existing job by replay. Same-key replay additionally compares the original initiator session and every immutable request/binding/policy field.
- request_schema_version=1, request_bytes BYTEA, request_checksum lowercase SHA256; exact typed request projection fields above, or equivalent enforced reconstruction. No generic job kwargs.
- expected_external_account_id and exact nullable expected_credential_ref. Null means require NULL, not wildcard. One account only; multi-account fan-out creates separately authorized jobs, not a wider implicit scope.
- created_at, authority_expires_at, policy_reference (nonsecret owner-approved identifier), policy_version positive; immutable execution snapshot max_attempts positive integer, lease_seconds positive integer, retry_backoff_seconds integral nonnegative sequence with length max_attempts−1. A zero backoff is allowed only if approved policy explicitly chooses it; no defaults are invented. Transport redelivery count is not attempt count.

Mutable columns:

- state in queued/running/succeeded/failed/cancelled/revoked/expired/blocked; version positive starting1, increment exactly once per accepted transition/renewal; attempt_count>=0 and <=max_attempts.
- current_attempt_id nullable scoped FK, next_attempt_at nullable, completed_at nullable, safe_reason nullable closed enum, result_sync_run_id nullable scoped Orders FK; result_coverage_state nullable complete/partial.

Creation: queued/version1/attempt_count0/current attempt NULL/next_attempt_at=created_at; completed/reason/result NULL. Authority deadline is explicit, future at DB creation time, no later than authorizing session expiry and each expiring captured fetch authority, and within approved owner policy. Deadline never extends on login refresh, retry, redelivery, permission regain, credential rotation or another user's session. Missing policy approval/values prevents production creation, not synthetic local testing.

### 2. user_orders_job_authorities — immutable exact metadata dependencies

One row per required credential dependency: org/account/job_id, credential_id, credential_kind, generation>0, payload_schema_version, provider, expires_at nullable. Unique scoped job+credential kind and exact credential ID; owner/provider FK to account; exact credential reference scoped by owner. This table is metadata only, no secret/verifier columns. Known kind/schema/expiry combinations follow canonical credential schema. Every credential used for a fetch is recorded; the worker may not substitute the newest live row.

Why a child relation: an approved source may use more than one credential in its acquisition chain. A single optional credential column silently loses one dependency; a generic JSON array loses strong ownership references. Local first implementation can restrict a handler to one exact fetch-access dependency; admitting another kind requires a trusted operation binding. No ingestion-bearer row variant is included for this initial remote-read job; add a separate exact upload/input authority contract later if needed.

Creation captures metadata and account binding under the creator's guard without persisting or passing a decrypted secret. Worker later obtains same-row `ResolvedCredentialForFetch`, verifies its safe binding equals the persisted expectations, and releases its short resolver transaction before I/O. A replacement between creation/resolution/publication invalidates the job; a new explicit user intent is needed. Authority child rows are immutable through end of job.

### 3. user_orders_job_attempts — durable claim/fencing history

attempt_id UUID4; org/account/job FK; attempt_number positive, unique scoped job+number; claimant_token UUID4 generated by trusted worker service during successful claim, unique within job and immutable. It is a fencing identity, not a credential or a worker authorization source, and never enters queue payloads. Persist claim_owner_label only if needed as a bounded server-assigned diagnostic; it is optional and grants no authority.

Fields: state in claimed/succeeded/failed/abandoned/cancelled/revoked/expired/blocked; version positive starting1; claimed_at; lease_expires_at>claimed_at; finished_at nullable; safe_reason nullable; result_sync_run_id nullable; result_coverage_state nullable. Mutable lease/version/state/finish/result only; identity/claim token/number immutable. claimed has finish/reason/result NULL; every terminal attempt has finished_at>=claimed_at and is immutable. succeeded has scoped result run and complete/partial coverage; all other terminal attempts have no success result.

Every success/retry/reclaim/cancellation result checks job version, current attempt ID, attempt version and token; worker is given these only after the claim commit. Old token cannot publish, renew or overwrite a winner. No provider dispatch marker is defined here because operation is bounded read acquisition. This fencing must never be advertised as permission to retry price/send mutations; those remain owned by their domain marker contracts.

### 4. user_orders_job_audit — append-only transition evidence

event_id UUID4; org/account/job FK; job_version_after; event_kind; occurred_at; actor_kind membership/delegated_worker; actor_membership_id required only membership, otherwise NULL; initiator membership/user/session derived by job FK rather than editable snapshot; attempt_id nullable scoped FK; before_state nullable; after_state; reason nullable closed enum. No arbitrary metadata or raw error text.

Unique(org,account,job_id,job_version_after) yields one transition audit per job version. Exact event replay compares its canonical bounded fields; it cannot mutate prior history. Every job/attempt transition and audit commits together; omitted/mismatched audit aborts. Successful result run publication+attempt/job result+audit are atomic in the domain publication transaction. Receipt/manifest evidence stays in Orders tables.

## Exact state and audit rules

| Operation/event | Job transition | Attempt effect | Actor / reason |
|---|---|---|---|
| job.created | absent→queued, version1 | none | membership=authenticated initiator; null |
| job.claimed | queued→running, version+1 | new claimed attempt; count+1; next_attempt_at NULL | delegated_worker; null |
| job.lease_renewed | running→running, version+1 | same claimed/live token, attempt version+1 and expiry extends only within authority deadline | delegated_worker; null |
| job.retry_scheduled | running→queued, version+1 | current attempt failed; clear current pointer; set bounded next_attempt_at | delegated_worker; one allowed retryable read failure |
| job.reclaimed | running→queued, version+1 | expired claimed attempt abandoned; clear pointer; next_attempt_at from policy | delegated_worker; LEASE_EXPIRED |
| job.succeeded | running→succeeded, version+1 | same attempt succeeded with exact result FK | delegated_worker; null |
| job.failed | running→failed, version+1 | attempt failed or abandoned, retain pointer | delegated_worker; PERMANENT_SOURCE_FAILURE or RETRY_BUDGET_EXHAUSTED |
| job.cancelled | queued/running→cancelled, version+1 | none before claim, otherwise current claimed attempt cancelled | currently authenticated initiating membership/session; USER_CANCELLED |
| job.revoked | queued/running→revoked, version+1 | none before claim, otherwise current claimed attempt revoked | membership for explicit initiator revoke, or delegated_worker observing exact referenced auth revocation; AUTHORITY_REVOKED |
| job.expired | queued/running→expired, version+1 | none before claim, otherwise current claimed attempt expired | delegated_worker; AUTHORITY_EXPIRED |
| job.blocked | queued/running→blocked, version+1 | none before claim, otherwise current claimed attempt blocked | delegated_worker; PERMISSION_DENIED, ACCOUNT_SCOPE_DENIED, ACCOUNT_DISCONNECTED, BINDING_CHANGED or SOURCE_CONTRACT_UNAVAILABLE |

Queued/running have completed_at NULL and no result; all terminal states completed_at required and no next_attempt_at. queued has current_attempt_id NULL; running requires a claimed current attempt. succeeded/failed retain terminal attempt; cancelled/revoked/expired/blocked have NULL attempt only when terminalized from queued. Audit attempt_id for reclaim/retry references the just-finished historical attempt despite cleared current pointer. No terminal state transitions or resurrection; exact user request replay returns its original state/result after current read authorization, not a new execution.

Closed additional retry codes: SOURCE_READ_UNAVAILABLE, SOURCE_READ_RATE_LIMITED, LOCAL_TRANSIENT_FAILURE. They permit retry only while policy budget/deadline permit, after whole local publication rollback, and only for this read-sync operation. Invalid identity, revoked/rotated credentials, source-contract mismatch and missing permissions never become retryable transport errors. Unknown error is not logged/stored verbatim; conservatively fail with a safe local code. Ambiguous local COMMIT requires readback, not immediate refetch.

If a policy backoff or required new lease would exceed authority_expires_at, expire instead of scheduling/claiming. Lease renewal cannot revive an expired lease and does not reset attempt_count. Only explicit verified live claimed execution renews; no unbounded extension beyond authority deadline. After a failed/expired attempt, a new attempt gets a new ID/token and source run identity. At most one current attempt may commit publication, enforced by its current-token/job-version fence.

## Trusted job → specified user guard integration

Proposed service API (not installed):

```python
create_user_orders_job(session, *, authenticated_actor, request, idempotency_key,
                       execution_policy, authority_expires_at) -> StoredJobView
claim_user_orders_job(session, *, organization_id, marketplace_account_id, job_id)
    -> ClaimedUserOrdersJob
acquire_user_orders_job_publication_guard(session, *, organization_id,
    marketplace_account_id, job_id, attempt_id, claimant_token,
    expected_job_version, expected_attempt_version) -> GuardedStoredJob
```

Creation principal is extracted from real authenticated ActorContext and canonical membership in trusted service, never request fields. Operation selects fixed `sync:run`; caller cannot provide arbitrary required_permissions. User/account/credential guard protects capture of immutable bindings until job+authority+created-audit commit. No fake worker principal, durable permission cache or automatic all-account delegation.

Worker entrypoint receives **exactly org/account/job ID**. It may do an unlocked scoped metadata locator read of immutable job+authority fields, solely to learn the stored principal and expected bindings. It then invokes `acquire_publication_guard` with a UserSessionPrincipal reconstructed from those stored identities and required_permissions=frozenset({"sync:run"}). The stored row, written by the authenticated create path, is the delegation evidence; an arbitrary queue membership or Python dataclass is not. Trusted service/DB mutation grants must prevent alternate unaudited creation paths. RLS alone does not cryptographically prove who supplied user IDs; the authenticated creation boundary remains essential.

After all user/membership/session/account/credential metadata locks, lock job UPDATE, then current attempt UPDATE, then run/projection rows. Recompare the locked job's complete immutable delegation with the locator snapshot and compare state/version/current-token/deadline. Claim writes new attempt+job+audit in that same transaction; no network. After commit, resolve the exact captured fetch authority through paired envelope outside publication locks, assert equality, then invoke only registered read adapter. This cannot eliminate the normal time interval between authorization and remote read, but stale/revoked results cannot publish.

Publication starts a fresh READ COMMITTED transaction, repeats user guard and stored job/attempt locks, rechecks lease and authority deadline via `clock_timestamp()` after domain lock waits, then performs Orders atomic publication+job success+attempt success+audit. Extend the transaction-bound final validation hook to check job/attempt token/state/deadline in addition to the existing user/session/account/credential checks. Distinguish expected in-transaction final succeeded state from a competing terminal state; finalization must permit its own exactly fenced success while rejecting tampering/reuse. A wrapper calling only the user guard once does not prove job revocation or lease fencing through commit.

Revocation/cancellation wins according to lock acquisition/commit order. Explicit initiator revocation follows auth metadata before job lock; it never takes job then later user/account locks. After live authority has already been revoked, a dedicated trusted **state-closing-only** recovery command may mark the stored job revoked/expired/blocked without requiring revoked user authority to become valid again. It cannot publish, fetch, renew, claim, change bindings or grant access; lock job/attempt only and acquire no lower-order auth/account locks afterward, storing only an observed allowlisted terminal reason. A failed authority check must never be converted into permission to continue work. State can remain denied pending this housekeeping without weakening the guard.

Job reads/status and idempotent API replay still require fresh authenticated user-session and allowed-account scope with fixed `cabinet:read`; exposing another user's job or command content is not implied by knowing its ID. Initial proposed visibility is initiating membership only; broader organization/operator management requires an explicit separate policy. Revocation of publication permission prevents execution even if basic status read remains permitted.

## Enqueue, retry and recovery without an outbox

Commit job/authority/audit first. Only afterward send locator `(organization_id, marketplace_account_id, job_id)` through the existing trusted Celery task. Never enqueue before commit, or place request bytes/actor/session/permission/binding/lease-token/provider error into args, kwargs or result backend. Return bounded status and job ID; callers obtain authorized detail through DB-backed status read.

No outbox is introduced. Therefore a crash between DB commit and broker send may leave a durable queued job undispatched. Preserve that limitation explicitly: authenticated replay/redelivery of the same job and a bounded trusted recovery command selecting existing authorized queued jobs can retry delivery, with DB claim/CAS suppressing duplicates. Such recovery may deliver already authorized rows only; it creates no new jobs or authority. No recurring scanner/beat schedule is activated by this proposal. An owner may later approve a recovery schedule; do not hide the delivery gap or call the handoff exactly-once.

Duplicate delivery while running cannot claim a second attempt. After uncertain claim/success commit, inspect the stored job/attempt using the same IDs; if succeeded, acknowledge without fetch. Expired running claim can only be reclaimed by CAS plus audit, within attempt budget/deadline. A read fetch may execute more than once after crash or lease expiry, but only the current fenced attempt may publish. Existing Orders manifests/run keys/snapshot evidence remain the domain authority; this mechanism does not permit provider POST retries, suppress price/send dispatch markers or promise external-effect exactly-once.

## Exact local decisions versus remaining policy/semantic gates

Ready local architectural decisions: four bounded relations; single-account/session-bound delegation; fixed operation/permission; UUID idempotency; safe typed byte contract; metadata-only authorities; immutable history, versions and CAS; exact current attempt fencing; enum/audit matrices; existing guard composition; safe terminalization; explicit no-outbox redelivery gap; default-off task registration/activation boundary. SQL widths/indexes/triggers/FKs and policy-field physical representation are T1 choices. No default production durations, concurrency, abuse limits or retries are chosen.

Still requiring owner/domain resolution before production or operation activation:

1. T3 has supplied material selectors and exact per-attempt identity in `2f0422076ac0b365d2e97432b9ce57d6aa6b350f`; these are incorporated above. Provider-complete snapshot/window semantics and production handler registration remain unproven. Store no invented source snapshot and never reinterpret a partial page as complete publication.
2. Owner-approved authority lifetime, finite attempt/lease/backoff policy and abuse/concurrency controls; audit/job/result retention and deletion procedure. Local synthetic policy fixtures are not production approval. No policy means production job creation stays unavailable.
3. Whether session logout/expiry should cancel queued work. **Recommended and proposed here: yes**, required for reuse of the existing session guard and user's explicit session-revocation requirement. Detached continuation is a different delegation contract, not a silent exception.
4. Actual read adapter capability and exact credential dependency chain. No bearer-only browser job and no automatic refresh/rotation substitution are included. A source requiring unrecorded credential renewal must get an explicit new authority contract.
5. Broker dispatch/recovery operational activation and ownership; authenticated status visibility wider than the initiating membership if desired. Neither changes the DB delegation proof by itself.

## Required eventual proof and limits

Test the authenticated create/duplicate/replay boundary, forbidden queue claims, missing stored job, cross-org/account/session scope, immutable bytes/bindings, two-session claim and publication fences, expiry during every wait/final flush, user/session/membership/account/credential revoke in both winner orders, same-ID generation rotation, job cancel during fetch, terminal/readback after uncertain commit, lost enqueue and duplicate redelivery, abandoned attempt budget, audit failure rollback and runtime ACL/append-only/deletion constraints. Use synthetic source reads only. Ensure inherited legacy org fallback, scenario/force and Celery auto-retry cannot invoke the new handler.

No claim is made that any of these tables/APIs/tests exists or has passed. A critical pass checked principal provenance, secret leakage, queue-as-authority, timing/fencing, immutable source-run replay, revoked-user housekeeping, delivery-loss gap, and external-effect retry separation. The proposal is limited to user-initiated Orders authority; other domain writers and autonomous scheduling remain outside it.
