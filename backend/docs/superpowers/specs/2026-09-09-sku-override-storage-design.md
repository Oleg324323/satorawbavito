# Account-scoped canonical SKU override persistence

## Decision and scope

Implement the bounded SKU-override portion of T2 request `bfc6c0eb156ef5b6a1d0a5b14066a6c7bb0ad01a`, with literal codec vectors `a6b9fd1975d509911abc9da666bc0049e49228ce`. Root authorized autonomous reversible technical choices inside this requirement and prioritized this actual T2 dependency. This is not a new general settings framework or a Production task.

Use three dedicated relations: immutable `wb_repricing_sku_override_versions`, mutable `wb_repricing_sku_override_heads`, append-only `wb_repricing_sku_override_audit`. T2 explicitly accepted the dedicated audit instead of the larger polymorphic `wb_repricing_state_audit`. Reuse existing parent keys, forced RLS conventions and account-first statement locking. Alternative service-only integrity would permit committed orphan history via runtime SQL; a generalized multi-domain event store would expand scope unnecessarily. Dedicated normalized rows with deferred reciprocal witnesses supply the required bounded persistence contract.

Only new migration, runtime grants additions, three scoped test modules, a synthetic literal fixture and handoff report may change. No old migrations, domain implementation, ORM, configuration, routes/tasks, dependencies, frontend, legacy state/writers or Production feature code changes. No production/working data/real credentials/`.env`/network/provider/push/deploy/flag actions. All PostgreSQL proof uses fresh allocator-owned disposable databases and roles over the accepted local Unix socket, with environment scrubbed and IP traffic denied. Heavy tests require explicit team queue handoff; no prefix catalog enumeration or unrelated workload inspection.

## Physical owner and typed values

`organization_id`, `marketplace_account_id`, `catalog_sku_id`, `actor_membership_id` are positive PostgreSQL INTEGER (INT4). Every relation has the first three owner fields. Versions also have `marketplace TEXT NOT NULL CHECK(marketplace='wb')` and composite FK `(organization_id,marketplace_account_id,marketplace)` to canonical `marketplace_accounts(organization_id,marketplace_account_id,marketplace)`. Owner SKU FK references `catalog_skus(organization_id,catalog_sku_id)`; actor FK references `iam_memberships(organization_id,membership_id)`. Heads/audit are scoped to the version relation and have direct account/SKU owner references as well; no cascade deletion or fabricated author.

Version PK is full owner plus `revision NUMERIC` without typmod; revision is finite, integral and positive. `parent_revision NUMERIC` is NULL exactly when revision=1, otherwise equals revision-1 and has a full-owner deferred self FK. Full-owner `command_id UUID NOT NULL` is unique and UUID4/RFC4122 variant checked via canonical UUID text; UUID cast normalization cannot prove original HTTP lexical spelling, so T2 retains strict command parsing. `created_at TIMESTAMPTZ NOT NULL` is finite. `actor_membership_id`, `canonical_request_bytes BYTEA`, `request_checksum TEXT COLLATE "C"` (64 lowercase hex, exact SHA-256 of bytes) are NOT NULL.

All fourteen override columns are nullable. Missing override means inherit; explicit zero/false remains distinct. Do not add formula clamps or a p_min<=p_max rule.

| Columns | Physical type and checks |
| --- | --- |
| automation_enabled, allow_negative_margin, night_median_enabled | BOOLEAN |
| p_min_kopecks, p_max_kopecks, rrp_kopecks, min_margin_kopecks, max_margin_kopecks | NUMERIC without typmod; finite, integral, >=0 |
| min_margin_pct, max_margin_pct, price_step_pct | NUMERIC without typmod; finite, no business clamp |
| price_step_minutes | INTEGER >0 |
| basket_norm_manual | INTEGER >=0 |
| basket_norm_mode | TEXT COLLATE "C", auto/manual/fallback_by_type only |

NUMERIC is required because accepted commands include `2**80` money/version. PostgreSQL native numeric storage/index limits still apply; this is not unlimited integer storage. No typmod, BIGINT narrowing, floating-point conversion, rounding or fabricated production resource cap. The golden fixture's 8192-byte budget is synthetic only.

