# T1 Review send and account in-app physical storage

Status: **IMPLEMENTED / UNVERIFIED**. Source-only package; no tests, imports,
compilation, lint, reviewers, PostgreSQL/Redis, role-script execution, provider,
network, production, activation or deployment gates were run for this package.
This is neither a Reviews/Notifications completion claim nor send authorization.

Revision `20260909_0074`, predecessor `20260909_0073`. Source predecessor at
dispatch: `d4fa2c6b195abd5063eb3bc2f08d9fca1e619167`, including 0073 follow-ups.
The exact resulting commit SHA is in the controller task report
`.superpowers/sdd/2026-09-09-review-send-in-app-storage/task-1-report.md`.

Changed source paths only:

- `backend/alembic/versions/20260909_0074_review_send_in_app.py`
- `backend/ops/runtime-db-role.sql` (new objects and exact grants only)
- this handoff

Inputs read: full approved storage design; T4 amendment
`8f4a6c4251a31da819e998e5ddc797e1a3c219be`; exact matrices
`b17638f7d71021c99098af20faf31fd3c6db0588`; unchanged send encoder,
send/in-app encoders, notification projection and eight frozen vectors at
`ae7db1d9ce33eaa8f7c7eeb41b1883796d403a3b`; authority/lease/receipt
clarifications at `8d29886b7cf7e276d8201f8fe2cefe3d1defe812`; actual 0071
keys/helpers, credentials/session/account parents and current runtime grants.

## Exact physical relations

Notation: `O = organization_id INTEGER NOT NULL, marketplace_account_id INTEGER
NOT NULL, marketplace TEXT COLLATE "C" NOT NULL`. All eight relations have O;
organization/account are positive INT4, marketplace is wb|avito. Every new TEXT
column uses C collation. `!` means NOT NULL below; unmarked fields are nullable.
Every UUID is nonzero. Every NUMERIC is finite, integral and positive, without
a precision/scale typmod or BIGINT narrowing. Every timestamp is finite and UTC
representable in Python years 1..9999. Checksums are 64 lower-hex characters.
No sequences, generated identity counters, JSON payloads or extra registries.

`review_send_commands`:

```text
O
command_id UUID!, review_id UUID!, draft_id UUID!, draft_revision NUMERIC!, decision_id UUID!
operation_kind TEXT!, idempotency_key UUID!, request_payload BYTEA!, request_checksum TEXT!
binding_checksum TEXT!, text_checksum TEXT!, creator_membership_id INTEGER!, created_at TIMESTAMPTZ!
state TEXT!, version NUMERIC!, current_attempt_id UUID, completed_at TIMESTAMPTZ
result_evidence_id UUID, reason_code TEXT, audit_event_id UUID!
```

Immutable intent/creator/time; UPDATE only state, version, current_attempt_id,
completed_at, result_evidence_id, reason_code, audit_event_id. Operation is exactly
`review.answer.create.v1`. Initial version1; each transition consumes exactly one
new version; no terminal restart. Request bytes must equal unchanged
`encode_review_send` using the actual fact's BYTEA external ID (legacy TEXT
fallback only when BYTEA is absent), actual scoped approved decision/draft,
binding and text checksum. Request SHA and bytes are both checked. Current
source/approval/epoch eligibility still requires the trusted T4 service.

`review_send_command_authorities`:

```text
O
command_id UUID!, origin_user_id VARCHAR(64)!, origin_membership_id INTEGER!
origin_session_id VARCHAR(64)!, account_binding_schema_version SMALLINT!
expected_external_account_id VARCHAR(128)!, expected_credential_ref VARCHAR(255)
credential_id UUID!, generation BIGINT!, credential_kind TEXT!, payload_schema_version SMALLINT!
credential_expires_at TIMESTAMPTZ, policy_reference TEXT!, policy_version NUMERIC!
lease_seconds NUMERIC!, authority_expires_at TIMESTAMPTZ!
```

Exactly one immutable authority per command. Generation is the actual credential
parent's positive BIGINT; schema fields SMALLINT=1. WB requires wb_api and NULL
expiry; Avito requires avito_oauth_access and finite expiry. Captured credential
ID/owner/provider/kind/generation/schema/expiry must equal the actual unrevoked
parent. Binding equals the connected canonical account and passes the existing
schema1 binding helper. Real session/user/organization/member equality and active
origin are checked on capture; deadline must be future, no later than session
or credential expiry, and after command creation. No ciphertext/key/verifier or
cached access is copied. Policy reference uses the existing safe label grammar
`[A-Za-z0-9_.:/-]{1,128}`; explicit positive policy version/lease seconds have no
assigned values, retry budget, default or invented policy-registry FK.

