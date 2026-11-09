# Canonical SKU override storage implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. TDD, verification-before-completion and a scoped independent task review apply.

**Goal:** Deliver the three normalized account-owned persistence relations required by T2's real canonical override service.

**Architecture:** One additive migration supplies typed immutable versions, mutable owner head, reciprocal immutable audit, canonical payload checks and forced org/account RLS. The existing atomic runtime-grant script narrows the new objects. Domain authorization, mapping, replay API and service remain T2-owned.

**Tech Stack:** Existing PostgreSQL16, Alembic, SQLAlchemy/psycopg and pytest; no new dependencies or services.

**Spec:** `backend/docs/superpowers/specs/2026-09-09-sku-override-storage-design.md` (binding, read fully).

## Global Constraints

- Only new migration, runtime grants additions, three scoped test modules, a synthetic literal fixture and handoff report may change. No old migrations, domain implementation, ORM, configuration, routes/tasks, dependencies, frontend, legacy state/writers or Production feature code changes.
- No production/working data/real credentials/`.env`/network/provider/push/deploy/flag actions. All PostgreSQL proof uses fresh allocator-owned disposable databases and roles over the accepted local Unix socket, with environment scrubbed and IP traffic denied. Heavy tests require explicit team queue handoff; no prefix catalog enumeration or unrelated workload inspection.
- Only one implementation agent, no child agents; actual RED before implementation, no skipped tests, baseline expansion, timeout widening or kill-as-success. All process exits and exact own cleanup evidence must be recorded.
- Use own worktree backend/.venv. The alternate `/tmp/satorna-backend-verify-20260908/bin/python` is lint-only; run Ruff from backend cwd, not root. No foreign test runtime.
- Additive tables/forced RLS/inherited-ACL intersection only. No role creation in migration, no SECURITY DEFINER, no old/default ACL alteration, no CASCADE downgrade or history cleanup. Real non-owner NOBYPASS runtime proof must not be replaced with owner/SQLite tests.
- T2 owns current mapping and authenticated service. Canonical SQL parity or actor FK/GUC alone is not authorization, source provenance or business formula acceptance.

## Task 1: Persist exact override revisions, head CAS and audit atomically

**Files (exactly seven):**

- Create `backend/alembic/versions/20260909_0070_sku_overrides.py`.
- Modify `backend/ops/runtime-db-role.sql` only for new objects/prerequisites/grants.
- Create `backend/tests/test_sku_override_schema.py` (allocator fixtures, literal codec/schema/migration/preservation tests).
- Create `backend/tests/test_sku_override_lifecycle.py` (synthetic SQL participant, actual RC/CAS/atomicity/reciprocal tests).
- Create `backend/tests/test_sku_override_rls.py` (real non-owner roles/RLS/ACL/hostile defaults/grant replay).
- Create `backend/tests/fixtures/t1_wb_repricing_override_golden_v1.json` (byte-identical synthetic literal fixture from T2 `a6b9fd1975d509911abc9da666bc0049e49228ce:backend/tests/fixtures/wb_repricing_override_golden_v1.json`; no codec copy).
- Create `backend/docs/superpowers/reports/2026-09-09-t1-sku-override-storage-handoff.md`.

**Consumes:** full spec; actual parent constraints in `app/platform/integrations/orm.py`, `app/platform/catalog/orm.py`, `app/cabinet/orm.py`; existing allocator `tests.test_orders_schema_candidate.cluster/disposable_database/migrate`; `tests.test_orders_schema_integration.runtime_script`; existing `test_repricer_approvals_schema.py` old-history preservation helpers; 0069 migration's account-first/ACL/empty-downgrade patterns (read, do not modify or couple the new codec to Production helpers).

**Produces:** exact three relations and column/helper contract in the spec; pure `wb_sku_override_bytes(integer,integer,integer,integer,uuid,numeric,jsonb)`, `wb_sku_override_decimal(numeric)`, `wb_sku_override_integral(numeric)`; trigger functions `wb_sku_override_account_lock()`, `wb_sku_override_row_guard()`, `wb_sku_override_validate()`. Migration `revision='20260909_0070'`, `down_revision='20260909_0069'`, no branch label/dependency. Before creation independently verify actual sole0069 and absent0070; if changed, stop and report to controller, no silent renumbering.

### Test-first fixture and RED

- [ ] Read exact instructions/spec/parent keys/allocator before any DB. Expose fixture aliases explicitly (`cluster = candidate.cluster`) in each dependent test module; avoid preceding task's fixture-registration setup failure. Use `candidate.disposable_database` for every own DB/role and preserve allocator exact names/absence output with pytest `-s`. Maintenance postgres is only for the allocator's exact own create/drop/absence, never broad prefix inventory.
- [ ] Copy only the committed synthetic JSON fixture byte-for-byte via `apply_patch`. Verify its byte hash against `git show` in memory; no T2 module imports, network or local app data reads. Add before implementation a schema test with a positive predecessor upgrade and actual missing-helper assertion:

