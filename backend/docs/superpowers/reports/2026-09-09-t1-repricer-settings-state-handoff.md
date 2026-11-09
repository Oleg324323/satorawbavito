# T1 → T2 typed Repricer settings/state storage

Status: **IMPLEMENTED / UNVERIFIED**. Source-first package only, 2026-09-09.
Migration `20260909_0075_repricer_settings_state.py`, revision `20260909_0075`,
predecessor `20260909_0074`. Dispatch branch `codex/arch-t1-platform`, HEAD
`297fcfd0f332902a72cecfcf4f0511ade0db6e07`. Exact source commit is recorded in the
controller task report after committing this three-path package.

Binding domain request, full state codec and all five literal inputs/bytes/hashes:
`21d458039e419fbe6c8b407312a2570a88a75dea`. Assignment codec and its committed literal
test are read from the same commit. Existing 0070 override storage/codecs and 0066
approval identity remain unchanged. Source graph observation found sole 0074 after
accounting for the existing 0040 merge tuple; no 0075 collision. This observation
is not an Alembic runtime/import gate.

## Scope and authority

Exactly nine new relations, all prefixed `wb_repricing_`: `settings_versions`,
`settings_heads`, `basket_norm_defaults`, `assignment_versions`, `assignment_heads`,
`liquidation_campaigns`, `liquidation_versions`, `liquidation_heads`, `state_audit`.
No JSON columns, settings aliases, financial inputs, calculation contexts, override
copies, old-parent indexes, backfill, current-writer changes or flag activation.

**No org-wide settings mutation permission has been approved.** SQL stores owner
identity, not live authorization. Runtime grants deliberately permit SELECT only
on settings versions/heads/children. The org writer stays closed until the owner
supplies its actual permission/scope decision and explicitly enables the matching
capability. One allowed account does not authorize org-wide changes. Account-owned
assignment/liquidation implementation can proceed independently.

T2 owns repositories: authenticated current membership, exact allowed account,
live allowed CatalogSku mapping, scoped historical replay before new-transition
eligibility, trusted payload resource limits, safe exceptions and activation fence.
No generic principal GUC, fabricated permission or all-account enumeration exists.
Settings auto-apply booleans are preferences only, never dispatch authorization.

## Exact columns and keys

In the descriptions below INT4 means PostgreSQL `integer`; NUMERIC is unconstrained
`numeric` with no precision/scale or BIGINT narrowing. TEXT uses `COLLATE "C"`.
All timestamps are `timestamptz`; no domain values have defaults.

The settings owner is `(organization_id INT4 > 0)`, FK to
`lk_organizations(organization_id)`. Account owners add
`marketplace_account_id INT4 > 0`, `catalog_sku_id INT4 > 0`, and
`marketplace TEXT NOT NULL = 'wb'`. Their FKs are exactly
`marketplace_accounts(organization_id,marketplace_account_id,marketplace)` and
`catalog_skus(organization_id,catalog_sku_id)`, alongside the organization FK.
The liquidation owner additionally includes `campaign_id UUID` constrained UUIDv4.
Marketplace is a discriminator, not a key part. All owner columns are NOT NULL.

Every `*_versions` row has these required columns in addition to its owner and
domain values: `revision NUMERIC`, `command_id UUID`, `audit_id UUID`,
`actor_membership_id INT4`, `created_at timestamptz`, `request_payload BYTEA`,
`request_checksum TEXT`. Only `parent_revision NUMERIC` is nullable. Revision is a
finite positive exact integer, parent NULL exactly at revision 1, otherwise parent
equals revision−1. Command and auxiliary audit UUIDs are v4. Actor >0 has composite
FK to `iam_memberships(organization_id,membership_id)`. Checksum is lowercase
64-character SHA256 of the full payload. PK is `(owner,revision)`; unique dedupe
key is `(owner,command_id)`. Scoped parent FK, reciprocal owner→head FK and
`(organization_id,audit_id)`→audit FK are DEFERRABLE INITIALLY DEFERRED.