`review_send_attempts`:

```text
O
attempt_id UUID!, command_id UUID!, review_id UUID!, sequence NUMERIC!
claimed_at TIMESTAMPTZ!, lease_token UUID!, state TEXT!, lease_expires_at TIMESTAMPTZ!
dispatched_at TIMESTAMPTZ, finished_at TIMESTAMPTZ, result_evidence_id UUID, reason_code TEXT
command_version NUMERIC!, audit_event_id UUID!
```

Identity/sequence/claim time/token immutable. Attempt UUID and token are DB random
UUIDv4, created by the winning claim audit. `command_version` is the command
version of this attempt's last audited mutation, not an independent counter or
retry policy. Mutable fields are state, lease_expires_at, dispatched_at,
finished_at, result_evidence_id, reason_code, command_version, audit_event_id.
Lease expiry is after claim; marker is no earlier than claim; finish is no earlier
than marker (or claim without marker). Marker/token are never reset.

`review_answer_evidence`:

```text
O
evidence_id UUID!, review_id UUID!, command_id UUID!, attempt_id UUID!, read_id UUID
evidence_kind TEXT!, evidence_version TEXT!, outcome TEXT!, observed_at TIMESTAMPTZ!
reconciliation_started_at TIMESTAMPTZ, provider_answer_id_utf8 BYTEA, answer_checksum TEXT
verifier_version TEXT!, evidence_payload BYTEA!, evidence_checksum TEXT!
```

Immutable exact evidence. Version=review-answer-evidence-v1. dispatch_ack has both
read fields NULL; reconciliation_read requires both and dispatch<=start<=observed.
All observations satisfy dispatch<=observed<=DBnow. Incomplete lacks answer ID or
SHA or both; exact/different requires both and checksum equality/inequality to
the immutable command text. Non-null answer ID is strict UTF8, Python-nonblank,
at most512 Unicode codepoints, preserving embedded NUL and combining forms.
Verifier is a safe version label, never authentication. Evidence may be appended
without a lifecycle mutation only for an already marked scoped attempt; late ACK
retention does not grant recovery closing authority.

`review_send_audit`:

```text
O
event_id UUID!, review_id UUID!, aggregate_id UUID!, aggregate_version NUMERIC!
event_kind TEXT!, occurred_at TIMESTAMPTZ!, actor_kind TEXT!, actor_membership_id INTEGER
command_id UUID!, draft_id UUID, policy_id UUID, decision_id UUID, attempt_id UUID
before_state TEXT, after_state TEXT!, reason_code TEXT, audit_payload BYTEA!, audit_checksum TEXT!
current_attempt_id UUID, command_completed_at TIMESTAMPTZ, result_evidence_id UUID
attempt_sequence NUMERIC, lease_token UUID, attempt_before_command_version NUMERIC
attempt_before_state TEXT, attempt_after_state TEXT
lease_before_expires_at TIMESTAMPTZ, lease_after_expires_at TIMESTAMPTZ
attempt_claimed_at TIMESTAMPTZ, attempt_dispatched_at TIMESTAMPTZ, attempt_finished_at TIMESTAMPTZ
```

Immutable history. Everything after audit_checksum is a typed physical witness,
not an extra canonical envelope field. review_id is also physical-only. No JSON
state snapshots. command_id=aggregate_id; one event per command version is
stronger than the requested owner/aggregate/version/event-kind uniqueness and
prevents two explanations for one transition. The audit INSERT derives all
physical witnesses from locked state, DB time and exact event kind. For an
existing attempt the input lease_token must match the current stored token;
the output retains that same token. The actor remains a trusted-service claim
requiring live auth, even though the SQL shape is checked.

`review_send_enqueue_intents`:

```text
O
command_id UUID!, command_version NUMERIC!, event_kind TEXT!, audit_event_id UUID!
enqueue_payload BYTEA!, enqueue_checksum TEXT!
```

Immutable send.ready; exact review-enqueue-v1. The matching audit must be
send.created or send.reclaimed and reciprocally requires this row in the same
commit. No delivery status, timestamps, ack, cleanup, transport leases or I/O.

