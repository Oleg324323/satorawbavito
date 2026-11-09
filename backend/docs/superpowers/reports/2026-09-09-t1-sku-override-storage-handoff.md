# Account-scoped SKU override storage handoff

Status: CLOSED / READY for the bounded T1 SQL persistence handoff to T2. Release commit: `54386d4a789b0b74ae30710035defe949ea44b6a` (`feat: add account-scoped SKU override storage`). Independent spec review: PASS; task quality: Approved, with 0 Critical, 0 Important and 2 Minor findings. Implementer PostgreSQL247, adjacent compatibility402 and controller independent PostgreSQL247 gates passed on identical feature sources. This release does not accept the whole branch, integration, T2 authenticated service or rollout.

Revision `20260909_0070` follows sole predecessor `20260909_0069`. No old migration, domain service, ORM, route, legacy writer, dependency, feature flag or Production feature changed. Only the six code/test/fixture paths below and this handoff are tracked changes. This supplies a SQL persistence contract, not authenticated-service, formula, mapping or rollout acceptance.

## Stored contract

All three tables have positive INT4 `organization_id`, `marketplace_account_id`, `catalog_sku_id`. Full owner means all three fields. All FKs use the actual canonical parent keys and have no cascade actions.

| Relation | Columns in addition to full owner | Keys and lifecycle |
| --- | --- | --- |
| `wb_repricing_sku_override_versions` | `marketplace TEXT NOT NULL; CHECK marketplace='wb'` (no default); `revision NUMERIC NOT NULL`; nullable `parent_revision NUMERIC`; `command_id UUID NOT NULL`; positive INT4 `actor_membership_id NOT NULL`; finite `created_at TIMESTAMPTZ NOT NULL`; `canonical_request_bytes BYTEA NOT NULL`; `request_checksum TEXT COLLATE "C" NOT NULL`; fourteen nullable override columns below | PK owner+revision; UNIQUE owner+command. Finite integral positive revision; first parent NULL, otherwise revision−1. Append only. Deferred self-parent FK and reciprocal owner→head FK. |
| `wb_repricing_sku_override_heads` | `current_revision NUMERIC NOT NULL`; `version NUMERIC NOT NULL`; finite `updated_at TIMESTAMPTZ NOT NULL` | PK owner. Finite integral positive version=current_revision. First INSERT version1; UPDATE preserves owner, advances exactly one, time never decreases. Deferred owner+current_revision→version FK. No empty heads, DELETE or TRUNCATE. |
| `wb_repricing_sku_override_audit` | nullable `before_revision NUMERIC`; `after_revision NUMERIC NOT NULL`; `command_id UUID NOT NULL`; positive INT4 `actor_membership_id NOT NULL`; `event_kind TEXT COLLATE "C" NOT NULL`; finite `occurred_at TIMESTAMPTZ NOT NULL` | PK owner+after_revision; UNIQUE owner+command. Finite integral after=coalesce(before,0)+1. `created` iff before NULL, otherwise `replaced`. Deferred before/after FKs to versions. Append only; contains no raw payload. |

Versions reference accounts through `(organization_id,marketplace_account_id,marketplace)`; heads and audit reference the canonical account pair directly. All tables reference `(organization_id,catalog_sku_id)`; versions/audit reference `(organization_id,actor_membership_id)`→`iam_memberships(organization_id,membership_id)`.

| Nullable override columns | Type and SQL validation |
| --- | --- |
| `automation_enabled`, `allow_negative_margin`, `night_median_enabled` | BOOLEAN |
| `p_min_kopecks`, `p_max_kopecks`, `rrp_kopecks`, `min_margin_kopecks`, `max_margin_kopecks` | NUMERIC without typmod; finite, integral, nonnegative |
| `min_margin_pct`, `max_margin_pct`, `price_step_pct` | NUMERIC without typmod; finite; signed values accepted, no business clamps |
| `price_step_minutes` | INTEGER >0 |
| `basket_norm_manual` | INTEGER >=0 |
| `basket_norm_mode` | TEXT COLLATE "C": `auto`, `manual`, `fallback_by_type` |

