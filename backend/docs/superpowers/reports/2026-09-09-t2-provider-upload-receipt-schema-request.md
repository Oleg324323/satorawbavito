# T2 → T1: minimal immutable WB upload receipt

Requested follow-up to `9c7e5baa6dc3b3b2f113671b03db6c301942b1e7` worker amendment.
Local source evidence only; no WB/docs/network calls, real payloads or current flow
changes. Proposed relation name below is not a registered migration. T1 owns DDL,
shared worker authority and accepted job reference; no permission/retention/TTL is
defined here.

## What the existing adapter actually exposes

| Local evidence | Available facts and limitations |
| --- | --- |
| `app/wb22_apply.py:42–55` `PriceApplyResponse` | `wbUploadId: int|null`, `wbStatus: int|null`, `applyMode`, `wbMutationSent`, local `observedAt`, derived labels, rowErrors/notes |
| `:164–205` upload POST and extraction | POST `/api/v2/upload/task`; ID extracted from `data.id` or `data.uploadID`. Successful transport without ID returns requires_attention and mutationSent=true, not proof of rejection |
| `:211–228` deferred polling | Returns processing with a known upload ID; this already demonstrates receipt is not completed price application |
| `:289–306` status rate limit | Preserves upload ID but sets `wbStatus=429`: field mixes HTTP status with task status, so never persist this field as an unqualified provider task status |
| `:307–338` final result | Same upload ID survives accepted/processing/requires_attention. Row details inspect only limit100/offset0; absence of rowErrors does not prove complete multi-row history |
| `app/wb23_runtime.py:663–735` status reader | `uploadId` is the caller's query argument, not independent confirmation of POST acceptance. GET history/tasks and first history/goods/task page; local fetched time, not provider accepted/applied time |
| `app/wb22_apply.py:69–78` `_as_int` | Rejects bool but calls int(value), which can truncate float. Pydantic response int therefore does not itself prove lossless external identity parsing |

No durable provider receipt owner exists here. No raw provider response, notes,
row error text, token or account secret may be stored in the proposed relation.
`wbMutationSent=false` on a failed transport is not reliable proof that the remote
server could not have accepted it; the existing flag cannot authorize a resend.

## Canonical fact: known task receipt, not price-applied state

Minimal grain: **one immutable known WB upload identity per exact owned attempt**.
`wb_repricing_upload_receipts` (name proposed) stores only the normalized ID that
the upload-response boundary observed for that attempt and its binding metadata.
It must not set approval applied, reopen ambiguous, mark a Catalog price current,
or imply completion of all size/nmID rows.0066 and the existing outcome kernel stay
unchanged. Source price observations remain separately owned facts.

| Column | Exact proposed type / nullability / constraints |
| --- | --- |
| receipt_id | UUID4 NOT NULL, server-created physical identity |
| organization_id / marketplace_account_id | positive INT4 NOT NULL; existing internal composite account/org FK and WB discriminator |
| approval_row_id | UUID4 NOT NULL; exact0066 approval scope FK, not UUID-only lookup |
| attempt_id | UUID4 NOT NULL; composite org/account/approval/attempt FK |
| action_key / request_checksum / dispatch_key | TEXT NOT NULL, lowercase `[0-9a-f]{64}`; equality to the same immutable0066 attempt/approval binding, not an alternative hash owner |
| wb_upload_id | TEXT NOT NULL, canonical positive ASCII decimal `[1-9][0-9]*`; no exponent/sign/padding/blank/NUL. No undocumented provider maximum or BIGINT narrowing |
| observed_at | finite TIMESTAMPTZ NOT NULL (no ±infinity), original aware local adapter observation time; not provider accepted_at/applied_at and not retroactively refreshed on replay |
| recorded_at | finite TIMESTAMPTZ NOT NULL (no ±infinity), repository database clock at first insertion |

No nullable payload fields in this minimal relation. No row means **unknown receipt**,
not zero ID, failed price action or proof of nonacceptance. No provider status enum,
generic JSON, error text, result_code, actor name or retention policy is invented.
The current response has no provider accepted timestamp, so no such column is proposed.

Client observation and database clock are different clock domains. Do not invent
a skew tolerance or CHECK(observed_at >= dispatch_at / observed_at <= recorded_at).
Preserve their exact finite instants even if clocks differ; neither timestamp
authorizes sending or proves causal ordering. The earlier committed marker and
trusted physical transaction sequence provide that causal boundary. Clock-skew
diagnostics are not a replacement for receipt identity or a reason to invent time.

Job/worker authentication linkage must use the accepted T1 durable job identity;
its type/FK is not yet published and cannot be guessed or replaced by a nullable
fake user/session ID. This is a mandatory service activation dependency, independent
of whether T1 prepares this small physical relation first. Audit/permission grants
remain T1-owned and must not let arbitrary runtime callers forge observed receipts.

## Provenance and binding gates

Receipt creation requires an existing **committed dispatch marker** on the exact
attempt (dispatch_at NOT NULL; marker version1 preserved even when current version2
is terminal), plus the accepted worker's exact scope/job/attempt authority. It is
append-only evidence, so may arrive after a terminal ambiguous outcome; it is not a
CAS transition of that approval/attempt and must not increment their versions.

