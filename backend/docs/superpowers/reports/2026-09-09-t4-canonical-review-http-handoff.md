# T4 canonical-only WB Review HTTP handoff

## Current approved amendment and integration boundary

Coordinator-approved amendment: explicit-account canonical-only ingestion replaces
the unsafe legacy dual-consumer for this path. No legacy global Review store is
read or written. This changes the integration mechanism, not the original parity
acceptance criterion. A successful endpoint alone does not close stage2/shadow.

Required local parity evidence: one synthetic provider page per invocation;
compare the same received DTO through direct canonical normalization and the
persisted service result; compare the existing legacy pure DTO conversion on
the common lossless subset without calling its fetch/store. Explicitly record
intentional differences (null/whitespace/date/opaque-ID loss and source versions)
instead of forcing canonical evidence to mimic legacy coercion. Also require
account ownership, partial/replay safety, fetch failure and fresh-process retry.
One fetch per invocation does not promise one fetch across repeated HTTP requests.
Automatic running-run/ambiguous-COMMIT recovery is not implied by manual retry.

Current frozen consumer input is77abab0ae4a5bef1544bccfbeb92e728d8b7c4e6,
including27c227e and835a82b. Platform prerequisites through0068 are present and
locally released/consumer-accepted at their recorded scope. Original900PASS
predates0068; final binding44PASS follows the recorded225/2 diagnostic run. Root
must perform the consolidated integration review; no independent final approval
or shared-app registration is claimed here. Frontend430/0/0 is separate evidence.

Historical-binding blocker at the end of this document is closed for repository
and guarded ingestion by27c227e; the later dormant single-fact read below also
checks this boundary, but is not registered in the shared application.
Real legacy shadow activation, production flags, live sends and deployments
remain absent. Local draft/send/notification implementation must not be blocked
merely by missing real production credentials or observation windows.

### Local amendment acceptance after the frozen milestone input

`test_review_canonical_parity.py` compares the exact DTO returned by the real
canonical adapter to the repository-decoded persisted observation, including all
canonical content fields and checksum. One fake page request supplies both; the
legacy `_to_feedback_row` converter is called purely on the same raw item, never
through its fetch/store. Common fields match; null and surrounding whitespace/NUL
differences remain explicit, and different source-schema versions/checksums are
not erased to fake equality. This is canonical projected-field parity, not every
legacy UI field such as brand/pros/cons or trusted live legacy-history ownership.

Four fresh spawned interpreters each create their own engine/provider: failed
fetch502 leaves a committed running reservation and no fact; explicit retry200
reuses that reservation; exact replay200 retains one observation/revision; denied
account403 performs zero provider requests. Each permitted invocation makes one
fake request, not one request across retries. Child SQL capture observes no legacy
rv_review_* access. Every child exits normally; no parent in-memory state is used
as the recovered owner. This proves manual retry across process boundaries, not
automatic rescheduling, crash-at-COMMIT reconciliation or legacy activation.

Actual new-module4PASS13.69s (one owned DB/role cleanup), then canonical HTTP,
shadow service, binding repository and parity combined **61PASS,2warnings,26.86s**
with four owned DB/role cleanups and naturalexit0. Scoped Ruff/AST/diff0.
Existing `/dev/null` passfile and framework deprecation warnings are retained.
No runtime/schema changes were needed. Main critical pass checked real adapter,
actual subprocess isolation, exact normalization/hash evidence, denied prefetch,
and no claim of automatic recovery. Root final independent integration review
remains; the frozen77abab0 milestone input is not silently replaced by this work.

## Scope and registration request

### Later canonical single-fact read (separate from frozen77abab0)

T1 registration input: `app.reviews.canonical_read.router`,
`GET /api/v2/reviews/wb/fact?marketplace_account_id=...&external_review_id=...`.
This is a single WB fact, not a list/queue, Avito endpoint, frontend cutover or
send capability. No shared registration/config/schema change by T4. It uses the
existing default-off exact-pair Review selector and independently requires live
`reviews:read`; ingestion's write permission does not substitute for read access.

The Engine-owned PostgreSQL root discovers membership/account metadata only,
then acquires the existing publication guard with no credential authorities.
Current user, membership, session, account scope/status and binding are locked
and revalidated before reading repository evidence and before root completion.
No credential decryption/provider request, legacy repository access or domain
write occurs. Guard metadata locks serialize with account changes; this is not
a lock-free/high-throughput read claim. Actual shared authentication may update
session `last_seen_at`, as with the sync route. The repository validates immutable
historical binding and checksum before returning text; rebind returns conflict,
never old text under the new binding.

Response `schema_version=canonical-review-fact-v1`: organization/account IDs,
marketplace `wb`, external Review ID, Review/observation UUIDs, version/revision
as positive decimal strings (lossless for JavaScript), nullable exact text,
answered/can_answer, source_order_state current/ambiguous, content checksum,
external product ID, source created/updated timestamps and source/normalization
versions. `can_answer` is source evidence, not authorization or send eligibility;
ambiguous is not silently promoted to current. No freshness guarantee, pagination,
rating/UI-detail parity or fabricated missing source metadata is claimed.