NULL means inherit. Explicit false/zero remains distinct. There is no p_min≤p_max check, typmod rounding, float conversion, BIGINT narrowing or invented percentage clamp. Native PostgreSQL NUMERIC storage/index limits still apply. The accepted `2**80` expected_version vector exercises only the pure helper; ordinary revision1 separately stores `2**80` money. Fixture `max_bytes=8192` is synthetic, not a production cap.

UUIDs require version4/RFC4122 variant in canonical database UUID text. This does not recover original HTTP lexical spelling; T2 retains strict input parsing. Checksum is exactly 64 lowercase hexadecimal SHA-256 of canonical bytes. Every version CHECK recomputes bytes from all actual typed columns, full owner, actor, UUID and revision−1. All explicit keys/checks/FKs use the `wb_sku_override_` prefix and stay below PostgreSQL's identifier length limit.

## Helpers, ordering and witnesses

Pure IMMUTABLE SECURITY INVOKER functions, each with fixed `search_path=pg_catalog,public`:

```
wb_sku_override_decimal(numeric) RETURNS text
wb_sku_override_integral(numeric) RETURNS boolean
wb_sku_override_bytes(integer,integer,integer,integer,uuid,numeric,jsonb) RETURNS bytea
```

The byte helper arguments are org/account/SKU/actor/command/expected-version/typed-projection, in that order. Projection has exactly the fourteen keys above; JSON null/boolean/number/enum types are checked before casts. Outer compact ASCII JSON keys are `actorMembershipId`, `catalogSkuId`, `commandId`, `commandKind`, `expectedVersion`, `marketplaceAccountId`, `organizationId`, `schema`, `values`; nested keys sort under C collation. Schema is `wb-repricing-sku-overrides/v1`, kind is `replace_overrides`. Money/percent/expected-version become quoted normalized fixed-point decimal strings; IDs/minutes/manual stay JSON numbers. Scalar NULL/nonfinite formatter inputs reject; the caller handles nullable overrides.

Non-public trigger functions are `wb_sku_override_account_lock()`, `wb_sku_override_row_guard()`, `wb_sku_override_validate()`, all SECURITY INVOKER with fixed search path. Every new INSERT/UPDATE/DELETE/TRUNCATE statement checks READ COMMITTED and exact canonical positive INT4 `app.organization_id`/`app.marketplace_account_id`, then locks the exact live WB account FOR UPDATE before tuple writes. The immediate FOUND check after a waited lock closes delete/provider-change gaps. Status/current external binding are publication-guard obligations, not extra SQL policy. History UPDATE and all DELETE/TRUNCATE reject even on zero-match statements.

Deferred triggers validate each captured head transition against its corresponding version and audit, then the final chain of that exact full owner: head, maximum/count continuity from1, reciprocal one-to-one audit, parent, actor, command, event and timestamps all agree. Each affected owner is checked independently. Changing a GUC cannot hide earlier queued witnesses. Valid multiple transitions in one root are allowed; a forged temporary head or missing intermediate audit cannot be excused by a consistent final head.

Normative T2 transaction order:

1. Establish live authenticated authority and exact account binding through the released guards, retaining the account lock in one READ COMMITTED root.
2. Look up command by full owner+UUID. Exact actor+canonical-bytes match returns the original revision after final authorization/commit, even after later revisions. A changed actor/content conflicts. Other owners' UUID namespaces do not expose this receipt.
3. For a new mutation only, prove current exact org/account/SKU offer mapping and lock actual offer rows in deterministic offer-PK order through commit with SHARE/UPDATE, not KEY SHARE. No article/name/nmId/count/status inference. There is no unique owner triple on offers and therefore no fabricated mapping FK.
4. Lock/read the scoped head; absent means version0. Require expected version, INSERT new version, INSERT first head or full-owner `UPDATE ... WHERE version=:expected RETURNING version` requiring exactly one result, INSERT audit.
5. Final guard validation and physical commit. Any CAS, SQL, injected or commit failure rolls back the entire root. No partial internal commit, ON CONFLICT success fallback or swallowed IntegrityError.

