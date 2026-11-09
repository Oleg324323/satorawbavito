# Review sender/executor authority — IMPLEMENTED / UNVERIFIED

## Actual T4 consumption follow-up: sealed read intent and queued cancellation

Source-only addition on base9960548b47021880f3a511d09facd508c9c7270c, consuming
the actual T4 composition handoff and send_service.py call sites at
a13a6f4ab84e210c86eca798d3311634280d408e. No tests, imports, compile, lint,
review/critic, PG/Redis or provider gates run. Original acceptance is still pending.

`ReviewReadAuthority.intent` now contains the exact immutable `ReviewSendIntent`
from the SAME locked command snapshot used by the final short capture_read root.
Its canonical request bytes, request SHA, text/binding checksum and scoped
review/draft/decision/command IDs are retained. T4 can decode the existing
canonical request for externalReviewId and use intent.text_checksum when routing
the authorized provider GET; no unguarded lookup, process cache or HTTP routing
input is needed. Construction requires keyword `intent` in addition to the
existing private mint arguments. Ordinary callers still use capture_read and
must not construct this capability. publish compares the newly locked command's
intent to the captured value before calling T4, and the final publication fence
repeats that comparison after flush. Frozen/redacted/non-serializable behavior,
same operator/session/read credential and one-use publication remain unchanged.

New exact user entrypoint:

```python
commands.cancel(
    authenticated_actor=real_current_actor,
    locator=scoped_command_locator,
    expected=exact_expected_state,
    participant=t4_cancel,
)
```

`t4_cancel(session, handle)` first calls
`handle.require_participation(session, ReviewAction.CANCEL)`. It uses the actual
ReviewSendRepository on `session.connection()` with the exact historical account
binding from `handle.capture.account`; its transition arguments are:

```python
repository.transition(
    command_id=handle.expected.locator.command_id,
    expected_version=handle.expected.version,
    expected_attempt_id=None,
    lease_token=None,
    event_kind="send.cancelled",
    actor_kind="membership",
    actor_membership_id=handle.principal.membership_id,
    reason="USER_CANCELLED",
)
```

T1 freshly authenticates the ORIGINAL creator user AND membership against the
immutable authority/command, requiring current active identity/membership,
reviews:send, a genuine current live session, and exact allowed connected account
external/ref binding. That user's new current session may cancel after the
original login/deadline/credential expires. Cancellation does not check/revive
the old session, credential or approver and cannot transfer to another operator.
The original user/member comparison is repeated by the final handle after flush.

Only exact queued expected version with no attempt is admitted. T4 performs the
existing0074 audit-first transition; T1 seals one version increment, cancelled,
USER_CANCELLED, completion time, no attempt and no result evidence, plus the
membership-authored send.cancelled audit. The unchanged original intent/authority
is checked too. The public return is `ReviewReadback` only after physical commit.
Terminal, repeated or stale requests raise REVIEW_CONFLICT before the participant;
they never create another event/authority or authorize a send. Other creator
user/membership is REVIEW_ACCESS_DENIED; normal live guard denial retains the
existing REVIEW_AUTHORITY_DENIED mapping. No cancellation was added to executor
initiation/closing action sets. Existing exact ReviewPublicationHandle root
registration suffices; publication_guard/store/schema/grants were not changed.

Deferred central cases for this addition: exact captured provider target/text
after origin expiry, intent substitution at publish, current read credential or
account rebind, original user's new-session cancel, other-user/member denial,
lost/revoked current permission/session while waiting, queued claim/cancel races,
stale/terminal/repeated calls, wrong audit actor/reason, attempt/evidence injection
and final-root sabotage. No notification root or additional policy was introduced.

Source-only package. Tests, test authoring, imports, compilation, lint, SQL/PG,
Redis, intermediate review and critic were NOT RUN per the explicit source-first
order. Final centralized acceptance remains required. No provider/network,
production, real credentials, role-script execution, registration, flags, push,
deployment, role provisioning or policy defaults. This is not full Reviews send
completion or permission to activate a worker.

