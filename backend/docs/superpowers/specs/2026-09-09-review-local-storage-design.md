# Account-owned local Review persistence

Inputs fully read: T4 exact matrices through b7c3bb1c2d50abd0fc29216257d5c58562d26820,
actual storage_payloads/decision_contract/historical_binding and ad793f9c4672210b25fe05e35dc1ddb03cf676ac
local request/result codec + eight fixed vectors. Existing0063/0065/0068 ownership,
lossless representations and binding helpers remain unchanged. SKU0070 is released
at54386d4a789b0b74ae30710035defe949ea44b6a, evidence6e35c06ba3d66790c0b0f7091d5d22f5c9ad5469.
After its release, offline inspection found exactly head20260909_0070 and no0071.
This slice uses20260909_0071/down20260909_0070 only if the same fresh pre-dispatch
check still holds; changed head or occupied target requires controller resolution,
never silent renumbering. This document is a design, not installed schema/READY.

## Scope and decision

Architectural, but limited to seven physical relations for local membership commands:
policy versions/heads, draft revisions, decisions, workflow heads, local audit and
completed local-command receipts. T4 owns actual live service/auth/HTTP and predicates.
No send/claim/leases/jobs/outbox/notifications/cleanup/real generation. Existing data
and deferred Production compatibility are preserved. A SQL FK/GUC is not authority.

Alternatives: mutable JSON settings lose immutable lineage and original retries;
reuse local audit.commandId breaks accepted bytes (it is reserved/null); a dedicated
typed completed receipt preserves historical results with narrow closed operations.
Choose the last. Cost: reciprocal witnesses and explicit transactional order, plus
separate later send/in-app schema rather than premature generic infrastructure.

## Shared types and ownership

Owner O=(positive INT4 organization_id, positive INT4 marketplace_account_id,
marketplace wb|avito matching canonical composite account FK). All entities/FKs carry
O; organization membership composite FK uses positive INT4 membership_id, never user_id.
UUIDs are canonical nonzero, not silently narrowed to v4. Service-generated IDs remain
service owned. Versions/revisions use finite integral NUMERIC without typmod; head
expected=0 only valid init. No float, typmod rounding, artificial BIGINT ceiling or
business retention limit. Native PostgreSQL storage/index limits remain explicit.

All times finite UTC-compatible timestamptz (canonical six microseconds, year1..9999).
Opaque Review ID resolves exactly against existing TEXT/BYTEA pair; no numeric/title
normalization. Draft text is strict UTF8 BYTEA including NUL, exact SHA256 and nonblank
under agreed Python whitespace semantics; never TEXT-only, trim/drop or JSONB recovery.

## Relations and private witness fields

1. review_policy_versions: O, policy_id, version, manual approval_mode, exact bounded
template/model labels; canonical policy bytes/SHA, actor, created_at, creation audit ID.
PK(O,policy_id,version), checksum binding candidate key. Immutable; no requirement that
policy versions form a contiguous chain (the accepted codec permits independently
created version>BIGINT). Exactly one policy.created audit and completed create receipt;
same immutable target under a different command UUID conflicts, not duplicate audit.

2. review_policy_heads: O unique/PK, head_id unique scoped, version>0, current policy
ID/version/checksum, updated_at. First selected head version1 only, no empty head;
subsequent changes preserve head identity and increase version exactly1, including
same-target explicit selection. Scoped current policy FK. Captured transition audit
must match exact OLD/NEW, not just final row; no UPDATE no-op, owner change or delete.

3. review_draft_revisions: O, review_id, draft_id, revision, source observation ID/SHA,
policy ID/version/SHA, text bytes/SHA, decision-binding bytes/SHA, created_at/actor;
typed generation ID/mode/template/model/start/complete/previous_draft_id with canonical
generation bytes/SHA. Scoped unique draft ID and per-review revision; immutable source
observation full checksum FK and exact fact identity. previousDraftId absent/null for
fake, required previous actual same-review draft for manual_edit; no imported actor.
Capture policy_head_id/version plus historical policy.selected audit ID exact target.
Never FK captured version to mutable latest version. Publication audit and workflow
head participation required; no orphan draft/history insert behind head.