`notification_in_app_events`:

```text
O
event_id UUID!, scope TEXT!, producer TEXT!, entity_id UUID!, source_version NUMERIC!
kind TEXT!, occurred_at TIMESTAMPTZ!, dedupe_key TEXT!, title TEXT!, details TEXT!, severity TEXT!
source_review_id UUID!, source_draft_id UUID, source_command_id UUID, source_audit_event_id UUID
event_payload BYTEA!, event_checksum TEXT!
```

Immutable account/reviews event; marketplace and all source_* fields are physical
FK witnesses excluded from bytes/HTTP/dedupe. approval_required requires
source_draft_id=entity_id and NULL command/audit; other kinds require
source_command_id=entity_id and a non-null immutable send-audit ID at the exact
source_version, with draft NULL. Occurred time is no earlier than its source and
no later than DBnow. No nullable account/system_attention/platform event.

`notification_in_app_receipts`:

```text
O
event_id UUID!, recipient_membership_id INTEGER!, read_at TIMESTAMPTZ
dismissed_at TIMESTAMPTZ, version NUMERIC!
```

Immutable identity; version is server-owned and absent from canonical receipt
bytes. INSERT initializes version1, stamps every requested non-null action with
one DB clock value and requires at least one action. UPDATE independently retains
each existing non-null timestamp; a NULL becomes the current DB timestamp once.
Version increases once if either/both timestamps are newly set, otherwise stays
unchanged. Earlier retry candidates never win over existing timestamps. No
ordering relation between read/dismiss and no clear/unread/reopen operation.

## Exact keys, FKs and indexes

PK/unique names are `rs_<table ordinal>_<suffix>`, ordinal0..7 in the order above.
`owner` below means O's three identity columns, not DB role ownership.

| Table | PK | Additional unique keys |
|---|---|---|
| commands | owner,command_id | owner,review_id,command_id; org,account,operation_kind,idempotency_key |
| authorities | owner,command_id | — |
| attempts | owner,attempt_id | owner,review_id,command_id,attempt_id; owner,command_id,sequence; owner,lease_token |
| evidence | owner,evidence_id | owner,review_id,command_id,attempt_id,evidence_id; owner,read_id (multiple NULL allowed) |
| audit | owner,event_id | owner,command_id,aggregate_version; owner,command_id,aggregate_version,event_id; owner,review_id,command_id,aggregate_version,event_id |
| enqueue | owner,command_id,command_version,event_kind | — |
| events | org,event_id | owner,event_id; org,account,dedupe_key |
| receipts | org,event_id,recipient_membership_id | — |

`rs_one_create`: unique(owner,review_id,operation_kind) WHERE state IN
(queued,leased,ambiguous,sent,conflict). Blocked/cancelled release a slot only for
a new explicitly authorized intent. Sent/conflict keep CREATE permanently locked.

Every FK is DEFERRABLE INITIALLY DEFERRED, NO ACTION, and explicitly named
`rs_<ordinal>_<suffix>_fk`; no parent key/index alteration. All eight have the
`account` FK owner→marketplace_accounts(owner). Remaining exact child→parent keys:

