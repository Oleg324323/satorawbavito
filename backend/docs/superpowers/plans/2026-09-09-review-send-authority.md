# Review shared initiation and closing authority

Binding inputs: T4 8d29886b7cf7e276d8201f8fe2cefe3d1defe812 follow-up in
2026-09-09-t4-local-services-send-in-app-handoff.md, exact b17638f matrices,
2026-09-09-review-send-in-app-storage-design.md and its actual committed schema
(SHA/columns supplied on dispatch). Existing concrete worker_identity and repricer
root-fence source are precedents, not permission to copy T2 transitions.

Implementation first, final tests/review later. No policy/role/lease defaults,
providers/production/real credentials/.env/PG/Redis/actions/flags/push/deploy.
T4 owns all Review lifecycle/evidence/adapter/worker/HTTP and notification services.

## Task 1: Implement Review-specific shared authority primitives

Read full binding documents and actual schema/parent0071/guard/login/credential
interfaces supplied on dispatch. Read actual T4 decision_contract and send_recovery
through git show, without importing absent T4 code or duplicating its predicates.
Do not read the whole plan; this task is the whole implementation package.

Allowed exact six paths:

- backend/app/platform/integrations/publication_guard.py, only strict Review
  secondary-membership and exact Review handle/root registrations, preserving
  all user/Orders/repricer/token contracts present by dispatch.
- New backend/app/platform/integrations/review_job_contract.py.
- New backend/app/platform/integrations/review_job_store.py, only scoped immutable
  authority/control-state metadata reads and authority-row creation, not T4 domain
  state transitions, provider evidence construction or a second Reviews repository.
- New backend/app/platform/integrations/review_job_authority.py.
- New backend/ops/review-executor-grants.sql, inert and explicit roles only.
- New backend/docs/superpowers/reports/2026-09-09-t1-review-send-authority-handoff.md.

No DDL renumbering/schema/old migrations, shared profiles/permissions/config,
T4 review_tasks/reviews modules/frontend/serverless, tests or other paths.

### Fixed authority distinctions

1. Authenticated send creation and pre-marker execution require sender
   reviews:send AND the stored decision's CURRENT approver reviews:approve, full
   account access and active users/memberships. Approver is an authorization
   dependency, not another authenticated HTTP principal: do not require the
   approver's historical login to remain live or fabricate a new one. Sender
   does require the genuine current/immutable-origin UserSessionPrincipal.
2. Derive approver member/user from the actual scoped immutable decision row,
   not caller JSON. Preliminary unlocked locator is not proof. Lock all involved
   users in sorted user-ID order, then all memberships sorted, then sender session,
   then canonical account/credential. Same sender/approver collapses duplicate
   locks and requires both permissions. Never acquire another user/member lock
   after account, nor create A→B/B→A inversions. Revalidate those same rows and
   current scope/permissions at final physical commit. Existing one-principal
   public acquire signature remains strict and unchanged; no generic additional
   principal/permission registry or membership-selected privilege injection.
3. T4 still validates actual current source, selected policy epoch, workflow head,
   current draft/decision/answerability via its own locked predicates. T1 locks
   current approver authority but cannot substitute it for these domain checks.
   Exact immutable command authority captures sender origin, connected account,
   credential metadata and explicit finite policy/lease/deadline from schema.
4. Executor initiation requires actual dedicated PostgreSQL login verified by
   existing worker_identity, not SET ROLE, a GUC, queue fields or an actor enum.
   Load stored immutable creator session/credential; no refreshed generation or
   automatic OAuth. Missing live origin/current approver denies claim/renew/mark
   dispatch/fetch. Queue only scoped command ID/version; no session/lease token/
   credential in broker payload. Expected current version/attempt/token is a
   concurrency fence, not authentication. No default executor/API pool fallback.
5. State-closing/evidence after marker uses separate exact executor authority and
   exact scoped command/attempt/token/immutable dispatch marker; it cannot fetch,
   claim, renew, clear a marker or authorize POST. Current initiator revocation
   does not prevent retaining owned evidence. Direct ACK closure live-lease only;
   post-expiry reconciliation requires real fresh read authority, not old ACK.
   A separate narrow pre-marker terminal-closure handle may let the trusted T4
   participant record a proven blocked state, without requiring revoked sender
   resurrection. It grants ONLY terminal closure of the exact queued/claimed
   command, never initiation/fetch. Domain safe reason is derived by T4 from
   current evidence, never an HTTP reason used as authority.
