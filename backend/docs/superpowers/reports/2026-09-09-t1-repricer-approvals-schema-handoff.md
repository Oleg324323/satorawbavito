# T1 → T2: account-owned approvals storage, revision 0066

2026-09-09. Local PostgreSQL prerequisite; independent controller review is the
next gate. This change installs storage only. It does not activate a repository,
authenticated worker, route, job, legacy writer cutover or provider call.

Authority: exact local Git `45f94a39627ebe2916903e9e5e0930844f3daaf5`
reserve/dispatch amendment and its three domain modules. The synthetic literal
fixture is byte-identical to
`e15495a89c16422b96811e1ba0ffa06580191a9e:backend/tests/fixtures/wb_repricing_sql_golden_vectors_v1.json`.
SQL parity uses those independently produced bytes; T2 Python modules were not
copied into or executed from this branch.

## Physical interface

Forward revision `20260909_0066` follows `20260909_0065`. Exactly three tables:

| Relation | Columns |
|---|---|
| `wb_repricer_price_approvals` | `approval_row_id`, `organization_id`, `marketplace_account_id`, `marketplace`, `approval_id`, `catalog_sku_id`, `nm_id`, `article_id`, `recommended_price_kopecks`, `request_checksum`, `action_key`, `request_format`, `canonical_request_bytes`, `discount_pct`, `size_id`, `min_price_kopecks`, `status`, `version`, `created_at`, `updated_at`, `claimed_by_membership_id`, `decided_by_membership_id`, `reason_code`, `safe_error_code`, `wb_upload_id`, `result_code`, `created_audit_id`, `claimed_audit_id`, `decision_audit_id`, `outcome_audit_id` |
| `wb_repricer_price_apply_attempts` | `attempt_id`, `organization_id`, `marketplace_account_id`, `approval_row_id`, `action_key`, `request_checksum`, `dispatch_key`, `claim_version`, `claimed_by_membership_id`, `status`, `version`, `reserved_at`, `updated_at`, `dispatch_at`, `finished_at`, `safe_error_code`, `wb_upload_id`, `result_code`, `reserved_audit_id`, `dispatched_audit_id`, `outcome_audit_id` |
| `wb_repricer_price_approval_audit` | `audit_event_id`, `organization_id`, `marketplace_account_id`, `approval_row_id`, `event_kind`, `actor_kind`, `actor_membership_id`, `before_status`, `after_status`, `before_version`, `after_version`, `occurred_at`, `attempt_id`, `before_attempt_version`, `after_attempt_version`, `reason_code`, `safe_error_code`, `wb_upload_id`, `result_code` |

Canonical org/account/catalog/membership references remain positive INTEGER.
Business and approval/audit/claim versions are finite integral unconstrained
NUMERIC; drivers return Decimal. Attempt versions and discount are SMALLINT.
Physical IDs are UUID4, supplied by the trusted application (no identity sequences
or UUID default). Exact business text is C-collated TEXT. Times are finite
TIMESTAMPTZ. No foreign key targets a mutable approval version or unbounded text/
NUMERIC. Surrogate and witness references are scoped composite deferred FKs.

Do not use `ON CONFLICT (organization_id, marketplace_account_id, approval_id)`:
there is deliberately no raw-text unique index. The scoped `action_key` and
one-lifetime-attempt constraints remain unique. Pending and recovery scan indexes
use UUID tie breakers. Exact ID lookup includes org, account and full text equality.

Installed serialization/validation functions (parameter order is contractual):

```text
public.repricer_exact_text(text) -> boolean
public.repricer_integral_finite(numeric) -> boolean
public.repricer_safe_code(text) -> boolean
public.repricer_ascii_json_string(text) -> text
public.repricer_integer_decimal(numeric) -> text
public.repricer_request_bytes(integer, integer, text, integer, numeric, text,
                             numeric, smallint, numeric, numeric) -> bytea
  org, account, approval ID, catalog SKU, nm ID, article,
  recommended price, discount, size ID, minimum price
public.repricer_action_key(integer, integer, text, text) -> text
  org, account, approval ID, request checksum
public.repricer_dispatch_key(integer, integer, text, text, uuid) -> text
  org, account, approval ID, action key, attempt ID
public.repricer_context_id(text) -> integer or NULL
  exact setting name; noncanonical/missing/out-of-INT4 values return NULL
```

