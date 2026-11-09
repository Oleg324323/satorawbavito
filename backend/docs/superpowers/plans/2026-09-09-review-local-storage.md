# Local Review persistence implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. TDD, verification-before-completion and independent task review apply.

**Goal:** Supply exact account-owned local policy/draft/decision history, CAS heads, audit and completed command receipts for T4's real service.

**Architecture:** One additive migration implements seven mutually constrained relations, fixed byte encoders, account-first guards, forced RLS and new-object privilege intersection. Test-only SQL participants prove storage and concurrency; they do not impersonate an authenticated service. T4 owns actual service composition after exact READY.

**Tech Stack:** Existing PostgreSQL16/Alembic/SQLAlchemy/psycopg/pytest; own backend/.venv and no new dependency/service.

**Spec:** `backend/docs/superpowers/specs/2026-09-09-review-local-storage-design.md` (binding, read fully).

## Global Constraints

- Only additive migration0071, new-object runtime grant additions, four scoped test modules, four copied synthetic fixtures, one handoff and the two narrowly amended existing test fixtures below may change in implementation.
- No old migrations/ORM/routers/domain code/config/dependencies/frontend/legacy writers, no broad/default ACL changes or role creation in Alembic, no SECURITY DEFINER, no production/working DB/Redis/real credentials/.env/providers/network/push/deploy/flags/backfill/cleanup.
- Tests use own worktree backend/.venv, scrubbed environment, accepted allocator-owned disposable Unix PG and explicit heavy-slot queue handoff. No prefix catalog inventory, unrelated workloads, SQLite RLS substitute, skips, timeout widening, baseline expansion or kill-as-success.
- Production assembly/printing remains deferred; old compatibility only. Real service/retention/delivery approval is separate, not guessed and not a blanket blocker for this local persistence.
- One sole implementation agent, no child agents. Read required spec/source/fixtures and record actual RED before migration implementation. No PG without explicit admission; report natural exit and exact own cleanup before any rerun.
- The alternate /tmp/satorna-backend-verify-20260908/bin/python is lint-only; run Ruff from backend cwd. No foreign test runtime or host/package configuration changes.

## Task 1: Implement the atomic local Review storage contract

**Files, exactly thirteen after the measured compatibility amendment:**

- Create `backend/alembic/versions/20260909_0071_review_local_storage.py`.
- Modify `backend/ops/runtime-db-role.sql` only new-table prerequisites and new object grants.
- Create `backend/tests/test_review_local_storage_schema.py` (allocator, typed schema, empty/populated preservation).
- Create `backend/tests/test_review_local_storage_codec.py` (fixed literal bytes, field/type/null/UTF8/timestamp tests).
- Create `backend/tests/test_review_local_storage_lifecycle.py` (trusted SQL participant, real commit/replay/CAS/epoch/manual edit).
- Create `backend/tests/test_review_local_storage_rls.py` (runtime privileges/RLS/hostile ACL/hidden downgrade).
- Create `backend/tests/fixtures/t1_review_storage_v1_golden.json`.
- Create `backend/tests/fixtures/t1_review_local_audit_v1_golden.json`.
- Create `backend/tests/fixtures/t1_review_local_command_v1_golden.json`.
- Create `backend/tests/fixtures/t1_review_account_binding_v1_golden.json`.
- Create `backend/docs/superpowers/reports/2026-09-09-t1-review-local-storage-handoff.md`.
- Modify `backend/tests/test_review_facts_schema.py` only `test_run_and_evidence_guards_even_with_broad_privileges`: rollback-only synthetic TRUNCATE diagnostic grant and restoration proof, preserving every existing exact error assertion.
- Modify `backend/tests/test_sku_override_schema.py` only the runtime `db` fixture: migrate actual `head` before invoking current runtime grants. Historical TARGET/PREVIOUS tests remain pinned.