Heads PK is owner; `current_revision NUMERIC NOT NULL`, `version NUMERIC NOT NULL CHECK(version=current_revision AND version>0)` finite/integral, `updated_at TIMESTAMPTZ NOT NULL` finite. Full-owner/current_revision FK to versions is deferred. A deferred owner FK from every version to its head forbids orphan history. First head is version1 only. UPDATE preserves owner and increments version/current_revision exactly one, with nondecreasing updated_at. DELETE/TRUNCATE are forbidden.

Audit PK is owner plus `after_revision NUMERIC`; full-owner command UUID is unique. Store `before_revision NUMERIC` (nullable for first), `after_revision`, `command_id`, `actor_membership_id`, `event_kind TEXT` (`created` iff before NULL, otherwise `replaced`), `occurred_at TIMESTAMPTZ`. Before/after full-owner version FKs are deferred; after=coalesce(before,0)+1. Actor and owner references are scoped. Every version must have exactly one matching audit and vice versa. No arbitrary JSON metadata, secrets or duplicate payload in audit.

## Canonical SQL contract

Export pure SQL helper:

```sql
public.wb_sku_override_bytes(
 organization_id integer, marketplace_account_id integer, catalog_sku_id integer,
 actor_membership_id integer, command_id uuid, expected_version numeric,
 values_row jsonb
) RETURNS bytea
```

`values_row` is an internal typed-column projection: exactly fourteen keys above; JSON null, booleans, NUMERIC JSON numbers for money/percent, INTEGER JSON numbers for minutes/manual, enum JSON string. It is not the API command body and may not omit keys. Reject extra/missing keys, wrong types, nonfinite/fractional integer fields, invalid owner/UUID and negative/nonintegral expected_version. Normalize allowed decimal mathematical values, not lexical JSON tokens. Construct this projection from actual typed stored columns for the row CHECK, never trust a supplied JSON payload instead of the columns.

Result is T2's compact, sorted ASCII JSON with exact keys/order:
`actorMembershipId`, `catalogSkuId`, `commandId`, `commandKind`, `expectedVersion`, `marketplaceAccountId`, `organizationId`, `schema`, `values`. Schema=`wb-repricing-sku-overrides/v1`, commandKind=`replace_overrides`. Nested values keys sorted with C collation. IDs/minutes/manual are JSON numbers, booleans remain booleans, enum is JSON string, money/percent/expectedVersion are quoted normalized decimal strings, NULL is JSON null. No JSONB text representation shortcut: its object ordering/spacing is not this codec. All valid strings are fixed ASCII enums/keys/UUID/numerals; no Unicode helper or unrelated codec dependency is needed.

Helpers may include `wb_sku_override_decimal(numeric) RETURNS text` (finite fixed point, normalize fractional trailing zero and signed zero) and `wb_sku_override_integral(numeric) RETURNS boolean` (false for NULL/nonfinite/fractional). Declare explicit safe search_path, SECURITY INVOKER and immutable pure helper semantics. Trigger helpers are separate, non-public invocation. Errors use fixed codes `wb_sku_override_invalid`, `wb_sku_override_context_invalid`, `wb_sku_override_account_missing`, `wb_sku_override_isolation_invalid`, `wb_sku_override_immutable`, `wb_sku_override_downgrade_nonempty`; no payload interpolation. T2 translates database failures through its safe service errors, not raw SQL exceptions.

Every version checks canonical bytes equal `wb_sku_override_bytes(owner,actor,command,revision-1,typed_column_projection)` and exact SHA256. This detects changes in all fourteen fields, owner, actor, command and expected version. It proves persisted payload equality, not authenticated author, source mapping or business validity. Test SQL against all six literal vectors without importing/copying T2 encoder code. Very large expected_version can be codec-tested independently; never seed a fake `2**80` consecutive history to test it.

## Transaction and continuous witnesses