Dispatch source b70f2d2afdd545005b59b65d7a40c66595b4bfcd; existing dedicated resolver
from T2 origin00961055428365f83d7dd05bd07f46786709c975, schema
a21a1cf563d3e54b347386ab7549aa5d347dabc9. Read full actual0074 and resolver handoffs,
0071 decision columns, actual0074 authority/transition/deferred protocols and
existing PublicationGuard/worker_identity/credential_store interfaces. T4 pure
decision/recovery/source handoff read at8d29886b7cf7e276d8201f8fe2cefe3d1defe812.
During implementation consumed actual T4 repository and full implementation
handoff atdc6a72dc89553f0014e977c45a5a99c42486133d; no T4 module imported/copied.
Unrelated controller/Avito commits in the same branch are preserved.

## Exact imports and construction

```python
from app.platform.integrations.review_job_authority import (
    ReviewJobCommands, ReviewJobExecutor, ReviewReconciliation,
    ReviewPublicationHandle, ReviewClosingHandle,
)
from app.platform.integrations.review_job_contract import (
    ReviewJobLocator, ReviewSendIntent, ReviewAuthorityPolicy,
    ReviewExpectedState, ReviewAuthorityCapture, ReviewCreated, ReviewReadback,
    ReviewAction, ReviewReadAuthority, CommittedReviewDispatch,
    ReviewJobError, ReviewReadbackRequired,
)
from app.platform.integrations.credential_store import make_executor_credential_resolver

resolver = make_executor_credential_resolver(
    session_factory=executor_session_factory, keyring_loader=keyring_loader,
    identity=executor_identity,
)
commands = ReviewJobCommands(session_factory=api_session_factory)
executor = ReviewJobExecutor(
    executor_session_factory=executor_session_factory, identity=executor_identity,
    policy=explicit_review_policy, credential_resolver=resolver,
)
reconciliation = ReviewReconciliation(
    session_factory=api_session_factory, credential_resolver=explicit_paired_user_resolver,
)
```

All factories/loaders/identities/policies/resolvers above are required trusted
bootstrap dependencies, not configured defaults or HTTP/broker values. Executor
requires the exact existing private concrete `_ExecutorCredentialResolver`
returned by `make_executor_credential_resolver`, and matching ExecutorRoleIdentity.
Each actual executor connection verifies session_user=current_user=dedicated
login with the existing concrete catalog checks. Its resolver independently
verifies the physical login before key/ciphertext reads on that same resolver
root. No global API-pool resolver fallback and no second encrypted store.
User reconciliation explicitly injects the existing paired user store callable;
it must return exact ResolvedCredentialForFetch, never an independently fetched
account snapshot. Ordinary API key/pool bootstrap remains its owner's obligation.

## Exact values

- `ReviewJobLocator(organization_id, marketplace_account_id, marketplace, command_id)`.
  Scope IDs are positive INT4, marketplace wb|avito, UUID nonzero.
- `ReviewSendIntent(locator, review_id, draft_id, draft_revision, decision_id,
  idempotency_key, request_payload, request_checksum, binding_checksum, text_checksum)`.
  Payload is the unchanged canonical review-send-request-v1 bytes, with matching
  SHA. T4 validates external ID/canonical intent/domain bindings; T1 does not
  introduce another request encoder. A retry candidate command UUID does not
  replace an existing idempotent command; result contains the original locator.
- `ReviewAuthorityPolicy(reference, version, lease_seconds)`: explicit safe label,
  positive exact integers; no assigned values/defaults. `authority_expires_at`
  is separately required, future and bounded by sender session/credential expiry.
- `ReviewExpectedState(locator, version, state, attempt_id, lease_token,
  attempt_command_version, attempt_state, dispatched_at)`: full scoped CAS fence.
  SQL NUMERIC versions normalize to exact Python int without BIGINT narrowing.
  It is NOT executor authentication. Queue carries scoped command ID/version
  only; retrieve fresh scoped expected state through executor.readback.
- `ReviewAuthorityCapture(locator, principal, account, credential, policy,
  authority_expires_at)` contains existing strict UserSessionPrincipal,
  ExpectedAccountBinding and ExpectedCredential; no credential payload.
- `ReviewCreated(locator, review_id, created_at, created_audit_event_id)` is the
  immutable original creation result, including on replay after state changes.
- `ReviewReadback(expected, lease_expires_at, result_evidence_id, reason_code)`
  is private scoped control metadata. Do not serialize it wholesale to HTTP/broker.