Controller/T2 clarification: mapping is required for a new mutation; an exact historical replay still follows live authority checks and may succeed when the current mapping is absent. Actor FKs, GUCs and canonical equality alone prove neither identity nor authorization. Use released `WB_SKU_OVERRIDE_READ_PERMISSIONS` and `WB_SKU_OVERRIDE_REPLACE_PERMISSIONS` with real UserSessionPrincipal/ExpectedAccountBinding and current final revalidation.

## Rights, errors and rollback

All three tables ENABLE+FORCE RLS; USING and WITH CHECK compare stored canonical org/account text to the trusted GUCs under C collation. Runtime is a real non-owner NOSUPERUSER NOBYPASSRLS NOINHERIT role. Versions/audit allow SELECT+INSERT; heads allow SELECT+INSERT+UPDATE. No DELETE/TRUNCATE/REFERENCES/TRIGGER/grant option, no sequences, no PUBLIC rights or runtime trigger-function execution. Migration intersects inherited new table and column ACLs without widening readers, grants pure helper execution only to surviving entitled grantees/owners, and changes no old/default ACL. The runtime script retains its existing transaction and adds forced-table prerequisites plus exact new table/column/function overrides after broad grants.

Fixed trigger/helper messages: `wb_sku_override_invalid`, `wb_sku_override_context_invalid`, `wb_sku_override_account_missing`, `wb_sku_override_isolation_invalid`, `wb_sku_override_immutable`, `wb_sku_override_downgrade_nonempty`. Validation/immutability messages use SQLSTATE23514; isolation25000; nonempty downgrade55000. Ordinary native type, FK, unique, RLS/permission errors retain their SQLSTATE and named constraints. T2 must translate all database failures into safe service errors rather than expose raw driver exceptions or payload-bearing native details.

Downgrade locks all three new tables in fixed order with ACCESS EXCLUSIVE and sets local row_security=off before checking emptiness. Any row, including another hidden scope, refuses rollback. Only this migration's constraints/triggers/tables/functions are dropped in dependency order, without CASCADE. Rollout stays disabled and old writers remain untouched; populated rollback requires a separately authorized retirement/migration design.

## Evidence and limitations

Initial test-first behavior RED: successful0069 bootstrap followed by helper lookup returning None; `assert actual is not None` failed naturally. Exact admitted one-test command used own backend `.venv`, env scrub, no-IP sandbox and local Unix socket. Result: 1 failed in3.06s, exit1. Exact allocator cleanup: `Orders cleanup verified: database=orders_test_d874dabbe6134812b3d7fdf46daaf8c4; roles=`. Slot released immediately. No setup failure counted as RED.

Fixture is byte-identical to committed synthetic T2 source `a6b9fd1975d509911abc9da666bc0049e49228ce:backend/tests/fixtures/wb_repricing_override_golden_v1.json`; SHA256 `a774bc630dccb42d4e36c7d00d15d50294159d7545562050320a54d73db4256d`. No encoder copied/imported. Runtime fixture test uses the literal hash so shallow clones work.

First three-file gate failed during migration bootstrap: 4 failed, 2 passed, 241 setup errors in25.33s, natural exit1. Root cause was unparenthesized SQL CASE expressions inside PL/pgSQL IF; the two expressions were parenthesized without relaxing any tests. All seven owned databases and associated roles were removed with exact absence checks. A separately admitted smoke then passed7 cases in7.90s, exit0.

