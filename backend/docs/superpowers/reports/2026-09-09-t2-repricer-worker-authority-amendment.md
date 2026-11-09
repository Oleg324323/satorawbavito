# T2 → T1: bounded repricer worker-authority amendment

Status: dependency request, **not an accepted worker permission policy or activation**.
Local evidence only. Existing Orders-only proposal cannot authorize a price mutation.
Accepted approvals schema: `dbf8d31d9fc0b9035b9c8a84727e70713956afe0` /0066.
Dormant scoped participant and committed user commands:
`bd3bb60a0baadc8d7683c8efd8bf4eff444c9643`.

## Existing boundaries and missing authority

`UserApprovalCommands.create_intent/claim/reject/block` owns a root transaction,
requires an authenticated `UserSessionPrincipal`, exact WB account binding and
service-fixed `price:send`. It returns after physical commit. T1 confirmed this
composition; it is not worker authority and exposes no worker/provider commands.

`ApprovalTransaction.reserve_attempt/mark_dispatch/record_attempt_outcome` currently
participates in a caller-owned transaction only. Values are uncommitted, its
membership check is not authentication, and its worker audit classification does
not authenticate a worker. No current router, job or provider uses this participant.

T1 must supply a reviewed trusted worker/job authority bound to durable job identity
and scope. A queue-provided membership ID, a frozen `AuthenticatedApprovalActor`,
or fabricating `UserSessionPrincipal` from historical IDs is insufficient.
The queue carries internal org/account/job IDs only, no credentials or permission list.

## Exact binding required at domain entrypoints

Common binding: internal positive INT4 `organization_id`, `marketplace_account_id`,
logical exact TEXT `approval_id`, immutable `action_key` and `request_checksum`.
Resolve by the full scope, never by UUID alone. `approval_row_id` is a physical UUID4
locator, not a replacement for the logical identity or account ownership.
The provider target is WB plus the exact expected external account identity and
accepted credential ID/generation/schema version, not whichever account is current.

| Entrypoint | Additional frozen predicates | Authority required |
| --- | --- | --- |
| User create/claim/reject/block | canonical request bytes for create; `expected_version` for transition; authenticated initiating membership | Current user/session/account checks, fixed `price:send`; implemented user boundary only |
| Future scheduled create/claim | same immutable request and expected approval version; durable originating job and initiator evidence | Accepted initiator-capable worker authority, current permission/account checks; no implicit system bypass |
| `reserve_attempt` | applying approval, claim version and claimant; one lifetime attempt UUID4 and its action/checksum | Initiator-capable job authority before reservation; return alone never authorizes send |
| `mark_dispatch` | exact attempt UUID4, reserved status, attempt version0, approval applying/claim version, claimant and deterministic dispatch key | Initiator-capable job authority plus exact usable WB credential binding before marker; caller commits marker before I/O |
| `record_attempt_outcome` | exact attempt UUID4, dispatch key/action/checksum, expected approval and attempt versions, allowlisted outcome, upload ID/result when applied | Separate state-closing authority bound to the already owned durable job/attempt; not new send authority |
| Future reconciliation | same owned attempt identity and upload ID if known; dispatched/ambiguous observation; no resend | State-closing authority for local facts; separately authorized valid credential for provider-history GET |

Versions are exact integers (approval/claim versions are PostgreSQL NUMERIC, not
silently narrowed floats/BIGINT). `dispatch_key` is already frozen by the pure
domain from org/account/approval/action/attempt identity; worker envelope must
bind the same value, not introduce a competing action key. Replays must compare
the immutable payload as well as the key. Expected versions come from durable rows
or a verified command, not “latest version” substitution after conflict.

## Revocation and uncertainty: safety floor, not invented roles

Initiation must fail closed after session/permission/account/credential revocation
or a changed expected binding. An old job cannot refresh itself into unrelated
authority or choose a newer credential merely to continue sending.