```text
C.draft: owner,review_id,draft_id,draft_revision,binding_checksum
  -> review_draft_revisions(owner,review_id,draft_id,revision,binding_checksum)
C.decision: owner,review_id,draft_id,draft_revision,decision_id
  -> review_decisions(owner,review_id,draft_id,draft_revision,decision_id)
C.creator: org,creator_membership_id -> iam_memberships(org,membership_id)
C.authority: owner,command_id -> H(owner,command_id)
C.attempt: owner,review_id,command_id,current_attempt_id -> A(owner,review_id,command_id,attempt_id)
C.evidence: owner,review_id,command_id,current_attempt_id,result_evidence_id
  -> E(owner,review_id,command_id,attempt_id,evidence_id)
C.audit: owner,command_id,version,audit_event_id -> J(owner,command_id,aggregate_version,event_id)
H.command: owner,command_id -> C(owner,command_id)
H.member: org,origin_membership_id -> iam_memberships(org,membership_id)
H.user: origin_user_id -> lk_users(user_id)
H.session: origin_session_id -> lk_sessions(session_id)
H.credential: credential_id -> marketplace_account_credentials(credential_id)
A.command: owner,review_id,command_id -> C(owner,review_id,command_id)
A.evidence: owner,review_id,command_id,attempt_id,result_evidence_id
  -> E(owner,review_id,command_id,attempt_id,evidence_id)
A.audit: owner,command_id,command_version,audit_event_id -> J(owner,command_id,aggregate_version,event_id)
E.attempt: owner,review_id,command_id,attempt_id -> A(owner,review_id,command_id,attempt_id)
J.command: owner,review_id,command_id -> C(owner,review_id,command_id)
J.attempt: owner,review_id,command_id,attempt_id -> A(owner,review_id,command_id,attempt_id)
J.evidence: owner,review_id,command_id,attempt_id,result_evidence_id
  -> E(owner,review_id,command_id,attempt_id,evidence_id)
J.actor: org,actor_membership_id -> iam_memberships(org,membership_id)
Q.audit: owner,command_id,command_version,audit_event_id -> J(owner,command_id,aggregate_version,event_id)
N.draft: owner,source_review_id,source_draft_id,source_version
  -> review_draft_revisions(owner,review_id,draft_id,revision)
N.audit: owner,source_review_id,source_command_id,source_version,source_audit_event_id
  -> J(owner,review_id,command_id,aggregate_version,event_id)
R.event: owner,event_id -> N(owner,event_id)
R.member: org,recipient_membership_id -> iam_memberships(org,membership_id)
```

C/H/A/E/J/Q/N/R abbreviate the eight tables in order. The existing draft and
decision keys transitively anchor the immutable fact/review. Real session/user/
member/credential metadata equality is additional row validation, using existing
parents without manufacturing new convenient composite indexes.

Child lookup indexes are generated deterministically by descending FK key length,
stable original FKS order, named rs_child_<zero-based FKS ordinal>. Only keys not
already covered by an unconditional unique/index left prefix get an index.
The partial CREATE index is never treated as unconditional coverage. Additional
`rs_audit_attempt` is (owner,command_id,attempt_id,aggregate_version), for exact
prior-attempt audit lookups. The committed FKS/KEYS arrays are the complete source
manifest. Generated child indexes are exactly the following:

```text
rs_child_8   commands(owner,review_id,draft_id,draft_revision,binding_checksum)
rs_child_9   commands(owner,review_id,draft_id,draft_revision,decision_id)
rs_child_13  commands(owner,review_id,command_id,current_attempt_id,result_evidence_id)
rs_child_14  commands(owner,command_id,version,audit_event_id)
rs_child_10  commands(organization_id,creator_membership_id)
rs_child_16  authorities(organization_id,origin_membership_id)
rs_child_17  authorities(origin_user_id)
rs_child_18  authorities(origin_session_id)
rs_child_19  authorities(credential_id)
rs_child_21  attempts(owner,review_id,command_id,attempt_id,result_evidence_id)
rs_child_22  attempts(owner,command_id,command_version,audit_event_id)
rs_child_26  audit(owner,review_id,command_id,attempt_id,result_evidence_id)
rs_child_27  audit(organization_id,actor_membership_id)
rs_child_28  enqueue(owner,command_id,command_version,audit_event_id)
rs_child_29  events(owner,source_review_id,source_draft_id,source_version)
rs_child_30  events(owner,source_review_id,source_command_id,source_version,source_audit_event_id)
rs_child_31  receipts(owner,event_id)
rs_child_32  receipts(organization_id,recipient_membership_id)
```

This is the source-defined index manifest, not an executed catalog assertion.
No old parent indexes are added.

Every table has `rs_<ordinal>_scalars` and `rs_<ordinal>_shape`; commands/evidence/
audit/enqueue/events additionally have `_bytes`. Identity, shape, time, positive
NUMERIC, canonical checksum, UTF8, evidence and graph constraints are cumulative.

## Transaction protocol and lifecycle

Use one physical READ COMMITTED root. Existing user/account publication guards,
multi-member checks, or future concrete worker/closing guards precede domain
locks. Then canonical account FOR UPDATE, source/policy/workflow locks needed by
the action, command FOR UPDATE, attempt metadata, and sorted notification UUIDs
when receipts are involved. Every new write statement takes the canonical
account lock first, matching 0071 local-source writes. Immutable history reads
use SELECT; this package grants no history UPDATE for locking convenience.

