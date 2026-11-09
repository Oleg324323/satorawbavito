# Review send and account in-app storage

T1 physical contract from committed T4 requests, not a provider-send rollout.
Read inputs: 8f4a6c4251a31da819e998e5ddc797e1a3c219be storage amendment,
b17638f7d71021c99098af20faf31fd3c6db0588 exact matrices,
ae7db1d9ce33eaa8f7c7eeb41b1883796d403a3b send/in-app codecs and eight literal
vectors, and 8d29886b7cf7e276d8201f8fe2cefe3d1defe812 authority/lease/receipt
clarifications. These supersede the original blanket post-marker live-lease rule.
Existing0071 local Review tables are preserved; no competing local storage.

## Boundary

Only additive PostgreSQL storage, narrow grants and exact handoff in this package.
No runtime domain implementation, workers, provider/parser/queue/routers, frontend,
Telegram/email/destination/preferences registry, production values, retention
cleanup or activation. T4 owns repository, lifecycle/recovery, evidence verification,
source-change/event transaction and user-facing operations. T1 separately supplies
shared multi-membership initiation and concrete executor closing authority.
SQL constraints and actor enums are not authenticated membership/worker proof.
No generic principal, outbox bus or untyped audit/evidence JSON.

Eight new relations:

1. `review_send_commands` — one durable immutable intent and mutable command
   lifecycle, exact scoped idempotency. Immutable owner/review/draft/revision/
   decision refs, operation, client idempotency UUID, request bytes and checksum,
   binding/text checksum, creator membership and created_at. Mutable state,
   positive exact NUMERIC version, current_attempt_id, completed_at,
   result_evidence_id, reason_code and current audit witness. Initial version1;
   each successful transition increments once, failed CAS/replay never does.
2. `review_send_command_authorities` — exactly one immutable scoped command
   authority, real origin user/membership/session, exact connected account binding
   schema1 external ID/ref, exact encrypted credential ID/generation/kind/schema/
   expiry, required explicit policy reference/version, lease_seconds and authority
   deadline. WB wb_api with NULL credential expiry, Avito avito_oauth_access with
   finite expiry; no cached-access substitution or auto-refresh. Capture deadline
   <= originating session and credential expiry when present. No raw credential,
   ciphertext, verifier, key inventory or fake queue-derived session. Counter
   fields use actual referenced parent types, not invented BIGINT version limits.
3. `review_send_attempts` — mutable lifecycle plus immutable audit. Server UUIDv4,
   exact scoped command/review, positive exact sequence, claimed_at and random
   UUIDv4 lease token immutable; state, lease_expires_at, dispatched_at, finished_at,
   evidence and reason constrained below. Unique scoped command/sequence and
   owner/lease token. Command version/current attempt/token is the CAS fence;
   do not invent a second independent retry budget or send lifecycle.
4. `review_answer_evidence` — immutable exact discriminated evidence and canonical
   bytes/checksum. Scoped review/command/attempt, evidence UUID, optional read UUID,
   kind/version, outcome, observed_at/read-start, optional provider-answer exact
   UTF-8 bytes and SHA, verifier label. No raw response, customer text or supplied
   verified boolean. One exact byte identity per evidence UUID and per non-null
   read UUID; no overwrite. UTF-8 BYTEA preserves embedded NUL and Unicode without
   normalization; maximum512 Unicode characters for nonblank provider-answer ID.
5. `review_send_audit` — immutable exact T4 send-event envelope, scoped refs and
   NUMERIC aggregate version; commandId=aggregateId. Typed auxiliary SQL witness
   columns may describe actual attempt/lease transition if necessary, never free
   JSON snapshots or caller-controlled authority. Publish every such column in
   handoff. Exact canonical envelope excludes physical witness-only columns.
6. `review_send_enqueue_intents` — immutable narrow send.ready rows, canonical
   review-enqueue-v1 bytes/checksum, unique owner/command/version/eventKind. Exact
   FK and reciprocal witness to initial send.created or send.reclaimed audit.
   Initial queued and each reclaimed queued version require its intent in the
   same commit. No delivery timestamp/status/lease/ack policy is invented. T4 may
   select current queued matching versions for bounded duplicate-safe delivery;
   the claim CAS, not deleting an intent, establishes ownership. No broker I/O.
7. `notification_in_app_events` — immutable account-scoped reviews events only,
   canonical notification-event-v1 bytes/checksum and fixed projection/dedupe.
   Event UUID/time preserved on replay; changed UUID/time under the same dedupe
   is a conflict, not overwrite. Exact source identity/version and physical FKs
   described below; no system_attention or organization-wide nullable account.