| Point of revocation/crash | Safe effect boundary | State-closing requirement for T1 design |
| --- | --- | --- |
| Before intent | No intent/send under denied authority | No action to recover |
| Intent committed, before marker | No provider POST after denied authority | A verified worker may record known local non-dispatch failure only under separately accepted closing authority; otherwise leave blocked recovery, not fabricate authority |
| Marker committed, before/around POST | Marker is not reusable send permission; revoked authority must not initiate a fresh POST | If non-dispatch cannot be proved, classify ambiguous; no automatic resend |
| Provider may have accepted, before local outcome commit | Never convert uncertainty to known rejection | Persist safe ambiguous observation using owned state-closing authority; a known upload ID needs a separate durable receipt/evidence contract, currently pending |
| Result committed, before ACK / duplicate delivery | Closed approval/attempt prevents another POST | Read/ACK the existing result under scoped worker authority; do not reopen/reclaim |
| Credential revoked while reconciliation needed | Do not use revoked credentials or borrow another tenant/account | Local ambiguity can remain; provider-history read waits for explicitly authorized matching-account read authority |

Revocation must stop *new external effects*, not erase already observed provider
facts. T1's accepted design needs to distinguish these two authorities so recovery
does not impersonate a revoked user. This document requests that distinction; it
does not grant a service role, extend a session, approve any TTL/lease duration or
define a retry policy. Until accepted, all worker entrypoints remain unwired.

`ambiguous` is terminal in the current domain and0066. Reconciliation may append
evidence but **cannot** silently call the existing API to turn ambiguous into applied
or create another attempt. Any future reconciliation state extension requires a
separately reviewed domain/schema change; proof of provider nonacceptance is not
inferred from timeout, absent upload ID or empty history page.

Current `ApplyOutcome` and0066 forbid `wb_upload_id` on ambiguous. Therefore the
existing `record_attempt_outcome` cannot retain a known upload ID with that status.
A scoped append-only provider receipt/evidence boundary is a concrete additional
T1 schema/service dependency; it must bind the exact attempt/dispatch identity.
Do not discard a known receipt, hide it in error text, or weaken the current
outcome invariants. No such receipt persistence exists in this slice.

## Minimum shared contract requested, and proof gates

1. Trusted resolver result identifies its action class (initiate vs close vs read),
   exact durable job, org/account and preserved initiator evidence. Action sets are
   service-fixed, not selected by the queue. Define the owner of durable job-to-
   approval/attempt binding and whether0066 audit needs a composite job reference.
2. Transaction-bound initiation validation composes accepted lock ordering
   user → membership → session → account → credential → domain without recreating
   a live user session. State-closing validation uses its reviewed authority,
   not a blanket skip of permission checks.
3. State what authenticated executor may close after initiator revocation and how
   job cancellation, credential generation changes and duplicate delivery are
   distinguished. No guessed service-account ID, permission, TTL or expiry default.
4. Tests: crossorg; two accounts; forged/reused job/attempt/action/dispatch IDs;
   changed payload under key; initiator revoke before/after marker; expired or
   changed credential; duplicate delivery; outcome after revoke; failed commit;
   crash after possible acceptance. Zero-row CAS causes zero fake provider calls.

External exactly-once is not promised: the local marker is a conservative durable
fence, not a provider idempotency guarantee. Final authority check cannot eliminate
the distributed revocation/POST gap; cancellation semantics at that boundary need
an explicit accepted contract. No scheduler, real provider request, schema, config,
frontend or current repricer change is part of this amendment.

## Existing identity evidence and bounded ownership split

Rechecked after T1 requested the exact job table/PK/type/lifecycle/trust source.
There is **no accepted price-worker job/principal contract** in this branch or the
inspected T1 proposal. The original T2 assignment explicitly says obtain the resolver
from T1; T1 has confirmed the worker contract remains pending. This assigns the
resolver to T1, not automatically all domain job-table/lifecycle design. T2 can
independently propose that domain table for T1 acceptance; trusted executor
resolution and activation require the missing shared contract.