For creation: after fresh authority and exact idempotency lookup, INSERT audit
send.created first, RETURNING its DB occurred_at/payload/ID; INSERT command using
that time and queued/version1 projection; INSERT its exact captured authority;
INSERT matching send.ready intent; commit. Command/authority insertion can be
reordered after the audit subject to parent validation; all reciprocal rows must
exist at commit. Replay first authenticates, then compares creator, exact intent
bytes and binding, and returns the immutable original; never mutate/recreate it.

For each transition: INSERT the audit first with event ID, scoped command/review,
new aggregate version, actor/ref/before/after/reason and optional evidence ID.
For existing attempts supply the exact current attempt ID AND lease_token as
the CAS witness; stale version/current attempt/token rejects. For claim supply
NULL attempt_id/token: the DB generates both random UUIDv4 values. RETURNING is
mandatory: the audit supplies DB occurred_at, derived witness values and exact
canonical bytes/checksum. Do not build a caller-time audit and assume it survived.
Apply the command and attempt mutation using exactly this returned projection;
set audit_event_id and the command version witness; create ready intent on
reclaim. Failed CAS, missing update or ghost audit fails the physical commit.
History is contiguous per command, before/after and monotone time are checked;
each attempt's previous version/state/lease matches its actual preceding event;
final command and every current/historical attempt match their final witnesses.

| Event | Command transition | Attempt transition / condition | Actor / reason |
|---|---|---|---|
| send.created | absent→queued, v1 | none; D/Q required | membership / NULL |
| send.claimed | queued→leased | new claimed, fresh UUID/token/sequence | worker / NULL |
| send.lease_renewed | leased→leased | claimed→claimed, old lease live, strictly extends, no marker | worker / NULL |
| send.dispatched | leased→leased | claimed→dispatched, old lease live, durable DB marker | worker / NULL |
| send.reclaimed | leased→queued | expired unmarked claimed→abandoned; clears command pointer | worker / LEASE_EXPIRED_UNDISPATCHED |
| send.ambiguous | leased→ambiguous | dispatched→ambiguous, marker retained | worker / RESULT_UNKNOWN |
| send.sent | leased/ambiguous→sent | dispatched/ambiguous→sent; exact evidence | worker, or member reconciliation / VERIFIED_EXACT_ANSWER |
| send.conflict | leased/ambiguous→conflict | dispatched/ambiguous→conflict; different evidence | same / VERIFIED_DIFFERENT_ANSWER |
| send.blocked | queued/leased→blocked | none or unmarked claimed→blocked | worker / one fixed blocked code |
| send.cancelled | queued→cancelled | none | membership / USER_CANCELLED |

Lease expiry at claim/renew is min(DBnow+explicit lease_seconds, captured authority
deadline), and must be future; renewal must extend an existing still-live claimed
lease. No after-marker renewal or replacement token. Direct ACK closes only a
current dispatched/leased worker result while lease remains live, including the
deferred callback. Reconciliation closing requires expired lease and a fresh
authorized reconciliation_read; it cannot use ACK or revive an expired login.
An ambiguous row has NULL completion and optionally incomplete evidence. Sent/
conflict have completion+matching exact/different evidence; blocked/cancelled
have completion, no evidence, no marker. Current command/attempt results/reasons
agree. Terminal states never reopen. Evidence authentication is outside SQL.

Exact blocked codes: ACCESS_REVOKED, SOURCE_CHANGED, POLICY_CHANGED,
NOT_ANSWERABLE, CREDENTIAL_UNAVAILABLE. Other persisted reasons are
LEASE_EXPIRED_UNDISPATCHED, RESULT_UNKNOWN, VERIFIED_EXACT_ANSWER,
VERIFIED_DIFFERENT_ANSWER, USER_CANCELLED. No raw provider exception or guessed
success/error reason. Policy/credential changes after dispatch cannot erase it.

In-app production: T4 must commit source change and new fixed event in the same
root, retaining original event UUID/time/payload on replay. DDL checks exact valid
source ownership/version/kind, not which Python service made the transaction.
No trigger on old 0071 writers and no historical backfill are introduced.

Receipt service derives current membership from live authentication, authorizes
exact account and every explicit visible UUID, locks UUIDs in sorted order, then
uses INSERT/ON CONFLICT UPDATE of read_at/dismissed_at only. A missing/invisible ID
aborts all. Non-null INSERT/UPDATE timestamp is only an action indicator: the DB
supplies time. Read RETURNING for both preserved timestamps and physical version.
GET has no side effect; no high-water/mark-future operation. Preferences never
substitute for recipient account authorization. Invalid one ID rolls back all.