Do not infer this marker from receipt existence, upload ID alone or a request DTO.
Composite FK / deferred equality checks must bind org, account, approval row,
attempt, action key, request checksum and dispatch key. A caller supplied upload ID
used for `build_apply_status` is insufficient provenance for initial receipt creation.
The service must already own the attempt's receipt before using that ID for history.
An ordinary FK/equality check alone cannot prove the marker committed in an earlier
transaction. The future service must read it in a fresh root after the dispatch
commit; a combined mark/POST/receipt transaction is forbidden and needs an explicit
boundary test. This request does not claim current DDL enforces prior-commit proof.

**Adapter acceptance gate still required:** consume the upload result at the POST
boundary before optional history polling. Validate the original upload identity
losslessly, rather than trust `_as_int`'s potentially truncated result. Existing
`PriceApplyResponse` alone is insufficient to prove that validation. A bounded new
adapter projection/test is required before wiring; do not rewrite the existing
working parser or claim this request implements that projection. Dry-run/local_mock
results must never create a provider receipt. Ambiguous without a returned ID has
no receipt row and remains a reconciliation/manual-review blocker.

Dormant implementation follow-up: `app/modules/wb_repricing_upload_receipt.py`
`extract_post_upload_id` now validates the original **decoded** POST response before
`_as_int`, returning only a canonical ID. It rejects lossy float/bool/coercion,
conflicting aliases, explicit errors and non-POST/non-success/local result contexts.
It does not parse raw JSON, detect duplicate raw JSON keys, authenticate caller
metadata, prove a committed marker, construct a scoped receipt row or persist it.
47 synthetic offline cases cover this projection; current WB adapter is untouched.
The acceptance gate above remains open for trusted adapter/service wiring and DB
receipt authority, not for this separately tested decoded numeric projection.

Further dormant follow-up: `extract_post_upload_id_from_bytes` validates bounded
original UTF-8 JSON, including duplicate/escaped-equal keys and numeric token shape,
before calling the decoded projection.48 added synthetic cases;95 combined raw and
decoded cases pass. Response-byte budget is explicit trusted adapter policy, not a
new provider limit. Current WB transport still does not supply/call this function;
passing reserialized or already lossy data cannot recreate original-byte evidence.

## Replay, conflicting evidence and recovery

UNIQUE `(organization_id,marketplace_account_id,attempt_id)`; receipt insert locks
the same canonical account/attempt boundary in accepted lock order. Same owned
attempt and same ID/binding returns the original receipt with no new row or audit,
even if transport retry supplies a later local observation time. The original time
is preserved, not silently rewritten. Same attempt with another ID or changed
binding is an explicit conflict: do not replace the receipt, pick latest or send
again. The service must preserve/confine conflicting evidence under a separately
reviewed diagnostic contract, not hide it in this canonical row.

Do **not** add UNIQUE(account,wb_upload_id) across attempts without provider evidence:
this local source does not establish whether WB may return an existing task for a
duplicate payload. The local attempt binding is not a proof of WB exactly-once or
of server-side request checksum equality.

### Mandatory receipt audit vocabulary (physical owner chosen by T1)

Proposed fixed event `provider_upload.receipt_observed`, not an approval transition
and not a new value silently inserted into0066's existing audit CHECK. Minimum:
UUID4 `audit_event_id`; required org/account/approval_row_id/attempt_id/receipt_id
with exact composite ownership references; fixed `actor_kind=repricer_worker`;
`actor_membership_id=NULL` (never fabricate an initiating user as the observer);
finite `occurred_at=receipt.recorded_at`. Required trusted durable job reference and
its composite FK use the forthcoming T1 authority contract, not a guessed ID type
or nullable bypass. This unresolved type is explicitly a DDL-finalization dependency.

UNIQUE full-scope receipt reference: exactly one creation event per canonical
receipt. Receipt and event require reciprocal/deferred witness validation at commit:
no missing event, ghost event, wrong scope, job binding or mismatched timestamp.
The immutable receipt reference supplies upload/action/checksum/dispatch data; no
raw-response audit payload or duplicate free-form metadata. Replay returns the
original receipt/event; conflicting evidence does not overwrite either. T1 may
choose a dedicated relation or an existing suitable append-only audit service but
must publish its exact receipt/job references, rather than infer auth from actor_kind.

Insert receipt and this audit in one physical transaction. A crash before this
transaction commits can still lose the observed ID; the dispatch marker then forces
ambiguity/no resend. A committed receipt survives restart and permits a separately
authorized history read. The receipt does not eliminate the external acceptance →
local commit gap and must not be advertised as doing so.

## Required synthetic acceptance tests (not yet implemented here)

- Two real sessions inserting the same attempt+ID: one canonical row and one audit;
  different ID conflicts, first immutable row unchanged.
- Crossorg, second account in same org, wrong approval/attempt/action/dispatch/hash:
  reject. Runtime cannot UPDATE/DELETE receipts or audit. FORCE scoped RLS.
- No marker, dry-run/local result, unknown ID, bool/float/coercible float, zero,
  negative/padded/exponent string: cannot silently create a trustworthy receipt.
- Known ID while processing/ambiguous persists independently; no applied transition,
  no price cache mutation and no synthetic actual-price fact.
- Lossless large decimal ID, exact replay, late receipt after ambiguous, restart,
  audit/transaction failure rollback, revocation handled by accepted closing authority.
- History query argument alone cannot create initial receipt; HTTP429 must never be
  mistaken for a WB task state. First page without errors is not completeness proof.

No SQL, provider calls, migrations, runtime state or business rules changed by this
document. Full authority and adapter-projection gates remain explicit dependencies.