```python
cluster = candidate.cluster
PREVIOUS = "20260909_0069"
TARGET = "20260909_0070"

def test_override_contract_not_satisfied_by_predecessor(cluster):
    with candidate.disposable_database(cluster) as database:
        result = candidate.migrate(database.url, "upgrade", PREVIOUS)
        assert result.returncode == 0, result.stderr
        owner = create_engine(database.url, hide_parameters=True)
        try:
            with owner.connect() as connection:
                actual = connection.exec_driver_sql(
                    "SELECT to_regprocedure('public.wb_sku_override_bytes(integer,integer,integer,integer,uuid,numeric,jsonb)')"
                ).scalar_one()
                assert actual is not None
        finally:
            owner.dispose()
```

This initial test intentionally queries predecessor only for RED. Once observed, change the upgraded revision to TARGET and name the test `test_override_contract_present`; do not weaken its assertion. Keep a separate historical predecessor-preservation test. Add all three new module test surfaces before runtime implementation, then run the focused positive-control/missing-helper test after fresh heavy admission. A missing fixture/allocator/migration-file failure is not behavior RED. Record exact output/natural exit/cleanup and release the slot while implementing.

- [ ] Add literal SQL parity and type assertions for all six vectors: transform fixture's money/percent input values to typed SQL numeric fields without float coercion, construct `jsonb_build_object` with all fourteen typed values, call the helper and compare byte-for-byte ASCII, byte length and independent `hashlib.sha256`. `2**80` expected version tests the helper only; seed ordinary version1 history with `2**80` money separately.
- [ ] Parameterize each of fourteen columns plus owner/account/SKU/member/UUID/expected version; mutation while retaining old bytes/checksum must fail. Cover all-NULL vs false/zero, exact negative/positive finite percentages, `1E-20`, `1E20`, precision beyond float, invalid field types/key set, NaN/±Infinity, fractional/negative money/version, INT4 limits, wrong provider/memberorg/SKUorg and UUID version/variant. Do not impose undocumented percent clamps.

### Minimal schema and codec

- [ ] Implement named pure helpers with fixed search_path and SECURITY INVOKER. Decimal rendering must be finite and context-independent; use this exact normalization shape, with `NULL` rejected by the scalar formatter and handled by caller:

```sql
IF value IS NULL OR value::text IN ('NaN','Infinity','-Infinity') THEN
 RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='wb_sku_override_invalid';
END IF;
IF value=0 THEN RETURN '0'; END IF;
rendered := value::text;
IF strpos(rendered,'.')>0 THEN rendered:=rtrim(rtrim(rendered,'0'),'.'); END IF;
RETURN rendered;
```

`wb_sku_override_integral` returns false for NULL/nonfinite/fractional, true otherwise. The payload helper validates full JSON object key set and each field type before casts, then manually concatenates the fixed outer JSON keys in specified order. Iterate nested keys in `ORDER BY key COLLATE "C"`; quote money/percent via `to_json(decimal_text)::text`, other scalar values retain exact normalized JSON scalar text. All accepted string values are fixed ASCII. Never call `jsonb::text` on the whole object or remove whitespace indiscriminately.

- [ ] Create the three full-owner tables with exact spec types, CHECK/FK/unique/parent/head reciprocal constraints. The version row CHECK constructs the fourteen-key JSONB projection from actual typed columns, uses revision-1 and compares helper bytes and SHA256. Audit contains no raw payload; head references immutable version, no empty row. Name explicit constraints with `wb_sku_override_` prefix for diagnosable handoff; use PostgreSQL limits on identifier length deliberately.
- [ ] Add BEFORE STATEMENT account-first lock on all four DML kinds. Validate RC and exact canonical positive INT4 scope before casts/locking; query exact org/account AND marketplace='wb' FOR UPDATE then immediately test FOUND. Canonical active status/current external binding are publication-guard obligations, not invented SQL status policy. Reject DELETE/TRUNCATE and history UPDATE, including zero-match/no-op statements, without taking business row locks first.
- [ ] Add BEFORE ROW full-owner context/immutable-owner/head+1 checks. Add deferred AFTER ROW constraints for captured transition AND continuous final witness per affected full owner. Revision/audit rows cannot commit alone; all owners are validated separately even if transaction later changes GUC (scope change must fail closed, not skip validation). Final head, max/count revision continuity, parent, one-to-one audit, actor/command/event/time and current updated_at agree. Multiple valid transitions in one root pass; missing intermediate audit or forged temporary head must fail even if final head appears consistent.

### Lifecycle and real concurrency

- [ ] In test-only participant use explicit SQL/typed params and the same connection root; simulate trusted context, do not fake auth claims. Sequence: account FOR UPDATE before head lookup; read exact scoped command first; equality comparison on bytes+actor+scope; check currentversion (absent=0); insert revision; insert head or full-owner `UPDATE ... WHERE version=:expected RETURNING version`; require one returned row; insert audit. Sample CAS SQL:

```sql
UPDATE public.wb_repricing_sku_override_heads
SET version=:new_version,current_revision=:new_version,updated_at=:at
WHERE organization_id=:org AND marketplace_account_id=:account
 AND catalog_sku_id=:sku AND version=:expected
RETURNING version
```