6. Fresh user reconciliation requires BOTH reviews:read and reviews:send for the
   actual current operator and exact account, not reviews:approve or a revived
   original creator session. Capture a newly authorized exact paired read
   credential and DB read-start under a short auth root; final publication checks
   that same current operator/read credential after provider I/O. Historical
   command account external/ref binding must still match; no reading a newly
   rebound external account or silently rerouting historical command. A fresh
   operator may use an explicitly captured current credential generation for this
   NEW authorized read; that never transfers original POST authority to it.
   Worker closing handle alone cannot manufacture such a provider read.

### Physical-root participation, not domain rewrites

Provide typed/redacted exact locator/expected-state/policy/read-authority/handle
objects and allowlisted safe errors. Names and signatures are finalized against
actual schema and published in handoff, not guessed by downstream consumers.
Shared helper construction from real ActorContext loads live membership before
the strict guard; no caller-supplied membership is trusted. Credential lookup
uses only existing paired store with explicitly injected bootstrap factory/pool
contract; wrapper exposes no ORM ciphertext or raw bearer/loggable secret value.

Root owners require clean Engine-bound PostgreSQL READ COMMITTED Session, no
joined Connection/autocommit/savepoints or pending ORM work. User creation must
retain one root through T4 command+authority+audit+ready intent commit. Exact
historical replay precedes new eligibility but still requires fresh authenticated
scoped access; it never emits a new send authorization. Authority row insert and
command metadata must be compared against captured origin/account/credential
under the final handle, not merely rely on a supplied snapshot dataclass.

Execute T4 trusted participant in the root for claim/renew/dispatch/reclaim/closure;
do not copy T4 transition/recovery code. Handle action classes admit only their
specific before→after state/version/attempt/token changes, with schema enforcing
all audit/evidence witnesses. Seal the participant result and revalidate after
flush at final commit. A caught participant/fence error poisons the root. All
existing listener-last/pending-second-flush/nested/ended-root protections remain.
Separate exact closing guard may occupy the same single state slot; it is never
a PublicationGuard/UserSessionPrincipal impersonation or arbitrary callback bus.

Provider I/O takes place outside locks. Only a successful new physical dispatch
commit can mint an in-process one-use dispatch result. Final live check immediately
before actual T4 POST consumes that opportunity, even on failure/crash. Recovered
marker/readback cannot mint another POST. Distributed revocation gap between DB
commit and remote call is explicit. Lost commit result requires durable scoped
readback/ambiguity, never a retry of POST or clearing history.

Reconciliation capture stores exact real actor/credential/account/read UUID/start
metadata, not provider output. T4 trusted adapter establishes scoped answer ID/
checksum and constructs correct evidenceKind; no accepted HTTP verified flag.
Fresh read result publication is a new guarded root with exact attempt/version;
old lease remains expired and no old session is revived. Late ACK may append
immutable evidence only; it cannot be relabeled or reused as fresh read.

### Inert executor privilege artifact

Validate already-provisioned distinct executor/API role identities with the same
concrete checks as worker_identity. Do not create/alter roles/passwords, touch
default privileges or execute the script. Narrow explicit SELECT/control-state
UPDATE/INSERT for actual Review participants only, no source mutation/history
rewrite/DELETE/TRUNCATE or API-login fallback. Required row-lock timestamp UPDATE
on existing identity/account/credential metadata is a disclosed capability, not
a read-only claim. Do not grant user/member identity, permissions, revocation,
credential payload or expiry UPDATE. Exact encrypted-row SELECT needed by the
existing paired resolver is allowed; no plaintext sources/verifiers/master keys.
Immutable Review policy/draft/decision/source lookup uses account serialization
where no UPDATE privilege is appropriate. If T4 needs actual row locks beyond
that, report exact query/column before broadening grants. No free SQL actor proof.

### Delivery and deferred final acceptance

One bounded source commit: `feat: add Review sender and executor authority`.
No amendments/rebase, no provider registration/activation. IMPLEMENTED / UNVERIFIED.
Handoff exact constructors/imports/actions/arguments/result/error types, metadata
and participant ownership, lock order, role/resolver bootstrap obligations and
distinction between execution and closing. Coordinate concrete T4 integration
before claiming full worker behavior; this package is only shared authority.

Final gates after all source packages: same/different approver cases and swapped
user-lock races, approver revoke/restrict/inactive during waiting, sender session
expiry, exact new-generation denial for POST, fresh operator post-origin-revoke
read success, no-read from closing, false role/SET ROLE/GUC denial, captured
deadline/lease afterwait, once-only marker, duplicate queue/version CAS, no raw
secret in errors/repr/broker, physical flush/root sabotage, late ACK versus fresh
read recovery, full actual T4 participant/schema/legacy guard regression. No
production policy, TTL, role mapping or provider semantics inferred from tests.