4. review_decisions: O, review_id, decision_id, exact draft ID/revision/binding SHA,
approved|rejected, actor, decided_at, decision audit ID. Immutable full scoped draft FK;
inherits immutable epoch/source/policy through draft. Time >= draft.created_at, exact
workflow transition witness. No HTTP approved=true interpreted as persisted authority.

5. review_workflow_heads: O+review_id unique, scoped head_id, version, current draft
ID/revision and optional current_decision_id, updated_at. Init draft revision1/head1
and decisionNULL. New draft increments draft revision and head version, clears decision;
decision preserves current draft revision and increments head version. Full scoped FK
ensures decision belongs to current draft. No empty head, rewinds or head-ID replacement.

6. review_local_audit: O,event_id, private aggregate_kind (policy_version|policy_head|
workflow_head), aggregate_id/version, five closed local event kinds, exact typed target
refs, actor, before/after state, occurred_at, canonical accepted audit bytes/SHA. Private
policy_version/draft_revision/review_id/local_command_id refs do not alter serializer.
Canonical actorKind=membership; commandId/attemptId/reasonCode=NULL. No private text or
binding descriptor in public audit envelope. Unique event ID scoped and one transition
per O/aggregate_kind/ID/version (stronger than event-kind-inclusive uniqueness); exact
event-kind↔category matrix prevents two different lifecycle events at the same version.
Scoped entity/head/receipt references, immutable; every entity/head transition has one
matching audit and every audit is justified by a real transition/entity.

7. review_local_command_receipts: O, local_command_id, actor, closed operation kind,
canonical request bytes/SHA, actual account binding schema/external ID/credential ref/
descriptor bytes/SHA, canonical result bytes/SHA, completed_at, unique scoped audit ID,
typed expected/result/head/entity refs needed to reconstruct exact request/result.
PK(org,account,local_command_id), actor and operation excluded from uniqueness.
Completed-only INSERT, no pending/failure/upsert state. Explicit null refs by operation.
Cross-row reconstruction uses immutable target/generation/policy/draft fields, not
unvalidated arbitrary request JSON. Result complete time=actual audit time; refs and
versions equal actual action, head expected+1, publish draft expected+1. Receipt↔audit
reciprocal reference, not one-way audit FK permitting orphan audit. Old receipt does
not claim current eligibility. Store descriptor private; actual account binding is
checked from locked physical row at new receipt creation, not trusted caller payload.

## Transaction guards

All DML takes account BEFORE STATEMENT lock first, strict canonical INT4 GUC and READ
COMMITTED; immediate NOT FOUND check. Actual service first obtains shared authenticated
membership/account guard then new schema participates in same caller-owned root.
No current context synthesized from JSON. New-row ownership must equal scoped GUC.
Immutable tables reject UPDATE including no-op/DELETE/TRUNCATE; heads only init/+1.

For actual services: fresh permission/account binding guard → exact scoped receipt
lookup before latest head/source checks. Matching actor/op/bytes/descriptor returns
historical original result after final live guard/root completion; changed actor,
operation, expected versions/text or rebind deny. New command checks current source,
current policy selection and actual workflow CAS, then writes complete entity/head/
audit/receipt root. Unknown COMMIT retries same UUID; no regenerate/rekey/assume success.

Immediate insertion/transition guards must check current policy/head/source context
at the relevant new action, while deferred checks validate immutable witnesses and
every captured OLD/NEW transition. This distinction permits valid select→draft→select
within one transaction without falsely requiring an earlier draft's epoch to equal
the final head, and prevents deferring every eligibility check until final pointers.
Current epoch mismatch on a NEW decision/publication fails; exact historical receipt
is still readable and never resets heads. P1→P2→P1 and same-target new select invalidate
older epoch for new actions without mass updates; future send service must check epoch.