8. `notification_in_app_receipts` — scoped event + recipient membership identity,
   independent read_at/dismissed_at and server NUMERIC version. Init1 with at least
   one timestamp. NULL may become one DB timestamp once; non-null can never
   change or clear, even to an earlier value. Increment exactly once for a real
   row change (one or both fields), no-op retains version. No timestamp ordering
   between read/dismiss. Unique org/event/member, with exact account-scoped FK.

## Ownership, bytes and graph

Every send relation has positive INTEGER organization/account, provider wb|avito
matching actual canonical account through existing composite FK. New notification
physical rows may carry marketplace solely for that FK/source binding; it is NOT
added to notification identity, canonical bytes, HTTP or dedupe. No accountless row.
All relations ENABLE and FORCE RLS using BOTH transaction-local organization and
account context, including no-context/wrong-account INSERT/UPDATE/SELECT/DELETE.
Context values and membership IDs are not authentication. New object owners must
also obey RLS; migration maintenance role privileges remain separate.

Use actual0071 draft/decision/fact/member/session/credential parent keys; never
invent an existing unique index or silently alter historical migrations. Necessary
new child lookup indexes should avoid duplicates/redundant left-prefix coverage.
Immutable source rows and identities cannot be rewritten/deleted/truncated.
No new old-parent indexes merely to manufacture convenient FKs: use existing
scoped composite keys plus explicit row equality validation where necessary.

Command request exactly matches unchanged T4 encode_review_send and actual draft,
decision, fact external identity, text/binding hashes. Preserve UTF-8 bytes,
embedded NUL and exact positive integer NUMERIC versions beyond BIGINT. Use
existing0071 strict UTF-8/canonical scalar helpers, not PostgreSQL JSONB re-encoding.
Source-time compatibility ruling: T4 normalizer uses Python regex Unicode Nd and
Python strip semantics; reviews-normalization-v1 does not pin a Unicode database.
New SQL normalized-ID validation therefore uses literal Unicode14 Nd ranges,
matching current backend/Dockerfile python:3.11 provenance, and exposes exact
review_send_unicode_version() =14.0.0. Never generate ranges from whichever host
interpreter executes Alembic. Static migration remains runnable on supported3.13;
do not add an interpreter-version upgrade ban. New T4 send-writer activation must
compare actual runtime normalization compatibility/version to this SQL marker and
fail closed on mismatch. Existing legacy normalizer/routes are not rewritten or
globally disabled. Full literal/Unicode parity is an unexecuted final gate; this
explicit new platform contract is not a claim an earlier Unicode policy existed.
New serializers have exact signatures and fixed safe errors. All nullable fields
are explicit, floating-point/lossy cast/Unicode or whitespace normalization denied.
Native PostgreSQL NUMERIC representability fails safely, never rounds intent.

All new command/state/attempt mutations require reciprocal immutable audit
witnesses in the same physical commit. No audit-only ghost, orphan command,
unwitnessed lease extension, missing authority or unexplained version gap. Audit
history is contiguous per command, exact before/after state, actual actor/ref
nullability, monotone DB timestamps and final current-state projection. Updates
must pass immediate identity/transition checks plus deferred cross-row checks;
switching tenant/account context cannot hide earlier deferred writes. Evidence
insert may commit independently only for an already dispatched scoped attempt;
it grants no lifecycle success without the corresponding allowed transition.

Partial unique(owner,review,operation) includes queued,leased,ambiguous,sent,conflict.
Blocked/cancelled free a slot for a NEW explicitly authorized intent; they never
restart. Same idempotency key compares creator/exact bytes/binding after fresh
auth and returns original record; completed state is not permission to send again.
No future edit/delete/reopen operation is added to bypass permanent CREATE lock.

## State and time contract

The full 8f4a6c4 and b17638f matrices are binding; implement every state/ref/reason
case and exact T4 send-audit codec. Summary of critical distinctions:

- queued has no current attempt/result/reason/completion; leased has current
  claimed/dispatched attempt, no result/reason/completion. Claim creates server
  UUID/token and a future lease; renewal only live claimed/pre-marker, command
  version+1; dispatch claimed→dispatched while command stays leased/version+1.
- Reclaim only expired undispatched claimed attempt: old attempt abandoned with
  finished_at/reason LEASE_EXPIRED_UNDISPATCHED, command queued clears pointer,
  new ready intent. Never revive/renew old token; later claim creates a new one.