**Consumes:** released0070 with unchanged0063/0065/0068 migrations; actual canonical account/member FK keys; `tests.test_orders_schema_candidate.cluster`, `disposable_database`, `migrate`; `tests.test_orders_schema_integration.runtime_script`; `tests.test_review_facts_schema` row builders and `tests.test_review_run_binding_migration` binding/preservation helpers (read before using; only the two test-fixture amendments above). Golden sources at immutable `ad793f9c4672210b25fe05e35dc1ddb03cf676ac:backend/tests/fixtures/reviews/`: storage-v1-golden.json, local-audit-v1-golden.json, local-command-v1-golden.json, account_binding_descriptor_v1.json respectively. T4 matrices/codec and amendments through176d243df4ef3349e03fa31f1d4231f3e9064271 are accepted inputs, not imported runtime modules.

### Measured compatibility amendment (2026-09-09)

Final new focused499 passed44.57s. Exact ten-module adjacent617 then naturally
returned375PASS/1FAIL/241setupERROR in220.04s with owned cleanup. Actual failures:
old Review evidence diagnostic expected its immutable trigger, but CASCADE first
hit missing TRUNCATE permission on the new draft relation; SKU shared runtime
fixture migrated0070 while current fail-closed runtime script requires0071.

The existing Review test deliberately verifies triggers under broader temporary
privileges in an owned rollback-only root. Keep its UPDATE/DELETE grants and exact
`Review evidence is immutable` assertions. Add TRUNCATE on all tables only inside
that disposable public schema/root so current/future FK CASCADE participants cannot
mask the intended trigger. Snapshot and verify actual role ACL restoration after
rollback. This grants no permanent/runtime privilege and changes no trigger/DDL.

Current runtime-script fixtures must migrate actual head, including the new local
Review `db` fixture, matching existing Orders integration fixture behavior. Keep
targeted0070/0071 presence, predecessor, downgrade and preservation tests pinned to
their respective revisions. Never trim runtime prerequisites or substitute grants.

First run the two previously failing exact nodes after the amendment, then unchanged
four-module focused gate and exact ten-module adjacent gate. Main independent gate
includes the four new modules plus these same two old regression nodes. Keep all
prior failed results, re-freeze twelve source/test/fixture hashes plus the handoff
as the thirteenth implementation path. No extra domain file, baseline edit, skip,
timeout change or old migration change is admitted.

**Produces:** `review_policy_versions`, `review_policy_heads`, `review_draft_revisions`, `review_decisions`, `review_workflow_heads`, `review_local_audit`, `review_local_command_receipts` and the fixed `review_local_*` helper family in the spec. Revision20260909_0071/down20260909_0070, no labels/dependencies. Complete actual columns/types/FKs/helper signatures and service transaction order in final handoff, not a generic repository API.

### Preflight and actual RED

- [ ] Verify clean dispatch BASE, sole0070 and no occupied0071 before new migration. Refuse changed head; no silent renumber. Confirm only approved worktree is used. Read full spec and actual parent/fixture/allocator code; set `cluster = candidate.cluster` explicitly in each dependent module.
- [ ] Copy exactly four committed synthetic fixtures via apply_patch. One-time compare copied bytes with immutable git-show output and record SHA256; runtime tests depend on local literal files/hash, not full Git history or T4 imports. No original codec copied into test implementation.
- [ ] Prepare all four focused test modules before SQL implementation, including positive-control predecessor then actual absent contract. Initial test:

```python
cluster = candidate.cluster
PREVIOUS = "20260909_0070"
TARGET = "20260909_0071"

def test_local_contract_missing_on_predecessor(cluster):
    with candidate.disposable_database(cluster) as database:
        result = candidate.migrate(database.url, "upgrade", PREVIOUS)
        assert result.returncode == 0, result.stderr
        engine = create_engine(database.url, hide_parameters=True)
        try:
            with engine.connect() as c:
                actual = c.exec_driver_sql(
                    "SELECT to_regclass('public.review_local_command_receipts')"
                ).scalar_one()
                assert actual is not None
        finally:
            engine.dispose()
```

- [ ] Collect/compile offline, report the exact one-test RED command, wait for explicit queue admission. Run once, preserve natural exit/own cleanup and release. A fixture/setup/migration syntax failure is not behavior RED. After observed missing relation, convert this test to TARGET with unchanged required assertion, rename `test_local_contract_present`, retain separate predecessor-preservation test.