Use PostgreSQL READ COMMITTED. Each INSERT/UPDATE/DELETE/TRUNCATE statement on the new relations first validates canonical positive INT4 `app.organization_id` and `app.marketplace_account_id`, then locks the exact canonical WB account FOR UPDATE, with **immediate NOT FOUND rejection**. Account lock is acquired before row locks. Preserve actual live account/provider after waits; unsupported isolation fails before business DML. Account-granularity serialization intentionally bounds race complexity; different accounts remain independent and different SKU writes survive serially within an account.

Versions/audit are INSERT-only; reject UPDATE/DELETE/TRUNCATE, including no-op UPDATE. Heads permit only initial INSERT and full-owner +1 CAS UPDATE; do not create an empty head. Deferred constraint triggers validate captured head OLD/NEW transitions against the corresponding immutable new version/audit even when another transition occurs later in the same root. Validate the final full-owner chain: head exists, current_revision/version equal highest revision, continuous chain starts at1, exactly one audit per version with equal owner/actor/command/time, audit before=parent, head updated_at=current version created_at. Detect orphan revision, ghost audit, skipped history, mismatched intermediate transition and final-head-only forgery. Valid multiple transitions in one root must pass. No global scan of other owners or privilege escalation.

Minimal trusted participant sequence after the real publication guard: check exact scoped command replay first; if found compare canonical bytes/actor/full owner and return the original revision after final authorization/commit, without another audit. Otherwise lock/read current head (account lock already held), require actual version=expected (absent means0), INSERT new full revision, INSERT first head or UPDATE head WHERE full owner AND version=expected with exact rowcount1, INSERT audit, validate/final commit. CAS mismatch or any SQL/commit failure rolls back all new rows. No ON CONFLICT success fallback or internal partial commits. Schema SQL itself cannot identify a real user from actor FK/GUC.

## RLS, grants and downgrade

All three tables ENABLE and FORCE RLS; both USING and WITH CHECK compare canonical stored org/account IDs as text with corresponding `current_setting(...,true)` under C collation. No context/wrong account/same SKU other account/wrong org are denied. Runtime is actual non-owner NOSUPERUSER NOBYPASSRLS NOINHERIT. Runtime versions/audit SELECT+INSERT; heads SELECT+INSERT+UPDATE; no history mutation, DELETE/TRUNCATE/REFERENCES/TRIGGER/grant option. No identity sequences required.

Migration narrows privileges on **new objects only**, intersecting any inherited table/column grants with these rights; PUBLIC receives none. Revoke nonowner execution of new trigger functions. Pure helper execution goes only to entitled surviving table/column grantees. Do not change preexisting/default ACLs, create roles, or broaden old helper privileges. Runtime script's existing atomic broad-grant interval must be followed by new-table/column/function narrowing in the same transaction, and new tables added to forced-RLS prerequisites. Table owners/superusers are migration authority, not runtime proof.

Downgrade acquires fixed-order ACCESS EXCLUSIVE locks on the three new tables, sets local row_security=off and refuses if **any** row exists, including another scope hidden by GUC. Drop only this migration's named constraints/triggers/functions/tables in dependency order, without CASCADE. Empty upgrade→downgrade→upgrade and old Orders/Review lossless history/ACL preservation are mandatory. Actual single predecessor head is checked immediately before implementation; no silent renumbering or editing historical migrations.

## T2 consumption and residual boundaries

T2 uses released `WB_SKU_OVERRIDE_READ_PERMISSIONS` for get/history and `WB_SKU_OVERRIDE_REPLACE_PERMISSIONS` for replace/replay with actual UserSessionPrincipal/exact ExpectedAccountBinding/current final revalidation. Before a new mutation, prove at least one current `marketplace_offers` row binds exact org/account/SKU, lock selected actual mapping rows in deterministic offer-PK order through commit after account guard, with SHARE/UPDATE (not KEY SHARE for mutable SKU binding). Do not infer mapping via article/nmId/name/account count or undocumented offer status. No direct `(org,account,SKU)` mapping FK exists because that tuple is not unique on offers. History reads remain guarded historical scope, not invented immutable current mapping evidence.

Immutable mapping/source/calculation context, algorithm settings, strategy assignments, liquidation, jobs/receipts, worker auth and formula parity are separate active dependencies. This migration neither implements them nor blocks them unnecessarily. Existing writers remain untouched and no feature flags are enabled.