Full three-file retry passed **247 tests in29.39s**, natural exit0, no skipped tests. It covers real non-owner RLS/rights, both observed CAS winners, waited account changes, whole-root rollback, intermediate/final witnesses, scoped replay, hostile defaults/column ACLs, empty roundtrip and populated predecessor history preservation. Its seven allocator-owned databases/roles were cleaned with exact absence checks. The exact seven adjacent modules (approvals schema/RLS/lifecycle, Orders schema integration, existing Production assignment schema/RLS/lifecycle) passed **402 tests in152.18s**, natural exit0. All allocated PostgreSQL resources were cleaned; report separately identifies three existing synthetic allocator-fault controls. No old tests were edited or waived.

Compile, scoped Ruff and diff checks pass. Ruff diagnostics for the seven adjacent historical modules are identical to BASE and all zero. Controller independently reran the same three-file gate: **247 passed in26.25s**, natural exit0, no skips. It verified all seven own database/seven role cleanup and absence checks, exact sole head0070, compile/Ruff/diff0 and identical six source hashes after the gate. The inherited libpq `/dev/null` passfile warning remains; output is not warning-free. Evidence is attributed to controller `.superpowers/sdd/2026-09-09-sku-override-storage/parent-verification.md`, fully read before this handoff was finalized.

Independent review of BASE `9ec82a268778162e86bb277bb0722d3524c3ca1a` → release `54386d4a789b0b74ae30710035defe949ea44b6a` is complete: spec PASS and task quality Approved, with 0 Critical, 0 Important and 2 Minor findings. The reviewer read the full frozen diff and attributed execution evidence; it did not rerun tests or reconstruct historical RED/cleanup events. The review is recorded in `.superpowers/sdd/2026-09-09-sku-override-storage/task-1-review.md`, read in full alongside controller verification for this evidence-only update. Exact commands, failures, cleanup identifiers and release rulings remain in `.superpowers/sdd/2026-09-09-sku-override-storage/task-1-report.md`.

The two non-blocking findings remain open for final whole-branch triage:

- **M1 — scoped-parent FK test isolation:** the wrong-SKU-organization and wrong-membership-organization cases in `test_provider_sku_and_member_parent_scope_rejected` wrap a version-only root in `pytest.raises(DBAPIError)`. Missing head/audit witnesses could still cause a commit error if a targeted FK were removed. The actual account/SKU/member FKs are present; targeted behavioral regression proof for these particular parent checks is limited by orphan-error masking. No storage defect was identified. A future test refinement can assert immediate INSERT rejection or the exact target constraint in an otherwise valid root.
- **M2 — inherited libpq warning noise:** recorded runs emit `password file "/dev/null" is not a plain file`. This does not invalidate the passing gates or indicate credential access, but output is not pristine. Any future allocator-owned regular-passfile adjustment needs controller-approved harness scope and exact cleanup evidence. This frozen task's mandated environment and test results remain unchanged.

Feature source SHA256 frozen after full247 pass:

```
d0b139845ba4204f24e0a05e3593706578a260b27a96e446e5cc94f341e2e50e  backend/alembic/versions/20260909_0070_sku_overrides.py
e572aa6b9c1e75cbadf3e21a48ed9f6e17379ebb579b85fe05714a383de81197  backend/ops/runtime-db-role.sql
063b6a3ff8f2e8a99b435904ea8df3a6c5771b1b5f4241445f0579a9db0a8422  backend/tests/test_sku_override_schema.py
58eddf1da8fb42325ac09188bfad4ee2cfa35219ded1ea61a0a6c7e414f5f618  backend/tests/test_sku_override_lifecycle.py
45c60e207716041ab64304570bce80c045625564bb3f44266b7070208b6e7da7  backend/tests/test_sku_override_rls.py
a774bc630dccb42d4e36c7d00d15d50294159d7545562050320a54d73db4256d  backend/tests/fixtures/t1_wb_repricing_override_golden_v1.json
```

Residual obligations: real T2 authentication/service mapping and retirement, immutable calculation/source context, algorithms/strategies/liquidation/jobs/receipts/worker authorization and formula parity are not implemented by this schema. Production assembly/printing remains explicitly deferred, retained only for compatibility. No production/provider/network/flag/push/deploy action is part of this change.