- ambiguous keeps immutable marker/current attempt and RESULT_UNKNOWN; optional
  incomplete evidence only. No completion time or POST retry. Both current
  attempt and command agree on result/reason/state.
- sent/conflict have exact/different evidence respectively, completion time and
  matching current attempt. They are terminal. blocked has no marker/evidence,
  permitted fixed pre-marker reason and optional blocked current attempt.
  cancelled is only queued→cancelled with USER_CANCELLED and no attempt.
- Direct dispatch_ack can close only the exact current dispatched attempt under
  valid closing authority while DBnow<lease expiry. Late ACK may be retained
  immutably, but cannot masquerade as reconciliation_read or close via recovery.
- Reconciliation read starts freshly after dispatch, observed>=read-start and
  <=DBnow. Actual recovery transition requires expired lease (DBnow>=expiry),
  exact current attempt/token/version and fresh read/closing authority. It does
  not renew the expired lease or require the original revoked login to revive.
- ACK read fields NULL, observed>=dispatch and <=DBnow. Incomplete lacks answer
  ID or checksum or both. Exact/different needs both and actual checksum equality/
  inequality to command text. Evidence authenticity is trusted adapter/service
  work, not a caller label or inference from sent request/HTTP200.
- Timestamps finite Python-representable years1..9999, sequence and chronological
  invariants explicit. Production lease duration, deadline/retention values are
  not assigned by migration or sample data. Authority expires at captured bound;
  refresh does not extend it. Post-marker evidence does not erase historical auth.

## In-app source, receipt and authorization boundaries

Only approval_required(draft UUID/revision), send_blocked(command UUID/version),
send_ambiguous(command UUID/version). Fixed titles/details/severity and exact
dedupe are recomputed from existing notification_contract.project_notification.
Approval source FK goes to immutable0071 draft; command source links to immutable
send audit at EXACT historical version with matching event kind, not a mutable
command version that changes later. Validate owner and entity equality. No guessed
system registry, external provider UUID or arbitrary message text.

DDL admits only valid referenced source. T4 service owns creation of source change
plus event in one physical root; schema alone does not prove which Python service
performed it. Do NOT add automatic triggers producing notifications on old local
writers, backfill historical events, or require old0071 writers to know new event
IDs. That would silently change the pre-rollout contract. T4 must implement/test
the new atomic producer before enabling it, retaining exact IDs/time on replay.

Receipt service derives current membership from real auth, authorizes exact account
and every explicit visible UUID, then locks sorted UUIDs and applies all-or-nothing
first-write merge. Invalid one ID rolls back all. No high-water/mark-future/unread/
reopen or GET side-effect. SQL locks/upsert/CAS preserve both first timestamps and
physical version; trusted DB clock, no client-supplied time/version. User preference
is not recipient authorization; no competing preference table or implicit opt-in.

## Privileges and rollback

Migration strips PUBLIC/default leaked table, column, sequence and exact helper
ACLs only on new objects. Trigger functions not directly executable by runtime.
API role gets explicit new relation SELECT/INSERT and only lifecycle/receipt UPDATE
columns required by authenticated domain participants; no identity/payload/history
UPDATE/DELETE/TRUNCATE. This SQL capability does not authenticate a worker; T1
executor-bound handles and T4 trusted services remain required. Do not claim strong
API-vs-worker mutation separation where user reconciliation shares the same SQL
capability. Separate inert Review executor grants follow the runtime authority
package, not invented shared-role provisioning in this schema commit.

Empty-only downgrade locks exactly these tables in stable order, checks unfiltered
history with proper maintenance authority, refuses ANY new row (including hidden
other-tenant/terminal/audit/receipt), explicitly removes only own dependencies and
never CASCADE. Existing0071 data and old ACLs preserved. A deploy rollback cannot
reset a dispatched/ambiguous command or return to an unsafe duplicate POST writer.

## Final gates (after complete implementation)

Run literal SQL parity for all eight T4 vectors (not eight self-generated expected
values), exact mutation/Unicode/NUL/>BIGINT/null tests, full lifecycle and graph
negatives, reciprocal enqueue, no-other-account/tenant/context forced RLS including
owner role and hidden-history downgrade, all first-write receipt races, scope/dedupe
and source-version FKs. Include actual current approver/revocation, executor login,
pre-marker live lease/post-expiry read/late ACK, marker persistence and uncertain
commit readback in combined runtime tests. Empty and production-shaped SYNTHETIC
upgrade→downgrade→upgrade; no real source/credentials/provider/production/Redis.
Schema commit is IMPLEMENTED / UNVERIFIED until those gates, not whole Reviews or
Notifications completion and never production send approval.
