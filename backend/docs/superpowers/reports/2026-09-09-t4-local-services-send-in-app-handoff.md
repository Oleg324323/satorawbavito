# T4 — local Review services and next storage interfaces

One implementation-first package; source commit is the commit containing this
report. No shared router registration, live provider, production, flag activation,
schema invention, legacy removal or CodeRabbit. This is not architecture completion.

## Actual runtime implementation

`app/reviews/local_tables.py` explicitly maps the seven T1 tables, in private
metadata; no reflection/create_all/DDL. `local_repository.py` implements four
frozen commands with exact request/result bytes: policy create/select, prepared
draft publication and approve/reject. `local_service.py` owns an Engine-bound
physical PostgreSQL READ COMMITTED transaction and live UserSessionPrincipal
guard, including final guard/commit. Permissions are reviews:write for authoring,
reviews:approve for decisions and reviews:read for context. Client organization
and membership claims must match freshly loaded authority. Account metadata is
read/locked; credentials are not resolved. Exact account GUC is context, not auth.

Receipt lookup precedes current source/policy/head admission. Same actor, intent
bytes and binding returns original committed result after later head changes;
changed actor/intent/binding cannot replay it. A new action takes account → source
FOR SHARE → policy head → workflow head locks. Captured policy epoch/witness,
source observation/checksum, exact current manual predecessor, generation
uniqueness and both head/revision expectations are checked. New drafts clear old
decisions. Unknown answerability permits local drafts/rejection, not approval.
CAS head + immutable entity + audit + completed receipt commit atomically. Errors
roll back the root. No internal commit or success-shaped storage fallback.
Receipt/audit completion is PostgreSQL clock_timestamp, normalized to UTC.

`local_preparation.py` prepares a visibly synthetic fake answer or explicit manual
edit from captured context; no first-draft manual_edit and no real LLM. It returns
immutable canonical command bytes. Prepare once and retain these bytes for
transport retries: this is publication idempotency, not exactly-once generation
or a durable pre-publication generation job. HTTP does not regenerate on retry.

`local_http.py` is dormant; T1/root must explicitly register its router separately:

- POST `/api/v2/reviews/local/commands`: the frozen local command envelope, with
  every version/revision/expected-version represented as a canonical decimal
  string on HTTP. Organization/account/membership remain bounded JSON integers.
  Service canonicalization converts versions to exact integers; raw body is not
  stored as authority. Success is the frozen result envelope, versions as strings.
- GET `/api/v2/reviews/local/context`: required marketplace_account_id/marketplace,
  optional review_id + external_review_id together. Returns selected policy/head,
  optional source/current draft/current decision/workflow head. No account binding
  descriptor, credentials, sendAllowed or raw audit/receipt/request payload.
- Existing default-off exact Review organization/account selector is enforced.
  No UI switch is enabled by merely registering the dormant router.
- 400 invalid,401 unauthenticated,403 denied,404 missing,409 disabled/conflict/
  changed source/policy/not-answerable,503 configuration/storage failure. Errors
  contain fixed codes only. Success/error responses set Cache-Control:no-store.
  Duplicate/unknown request fields and numeric HTTP versions are rejected; default
  validation responses cannot echo private raw command bodies.

`frontend/src/features/wb-reviews/canonicalLocalReviews.ts` is the dormant typed
consumer: closed shapes, exact organization/account/provider and review identity,
decimal-string arithmetic, matched command/result refs, nullable source fields,
no blind retry, no fake approval, no UI/legacy/send cutover. Context may immediately
become stale; only the guarded mutation determines current eligibility.

## Consumed platform chain, unchanged

T1 d57c544 →54386d4 →26752549244eb8b97086d5a140c6cf5c0438ee82 were cherry-picked
as b3d17a2 →7328693 →4f196cd. 0069/0070 are required parents of0071, not resumption
of deferred Production service work. No T4 edits to their migration/runtime ACLs.
T1 labels0071 implemented/integration acceptance pending. These local tests do not
replace the independent platform501/combined integration acceptance.

## Exact next storage codec interfaces — for T1

New `app/reviews/send_payloads.py` uses existing sorted-key, compact UTF-8 JSON,
ensure_ascii=false, no floats/normalization, canonical UUIDs and six-microsecond
UTC timestamps. All key sets below are closed, all nullable fields explicit.
Owner fields mean organizationId/marketplaceAccountId/marketplace; new codec
owner/member IDs are positive INT4, versions exact positive integers, not BIGINT.
Existing `storage_payloads.encode_review_send` is unchanged.