Action key encodes account as a JSON STRING; request and dispatch encode account
as a JSON NUMBER. ASCII reconstruction preserves combining forms, supplementary
Unicode, exact escapes and explicit NULLs. `repricer_integer_decimal(NULL)` emits
`null`; fractions and nonfinite values raise `repricer_numeric_invalid`.
Pure helpers are immutable except the STABLE context reader. All have fixed
search_path. Trigger-only VOLATILE functions are `repricer_account_lock()`,
`repricer_row_guard()`, `repricer_validate()`, all SECURITY INVOKER and without
runtime/PUBLIC EXECUTE. No callable mutation API is installed.

## Transaction and witness insertion protocol

Use the accepted Engine-bound root Session helper
`set_marketplace_account_context(session, *, organization_id, marketplace_account_id)`.
It supplies transaction-local context, never permission. Acquire the trusted
authorization locks first: user SHARE → membership SHARE → session SHARE → WB
account UPDATE → exact credential metadata SHARE when needed → approval/attempt.
Use READ COMMITTED, no SAVEPOINT/autocommit/Connection-bound Session. Errors must
escape and roll back the caller's root. No provider I/O under these locks.

Each table has BEFORE STATEMENT account locking, before domain tuple mutation,
and BEFORE ROW scope/lifecycle/immutability checks. The separate VOLATILE exact
identity query sees a committed winner after waiting; the deferred validator
rechecks uniqueness under the held account lock. Repository exact replay performs
a fresh post-lock query, compares all immutable intent fields and bytes, then
returns the stored snapshot with no UPDATE/audit. A zero-row CAS writes no audit.

Generate witness UUIDs before inserting/updating the business row. Insert its
audit in the same transaction. FKs and captured transition validation defer to
commit, allowing these committed prefixes and the entire chain in one root:

1. pending V0 + `created_audit_id` + `approval.created` membership audit.
2. pending→applying V1 fills `claimed_audit_id`; claimant equals audit actor.
   Applying with zero attempts is a valid committed prefix.
3. Insert reserved A0 with frozen `claim_version=1`, claimant, dispatch key and
   `reserved_audit_id`; audit `attempt.reserved`. Approval is unchanged.
4. Reserved→dispatched A1 fills the write-once marker and `dispatched_audit_id`;
   `attempt.dispatched` audit. Approval remains V1 with its original update time.
5. Finish attempt and approval in one transaction. Approval V2 and attempt A2
   share the same `outcome_audit_id`, outcome metadata and finish/update time.
   One worker audit owns both mutations. Pre-marker failed is instead attempt A1.

Pending reject/block fills `decision_audit_id`, increments approval version once,
and supplies membership+reason audit. New failures use the exact eight-code
allowlist; post-marker failed accepts only `WB_APPLY_REJECTED`. Terminal rows and
markers cannot be revived or replaced. A marker replay returning zero rows is a
conflict, never send permission. SQL cannot authenticate provider rejection or
prove that a call occurred after a successful commit.

Captured OLD/NEW comparisons validate intermediate transitions even if final
state advanced again in the same transaction. Inverse audit validation requires
the exact durable witness UUID, rejects extra historical/ghost rows, and shares
outcomes across approval and attempt. Missing, duplicated, misbound or mismatched
audits abort the transaction. There is no fourth history/outbox relation.

Legacy import is INSERT only with `request_format=legacy`, bytes and v1-only typed
fields NULL, and a backfill `approval.imported` audit. Only CURRENT_USER equal to
the approvals table's catalog owner may import; a caller GUC or role inheritance
cannot grant import. Preserve key/checksum/status/version/actors exactly after
canonical identity preflight. Imported terminal/applying records have no attempt;
all legacy lifecycle updates are denied. No real import was performed.

## RLS, ACL and downgrade

All three relations ENABLE/FORCE RLS with organization AND account predicates.
Missing or malformed context denies. Runtime gets SELECT/INSERT/UPDATE on root
and attempt, SELECT/INSERT on audit; no DELETE/TRUNCATE/TRIGGER/REFERENCES, owner,
trigger bypass, sequence or grant-option authority is introduced here.

Migration intersects existing new-table relation and column ACLs with those
allowlists, removes PUBLIC/default function execution and grant options, and
restores necessary pure-helper execution for both table and column grantees.
Existing Orders/Reviews ACLs, data and default privileges are unchanged. This
requires exclusive privileged migration/ACL administration; table locks do not
prevent a separate privileged administrator from altering ACLs.

The runtime script applies new-object overrides after its broad grants inside
the existing transaction. It also clears preexisting new-function PUBLIC and
grant-option rights and new-table column overrides. Historical feature fixtures
pin0066; current script fixtures independently migrate latest head. A future
single successor does not invalidate the0066 ancestry gate.

