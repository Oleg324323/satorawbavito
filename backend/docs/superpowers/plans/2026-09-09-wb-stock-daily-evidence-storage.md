# WB warehouse daily revisions and reviewed correction evidence

Binding requests:907ec7b price/stock schema request section7–8 and
0c8fca9 backend/docs/superpowers/reports/2026-09-09-t2-stock-revision-evidence-schema-request.md.
Exact dormant codec at0c8fca9:app/modules/wb_stock_evidence_codec.py and its eight
literal tests/fixtures/wb_stock_evidence_golden_v1.json. Main read both requests,
whole codec and all eight literals. Current source parents are the separate
2026-09-09-wb-current-source-storage package, not yet implemented at plan creation.

This is additive storage, not a source-provider proof, permission policy, mutable
approval framework or activation. No automatic filling of missing historical days.
Current parser has no proven source-observed timestamp: received basis only.

## Task 1: Implement immutable daily history and exact reviewed evidence

Read all binding inputs, actual current-source migration/handoff after it commits,
exact account/member/grant helpers and all eight literals before source edits.
If current-source parents are absent or ambiguous, stop this package; never create
guessed parent columns/indexes or recreate the earlier source families.

Exactly three paths:

- New backend/alembic/versions/YYYYMMDD_NNNN_wb_stock_daily_evidence.py, next exact
  revision/down_revision supplied at dispatch after actual single-head and filename
  absence checks. Stop on mismatch, no silent revision reservation/renumbering.
- backend/ops/runtime-db-role.sql, only new object and exact helper grants.
- backend/docs/superpowers/reports/2026-09-09-t1-wb-stock-daily-evidence-handoff.md.

No tests/reviews/import/compile/PG/Redis during this source-first phase. Final gates
are deferred, not waived. No old migration edits, parent grants/index changes,
T2 runtime/codec/test edits, routers, source fetches, role provisioning, secrets,
production/working services, flags, provider/network operations or current writers.

### Six tables and common ownership

wb_stock_daily_revisions, wb_stock_daily_heads, wb_stock_revision_evidence,
wb_stock_revision_evidence_diffs, wb_stock_revision_evidence_decisions,
wb_stock_daily_audit. Last is a narrow typed local family; do not extend older
source audit helpers or create a universal evidence/audit repository.

Every table is positive canonical INT4 org/account, exact WB provider composite
account FK, forced organization AND account RLS. Root/command IDs canonical UUID4;
daily revision/head/related codec integers use exact positive NUMERIC integers,
not float/BIGINT truncation of unbounded codec revision. Native source identities
remain positive BIGINT; nullable chrt remains product-grain NULL, not nmId/zero.
All proposal/decision/member/run/daily links include exact organization/account
and actual immutable parent identity. No UUID-only cross-scope lookup.

Account row is the common physical serialization lock before new daily head or
evidence/decision reads. Immutable proposal/decision history has SELECT-only plus
INSERT ACL, not fake UPDATE privileges just to issue SELECT FOR UPDATE. This
narrows the request's logical proposal lock to canonical account FOR UPDATE;
uniques/CAS/reciprocal constraints still arbitrate direct writes. Handoff documents
the physical lock order: live user/member/session first when applicable → account
→ immutable proposal/decision SELECT → mutable daily head FOR UPDATE. SQL RLS or
actor_kind does not itself establish a live user/executor identity.

### Daily model and date eligibility

Full daily owner org/account/stock_scope/request_checksum/business_date_msk;
stock_scope wb_warehouse. Immutable revision source_run_id references actual sealed
complete compatible wb_warehouse_v1/wb-warehouse-stocks/v1 run, with exact request
bytes/checksum and all accepted pages. No mutable current-head FK in history.
Preserve basis/effective_observation_at/created_at/actor/correction_reason/parent
and evidence/decision/checksum binding on correction. Head has full owner,
current_revision FK, version=current_revision, no empty head.

Initial revision/head1 commits atomically, worker actor and NULL member/reason/
parent/evidence. Manual initial creation is outside v1. Exact same run replay is
a service no-op preserving original IDs/times/history, not another audit insert.
Automation privilege is a future explicit trusted executor policy, not permission
to invent a user session or expose a caller-provided worker enum as authority.

Current v1 basis MUST be received. effective_observation_at equals the accepted
run receive time (last accepted page). All accepted page receive timestamps, when
converted to Europe/Moscow, must have the same business date equal to owner date.
Cross-midnight collection is still valid complete current source but ineligible
daily. source_observed basis requires a later actual timestamp-bearing adapter;
neither a source_observed_at value nor arbitrary caller basis opts into it here.

Correction revision=parent+1, full immutable parent FK, acting org membership,
safe reason grammar [A-Za-z][A-Za-z0-9_]{0,127}, accepted evidence/decision and exact
proposal checksum. Persist after_run_id as source_run_id. Require head still equals
proposal.before_daily_revision/before_run_id before CAS. Changed head means rollback
entire correction, not overwrite/recompute a new parent. Reapply an already consumed
acceptance cannot advance again. No new parentless history or no-op new revision.

Correction cannot override date eligibility: both before/after runs must be eligible
for the same exact business date. Today's received snapshot cannot replace an old
day. No invented day-close cutoff, TTL or retention; source storage does not grant
business authorization. Missing coverage stays unknown. Approval cannot create
missing independent evidence or assert provider deletion/stock reserve/finance truth.

### Proposal, normalized diff, immutable decision

Proposal binds before/after DIFFERENT complete runs and exact manifests/context,
immutable before daily revision whose source is before_run_id, proposer member,
DB proposal time, scoped command UUID, exact canonical proposal BYTEA/SHA, sanitized
explanation BYTEA/SHA, exact nonblank reviewed reference, diff checksum/counts>0.
One atomic insert of proposal plus all diff children and reciprocal integrity
checks; no append/edit/delete after commitment. No metadata-only ghost proposal.