No savepoint success fallback, auto-upsert or swallowing IntegrityError; outer context must roll back all writes on failure. A replay after later revisions returns its original result only if exact persisted command matches, does not require current CAS version equal old expected and adds no row. Different actor/content with same command fails. Cross-owner lookup never reveals another owner's receipt; same UUID in separate owner namespaces is not a globally unique identity.

- [ ] Test first revision/head/audit, next revision, valid multi-step same-root, same command replay before/after later head, changed actor/payload conflict, failure after each first insert/head/audit and physical commit failure, no ghosts/orphans, no history update/delete, same-SKU separate account and separate-SKU state survival.
- [ ] Use actual two connections and event-controlled barrier/observed PostgreSQL lock wait, not sleeps as race proof. Both possible same-head CAS winners; loser leaves no revision/audit, account rebind/wrong-provider after wait denied, account missing after waited delete denied, different-account progress independent. Lock-order proof separates new BEFORE STATEMENT acquisition from any inherited FK behavior; do not claim generic deadlock freedom.

### RLS, grant intersection and migration

- [ ] Apply ENABLE+FORCE RLS using stored canonical org/account text comparisons for both USING/WITH CHECK. Test SELECT/INSERT/UPDATE/DELETE with no context, wrong org, same org wrong account, correct scope and wrong provider. SELECT denial means zero visible rows, not necessarily an exception; mutation denied/no row may not be reported as successful service action.
- [ ] Intersect new table+column inherited privileges with spec allowlist; PUBLIC gets none, trigger functions no runtime execute, pure helpers only entitled execution. Do not touch old/default ACLs. Update existing runtime script forced-table prerequisite and exact new-object overrides after broad grants inside the existing transaction. Re-run runtime script and hostile defaults tests (PUBLIC/role/table/column/grant-option/trigger privilege), checking exact role flags and immutable rights plus a real allowed transaction.
- [ ] Empty upgrade→downgrade→upgrade without stamp; populated new owner or hidden other-scope data refuses downgrade. Fixed new-table exclusive locks + row_security off before emptiness; no CASCADE. Use accepted preservation helpers for synthetic populated Orders/Reviews lossless NUL/text/numerics/ACL state before and after0070; do not modify historical fixture pins. New graph assertions allow a future sole successor, do not permanently assert head==0070.

### Final gates, handoff and commit

- [ ] After fresh queue admission run new three-file gate; then exactly adjacent `test_repricer_approvals_schema.py`, `test_repricer_approvals_rls.py`, `test_repricer_approvals_lifecycle.py`, `test_orders_schema_integration.py`, `test_production_assignment_schema.py`, `test_production_assignment_rls.py`, `test_production_assignment_lifecycle.py` as compatibility only. On failure capture natural output/cleanup and report before changing scope or rerunning. Do not waive a historical-fixture failure or silently edit an eighth path.

```sh
# backend cwd; controller supplies same-plan Unix-only sandbox, no inherited DB URL
/usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 ORDERS_TEST_USE_LOCAL_CLUSTER=1 /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-sku-override-storage/task-1-sandbox.sb .venv/bin/python -m pytest -q -s --tb=short tests/test_sku_override_schema.py tests/test_sku_override_lifecycle.py tests/test_sku_override_rls.py
.venv/bin/python -m compileall -q alembic/versions/20260909_0070_sku_overrides.py tests/test_sku_override_schema.py tests/test_sku_override_lifecycle.py tests/test_sku_override_rls.py
git diff --check
```

- [ ] Main independently runs covering final gate after resource handoff. Run scoped Ruff from backend cwd, compare inherited diagnostics at recorded BASE. Record one head, exact source hashes and exact seven changed paths. Handoff lists complete columns/types/FKs/checks/helpers/safe SQL codes/grants, normative participant ordering, exact measured RED/GREEN/failed gates, native NUMERIC limits, owner cleanup, T2 auth/mapping/retirement obligations, disabled rollout and empty-only rollback. No domain acceptance claim.
- [ ] Self-review and commit exactly `feat: add account-scoped SKU override storage`; supply full SHA and full execution report. Independent spec/quality review plus main verification precede exact READY to T2. Stop this task, do not begin unrelated schema/services.

## Preflight coverage

| Task/interface | Consistency |
| --- | --- |
| Task1 exact paths and tests | Seven paths include fixture and handoff; previous migration/test files read-only |
| Codec → normalized rows | Fourteen field projection and revision-1 participate, full literal fixture parity; no arbitrary JSON state |
| Numeric → FK/revisions | NUMERIC no typmod finite/integral matches accepted2**80 helper domain; actual history starts1 |
| Account guard → head CAS | BEFORE STATEMENT exact WB account lock plus captured transitions; no owner/root-auth conflation |
| Revision/head/audit → final checks | Both directions, captured intermediate head changes and valid multi-step root covered |
| RLS → grants | FORCE pair scope plus narrow rights; role GUC is trusted service context, not user authentication |
| Shared schema → T2 | SQL-only no service implementation; exact permission exports already released47ca951 |
| Migration → old modules | Empty and populated preservation; retained Production compatibility only, no new deferred feature |