Deferred validation verifies no orphans/ghost versions, one receipt/audit per action,
complete chronological policy/workflow transition chains, contiguous head versions,
draft revision progression independent of decision head increments, and exact final
pointers. Captured GUC owner cannot be hidden by scope switching at commit. Physical
rollback after any intermediate write leaves no entity/head/audit/receipt fragment.

## Exact bytes, RLS, ACL and rollback

New small SQL helpers generate only agreed fixed formats from typed projections:
policy, generation, decision binding, local audit, local request/result. Scalar string
helper accepts strict UTF8 BYTEA, emits JSON quoted bytes with exact Python escapes
(quote/backslash/control characters, NUL as \\u0000), leaves valid non-ASCII UTF8 bytes
unchanged. No whole-object jsonb::text, float, locale collation or decode-NUL-to-TEXT.
Numbers exact fixed integer text, sorted fixed keys and explicit nulls. Reuse existing
0068 binding codec for actual representable account metadata; no arbitrary NUL account
ID accepted merely because pure descriptor codec permits it. Main independent literal
fixtures include original T4 policy/generation/decision/audit vectors plus eight new
request/result vectors; copy from immutable commits, no runtime domain imports/git refs.

FORCE RLS O account isolation with canonical text GUC comparison, both USING/WITHCHECK;
no context/wrong org/wrong account denied. SQL provider must match canonical account.
Runtime nonowner/NOSUPERUSER/NOBYPASSRLS/NOINHERIT; immutable S/I, heads S/I/U only.
Migration intersects only new relation/column ACLs, PUBLICnone, removes grant options;
pure helpers only entitled roles, trigger EXECUTE none. No SECURITY DEFINER/old ACL or
default privilege mutation. Runtime script atomic broad grant then exact new narrowing.

Empty-only downgrade takes fixed new-table ACCESS EXCLUSIVE locks, row_security=off,
refuses any rows even hidden from runtime, drops explicit new dependencies without
CASCADE. Additive schema, no old migration rewrites/backfill/truncation. Once new history
exists rollback keeps decoder/schema and compatible encrypted reader; never delete
history/return unsafe writer to make downgrade pass. Operational flags unchanged.

## Acceptance and implementation boundaries

Must prove genuine predecessor RED, exact literals+typed mutation checks (not masked
by deferred orphan failure), empty upgrade/down/up, populated old0065NUL preservation,
runtime ACL/defaultACL/columnACL intersection, RLS independent of ordinary row guards,
all four atomic lifecycle actions, duplicate/different command races and lost-response
historical retries, forged intermediate transitions and valid multi-transition roots,
policy ABA, late current-source and revoked authority in real T4 service composition.
DDL tests must label trusted SQL participant vs actual authenticated service; nofake
principal or API/RLS claim from SQLite/in-memory stand-ins. All own disposable Unix PG.

Keep the seven relation names and shared types above. Source-current checks versus
trusted service checks remain explicitly separate; do not strengthen draft publication
to answerability (pure publication does not require it; approved decision does,
rejection does not). The resolved predecessor/generation/whitespace requirements below
are binding and supersede earlier shorthand. Final acceptance separates these gates.

Source provenance contract: a new draft's immutable source observation
must carry the accepted0068 source-run descriptor, not merely a matching org/account FK.
New action receipt binding and source provenance must match, while later rebind must
not rewrite/delete the historical receipt or run. Missing legacy binding is not guessed.
Separate immutable cross-row descriptor equality and physical current-row predicates
(DDL) from actual decoded-source validation and live session scope (T4 service); do
not claim authentication or normalized-provider truth from SQL. Mutation ordering
must retain account-first then exact fact/head locks; avoid
adding a reverse lock acquisition to old Fact triggers outside this additive scope.

## Resolved detail updates