Preserve document bytes; codec permits valid UTF-8 bytes and rejects whitespace-only
document. Do not silently decode/rewrite PostgreSQL TEXT or normalize Unicode.
Reference requires exact codec nonblank/trim/NUL-free UTF-8 shape. Evidence document
validation must not claim to identify credentials/PII/signed URL secrets; approved
sanitization/limits/retention is a missing service policy and activation blocker.
No URL fetch, raw provider response or arbitrary exception evidence in the service.
The pinned synthetic8192 budget is NOT a production limit. New SQL scalars must
preserve Python canonical ensure_ascii JSON, including non-BMP surrogate escapes,
without JSONB re-encoding changes, whitespace normalization or bigint float casts.

Diff physical UUID4 plus full owner/evidence/native nm_id/chrt_id/warehouse_id UNIQUE
NULLS NOT DISTINCT. Never put nullable chrt in a PK. Exactly one of added/removed/
changed, with before NULL/after hash, before hash/after NULL, or both unequal hashes.
Lower64 hashes only. Recompute exact FULL OUTER JOIN semantic difference of BOTH
sealed parent fact sets by native identity, comparing all three presence/count
pairs. Stored diff set must equal it: reject omitted/additional/unchanged/swapped
identities/hashes and net-zero aggregate changes. No count-only proof.

Exact ASCII row bytes tag wb-stock-evidence-row/v1 and three [presence,value] pairs;
native identity is outside the row hash, display names/ignored raw fields excluded.
Identity tuple strings exactly (nmId,decimal_nm,chrtId,decimal_chrt_or_null,
warehouseId,decimal_warehouse). Diff tag wb-stock-evidence-diff/v1 with rows sorted
by Python lexical STRING tuple order using equivalent byte/C collation, not numeric
or locale order. Source raw manifest binding remains separate from semantic row hash.

Exact proposal tag wb-stock-evidence-proposal/v1 and fixed field order from actual
EvidenceProposal.canonical_bytes, including immutable IDs/context/date/revision/
member/command/document hash/reference/diff hash/category counts. DB recomputes
bytes/hash from typed graph; never trust supplied checksum alone or use JSONB text.
Timestamps excluded from codec; same intent replay must preserve original stored
time/UUID after fresh authentication and scoped command lookup BEFORE new UUID
generation. Hash matches require exact immutable intent/byte equality, not blind
digest equality. Domain replay implementation belongs T2, not migration claims.

One decision per full scoped evidence, its UUID/command unique in scope, outcome
accepted/rejected, current acting org member, trusted DB timestamp>=proposal time,
reviewed_proposal_checksum exact, safe reason grammar. Immutable terminal decision,
no retract/reopen/appeal/expiry defaults. Concurrent contrary intent has one winner;
same original command/actor/intent can read back original immutable decision. A
rejected proposal never authorizes daily change; fresh auth always required later.

### Reciprocal lineage/audit and actual evidence consumption

Daily.created/corrected only, exact daily owner/run and before/after revision,
before/after head, DB occurrence time, worker/NULL for initial or membership/current
actor for correction. One typed audit witness per actual revision/head transition;
audit-only rows and unaudited revision/head advancement fail by transaction end.
Evidence proposal and decision are their own immutable audit facts; no duplicate
mutable evidence status or mandatory notification/queue. Exact replay/CAS loss
leaves no additional audit/diff/decision/daily row.

Accepted daily correction has scoped FK to actual decision and deferred graph
equality with accepted proposal, before parent and after complete run, exact full
diff and date. Only a future authenticated T2 service reconstructs SourceRuns and
RevisionEvidence reference stock-evidence:<evidenceUUID>:<decisionUUID> from those
actual accepted rows. A client supplied reference is explanation metadata, never
authority. No storage claim that source SHA or reviewer explanation is WB signature.

### ACL, rollback and handoff

New-only exact-column INSERT/UPDATE/head grants and helper-signature EXECUTE scrub;
no runtime DELETE/TRUNCATE/history UPDATE, no PUBLIC helper/SECURITY DEFINER bypass,
no data copying/backfill or old-table hooks. Trigger/evidence consistency does not
turn a runtime SQL connection into authenticated membership. Empty-only downgrade
checks every new table INCLUDING RLS-hidden proposed/rejected/audit/history rows,
refuses any material data, no CASCADE/old schema edits. Rollback before activation
keeps disabled writer and immutable history; no legacy history overwrite fallback.

Handoff lists exact six relations/column contracts/keys/helpers/grants/physical lock
order, transaction examples and source parent dependency, codec versions/eight
literals, date limitation and strict separation of storage vs auth/provider proof.
State IMPLEMENTED / UNVERIFIED, never READY or complete Stage5.

Missing owner policy: fixed proposal/read/accept/reject/apply permissions, proposer
self-review/four-eyes rule, any acceptance invalidation contract, explanation limits/
sanitization/retention, trusted initial worker authority. Keep these mutation APIs
closed; no guessed permission/profile/global-account widening. Independent source
facts/storage continue. Later user auth is not conferred by past reviewer FK.

One bounded schema source commit. No amendment/rebase of communicated commits.
Final verification after all source: eight SQL literal byte/hash vectors; actual
synthetic disposable PG RLS all operations, immutable graph sealing/full diff/NUL/
Unicode/BIGINT/missing-zero, page/date midnight rules, two reviewer/head races,
audit/commit failure atomicity/restart/replay, denied grants/hidden-history downgrade,
empty and production-shaped synthetic migration cycle, compatibility/source guards.
No production/provider/working services or invented test-green claims.