| Candidate evidence | Why it does not supply the required reference |
| --- | --- |
| `3477fb88a4b2120a5801401b8860338082bf72bb:backend/docs/superpowers/reports/2026-09-09-t1-user-job-authority-proposal.md` (also latest modification of that file in inspected T1 worktree) | Proposal-only `user_orders_jobs`, UUID4, fixed orders.sync.v1/sync:run; explicitly excludes price/send mutation and detached service authority. Its claimed/lease/read-retry lifecycle cannot authorize repricer resend or post-revocation receipt closing |
| `app/control_plane/orm.py:41–63` `cp_sync_jobs` | Existing INTEGER job_id, generic sync status fields; declared model does not establish the needed price org/account/initiator/attempt delegation. A convenient existing integer PK is not trusted job authority |
| `app/repricer_sprint_b.py:196–217,769,796,896–898` | `ApplyJobView.jobId` is text `job_<uuidhex>` stored in `_MEMORY.jobs`; not a durable scoped PostgreSQL job FK or executor principal |
| Accepted0066 `wb_repricer_price_apply_attempts` | Real scoped UUID4 attempt, immutable action/request/dispatch keys and one-lifetime-attempt state exist. This is an execution fact, not an accepted job delegation or worker authentication source; do not relabel attempt_id as job_id without an explicit architecture decision |
| `app/platform/integrations/publication_guard.py` | Accepted UserSessionPrincipal guard only. `repricer_worker` in0066 audit and the pure domain is an actor classification, not a trusted worker principal |

Therefore no exact table/PK/FK can honestly be supplied **as already accepted**.
Do not select an arbitrary UUID/string/int job key, use a nullable user, serialize
membership from Celery as authority, infer a service role from an audit enum, or
reuse Orders read-retry semantics for provider POST. The previously requested
marker-before-provider and fresh-root receipt boundary remain mandatory; no FK
alone proves a previous physical commit.

The following proposed domain table removes the missing physical reference without
claiming it already exists or authenticates a worker. T1 accepts/implements the
table and owns the trusted executor resolver. T2 owns its domain binding/consumer.

## Proposed v1 job contract for T1 acceptance (not schema READY)

Use one **immutable job binding per approval**, not a second mutable lifecycle that
duplicates0066. Proposed table `wb_repricing_jobs`; server-generated canonical UUID4
`job_id` PK, additionally UNIQUE(org,account,job_id). UUID4 is the same explicit new
identity convention as0066 attempt/receipt IDs, not a reinterpretation of legacy
`job_<uuidhex>` or cp_sync_jobs INTEGER. Legacy jobs are not auto-imported.

| Required columns | Type / exact constraints |
| --- | --- |
| organization_id, marketplace_account_id | positive INT4; existing composite account/org/WB FK |
| approval_row_id | UUID4; exact composite0066 approval FK; UNIQUE(org,account,approval_row_id), one job per canonical action |
| action_key, request_checksum | lowercase64 SHA256 TEXT; equality to referenced immutable approval; job does not rebuild or own a competing action key |
| operation_kind | TEXT fixed `wb.price_apply.v1`; closed service registry selects it, never the queue |
| initiator_user_id, initiator_session_id | VARCHAR(64) NOT NULL; exact existing user/session identities; session must belong to that user |
| initiator_membership_id | positive INT4 NOT NULL; composite org/member/user FK or equivalent deferred equality to existing IAM rows |
| created_at | finite TIMESTAMPTZ NOT NULL, DB clock |
| created_audit_id | UUID4 NOT NULL, reciprocal deferred creation witness |

No nullable fake initiator, standalone job state/version, retry counter, lease duration,
priority, expiry default, credentials or generic kwargs are proposed. The row captures
the immutable request linkage and authenticated origin; all price status/version and
attempt dispatch/outcome facts continue to belong to0066. Detailed credential/user
delegation metadata needed by T1's authority resolver belongs to that accepted
authority contract, not a made-up permissive blob on this job row.