All authority/CAS/dispatch/read values and handles have redacted repr/str and
reject copy/deepcopy/pickle. SQL rows remain private immutable metadata mappings.
The existing paired wrapper alone provides explicit secret access for trusted
provider code; no ORM ciphertext or raw bearer is returned by a shared handle.

## T4 participation and physical root

Every mutation participant is the trusted server callback `(session, handle)`.
It calls `handle.require_participation(session, ReviewAction.EXACT_ACTION)` before
its SQL, constructs ReviewSendRepository on `session.connection()` with T4's
actual ReviewBindingDescriptor, performs its domain operation and returns.
The shared owner ignores callback return values, seals actual stored results,
revalidates after flush and commits; provider I/O must remain outside this root.
No callback may commit, roll back, open a savepoint, swap a Connection, install a
later before_commit listener, perform provider I/O or catch/continue fence errors.

Publication handles expose `.action`, `.expected` (None for creation), `.principal`,
`.approver_membership_id` (None on reconciliation/readback), `.intent`, `.capture`,
`.read_authority` (only reconciliation) and `.authority_values` (creation only).
Closing handles expose only `.action`, `.expected`, `require_participation` and
`revalidate_before_write`; they do not impersonate PublicationGuard or a user.

Actual creation call:
`commands.create(authenticated_actor=real_actor, intent=typed_intent,
policy=explicit_policy, authority_expires_at=deadline, participant=t4_create)`.
Inside t4_create use actual `ReviewSendRepository.create` with command/review/
idempotency IDs and canonical decoded intent, `actor_membership_id=handle.principal.membership_id`,
`authority=handle.authority_values`. This dict has exactly the15 actual0074
authority fields excluding owner/command: origin_user_id, origin_membership_id,
origin_session_id, account_binding_schema_version, expected_external_account_id,
expected_credential_ref, credential_id, generation, credential_kind,
payload_schema_version, credential_expires_at, policy_reference, policy_version,
lease_seconds, authority_expires_at.
T4 owns audit→command→authority→ready insertion using DB RETURNING. T1 does NOT
insert authority again. Final handle compares actual command/authority rows to
captured origin/account/credential/policy/deadline, not callback assertions.
Replay freshly authenticates/scopes sender and checks creator, exact intent and
historical binding before new approver/credential/deadline eligibility. T4
participant is not called and no new send opportunity is returned for replay.

Creation/initiation derive approver from exact scoped immutable approved decision.
Lock order is ALL involved users sorted by user_id, ALL memberships sorted by ID,
sender session, account FOR UPDATE, credential metadata FOR SHARE, then command/
attempt control rows. Same sender/approver collapses locks and requires both
reviews:send and reviews:approve. Approver needs current active user/member,
permission and account scope, not a historical login. Sender must have the exact
real live session. Final checks revalidate these same locked rows; no additional
member/user can be acquired after account. Existing public one-principal guard
signature, Orders/repricer/token/Production contracts remain strict.

T4 must still validate current source, selected policy epoch, workflow head,
current draft/decision and answerability under the retained account serialization
before each new creation/pre-marker operation. These predicates are not copied
into T1. T4's immutable source/policy/draft/decision queries use SELECT under the
account lock; inert grants deliberately do not allow FOR UPDATE/FOR SHARE on
those history/source tables. T4 owns authentic evidence and correct canonical
payload construction, including Unicode14 mismatch fail-closed behavior.

Root factories must return unused clean Engine-bound PostgreSQL READ COMMITTED
Sessions. Joined Connections, AUTOCOMMIT, savepoints, pending ORM work and roots
already in use are rejected. Review handles capture both the concrete Connection
and physical transaction; existing final-listener/pending-second-flush/ended-root
protections remain. Participant and fence failures poison the root even if caught.
Unknown commit or cleanup outcome raises ReviewReadbackRequired with scoped
locator. No automatic repeat of POST or marker clearing.

## Executor and reconciliation actions