### Exact scalar and fixed-format encoders

- [ ] Implement the named helpers in the new migration with SECURITY INVOKER and fixed search_path. Pure helpers have no business-table reads and return the explicit type specified. Numeric normalization algorithm:

```sql
IF value IS NULL OR value::text IN ('NaN','Infinity','-Infinity')
   OR value<>trunc(value) OR value<0 OR (NOT allow_zero AND value=0) THEN
 RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='review_local_invalid';
END IF;
RETURN trunc(value)::text;
```

The actual integer formatter must emit scale-free integer text even for stored1.000; test this independently. Timestamp formatter validates finite/year1..9999, emits exact UTC six digits/Z without session-timezone effects. No arbitrary clock/TTL upper bound beyond representation.
- [ ] UTF8 scalar encoder validates `review_strict_utf8`, escapes controls/quote/backslash exactly and preserves raw non-ASCII; NUL becomes the literal JSON escape, never TEXT decoding of NUL. Avoid repeatedly copying remaining suffixes of large inputs. Nonblank test uses the exact whitespace set in the spec, NUL is nonblank. Test all whitespace scalars, all controls0..31, DEL, U+0085/U+2028, Unicode normalization variants, emoji, empty/invalid UTF8, surrounding spaces and escaped literals.
- [ ] Implement only fixed policy/generation/decision-binding/audit/request/result formats. Full keysets/null matrices, typed finite integral NUMERIC and UUID grammar from spec/T4 literals are binding. Build stored expected bytes from actual typed row values and immutable referenced rows, never from a caller's arbitrary request JSON. Account binding uses accepted0068 helper and current representable physical account metadata, including nullable credential ref; no reimplementation that admits impossible NUL account IDs.
- [ ] Add literal direct SQL parity for all four fixture sets; compare exact bytes, byte length and frozen SHA. Accepted policy/generation/draft/head version>BIGINT is preserved by helpers; do not seed a fictitious enormous consecutive head chain. Independently store large policy versions in a valid ordinary head1 workflow.

### Typed normalized rows and reciprocal graph

- [ ] Create all seven exact relations. Use positive INT4 owner/member and fixed provider/account FK, canonical nonzero UUID, finite integral NUMERIC without typmod for versions, BYTEA+64lowerhex SHA for exact payloads/text. Add scoped account/member/fact/observation-checksum/policy/draft/head/audit/receipt FKs and needed composite candidate keys. No CASCADE actions or extra generic relation. Audit/receipt private typed refs support original canonical bytes without exposing text or descriptor.
- [ ] Enforce policy-created one witness/one receipt per immutable target. Policy versions need not be contiguous. Heads init1 only, preserve identity, +1 CAS and nondecreasing times. Draft revision starts1 and increments only on new draft, not decision. Current decision matches exact current draft. Manual edit requires scoped current predecessor; fake has none. UNIQUE(org,account,generation_id) excludes review/actor/mode, and new key cannot republish occupied generation.
- [ ] Store complete typed receipt expectations/results; unused fields NULL for each of four operations. Reconstruct request bytes from original immutable target/source/generation/text and typed expectations. Result contains original IDs/time, audit occurred_at=completed_at, result head=expected+1/published revision=expected+1. Audit canonical commandId staysNULL while private local_command_id supplies reciprocal receipt FK. One exact event per aggregate category/ID/version; no second kind at same transition version.
- [ ] Add immediate row CHECKs for typed payload/SHA and immediate scoped parent FKs where possible, with reciprocal cycles DEFERRABLE INITIALLY DEFERRED. Negative immediate tests use this pattern so orphan commit failures cannot mask the tested constraint:

```python
with owner.connect() as c:
    transaction = c.begin()
    try:
        set_scope(c, organization_id, marketplace_account_id)
        with pytest.raises(DBAPIError) as caught:
            c.execute(invalid_insert, complete_typed_parameters)
        assert caught.value.orig.sqlstate in {"23503", "23514"}
    finally:
        transaction.rollback()
```

