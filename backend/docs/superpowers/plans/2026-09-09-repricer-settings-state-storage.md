# Repricer settings, assignments and liquidation physical storage

Binding domain spec:
21d458039e419fbe6c8b407312a2570a88a75dea:
backend/docs/superpowers/reports/2026-09-09-t2-settings-state-schema-request.md.
That full request and exact codecs are authoritative; the physical resolutions
below restrict this package, not the financial rules. Existing0070 SKU overrides
are already implemented and must NOT be duplicated, moved or re-audited here.
Implementation precedes final tests/review by explicit user order.

## Task 1: Implement the three remaining typed state families

Read the complete binding request, same-commit modules/wb_repricing_state_commands.py
and five literal vectors tests/fixtures/wb_repricing_state_golden_v1.json; read
actual modules/wb_repricing_assignments.py with its committed exact command
fixtures; use git show if domain files absent locally. Read existing0070 helpers,
0066 approval keys, catalog SKU/mapping and IAM/organization keys and grants.

Allowed exactly three source paths:

- New backend/alembic/versions/YYYYMMDD_NNNN_repricer_settings_state.py. Controller
  supplies exact next revision and sole predecessor immediately before dispatch;
  stop on mismatch/collision, never silently renumber or edit old migrations.
- backend/ops/runtime-db-role.sql, new relations/helper grants only.
- backend/docs/superpowers/reports/2026-09-09-t1-repricer-settings-state-handoff.md.

No tests or intermediate PostgreSQL/Redis/import/compile/review gates now. No T2
modules, providers, production, real credentials/.env, flag/default activation,
backfill/globals import, foreign worktree or shared frontend edits. No preferences,
economics/calculation-context or unsupported strategy fallback JSON storage.

### Physical choices, not domain redesign

Nine new tables using requested names:
wb_repricing_settings_versions, wb_repricing_settings_heads,
wb_repricing_basket_norm_defaults, wb_repricing_assignment_versions,
wb_repricing_assignment_heads, wb_repricing_liquidation_campaigns,
wb_repricing_liquidation_versions, wb_repricing_liquidation_heads,
wb_repricing_state_audit.

Settings versions/children/head are org-owned, FORCE org RLS, not duplicated per
account. Assignment/campaign/revision/head are exact org/account/CatalogSku WB
owned, FORCE BOTH org/account RLS with canonical account-provider and SKU-org FKs.
State audit supports only settings/assignment/liquidation;0070 keeps its existing
separate override audit. This is not a global new audit bus. Domain-scoped audit
owner/ref matrix has exactly the active family revision columns and NULL others;
settings has no account/SKU/campaign. Use NULLS NOT DISTINCT unique where needed,
not a nullable identity loophole. Current context must not hide deferred writes.

Immutable versions and audit; mutable heads only. First revision/head/audit same
commit, no orphan or empty head; revision and head.version positive exact NUMERIC,
new=old+1, parent NULL only initial1, head current_revision=version. Exact owner+
command UUIDv4 dedupe; same actor/request bytes/binding historical replay before
new-transition eligibility, other intent conflicts. Expose request_payload BYTEA
and SHA on every immutable command revision; SQL reconstructs exact domain bytes,
not hash-only equality. Auxiliary typed audit witness fields documented explicitly,
no raw JSON or user-supplied before/after. Full reciprocal graph and contiguous
history checks; old references cannot mutate. Native NUMERIC overflow rejects,
no forced precision scale, float, BIGINT narrowing or exponent-induced rounding.

Settings has all and only exact AlgorithmSettingsValues scalar fields, all explicit
and non-null. Finite percentages permit negative and zero per existing contract,
no new business clamp. Exact integer bounds, enums and timezone text from codec;
no alias columns or guessing timezone normalization. Basket child enum keys unique
and sorted by garment for request bytes; fallback_by_type requires all three at
commit, auto/manual allow exactly the explicitly supplied zero-to-three children.
Stored auto-apply preferences are NOT deployment/worker authorization.

Assignments exactly four strategy IDs or explicit NULL unassign; interval only
for plan_fact_interval, assigned_at and manual|legacy_import source required.
First explicit unassign is legitimate revision1/cleared audit, no fictional prior
assignment. Other legacy custom configs remain blocked for import/cutover.