Standalone errors use `detail.code`:401 REVIEW_READ_AUTHENTICATION_REQUIRED;
403 REVIEW_READ_DENIED;409 REVIEW_READ_DISABLED/REVIEW_READ_CONFLICT;
400 REVIEW_READ_INVALID;404 REVIEW_READ_NOT_FOUND;
503 REVIEW_READ_STORAGE_UNAVAILABLE/REVIEW_READ_CONFIGURATION_INVALID.
Query validation uses standard422. Shared app uses its existing error envelope
and sanitized validation handler described below; its acceptance belongs to T1.

Evidence: missing-module pure RED1FAIL -> disabled read GREEN1PASS with every
network action denied. Final read14 + canonical HTTP21 + run-binding17 + shadow19
= **71PASS,2warnings,18.28s**, naturalexit0, four owned disposable databases/roles
verified absent after cleanup. Cases include signed bearer and live revocation,
wrong provider/tenant denied before body query, rebind409, null/empty/NUL exactness,
SQL capture with zero INSERT/UPDATE/DELETE and rv_review_* access, safe503/404/422.
Large-version HTTP projection wraps a real validated repository snapshot with a
synthetic BIGINT version; it does not claim billions of database CAS transitions.
Two diagnostic runs failed only that new fixture: first its text-ID UPDATE matched
no lossless BYTEA identity; then an exact-ID UPDATE was correctly rejected by the
database monotonic-version guard. The final projection fixture preserves both
constraints; no trigger or migration was weakened. Scoped Ruff/diff checks pass.
Main-agent critical pass checked auth-before-body, historical provenance, transaction
completion, wire precision, safe errors and these evidence limits. Root independent
integration review remains. This addition does not silently move frozen77abab0.

Coordinator explicitly approved canonical-only explicit-account sync. It must not
feed the global legacy Review store. This supersedes dual-consumer shadow for this
path only; legacy isolation and trusted legacy-source binding remain separate gaps.

T1 registration input: `app.reviews.canonical_router.router`, containing
`POST /api/v2/reviews/wb/sync`. The router is NOT registered in the shared app by T4.
Only synthetic test apps include it. Request to T1 is default-off registration,
not operational activation. Existing legacy routes/tasks/frontend remain unchanged.

Prerequisites consumed: physical guard8338ef7 via2dc09de; lossless0065 via74bde41,
decoder/writerdab11ec/0ce0517; fixture34b5f62 via79f83b9; selector434ff5f via9981e72.
No competing schema/config change or pending approvals migration was imported.

## Wire contract

`ReviewCanonicalSyncRequest`: snake_case, extra fields forbidden. Required:
marketplace_account_id strict positive INT4, request_id UUID, is_answered strict bool.
Optional nm_id is a positive JS-safe integer; take1..5000 defaults100; skip0..INT4_MAX
defaults0; order dateAsc/dateDesc defaultsdateDesc; optional nonnegative date_from/
date_to Unix seconds bounded at year9999, ordered when both provided. One explicit
answered/unanswered stream and one page per HTTP request; no implicit loop or
cross-stream aggregation. No scenario, secret, actor, permission or legacy payload.

`ReviewCanonicalSyncResponse`: sync_run_id UUID string, manifest_checksum lowercase
SHA256, observed_count integer, completeness always partial. It contains no Review
body, provider payload, credential reference or secret. It is not a printable/send
capability or a public canonical Review read response.

Standalone synthetic-app errors use `detail.code`. In the actual shared app,
app/main.py's existing HTTPException handler emits `{error:{code,message,details}}`;
consumers must use `error.code`. The safe code vocabulary below is unchanged.
Shared-app registration tests belong to T1; T4's standalone tests do not establish
the final shared envelope.

- 401 REVIEW_SYNC_AUTHENTICATION_REQUIRED (real existing bearer/session resolver).
- 403 REVIEW_SYNC_DENIED (live permission/scope, session or authority change).
- 409 REVIEW_SYNC_DISABLED, REVIEW_SYNC_CONFLICT, REVIEW_SYNC_CREDENTIAL_UNAVAILABLE.
- 400 REVIEW_SYNC_INVALID (including invalid received canonical source fields).
- 502 REVIEW_SYNC_PROVIDER_UNAVAILABLE (provider failure or malformed page envelope).
- 503 REVIEW_SYNC_STORAGE_UNAVAILABLE or REVIEW_SYNC_CONFIGURATION_INVALID.

Standalone FastAPI body-validation422 remains the standard framework contract.
The shared app instead returns error.code=VALIDATION_ERROR and removes input/ctx
from validation issues. Shared handler/default-off/auth/422 acceptance is still due.
Fixed error
codes do not echo provider errors, SQL, credentials or bearer strings. Dependencies
also translate engine/config acquisition failures. Shared auth may update session
last_seen_at; canonical-only does not mean zero authentication/control-plane writes.