Every `*_heads` row has the same owner and required `current_revision NUMERIC`,
`version NUMERIC`, `updated_at timestamptz`; PK owner; scoped current-revision FK
DEFERRABLE INITIALLY DEFERRED. Version is positive integral and equals current
revision. INSERT starts at 1; UPDATE keeps owner/discriminator and advances exactly
one. Updated time is nondecreasing and equals that revision's created time. Every
captured head mutation, including intermediate updates in one transaction, must
reference its own matching revision; the final head must end the complete history.

### settings_versions and basket_norm_defaults

Required `formula_compatibility_version TEXT` and `night_median_timezone TEXT`
use existing Python-strip-compatible `repricer_exact_text(text)`, preserving text.
The timezone is not normalized by SQL.

Required BOOLEAN columns:
`night_median_enabled`, `night_median_collect_enabled`, `night_median_global`,
`night_median_auto_apply_enabled`, `worker_auto_apply_prices_enabled`,
`min_price_sync_enabled`, `price_jump_protection_enabled`, `discount_step_enabled`,
`price_rounding_enabled`, `liquidation_auto_flag_enabled`.

Required finite NUMERIC columns, accepting negative/zero without new business clamps:
`target_margin_pct`, `price_step_pct`, `max_price_change_daily_pct`,
`promo_margin_threshold_pct`, `price_jump_stock_value_min_pct`,
`price_jump_spp_min_pct`, `price_jump_stock_qty_min_pct`, `csv_max_cost_drop_pct`,
`csv_max_price_drop_pct`, `discount_step_pct`, `night_median_apply_delta_pct`,
`warmup_margin_pct`, `warmup_daily_limit_pct`, `liquidation_step_pct`,
`liquidation_min_cogs_pct`.

Required INT4 >0: `sync_interval_minutes`, `basket_norm_period_days`,
`cart_comparison_days`, `plan_fact_fact_period_days`, `plan_fact_interval_hours`,
`warmup_days`. Required INT4 >=0: `cart_high_baskets_threshold`,
`cart_low_baskets_threshold`, `basket_norm_auto_min_orders`, `warmup_exit_baskets`.
Required INT4 0..23: `night_median_window_start_hour`, `night_median_window_end_hour`.

Required TEXT enums: `night_median_mode` conservative/aggressive;
`basket_signal_mode` matrix/thresholds; `basket_norm_mode`
fallback_by_type/auto/manual; `plan_fact_metric` orders/revenue/margin.

`basket_norm_defaults` columns are required `organization_id INT4 >0`,
`settings_revision NUMERIC positive integral`, `garment TEXT`
tshirt/hoodie/longsleeve, `norm_units INT4 >=0`. PK
`(organization_id,settings_revision,garment)` is also the needed child index.
Scoped revision FK is deferred. Fallback requires all three children at commit;
auto/manual retain the explicitly supplied zero-to-three. Children are immutable;
adding one to old history changes reconstructed bytes and rejects at commit.

### assignment_versions

Nullable `strategy_id TEXT` is exactly baskets_orders/night_price_mode/
plan_fact_interval/illiquid or explicit NULL. Nullable `interval_hours INT4 >0`
is present exactly for plan_fact_interval. Required `assigned_at timestamptz`,
`source TEXT` manual/legacy_import. assigned_at <= revision.created_at. Explicit
unassign is allowed as the first revision: both strategy/interval NULL, parent
NULL, event cleared. Other custom strategy/config branches remain unsupported.

### liquidation_campaigns, liquidation_versions and liquidation_heads

Campaign PK is `(org,account,SKU,campaign_id)`. All campaign owner/origin columns
are immutable: required `created_at timestamptz`, `started_by_membership_id INT4 >0`
with scoped IAM FK, `start_price_kopecks NUMERIC positive exact integer`.
Campaign→head and version→campaign FKs use the complete owner and are deferred.

Both liquidation version and head have this exact typed state projection:

- Required `state TEXT`: active/paused/completed/cancelled.
- Required `current_price_kopecks NUMERIC`, `target_price_kopecks NUMERIC`:
  positive finite exact integers.