Liquidation campaign immutable UUIDv4/origin member/time/start money, a separate
mutable head retains exact current state projection plus revision/version. Partial
UNIQUE(org,account,SKU) on head state IN(active,paused) enforces one unfinished
campaign; terminal heads retained, no campaign recycling. State projection must
equal referenced immutable revision. No paused→paused or terminal transition;
active→active/paused/completed/cancelled and paused→active/completed/cancelled only.
Revision1 active and actor==origin. Current/target money positive exact integers;
positive finite step, hold count INT4>=0; active requires next_step_at, othersNULL.
Confirmation member/time bothNULL or bothpresent; target/step change while new
guard requires confirmation must clear both. That is a version-bound decision,
not proof of fresh permission; service verifies actual confirming membership.

resulting_approval_id is the exact logical TEXT in T2 bytes, NOT UUID. Actual0066
has scoped physical approval_row_id unique, but logical approval_id uniqueness is
trigger-enforced, not an FK key. Store a nullable resolved physical
resulting_approval_row_id together with logical ID, bothNULL or bothpresent; scoped
FK to real0066 physical key and equality check of logical ID/account/SKU. If parent
catalog_sku_id is NULL or differs, reject reference; never infer SKU from article.
The auxiliary physical UUID is excluded from T2 canonical request. Do not add an
old-parent unique index or cast the logical ID to UUID merely to create this FK.
No resulting approval means no reference, not a successful send/current-price fact.

Timestamps finite years1..9999, canonical UTC six-microsecond Z bytes, chronology
and audit time checks under DBclock. Actual source request allows planned next_step
times; no invented freshness/timezone/lease/retention policy. Existing decimal and
ASCII escaping helpers may be reused by exact signature; do not alter0070 codecs.
Reconstruct exact settings/children, assignment and liquidation command bytes,
including canonical decimal strings for versions/money/NUMERIC and JSON integers
for bounded INT4. Synthetic16384 budget is not a production payload limit.

Account domain mutations serialize canonical account before domain heads; immutable
history lookup requires no UPDATE privilege. Org settings mutations serialize the
existing organization before settings head, with actual minimal row-lock privilege
requirements disclosed. No generic GUC-supplied principal or global setting edit
from one arbitrary allowed account. SQL validates owner, not live authorization.
The org-settings shared permission/scope decision remains a T1/owner runtime input;
inert DDL need not guess whether selected-account members can edit all-org settings.

Narrow ACLs on new objects only: remove PUBLIC and inherited/default table/column/
exact-function leaks, immutable SELECT/INSERT, head UPDATE only mutable columns,
no DELETE/TRUNCATE/history updates/trigger-function execution. Grant only exact
helper dependencies; avoid duplicate child indexes. Downgrade unfiltered empty-only
under actual maintenance authority, stable locks across all nine relations, refuse
any hidden retained row, explicit dependencies, no CASCADE. Preserve all0070/0066
objects and preexisting user data; no operational roles created or grant script run.

### Handoff and final verification

Publish exact full columns/types/FKs/constraints/helpers/grants, all safe SQL codes,
head/revision/audit write tuples, account/organization lock requirements, lossless
logical+physical approval reference and RLS context distinction. T2 remains domain
service owner. No claim reproducible calculation context: immutable mapping and
dated source/economic references remain absent prerequisites, not updated_at tokens.

Record pending final SQL parity for all five pinned settings/liquidation vectors
and existing assignment literals; negative fields/children/aliases/enum/NUMERIC
cases, forced no-context/wrong-org/account RLS, same-head CAS one winner, different
SKU edits preserved, partial active/paused slot, terminal/history/confirmation
invalidation, full rollback and exact replay, actual approval alignment, ACL/empty
downgrade and synthetic empty/production-shaped upgrade→downgrade→upgrade. Tests
must consume pinned literals, not generate expected bytes with implementation.

One source commit: `feat: add typed repricer settings and state storage`.
IMPLEMENTED / UNVERIFIED; no amend/rebase, current writer/parity/activation remain
explicitly pending. Do not replace source implementation with more planning/tests.