Creation requires an existing v1 pending approval (or creation of that approval in
the same root), live authenticated principal with fixed price:send and exact account
binding. It writes job+`repricer_job.created` audit atomically; actor_kind membership,
actor_membership_id equals initiator, event references full scoped job/approval,
occurred_at=job.created_at. Unique scoped job creation witness, no orphan/ghost event.
No actor name or raw payload. Replay by the same approval additionally compares exact
initiator user/member/session and immutable keys; matching request returns original
job/audit, conflicting origin/key is conflict. Replay does not reauthorize execution.
The create-command replay lookup follows fresh authenticated price:send/account
checks and precedes the pending-only **new row** guard. Exact existing job replay
may therefore return its original row after applying or terminal status; it neither
creates a new job nor authorizes another execution. No broader status-read permission
is defined by this rule.

Execution lifecycle is derived, not independently writable:

`approval pending → approval applying (claim CAS) → attempt reserved → dispatched
→ applied/failed/ambiguous`; rejection/blocking follows existing approval transitions.
There is at most one0066 attempt for the approval, so job-to-attempt linkage resolves
by full org/account/approval scope. A job record itself never grants another claim,
attempt or resend. Terminal facts cannot reopen. Job-specific cancellation/renewal,
lease reclaim and detached scheduling are **unsupported v1 operations**, not aliases
for approval reject or a new implicit retry policy. Current user commands are not
silently changed to create jobs by this proposal.

V1 actor alignment: job.initiator_membership_id must equal the approval/attempt
claimed_by_membership_id for any claim/reserve/dispatch driven by this job. The
trusted domain command checks the frozen initiator and claim CAS in the same root;
receipt binding must also reject a differently claimed attempt. If another manual
actor claims an approval already bound to this job, execution fails closed; it must
not rewrite the job origin or impersonate either actor. Delegation transfer is an
unsupported separate design. Wiring/cutover needs a regression for job by A versus
manual claim by B and a one-writer fence. This does not change current standalone
user commands or invent new permissions for safe terminal rejection/blocking.

Receipt and receipt audit reference `(org,account,job_id)` using the concrete UUID
column above. Deferred binding also requires job.approval_row_id equals the receipt's
approval and the referenced attempt's approval; action/checksum/dispatch remain exact.
The receipt may arrive after ambiguous because the job row and binding are immutable;
it never changes either approval lifecycle or job-origin evidence. FORCE org/account
RLS, restrictive scoped FKs, immutable job/audit and empty-only downgrade are required.

### Trust source and the remaining T1 resolver boundary

The **origin of the job record** is trusted authenticated creation plus enforced
atomic audit and restricted writes; neither row existence nor job ID authenticates
an executor. Queue envelope contains org/account/job IDs only. The trusted registered
worker must obtain T1's execution/closing authority by resolving that stored binding
and the applicable live account/user/credential facts. No UserSessionPrincipal is
fabricated from arbitrary queue fields; no resolver implementation or service identity
is invented here. If T1 requires an additional platform authority row, its concrete
scoped reference must be accepted before activation, not replaced by NULL.

Initiation authority stops new POST after relevant revocation; state-closing authority
must separately permit only owned receipt/outcome evidence when initiation is no
longer valid. Only T1's reviewed resolver supplies that distinction. This proposal
does not grant closing rights merely because actor_kind=repricer_worker or a process
knows job_id. A job cannot extend expired/revoked authority or substitute credentials.
No owner-approved authority policy means production job execution remains disabled.

Provider call sequence stays: committed approval/job → claim/reserve → physical
marker commit → authorized POST → fresh-root receipt/outcome publication. Tests must
observe those separate physical roots and zero fake calls on failed CAS/authority;
FKs cannot prove previous commit. Add two-session job creation/replay, foreign
job/approval/attempt binding, forged origin, audit rollback, terminal non-resurrection,
revoke-before-marker and close-after-revoke tests once T1 provides the authority.

This is a complete bounded **domain job-reference proposal**, not the missing trusted
worker implementation. It allows T1 to choose/accept the exact FK target without
waiting for a fabricated principal. No shared code/schema or production was changed.