Main fully read/accepted T4 bd44a911c25e05a948f86d979768e548a9e2a048 and
176d243df4ef3349e03fa31f1d4231f3e9064271. Manual edit uses immediately current
same-review predecessor; no first-manual-edit, stale predecessor with fresh versions,
reuse of same draft ID or currentness FK to mutable head. Same-text new draft clears
decision. New explicit manual edit may capture fresh source/policy, not relabel old
output. Generation UNIQUE(org,account,generation_id), not including review/actor/mode;
new command UUID with already-published generation always conflicts even exact bytes.
Original exact command replay is still first and stable. Rollback does not reserve
generation; two new commands racing same generation have one atomic winner. No new
time upper bound/TTL; generation completed>=started is the agreed representation.

To preserve Python nonblank semantics for exact UTF8 including NUL, validate with
existing review_strict_utf8 then compare code points against explicit whitespace
set U+0009..000D,001C..001F,0020,0085,00A0,1680,2000..200A,2028,2029,202F,205F,3000.
NUL is valid and not whitespace. Avoid locale regexp/trim as a surrogate. Test each
whitespace scalar, combined all-whitespace, mixed valid nonblank/control/NUL/Unicode,
invalid UTF8 and empty bytes. New scalar helper does not alter existing0065 behavior.

Receipt typed expectation/result columns are not free-form metadata: review_id and
expected_head_version/expected_draft_revision/expected_policy_head_id/version plus
result policy_id/version, draft_id/revision, decision_id, head_id/version. Every
operation's unused fields NULL, including expectations not applicable to it. Request
is reconstructed from these fields and scoped immutable policy/draft/generation or
decision records; no arbitrary JSON keyset retained as authoritative state. Publish
expected draft revision+1 equals actual stored revision; decision expected draft
revision is represented by exact result draft revision, not a second conflicting
column. Request externalReviewId from exact scoped fact identity. Result audit ID/time
are original persisted IDs/time; no latest-head recomputation.

Audit private fields aggregate_kind, review_id, policy_version, draft_revision and
local_command_id support complete typed FKs without modifying review-audit-v1 bytes.
All entity publication rows have scoped audit_event_id; audit has scoped completed
receipt FK; receipt has unique scoped audit_event_id. Deferred reciprocal links and
captured transition checks must reject ghost audit/receipt, not just satisfy FK
existence. Use constraint/index/trigger prefix review_local_ and the fixed helper
family below; all emitted SQL messages use closed safe codes, driver/native detail
remains private service translation. Internal helper argument layouts are implementation
details, not a second T4 API; exact typed signatures appear in the final handoff.

Additional Postgres skill review: keep leading owner composite indexes for scoped
lookup; index actual child FK lookup paths where not covered by existing PK/unique
prefix. No blanket duplicate indexes or broad old-table privilege reform. Keep
account-first lock order, transactions contain no provider/generation/network work;
RLS is defense in depth under trusted service context, not authentication from GUC.

Test hygiene requirement for the next schema: all immediate CHECK/FK negative cases
must assert the targeted INSERT/UPDATE raises within an outer rollback-only root,
or write a otherwise-complete valid bundle and verify the expected constraint. Do
not let missing deferred head/audit/receipt witnesses satisfy an unrelated wrong
provider/member/SKU/Review-parent test. Positive-control each relevant complete
bundle and validate exact independent failure surface. Constraint existence checks
and behavioral FK proof must remain separately reported.

## Fixed implementation surface and safe failures

Pure helper family: review_local_utf8_json_string(bytea) RETURNS bytea,
review_local_nonblank_utf8(bytea) RETURNS boolean,
review_local_integer_text(numeric,boolean) RETURNS text (second argument permits zero),
review_local_timestamp_text(timestamptz) RETURNS text;
review_local_policy_bytes, review_local_generation_bytes,
review_local_decision_binding_bytes, review_local_audit_bytes,
review_local_request_bytes and review_local_result_bytes each return BYTEA from
explicit typed fields/internal projections. None loads business rows or decides
authorization. Complete fixed field/key/null matrices above and T4 literal bytes are
binding; arbitrary caller JSON cannot substitute for actual persisted typed fields.
No new generic JSON-business-state framework or reused Production codec.