Use exact expected constraint when testing FK identity rather than broad SQLSTATE alone. For deferred target tests write an otherwise valid complete bundle and assert the target error. Include positive complete controls, not merely static DDL text searches.

### Account-first action and captured-history validation

- [ ] Implement BEFORE STATEMENT account lock for every new DML kind. Canonical GUC validation occurs before numeric casts, READ COMMITTED required, query exact account/provider FOR UPDATE then immediate FOUND check. No old trigger rewrites or business-row-first order. History UPDATE/no-op/DELETE/TRUNCATE reject; heads permit only init/+1, immutable owner/head identity.
- [ ] At new draft/decision INSERT lock exact fact FOR SHARE, then policy/workflow heads. Enforce source=current observation/unambiguous, current epoch/target, new manual predecessor and current draft for decision; approved checks unanswered/can_answer true while rejection/publication do not. Source-run accepted binding must match new action receipt binding; missing legacy binding never inferred. Service still supplies real auth and normalized-source validation; SQL test context is explicitly trusted, not a fake UserSessionPrincipal.
- [ ] Deferred validators compare each captured OLD/NEW head transition with matching immutable entity/audit/receipt, then complete scoped chains/final pointers. Verify earlier transitions even after later actions in same root; do not validate old epochs against final latest policy as if newly executed. Reject ghosts, missing/interchanged witnesses, skipped/reused versions, orphan history, wrong actors/command bytes/time and scope-switch hiding. Valid multiple actions in one root pass.
- [ ] Test-only SQL participant follows account-first root, fresh exact receipt lookup BEFORE current head/source/predecessor checks, then new mutation. Full-owner command UUID+actor/op/bytes/binding match returns original immutable result; changed actor/kind/bytes/expectation/rebind rejects. No ON CONFLICT success fallback, cached absence, internal commit or exception swallow.

```sql
UPDATE review_workflow_heads
SET version=:next_version,current_draft_id=:draft_id,
    current_draft_revision=:draft_revision,current_decision_id=:decision_id,
    updated_at=:at
WHERE organization_id=:org AND marketplace_account_id=:account
  AND marketplace=:provider AND review_id=:review_id AND version=:expected
RETURNING version;
```

Require exactly one returned row, else root rollback; insert target/head/audit/receipt in one physical transaction. Use consistent exact column names in migration/tests/handoff; the head uses `current_draft_revision` for the explicit draft revision field.
- [ ] Cover four operations, first-head absence, same-target select+1, selectA→B→retryA, publish→decision/newdraft→retry, approve→reject→oldretry, unchanged counts/pointers on replay, same-key different actor/kind/text/expected conflict, same-generation different key conflict, lost-response fresh-connection retry and failure injected after each write plus physical commit failure.
- [ ] Cover P1→P2→P1, same-target new selection, wrong head UUID, preparation switch, forged fresh epoch over old capture, missing/wrong-owner selection witness, new current draft+decision success. Manual edit cases: current/old/foreign/first/stale versions, same text clears decision, same generation reuse across mode/review, rollback leaves generation unreserved.
- [ ] Real concurrency uses two connections/events and observed PostgreSQL blocking relationship, not sleep-only proof: both first-head/CAS winners, duplicate key and duplicate generation single winner, select versus publication/decision, waited account rebind/provider/delete, independent other-account progress, account-before-business tuple lock. Prove loser leaves no entity/audit/receipt. No broad deadlock-free assertion.

### Forced RLS, new ACL intersection and migration safety