- Required `step_pct NUMERIC`: finite >0; `hold_orders_to INT4`: >=0.
- `next_step_at timestamptz`: present exactly while active, otherwise NULL.
- Required `requires_negative_margin_confirm BOOLEAN`.
- Nullable `confirmed_by_membership_id INT4 >0`, `confirmed_at timestamptz`:
  both NULL or both present; scoped IAM FK; campaign.created_at <= confirmed_at
  <= version.created_at.
- Nullable `resulting_approval_id TEXT`, `resulting_approval_row_id UUID`:
  both NULL or both present. Logical ID uses exact nonblank text validation.

Partial UNIQUE `(organization_id,marketplace_account_id,catalog_sku_id)` on head
WHERE state IN ('active','paused') retains the unfinished slot while paused.
Terminal heads are retained, and campaign IDs cannot be recycled. First revision
is active and actor equals immutable origin member; revision.created_at >= campaign
created_at. Allowed later transitions: active→active/paused/completed/cancelled;
paused→active/completed/cancelled. Terminal and paused→paused reject. Changed target
or step with new requires-confirm=true must clear confirmation. This check does
not prove actual confirming membership authorization; T2 verifies that separately.
Final and intermediate head projections must exactly equal referenced revisions.

The physical approval FK is exactly
`(organization_id,marketplace_account_id,resulting_approval_row_id)` → existing
`wb_repricer_price_approvals(organization_id,marketplace_account_id,approval_row_id)`.
The deferred witness also requires parent logical `approval_id` equality and exact
non-NULL `catalog_sku_id` equality. No article inference. Existing 0066 makes that
binding immutable. Logical TEXT, including Unicode, is in the canonical request;
auxiliary physical UUID is excluded. No resulting approval means no reference and
is not evidence of a send or price observation.

### state_audit

Required auxiliary `audit_id UUIDv4 PRIMARY KEY` and unique `(organization_id,audit_id)`
provide a non-null physical reciprocal FK. This UUID is independently supplied by
the trusted transaction and excluded from T2 bytes; it is not the dedupe scope.
Required `organization_id INT4 >0`, `domain TEXT` settings/assignment/liquidation,
`command_id UUIDv4`, `actor_membership_id INT4 >0`, `occurred_at timestamptz`,
`event_kind TEXT` created/replaced/cleared/paused/resumed/completed/cancelled.
Organization, account-provider, SKU and actor FKs use the exact canonical keys above.

Nullable owner columns: `marketplace_account_id INT4 >0`, `catalog_sku_id INT4 >0`,
`marketplace TEXT`, `campaign_id UUIDv4`. Settings requires all four NULL; assignment
requires WB/account/SKU and campaign NULL; liquidation requires all four present.
Nullable NUMERIC columns are exactly `settings_before`, `settings_after`,
`assignment_before`, `assignment_after`, `liquidation_before`, `liquidation_after`.
The active family's after is required, before is NULL only initially, and all other
family columns must be NULL. Each before/after has its full scoped deferred FK;
after is positive integral and equals coalesce(before,0)+1.

UNIQUE NULLS NOT DISTINCT `(organization_id,domain,marketplace_account_id,
catalog_sku_id,campaign_id,command_id)` closes nullable dedupe loopholes. Complete
history iteration validates every revision's reciprocal audit ID, owner, command,
actor, parent/after, event and equal created/occurred time. Counts equal head.version,
revisions are contiguous and chronological, and final time/revision equal the head.
No raw metadata, names, request JSON or caller-supplied before/after values are stored.
Revision reference columns are typed witnesses, not arbitrary change descriptions.

Events: settings created/replaced; assignment cleared when new strategy is NULL,
otherwise created/replaced; liquidation created, active→active replaced,
active→paused paused, paused→active resumed, then corresponding terminal event.

## Exact helper signatures and bytes

New pure functions (all public, SECURITY INVOKER, fixed search_path):

- `wb_state_uuid(uuid) RETURNS boolean`
- `wb_state_time(timestamptz) RETURNS boolean`
- `wb_state_timestamp(timestamptz) RETURNS text`
- `wb_state_json(jsonb) RETURNS text`
- `wb_state_settings_bytes(wb_repricing_settings_versions, wb_repricing_basket_norm_defaults[]) RETURNS bytea`
- `wb_state_assignment_bytes(wb_repricing_assignment_versions) RETURNS bytea`
- `wb_state_liquidation_bytes(wb_repricing_liquidation_versions, wb_repricing_liquidation_campaigns) RETURNS bytea`