Downgrade first locks all three relations ACCESS EXCLUSIVE, then sets local
row_security=off as a refusal defense and checks all rows. Genuine all-row
visibility is required: a FORCE-RLS-hidden owner receives a refusal, not an empty
result. Nonempty or hidden data leaves all schema/data intact. Only proven-empty
relations can be removed; no CASCADE or evidence deletion.

## Verification and limits

Before DDL, disposable0065 produced eleven expected undefined-helper/table
failures plus four passing literal-source/boundary controls: INTEGER/BIGINT
overflow SQLSTATE22003 and incompressible raw-TEXT B-tree SQLSTATE54000. Actual
first implementation proof31PASS expanded to140PASS. Comprehensive account-context
+ three new test modules on final source:283PASS82.06s, natural exit0, no skips.
Prescribed adjacent Orders/Reviews group:390PASS234.72s, natural exit0, no skips.
Affected RLS group after the grant-option correction:31PASS26.01s, natural exit0.
The ignored same-plan `task-2-report.md` contains exact commands, failures,
cleanup and final adjacent verification counts.

Real PostgreSQL16 Unix-socket sessions prove all lifecycle prefixes, multi-step
captured witnesses, five account-lock races, >BIGINT numbers, 8000-digit imported
versions, distinct/equal long IDs,4096 request boundary, Unicode controls,
rehashed lexical tampering, FK mismatches, runtime permissions, defaults/column
ACL intersection and empty/nonempty/hidden downgrade. All own random DBs/roles
were removed with exact absence assertions. This is disposable-database proof,
not a newly initialized native server or production schema proof.

PostgreSQL has finite storage/resource limits; neither Python integers nor text
are claimed unlimited. Invalid NUL/surrogate text and malformed lexical UUIDs
must be rejected by the accepted domain boundary before SQL casts. Custom SQL
errors are fixed codes; trusted service code must sanitize generic database
exceptions, which can otherwise expose statement/constraint details.

Used executing-plans, TDD, PostgreSQL and verification guidance. Manual isolated
Critic Pass found and fixed the column-reader helper privilege and runtime
function grant-option gaps. Named local preflight-critic skill is unavailable;
no reviewer subagent was spawned. T2 repository, authorization/provider and
activation acceptance remain separate controller/shared integration work.

## Later test-only regression strengthening

The verification above is the original0066 implementation evidence. A later
test-only follow-up from baseline
`99b295e1946314f312d1a1c1fc46fd21a213724f` strengthens two Minor review gaps;
it does not change revision0066, runtime SQL, ORM/domain code, role scripts or any
consumer.

The new populated0065 fixture commits one Avito Orders run/order/item/observation
and one complete Review run/fact/observation/run-item unit under the existing
guards. It snapshots exact driver-returned rows for those relations and their
organization/account parents, including identity values, timestamps, watermark
links and `review_observations.text_utf8` bytes
`b"preserved\x00\xd0\xbe\xd1\x82\xd0\xb7\xd1\x8b\xd0\xb2\xf0\x9f\x9a\x80"`.
Every selected relation is asserted nonempty. The same exact rows and preexisting
relation, column and default ACLs are compared after0066 upgrade, empty0066
downgrade to0065, and re-upgrade. New approvals relations are asserted empty when
present and absent at0065. A transaction-local permitted staging-run mutation
proves the row comparison fails, then rolls back; the ordinary unchanged
comparison passes. This is representative synthetic old-history preservation,
not universal production-shaped parity.

Optional numeric checks now build otherwise-valid independently serialized v1
requests. `size_id=1,min_price_kopecks=50` and both fields above BIGINT commit
with one creation audit and exact NUMERIC round-trip. Fractions and NaN/positive
Infinity/negative Infinity for each optional field must fail specifically with
SQLSTATE `P0001` and `repricer_numeric_invalid`; size0 and minimum49 instead bind
to their exact `23514` column CHECK names. Required numeric/version negatives are
also tightened to exact column CHECK names. An isolated disposable replacement
of only the numeric predicate changes the fractional failure to
`repricer_request_exact`, proving the diagnostic assertion detects a removed
numeric guard; the database is then destroyed.

The final prescribed three-module PostgreSQL gate on the strengthened tests reports
`199 passed in 124.46s`, natural exit0, with exact disposable database/role cleanup
messages. Focused RED was exactly two expected assertion failures; focused GREEN
was `26 passed, 142 deselected`. Compileall, scoped Ruff and `git diff --check`
also exit0. These are later local results and do not rewrite the original counts
above or claim a new full-backend green run. The inherited libpq `/dev/null`
passfile warnings remain visible and separately tracked as M3; no real passfile
was read and no warning was suppressed. Independent acceptance of this follow-up
remains the controller's gate.