Queue delivery is a later T4 bounded duplicate-safe scan of current queued
versions matched to immutable intents; the command claim CAS wins ownership.
Do not delete intents as acknowledgement or perform broker/provider I/O in root.

## Exact SQL helpers, privileges, errors and rollback

All functions below are `public`, SECURITY INVOKER, fixed search_path=pg_catalog,
public. Byte helpers return BYTEA, immutable; they reuse 0071 strict UTF8, JSON
string, UUID, NUMERIC, checksum, label and UTC timestamp scalars, never JSONB
re-encoding. Eight-vector mapping is request; two evidence vectors; audit;
enqueue; event; receipt; visible action. The event identity helper is its fixed
dedupe derivation, not another event registry.

```text
review_send_unicode_version() -> TEXT
review_send_uuid4(UUID) -> BOOLEAN
review_send_answer_id(BYTEA) -> BOOLEAN
review_send_external_id(BYTEA) -> BOOLEAN
review_send_request_bytes(public.review_send_commands,BYTEA) -> BYTEA
review_send_evidence_bytes(public.review_answer_evidence) -> BYTEA
review_send_audit_bytes(public.review_send_audit) -> BYTEA
review_send_enqueue_bytes(public.review_send_enqueue_intents) -> BYTEA
notification_in_app_identity_bytes(public.notification_in_app_events) -> BYTEA
notification_in_app_event_bytes(public.notification_in_app_events) -> BYTEA
notification_in_app_receipt_bytes(public.notification_in_app_receipts) -> BYTEA
notification_in_app_visible_action_bytes(INTEGER,INTEGER,INTEGER,UUID[],TEXT) -> BYTEA
review_send_account_lock() -> TRIGGER
review_send_audit_guard() -> TRIGGER
review_send_row_guard() -> TRIGGER
review_send_validate() -> TRIGGER
```

External-ID normalization has deterministic literal Unicode14 Nd ranges (660
decimal codepoints) and existing Python whitespace semantics; original BYTEA is
preserved. Repository Dockerfile pins python:3.11-slim, but pyproject supports
newer Python too. The migration does not derive SQL from host Unicode or block
upgrades on newer Python. `review_send_unicode_version()` exposes `14.0.0`.
**T4 must confirm normalization compatibility and fail closed for the affected
send writer on a mismatched Unicode contract before activation.** T4 accepted
this bounded marker/mismatch contract through the controller; actual consumer
implementation and literal/Unicode parity remain pending. This is a narrow
codec compatibility dependency, not an all-database blocker.

Migration strips all PUBLIC/non-owner leaked default table and column privileges
and exact helper ACLs only on these new objects. It creates no sequences. It does
not preserve arbitrary inherited grants: explicit new rights come from the
updated runtime role source, which has not been executed. All eight ENABLE/FORCE
RLS with BOTH transaction-local org/account; owner is subject to FORCE RLS.
Deferred callbacks reject context changes hiding earlier writes.

Runtime exact grants: SELECT/INSERT all eight; UPDATE only the C/A columns listed
above, and read_at/dismissed_at on receipts. No version UPDATE grant for receipt,
no identity/history/payload UPDATE, DELETE or TRUNCATE. EXECUTE only the twelve
non-trigger signatures above; trigger functions revoked from PUBLIC/runtime.
Old objects/ACLs are untouched by the migration. Existing parent/helper grants
remain needed. A non-superuser, NOBYPASSRLS runtime role remains required.

These API SQL capabilities admit authenticated user reconciliation and do not
prove API/worker separation. Concrete worker executor grants/handles come from
the next T1 shared authority package; none is provisioned here. Membership IDs,
GUCs, UUIDs, lease tokens, policy labels and actor enums are never authentication.