| Encoder | Exact fields beyond owner |
|---|---|
| encode_review_send (existing) | schemaVersion=review-send-request-v1, operationKind=review.answer.create.v1, externalReviewId,draftId,draftRevision,decisionId,bindingChecksum,textChecksum |
| encode_review_answer_evidence | evidenceVersion=review-answer-evidence-v1,evidenceKind,evidenceId,reviewId,commandId,attemptId,outcome,readId,reconciliationStartedAt,observedAt,providerAnswerId,answerChecksum,verifierVersion |
| encode_review_send_audit | Existing review-audit-v1 envelope and exact b17638f send-event matrix, not local-audit encoder; commandId=aggregateId, scoped refs, explicit nullable before/after/reason/actorMembershipId |
| encode_review_enqueue | schemaVersion=review-enqueue-v1,commandId,commandVersion,eventKind=send.ready |

Evidence discriminator dispatch_ack requires null readId/read-start;
reconciliation_read requires UUID readId and read-start<=observed. Incomplete lacks
at least answerID or checksum; exact/different requires both. Provider answer ID
is exact nonblank UTF-8, at most512 characters (existing recovery predicate), not
normalized. Codec does NOT prove provider ownership, dispatch<=read-start,
observed<=DBnow, outcome-vs-command checksum, scoped FKs or authority. Those are
mandatory trusted service and physical admission checks. No raw response/text.

### Resolved lease/reconciliation wording conflict

The original blanket live-lease wording must NOT make existing e4818e3 recovery
unreachable. Claim obtains a future lease; pre-marker renew/dispatch requires
DBnow<lease_expires_at plus exact current attempt/token/version. There is NO renewal
after marker. `evaluate_recovery` waits while the old lease is alive; after expiry,
post-marker recovery uses exact current attempt/token/version, fresh authorized
read/closing authority and verified reconciliation_read. It does not revive the
old lease, create a new send attempt or resend. Missing evidence stays ambiguous.
Fresh permission/account checks still apply; a token or UUID alone is not auth.

Direct ACK closing is only an authenticated current-attempt result while the
dispatch lease is still live. A late ACK after expiry may append immutable ACK
evidence under valid closing authority, but cannot impersonate reconciliation_read
or bypass recovery fencing to close the command. If it cannot satisfy that
authority it performs no write. Subsequent recovery needs a real fresh authorized
read, not fabricated read timestamps on the old ACK. Existing markers/terminal
states never reset because access, policy epoch or credentials changed.

### Account-scoped in-app codec, not external delivery

New `app/notification_storage_payloads.py`:

- `encode_notification_event`: exact schemaVersion=notification-event-v1,eventId,
  organizationId,marketplaceAccountId,scope=account,producer=reviews,entityId,
  sourceVersion,kind,occurredAt,dedupeKey,title,details,severity. Three kinds only:
  approval_required/send_blocked/send_ambiguous. Fixed display/dedupe recomputed
  using existing notification_contract.project_notification; no arbitrary text.
  Approval references draft UUID/revision; other kinds command UUID/version.
  Physical source FK/kind match and source-change/event atomicity remain mandatory.
  No org-wide system event before a real platform registry; no marketplace field
  invented in existing notification identity (account FK determines it).
- `encode_notification_receipt`: exact schemaVersion=notification-in-app-receipt-v1,
  organizationId,marketplaceAccountId,eventId,recipientMembershipId,readAt,
  dismissedAt. At least one timestamp non-null; both independently first-write
  wins, neither implies/reset the other. No artificial timestamp ordering.
- `merge_notification_receipt`: pure same-owner/event/member merge of validated
  existing row and server-authored action; retains every existing non-null field
  even if a retry supplies an earlier timestamp. This is NOT a concurrent UPSERT
  implementation. Physical writer must lock/upsert atomically and revalidate auth.
- `encode_notification_visible_action`: exact schemaVersion=notification-visible-action-v1,
  organizationId,marketplaceAccountId,recipientMembershipId,eventIds,action=read|dismiss.
  Nonempty unique explicit canonical event UUID list. Byte array order retained;
  service must acquire locks in sorted UUID order and authorize ALL IDs before
  any mutation. No high-water, mark-future, unread or reopen. Recipient membership
  is server-derived live authority, not a trusted client field. Event/recipient
  access must be rechecked on every retry; uniqueness is(org,event,membership).