The composite arguments are typed SQL rows; no table-query or authentication is
performed by codecs. Settings child array in validation is loaded from the exact
revision and sorted by garment. The JSON helper receives transient internally
constructed values, recursively emits sorted compact ASCII with the existing
`repricer_ascii_json_string(text)`, and exposes no arbitrary storage extension.
NUMERIC money/version/percentages are canonical decimal **strings**, bounded INT4
and booleans retain JSON types, nulls are explicit, timestamps are UTC six-digit
microseconds with Z. Timestamp validation accepts finite years 1..9999. SHA256
alone is insufficient: every revision's payload must equal full reconstructed bytes.
Native NUMERIC limits remain hard representability limits; no precision/scale,
float conversion, exponent rounding policy or synthetic 16384 production limit.
Do not regenerate expected literals through these helpers for acceptance tests.

Exact reused dependencies: `repricer_exact_text(text)`,
`repricer_ascii_json_string(text)`, `wb_sku_override_decimal(numeric)`,
`wb_sku_override_integral(numeric)`. Their implementations/signatures are unchanged.
Trigger-only functions: `wb_state_lock()`, `wb_state_guard()`,
`wb_state_settings_witness()`, `wb_state_assignment_witness()`,
`wb_state_liquidation_witness()`. Runtime has no EXECUTE grant on these.

## Transaction tuples, locking and replay

Use one READ COMMITTED transaction with trusted `app.organization_id`; account
families additionally require exact `app.marketplace_account_id`. Settings RLS uses
only org. Audit RLS uses org and requires account for non-settings rows. Every table
has ENABLE + FORCE RLS. Deferred triggers compare their captured owner to current
context before querying; changing GUCs cannot hide prior deferred writes.

For assignments/liquidation, lock the canonical WB marketplace_accounts row FOR
UPDATE **before** domain heads. For org settings, the required serialization row
is the exact `lk_organizations` row FOR UPDATE before settings head; this is only
a future authorized service contract. SQL statement triggers acquire these locks;
audit row insertion acquires the same family lock. Avoid mixing unrelated owners
or switching transaction context. Existing parent SELECT and UPDATE on at least
one column are needed for PostgreSQL SELECT FOR UPDATE; full parent UPDATE is not
required. This package adds no old-parent grants. Existing runtime broad grants
already include them, while future narrower roles must explicitly supply the
minimal row-lock capability after authorization is decided.

After authenticating/authorizing the owner, SELECT history by full owner + command
UUID without FOR UPDATE. Exact existing actor, canonical payload/checksum and
binding return that original revision/audit without new writes, even after later
transitions. Different intent is a conflict. UUID-only lookup, current-head-only
replay and head UPDATE privilege for history reads are forbidden. Database unique
constraints reject duplicate insertion; repository resolves replay explicitly.

For a new command, use trusted expected_version N, revision N+1, parent NULL at
N=0 otherwise N; choose one new auxiliary audit UUID and one DB-clock timestamp.

1. Settings tuple, still runtime-closed: INSERT full settings revision with exact
   payload/hash/audit UUID; INSERT exactly supplied typed children; INSERT head at
   N=0 or UPDATE head with full org + version=N CAS; INSERT audit domain settings,
   settings_before=parent/settings_after=N+1, other refs/owners NULL, matching
   command/member/audit UUID/time/event.
2. Assignment tuple: INSERT full assignment revision; INSERT initial head or
   UPDATE `(org,account,SKU)` head WHERE version=N setting current_revision=version=N+1,
   updated_at=revision.created_at; INSERT matching domain assignment audit, NULL
   campaign/settings/liquidation refs, assignment_before/after and derived event.
3. Liquidation tuple: INSERT immutable campaign only at creation; INSERT full
   liquidation revision including resolved physical approval UUID if present;
   INSERT head or CAS by full `(org,account,SKU,campaign)` + version=N, copying all
   eleven state projection columns and revision/version/time; INSERT corresponding
   liquidation audit with other family refs NULL and transition-derived event.