Trigger helpers: review_local_account_lock(), review_local_row_guard(),
review_local_validate(). Additional small private fixed-format helper functions may
only support this exact family, not introduce another relation or consumer boundary.
All helpers are SECURITY INVOKER with explicit search_path=pg_catalog,public;
pure functions IMMUTABLE, row-reading guards VOLATILE. Closed messages:
review_local_invalid, review_local_context_invalid, review_local_account_missing,
review_local_isolation_invalid, review_local_immutable, review_local_source_unbound,
review_local_policy_changed, review_local_predecessor_stale,
review_local_downgrade_nonempty. Integrity validation SQLSTATE23514, isolation25000,
nonempty downgrade55000. Native CHECK/FK/unique/type/permission errors are translated
by T4 into safe service errors, never logged/returned with raw request/text/descriptor.

Only additive migration0071, new-object runtime grant additions, four scoped test
modules, four copied synthetic fixtures and one handoff may change in implementation,
plus the two bounded existing test fixtures in the measured amendment below.
No old migrations/ORM/routers/domain code/config/dependencies/frontend/legacy writers,
no broad/default ACL changes or role creation in Alembic, no SECURITY DEFINER,
no production/working DB/Redis/real credentials/.env/providers/network/push/deploy/
flags/backfill/cleanup. Tests use own worktree backend/.venv, scrubbed environment,
accepted allocator-owned disposable Unix PG and explicit heavy-slot queue handoff.
No prefix catalog inventory, unrelated workloads, SQLite RLS substitute, skips,
timeout widening, baseline expansion or kill-as-success. Production assembly/printing
remains deferred; old compatibility only. Real service/retention/delivery approval
is separate, not guessed and not a blanket blocker for this local persistence.

For new publication/decision, SQL row guards lock the exact scoped Review Fact FOR
SHARE after the account, then policy head and workflow head in that order. Missing
head means absence only for first publication. Require fact.source_order_state=current
and fact.current_observation_id=the submitted immutable source observation; current
policy head identity/version and target/checksum match the captured/submitted epoch.
Decision additionally requires the current workflow draft ID/revision; approved
requires observation.answered=false and can_answer=true. Rejection/publication do
not require answerability. The actual T4 service repeats its decoded-source/current
context and live permission checks in the same root; no fake participant is an auth
proof. Guards check NEW action at INSERT, not all old history against final pointers,
so a later source/policy switch does not corrupt earlier valid captured witnesses.
Use fixed safe review_local_source_changed and review_local_not_answerable codes
(SQLSTATE23514) for those additional physical predicates.

## Measured compatibility amendment (2026-09-09)

The first exact ten-module adjacent run naturally returned375PASS/1FAIL/241setupERROR
in220.04s, after the new focused499 passed44.57s. Only two existing test paths are
additionally allowed: `tests/test_review_facts_schema.py`'s broad-privilege evidence
test, and `tests/test_sku_override_schema.py`'s runtime `db` fixture.

The old evidence test's TRUNCATE CASCADE now reaches new FK children before its
expected old immutable trigger; missing child TRUNCATE grants mask that assertion.
Grant TRUNCATE on all tables only within this test's exclusively owned disposable
public schema and rollback-only root. Preserve its existing UPDATE/DELETE grants,
exact old immutable-error assertions and verify ACL restoration after rollback.
No application/runtime grants, SQL guards or historical migration may change.

The current runtime grant script requires current canonical relations. Its SKU
fixture must therefore migrate actual head, as the existing Orders fixture does;
the new local Review runtime fixture must follow the same rule. All explicit
revision-pinned presence/preservation/upgrade-downgrade tests remain unchanged.
No conditional runtime-prerequisite weakening or mocked substitute grant script.

Implementation scope is now thirteen paths (twelve source/test/fixture hashes and
the handoff). Preserve measured failures, first verify both exact old nodes, then
repeat focused/ten-module compatibility and independent main covering checks.
This does not expand domain implementation, baseline failures or production access.