Canonical producer retries must retain original event ID/time/payload. A fresh
different UUID/time under the same dedupe identity is not evidence of an exact
retry; never overwrite the original row. Source receipt/idempotency must prevent
new event generation before lookup. Receipt timestamps come from the guarded DB
transaction, not client clocks. Source versions and first-action timestamps do
not establish production delivery policies.

Literal vectors: `tests/fixtures/reviews/send-in-app-storage-v1-golden.json`, eight
independently frozen UTF-8 JSON/SHA vectors, >BIGINT versions, exact NUL/Unicode,
ack/read/null distinction, enqueue, fixed in-app event and explicit receipt/list.
No new Telegram/email policy, destination, provider receipt, retention or TTL.

## Verification and honest remaining work

### Follow-up shared authority and physical receipt version clarification

Existing `decision_contract.validate_review_send` at98d1331 requires sender
reviews:send and approver reviews:approve; reviews:send already exists in cabinet
permissions. Explicit service mapping: send.create and pre-marker claim/dispatch
revalidate sender reviews:send plus current approver reviews:approve and all
source/draft/decision/policy-epoch predicates. Ordinary inspection is reviews:read.
User-initiated mutating reconciliation requires BOTH reviews:read and reviews:send
for the fresh current operator/account, not a new approval of an already dispatched
operation. Post-marker closing does not treat changed policy/source eligibility
as permission to cancel, erase the marker or create another send.

An immutable command-authority relation may retain real creator user/membership/
session plus exact credential ID/generation (WB wb_api, Avito avito_oauth_access)
and account binding. No raw credential or fake session constructed from queue
fields. Pre-marker worker execution revalidates this authority and explicit
policy reference/version, leaseSeconds and deadline; no defaults are assigned.
Fresh authorized reconciliation must still be possible after the original creator
session expires/is revoked, using a separate trusted read/closing authority. It
must not rely forever on that old session or its expired lease. Missing closing
authority means no write, not an unrestricted worker path.

The original physical notification receipt `version` requirement remains: init1,
increment once only if a previously null read/dismiss field becomes non-null;
an exact no-op retains version. The new receipt codec intentionally encodes
identity/content/timestamps, not physical CAS metadata; it does not remove the
version column or conditional-write/locking requirements. Caller-supplied visible
actions do not contain a mutable physical version. T1 owns the exact SQL fence.

### Executed gates

Implementation was completed before tests, as directly requested by the user.
Initial local-service/codecs/read gate:91PASS/1FAIL/2warnings9.51s. Isolated diagnostic
reproduced22003 BIGINT overflow3.17s. Actual compiled SQL showed Numeric comparison
with inferred `::BIGINT`; explicit column-typed bind parameters fix that consumer
defect, no schema/policy narrowing. Repeated combined gate:92PASS/2warnings7.92s.
Every allocator-owned database/role was verified absent after cleanup (2+1+2).
No application DB used; OS sandbox allows only local Unix maintenance socket;
all other network denied and sensitive config paths denied.

New codec and adjacent pure suite:287PASS0.23s, all network denied. Frontend10PASS
0.23s and TypeScript build0. Initial frontend runner invocation had conflicting
min/max workers and ran zero tests; matching min/max1 fixed only invocation.
Scoped Ruff and git diff checks passed. Final DB-clock-corrected local service:
17PASS/2warnings4.67s, natural exit0, its additional owned database/role cleanup
verified absent. No active test process, reservation or pending cleanup remains.

Main-agent critical pass checked source/epoch/receipt ordering, ownership, HTTP
integer precision, no audit/private data leakage, deferred Production and absence
of provider actions. The local preflight-critic skill file is unavailable;
CodeRabbit was explicitly excluded. Independent integration review remains root's
gate, not a claim implied by these focused tests.

Remaining: real durable send/attempt/evidence/outbox worker services after T1
storage/authority; atomic in-app event production and receipts after its DDL;
full Review list/history/legacy moderation/prompt parity; durable pre-publication
generation orchestration; actual UI cutover and shared registration; process-
restart/full integration/whole backend acceptance. Existing legacy stays working.
Pure codecs are NOT completion of these services or authorization for a rollout.