All must commit together. Require exactly one affected CAS row, otherwise rollback
the complete transaction. Roll back also on uniqueness, deferred witness or FK
failure; do not retain revision/child/audit debris or retry just the head. Campaign
slot conflicts remain domain conflicts. No provider operation belongs in this tuple.

## ACL and downgrade

Migration strips every nonowner table and column ACL on the nine new objects,
including default grants to inherited group roles; removes PUBLIC/default EXECUTE
on the exact new pure/trigger signatures. It does not modify old ACL defaults,
objects, roles or function implementations. No operational role script was run.

The additive runtime grant block follows existing broad grants. It strips runtime
and PUBLIC table/column rights for the nine objects; grants settings SELECT only;
grants SELECT/INSERT to assignment/liquidation histories, campaigns, heads and audit.
Assignment head UPDATE is only current_revision/version/updated_at; liquidation
head UPDATE is only these three plus the eleven projection fields listed above.
There is no history UPDATE, DELETE, TRUNCATE or head-owner UPDATE grant. Exact new
pure codecs and their four existing pure dependencies are callable; trigger
functions are not. This script changes capabilities only if separately executed.

Downgrade acquires ACCESS EXCLUSIVE locks across all nine tables in stable order,
sets LOCAL row_security=off, and checks unfiltered emptiness. Actual maintenance
BYPASSRLS/superuser authority is required; lack of authority fails instead of
hiding rows. Any retained row rejects downgrade. Trigger/function/FK/table removal
is explicitly ordered, without CASCADE; every 0070/0066 object is preserved.

Fixed SQL exception messages: `wb_state_invalid`, `wb_state_time_invalid`,
`wb_state_immutable`, `wb_state_context_invalid`, `wb_state_owner_missing`,
`wb_state_conflict`, `wb_state_children_invalid`, `wb_state_transition_invalid`,
`wb_state_confirmation_invalid`, `wb_state_approval_invalid`, `wb_state_graph_invalid`,
`wb_state_projection_invalid`, `wb_state_audit_invalid`, `wb_state_payload_invalid`
use SQLSTATE 23514. `wb_state_isolation_invalid` uses 25000;
`wb_state_downgrade_nonempty` uses 55000; grant precondition
`wb_state_forced_rls_required` uses 23514. Native NOT NULL 23502, FK 23503,
unique 23505, check 23514, NUMERIC overflow 22003, privilege/RLS 42501,
deadlock 40P01 and serialization 40001 are also possible. Reused decimal helper
can emit `wb_sku_override_invalid`/23514. Services must map these to safe domain
errors and never return raw PostgreSQL DETAIL, which may contain input values.

## Deferred final gates and dependencies

**All gates below NOT RUN for this package by explicit source-first instruction.**
No tests authored, RED/test execution, import/compile/lint, Alembic runtime,
PostgreSQL, Redis, role execution, review or critic pass. No PASS/READY claim.

Central final verification must consume the five pinned literal state vectors
(settings-zero, settings-exact, liquidation-active, liquidation-paused,
liquidation-terminal-large) and committed exact assignment literal. It must cover
SQL bytes/hash parity; negative fields/children/aliases/enums/NUMERIC; canonical
times and representability limits; no-context/wrong-org/account forced RLS;
context-switch deferred writes; narrow ACLs and denied history mutations;
same-head CAS one winner, different SKU updates preserved; partial active/paused
slot; terminal rejection, exact projection and confirmation reset; complete
rollback, immutable old children/refs, exact historical replay; actual logical +
physical approval alignment and null/mismatched parent SKU; reciprocal contiguous
audit/head graph; all empty-only downgrade cases under real maintenance authority;
synthetic empty and production-shaped upgrade→downgrade→upgrade. Final independent
review/critic and appropriate adjacent regressions remain deferred, not waived.

Reproducible calculation context remains unavailable: immutable mapping and dated
source/economic references still need their actual contracts. Mutable updated_at
is not a mapping version. Org permission, T2 repository proof, unsupported legacy
strategy/settings compatibility, current-writer parity and activation remain open.