All worker mutation calls use keyword `locator, expected, participant`:
`claim`→CLAIM, `renew`→RENEW, `reclaim`→RECLAIM. `mark_dispatch`→DISPATCH adds
`resolved_credential` and returns CommittedReviewDispatch only after physical
commit. `resolve_fetch(locator,expected)` has no callback; it checks origin,
approver, exact captured generation/policy/deadline/lease before and after paired
credential resolution outside locks. No OAuth exchange or new-generation POST.
`before_provider_io(dispatch,resolved_credential)` consumes the one-use committed
object before another live physical check, immediately before T4's POST. Every
failure/crash consumes it. A recovered durable marker cannot mint this object.
The distributed gap between final DB check and remote request remains explicit.

Separate concrete executor closing calls:
`block_before_dispatch`→BLOCK only queued or unmarked claimed→blocked, with T4's
proven safe reason; `close_ack`→ACK only live dispatched lease→sent/conflict using
exact dispatch_ack; `mark_ambiguous`→AMBIGUOUS only marked leased→ambiguous;
`append_ack`→APPEND_ACK only immutable scoped dispatch_ack evidence, no lifecycle
change even after expiry; `readback(locator)`→READBACK has no participant/writes.
These do not require resurrecting sender/approver authority and cannot fetch,
claim, renew, reset a marker or authorize POST. Immutable marker, exact attempt/
token and version are retained and sealed. Direct ACK live lease is checked again
at final commit; expired ACK can only be retained as ACK.

`reconciliation.capture_read(authenticated_actor, locator, expected)` requires
BOTH reviews:read and reviews:send for a fresh actual operator/session and exact
historical account external/ref. It authorizes before paired resolution and then
captures exact current credential generation, random read_id and DB started_at
under a second short current-user root. Old creator revocation is irrelevant.
Returned ReviewReadAuthority exposes locator/expected/principal/account/credential/
read_id/started_at/resolved_credential for trusted T4 GET/evidence adapter only.
After actual provider GET outside locks call
`reconciliation.publish(authenticated_actor, read_authority, participant)`.
It consumes the capture once and checks the SAME current operator/session/read
credential plus exact old attempt/version/marker under a new root. Final evidence
must be one NEW reconciliation_read with the captured UUID/start; old lease stays
expired. T4 derives scoped answer identity/checksum and outcome from actual read.
Exact/different may close sent/conflict as membership. Incomplete may append the
read while leaving lifecycle unchanged; actual0074 only permits worker actors
for send.ambiguous, so use separate executor.mark_ambiguous if still leased.
An existing ambiguous row never repeats send.ambiguous. No read is fabricated
from a late ACK or a closing handle.

## Inert grants, errors and deferred acceptance

`backend/ops/review-executor-grants.sql` validates provisioned distinct role
identities with the same concrete worker flags/membership/ownership checks. It
grants narrowly scoped metadata SELECT, encrypted-row SELECT required by the
existing paired store, real timestamp UPDATE capabilities needed for row locks,
Review control-column UPDATE and attempt/audit/evidence/ready/event INSERT.
No command/authority INSERT for executor, immutable/source mutation, receipts,
permission/session-expiry/credential-payload UPDATE, DELETE/TRUNCATE, key/verifier,
role provisioning or default-privilege change. Pure scalar/codec helper EXECUTE
is explicit. The file was not executed; real role/key custody remains an owner
decision. No new operational role names/TTL/provider policies were chosen.

Allowlisted errors: REVIEW_CONTRACT_INVALID, REVIEW_ACCESS_DENIED,
REVIEW_AUTHORITY_DENIED, REVIEW_NOT_FOUND, REVIEW_CONFLICT, REVIEW_FENCE_INVALID,
REVIEW_PERSISTENCE_FAILED, REVIEW_READBACK_REQUIRED. SQL/provider messages and
parameters are not exposed. Publication/login denial maps to authority denied;
unknown participant error maps to persistence failed. Root readback ambiguity
must be surfaced to T4 and resolved via durable lookup, never blind send retry.

Pending central gates: real T4 composition/imports, same/different/swapped
approvers, lock races, current-user/member/session revocation and expiry,
generation changes and post-origin-revoke fresh read, false login/SET ROLE/GUC,
deadlines after waits, once-only dispatch/lost commit/restart/CAS, physical root
sabotage, live ACK versus expired fresh read, redaction/serialization, actual0074
witness/Unicode/RLS/grants and all legacy guard/Orders/repricer regressions.
No test or review claim is implied by source delivery.