## Ownership, transactions and source evidence

Pure exact-pair selector is checked independently of authorization; defaultFalse,
empty allowlist. A fresh Engine-owned root discovers exact WB account/membership,
then shared guard checks live reviews:write, session and selected-account access
before credential resolution/provider fetch. Metadata discovery alone is not a grant.

Paired credential resolver supplies the actual nonempty WB token and binding.
Frozen preflight external-account/ref is compared with that paired binding; the
next guarded reservation revalidates its exact authority. Reservation commits before
provider I/O; no publication lock is held during fetch. Publication uses a separate
guarded root and returns only after commit. Revocation after fetch blocks publication.
No fabricated worker actor, user-token fallback or legacy `_run_db`/memory fallback.

Request source key is `http:` plus canonical request UUID. Checksum binds the explicit
account and all page selectors, excluding the request UUID itself. Changed request
under the same key conflicts before fetch. Identical retries may fetch again; exact
received evidence replays without duplicate facts, changed evidence conflicts.
This is idempotent persistence, NOT exactly-once provider reads or automatic retry.

New `canonical_wb_fetch.py` uses one shared rate-limited client request with the exact
paired token override. It requires `data.feedbacks` to be a list, rejects malformed
elements/pages rather than silently skipping, and keeps raw scalar values in a
redacted canonical DTO (`wb-feedbacks-api-v1`). Canonical normalization validates
them before fact writes. Null/empty/whitespace/NUL text is preserved; opaque IDs are
not stripped (invalid padded IDs are rejected by the existing canonical contract).
Missing/malformed source timestamp is rejected, never replaced by current time.
Answer-text presence must agree with the requested stream; mismatches fail closed.
No send eligibility is inferred. Legacy WB adapter remains byte-for-byte unchanged.

Coverage is conservatively partial even for an empty page. No provider completeness,
terminal pagination, tombstone or date-window semantics are inferred from page size.
Failed fetch/publication leaves its committed reservation running. Failed-run
finalization, process-restart recovery and ambiguous physical-COMMIT recovery remain
unimplemented; an injected pre-COMMIT error proves only its tested rollback case.

## Verification ledger

Initial missing HTTP3RED→3PGPASS. Actual completed account rebind after preflight
produced200 instead403:1RED→frozen expected-binding fix. Independent critic found
legacy normalization would lose raw evidence; separate adapter7RED→7PASS and actual
raw FakeWbApiClient→HTTP→PostgreSQL cases cover null/empty/whitespace/NUL/sourceversion
and invalid dates. Intermediate test corrections aligned padded-ID expectations with
the existing reject-not-strip contract and read evidence from actual repository/SQL
fields rather than nonexistent snapshot attributes.

Real signed bearer authentication, missing/tampered bearer, default-off, denied
account before credential/fetch, live permission/scope, session/credential revocation
during fetch, publication commit injection, request replay/conflict, malformed
request and OpenAPI schema are exercised. Canonical success with legacy memory/DB
access forbidden and runtime SQL capture without rv_review_* proves this path avoids
the global store. Engine-config1RED500→503 and credential-outage1RED409→503 are fixed.

Pre-final expanded suite:897PASS2warnings49.13s. Final combined run including the
three additional HTTP cases and final error mapping: **900 passed, 2 warnings in
49.16s**, no skips, natural exit0. This is the selected28-file Review/credential/
guard/selector/adjacent set, not the full backend suite. Scoped Ruff/compile/diff0.
Independent final static criticPASS: prior P1 lossless and preflight defects closed,
no new important findings. The critic did not independently rerun the900tests.
All runs use scrubbed environment, Unix-only sandbox and fresh owned disposable
PostgreSQL databases/runtime roles with cleanup checks. Provider boundary is fake;
no actual WB, Avito, LLM, production, Redis, print/export or delivery action occurs.

Remaining stages include account-owned policy/draft/send/notification services and
their DDL, canonical public reads/frontend integration, legacy isolation/retirement,
full integration baseline and separately authorized canary. No architecture-complete
or production-readiness claim follows from this bounded HTTP slice.

## Historical pre-27c227e binding gate — closed at repository layer

Read-only trace plus actual disposable1case reproduces old Review evidence visible
after a completed account rebind even under a fresh live guard. See
2026-09-09-t4-review-historical-binding-request.md and
tests/test_review_historical_binding_gap.py. Current internal owner IDs do not encode
historical external binding. At that checkpoint it blocked public historical reads
and operational activation, requiring current-head publication/replay checks across rebinds.
The earlier900PASS is bounded HTTP/in-flight verification, not a proof that this
newly characterized completed-rebind gate is closed. Router registration may still
proceed separately while its policy remains default-off.