- [ ] ENABLE+FORCE RLS all seven, canonical stored org/account text comparisons both USING/WITHCHECK. Real runtime nonowner/NOSUPER/NOBYPASS/NOINHERIT; immutable S/I, heads S/I/U, no DELETE/TRUNCATE/REFERENCES/TRIGGER/grant options. Test no context/wrong org/sameorgwrongaccount/unknown provider and positive permitted bundle. Independently test RLS with ordinary triggers disabled inside rollback-only owned diagnostics, not confuse trigger denial with RLS proof.
- [ ] Intersect only new relation/column inherited rights, PUBLICnone; trigger functions no nonowner EXECUTE, pure helper access only entitled roles. Reuse accepted dependency helper rights without widening old ACL. Existing atomic runtime script adds new forced prerequisites and precise table/column/function narrowing after broad grants. Hostile PUBLIC/reader/writer/column/default/grant-option cases, script replay and failure atomicity preserve old ACL/defaults.
- [ ] Empty upgrade→downgrade→upgrade without stamp. Populated original Orders/Review NUL/Unicode/numeric/ACL snapshots preserved. Any new row, including hidden foreign scope, blocks downgrade before destructive DDL. Fixed new-table ACCESS EXCLUSIVE order, row_security=off, explicit dependency drops without CASCADE. Future graph tests attach artificial successor to actual sole head and do not hard-pin head0071 forever.

### Verification, handoff and exact commit

- [ ] After exact RED/implementation, request one small migration/literal smoke admission before full gate to expose PL/pgSQL syntax separately. Record actual output/cleanup and release. Then fresh queue admission for all four new modules; no hidden retries on failure.

```sh
# backend cwd; exact accepted Unix-only sandbox is prepared by controller.
/usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 ORDERS_TEST_USE_LOCAL_CLUSTER=1 /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-review-local-storage/task-1-sandbox.sb .venv/bin/python -m pytest -q -s --tb=short tests/test_review_local_storage_schema.py tests/test_review_local_storage_codec.py tests/test_review_local_storage_lifecycle.py tests/test_review_local_storage_rls.py
.venv/bin/python -m compileall -q alembic/versions/20260909_0071_review_local_storage.py tests/test_review_local_storage_schema.py tests/test_review_local_storage_codec.py tests/test_review_local_storage_lifecycle.py tests/test_review_local_storage_rls.py
git diff --check
```

- [ ] Following separate fresh admission, exact adjacent modules: test_review_facts_schema.py, test_review_lossless_migration.py, test_review_lossless_rls.py, test_review_run_binding_migration.py, test_review_run_binding_rls.py, test_review_run_binding_acl.py, test_sku_override_schema.py, test_sku_override_lifecycle.py, test_sku_override_rls.py, test_orders_schema_integration.py. No new deferred Production tests. Record natural exit, inherited warnings and exact own cleanup; distinguish any old synthetic allocator controls from real resources. No eleventh/extra module or new path fix without controller ruling.
- [ ] Freeze twelve code/test/fixture source hashes, complete all actual columns/nullability/FK/check/index/trigger/helper signatures/grants/safe codes/order and exact measured gates in handoff. Keep physical constraints vs T4 auth/replay/HTTP/rollout acceptance distinct. Four copied fixture hashes/source commits and byte limitations explicit. Main independent covering gate after resource handoff, fresh one-head/compile/Ruff/diff/hash checks.
- [ ] Self-critique, then only on controller checkpoint commit exactly `feat: add account-owned local review storage`. Report full SHA, exactly thirteen paths, all failures and cleanup. Independent task spec/quality review and main verification precede exact READY to T4; no next schema/consumer action in this task.

## Preflight matrix

| Shared interface | Consistency check |
| --- | --- |
| Thirteen paths / task | One coupled graph+codec/RLS unit plus two measured test-fixture compatibility repairs; no old migrations/domain files |
| Pure bytes / physical rows | Fixed complete formats from typed fields; NUL only BYTEA path; NUMERIC no rounding |
| Four command receipts / audit | canonical commandIdNULL preserved; separate private scoped key and reciprocal event refs |
| Policy head / draft epoch | historical selected witness, captured epoch not FK current version; ABA rejects new actions |
| Workflow version / draft revision | separate counters, decision advances only head, publication clears decision |
| Manual generation / replay | unique owner generation; new command conflicts; exact old command returns original first |
| Physical source / real authority | current rows/binding locked in SQL; actual session/decoded-source T4 responsibility |
| RLS / grants / downgrade | real nonowner and independent policy tests; new ACL-only intersection; hidden nonempty refusal |
| Immediate negatives / deferred graph | targeted error cannot be masked by missing unrelated witness |
| Final artifacts / READY | main gate plus independent review, no provider/operational activation inference |