Fixed SQL messages (SQLSTATE23514 unless noted): review_send_invalid,
review_send_context_invalid, review_send_account_missing, review_send_immutable,
review_send_transition_invalid, review_send_lease_invalid,
review_send_authority_invalid, review_send_evidence_invalid,
review_send_closing_invalid, review_send_time_invalid, review_send_source_invalid,
review_send_request_invalid, review_send_witness_invalid,
review_send_history_invalid, review_send_enqueue_invalid;
review_send_isolation_invalid (25000), review_send_downgrade_nonempty (55000).
Inherited scalar messages: review_local_invalid, review_run_binding_invalid.
Native NOT NULL/unique/FK/check/RLS/privilege/numeric-representability errors remain
possible; trusted adapters map SQLSTATE/known constraint identities without
exposing raw driver details, bodies, credentials or customer content.

Empty-only downgrade locks exactly the eight tables in sorted stable order with
ACCESS EXCLUSIVE, sets LOCAL row_security=off and checks every unfiltered row.
An insufficient maintenance role errors instead of seeing false emptiness.
Any hidden tenant, history, terminal, intent or receipt row rejects rollback.
Only own explicit triggers/FKs/checks/functions/tables are removed, no CASCADE;
0071 data, indexes and ACLs are preserved. A deployment rollback cannot erase
dispatch/ambiguous state or resume an unsafe duplicate POST writer.

## Real remaining owners and final gates

T1 next: concrete multi-member initiation/current-approver guards; sender
reviews:send plus current approver reviews:approve; pre-marker worker guard tied
to real origin/session/account/credential/policy/deadline and actual executor
login; distinct post-marker fresh read/closing authority surviving original
session expiry/revocation; closing roles/capabilities without enum impersonation.

T4 next: private actual table bindings/repository, audit-first returned-witness
consumer, exact CAS/idempotency/restart recovery, trusted evidence adapter and
fresh read, uncertain-commit readback, transport scheduling/enqueue delivery,
marker commit before any provider POST, local-source→notification atomic producer,
receipt root/auth/batch locking, dormant HTTP/UI integration, visibility/read
models and actual runtime Unicode compatibility check. User reconciliation needs
BOTH reviews:read and reviews:send for the current authorized account operator.
No dependency on external Telegram/email destinations/preferences/system registry.

All following gates are **NOT RUN** and must remain in the combined final plan:

- Literal SQL parity of all eight original frozen JSON/SHA vectors, independently
  loaded from ae7db1d; exact-key/null/mutation/>BIGINT/UTF8/NUL/Unicode/decimal
  normalization tests, including Unicode database mismatch and 512-codepoint IDs.
- Upgrade on supported Python runtimes independent of Unicode data, and matching
  writer contract enforcement. No self-generated expected golden values.
- Synthetic PostgreSQL lifecycle and negative matrices: every event, terminal,
  deadline/live lease/renew/dispatch/reclaim, stale version/attempt/token,
  late ACK versus expired reconciliation, evidence equality/auth distinction,
  absent markers, impossible refs/reasons/nulls, byte conflicts and lease bounds.
- Missing command/authority/attempt/audit/enqueue reciprocal writes, ghost audits,
  intermediate lost transitions, audit-only lease extension, contiguous history,
  marker permanence and final command/historical attempt projection failures.
- Literal scoped PK/FK/check/index catalog and grant validation; PUBLIC/default
  table/column/helper leaks; no history DELETE/TRUNCATE; FORCE RLS owner/runtime,
  absent/wrong org/account context on all operations and deferred context switch.
- Notification fixed projection/dedupe/replay-ID/time conflicts, historical source
  version/kind FK, changed mutable command after historical event, no old writer
  hooks/backfill; source+event root rollback and restart identity preservation.
- Concurrent first-read/first-dismiss/both merges, earlier retries, real/no-op
  physical version increments, exact member/account visibility, all-or-nothing
  explicit visible lists, sorted locks, membership revocation and GET purity.
- Actual approver revocation versus sender/worker guard, executor login
  impersonation, original session expiry followed by fresh authorized closing,
  unavailable closing authority, pre-marker/after-expiry read races, marker commit
  before I/O, uncertain commit readback, process restart and duplicate-safe enqueue.
- Empty and production-shaped SYNTHETIC upgrade→downgrade→upgrade; hidden other
  tenant rows must defeat downgrade, parent0071 content/ACL preservation, no real
  credentials/provider/production/source data/Redis. Maintenance role privileges
  are a real runtime dependency and were not exercised in this source package.
- Combined installed-wheel/import, lint, pure/backend/frontend and independent
  review gates only after source implementation finishes, with original literal
  vectors and realistic synthetic service composition; no unexecuted PASS claims.
