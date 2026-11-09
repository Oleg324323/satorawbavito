# Review Facts lossless representation implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist every successfully canonicalized NUL-bearing Review scalar/coverage value without changing semantic identity or checksum.

**Architecture:** One forward migration adds eight nullable BYTEA alternatives across three existing Review relations, pair/UTF8/JSON-object checks and exact old/new identity comparisons. Preserve legacy columns and data, require decoder-first application activation and retain decoder-capable rollback. T4 alone owns repository codec changes.

**Tech Stack:** Existing PostgreSQL/Alembic/SQLAlchemy/pytest; no extension, package or service changes.

**Spec:** `backend/docs/superpowers/specs/2026-09-09-review-facts-lossless-representation-design.md` at405033a, read fully. Exact T4 domain prerequisites268ea1d/a836560/316eaf8 reports are local-Git evidence, not code to copy.

## Global constraints

- Worktree `/Users/bratishka/Downloads/satornawb-main/.worktrees/arch-t1-platform`, branch `codex/arch-t1-platform` only. No production/working DB/Redis/provider/network/secretfiles/.env/key access, push/deploy/backfill/flags/host configuration.
- Only fresh random own disposable PostgreSQL DBs and roles through authorized Unix `/tmp` maintenance connection, scrubbed env, pass/service/netrc disabled, OS secret-file/IP denial. Existing app data/roles/server configuration are never inspected or changed. Cleanup exact owned identifiers in finally and prove absence. Reuse tracked helper; no native initdb success claim or SQLite RLS substitute.
- Own backend/.venv for all app/tests. Approved external Ruff executable is lint-only, never another app runtime. No dependencies/install.
- No T4 normalizer/ORM/repository/tests edits or copies, no frontend/routers/tasks/shared config. Three existing schema relations, eight BYTEA fields, two pure validation helpers and three replaced trigger functions only. Never rewrite0063/0064 or their tests to make acceptance easier.
- Preserve NUL/Unicode/combining/long identities, semantic checksum inputs, UUID/scoped FK targets, RLS/account lock/version/immutability. No trimming/hash identity/bytecap/surrogatepass, no default scope or cache fallback.
- Current solehead0064 is verified; new target0065 is `20260909_0065_review_lossless.py`. At dispatch recheck exact solehead20260909_0064 and absence0065. If changed/occupied, stop for controller amendment, never renumber silently. This schema is now sequenced before the queued approvals schema; its plan must be explicitly renumbered only after actual new head exists.
- Historical feature fixture pins0065; actual runtime-script test uses independently latest-head DB. One-head plus0065 ancestry gate must remain valid after successors. No real migration execution or decoder activation outside disposable synthetic DBs.

## Task 1: Add lossless schema representation and acceptance

**Files:**
- Create `backend/alembic/versions/20260909_0065_review_lossless.py`.
- Modify `backend/ops/runtime-db-role.sql` only the new Review run-column/helper grants inside the existing transaction, after broad grants.
- Create `backend/tests/test_review_lossless_migration.py` (schema/roundtrip/UTF8/JSON and shared disposable fixtures).
- Create `backend/tests/test_review_lossless_rls.py` (runtime/ACL/identity races/downgrade).
- Create `backend/docs/superpowers/reports/2026-09-09-t1-review-lossless-handoff.md`.

**Consumes:** actual0063 definitions/runtime ACL and fixed historical/latest test patterns; the spec's exact eight-field matrix and T4 Unicode-scalar commitment268ea1d. Read0063 fully and focused existing test_review_facts_schema/test_orders_schema_candidate helpers before changes. Use local git show for exact foreign reports/serializer evidence only.

**Produces:** sole0065 after0064; helpers `public.review_strict_utf8(bytea) -> boolean` and `public.review_coverage_json_object_utf8(bytea) -> boolean`; eight additive fields/pair checks; forward replacement of actual `review_exact_identity_guard`, `review_run_guard`, `review_fact_guard`; exact safe conflict names and old/new codec handoff. No domain implementation.

- [ ] Step1 write/run real0064 representation RED before DDL: synthetic valid UTF8 scalar containing00 cannot be inserted into each corresponding old TEXT field; JSONB object with `streams[].name` escapedNUL fails while plain object control succeeds. Use savepoints/new transactions so one expected database error does not invalidate later cases. No customer content in output; assert SQLSTATE/rollback only. Include normal successful old-row control, not only missing-column/function failures.

```python
with pytest.raises(DBAPIError) as raised:
    with connection.begin_nested():
        connection.execute(text("SELECT CAST(:coverage AS jsonb)"),
                           {"coverage": '{"streams":[{"name":"synthetic\\u0000name"}]}'})
assert raised.value.orig.sqlstate == "22P05"
```

Savepoint rollback occurs before pytest catches the error and before outer transaction reuse. Record actual server SQLSTATE if encoded-NUL error differs from this expected unsupported-Unicode state; never hard-code a false claim. Separate expected missing0065/helper RED from accepted-input representation failure.

- [ ] Step2 add helpers using built-in decoder, not a Unicode FSM. Frozen skeleton:

```sql
-- Within review_strict_utf8(value bytea), IMMUTABLE STRICT:
-- position00 in remaining bytes, validate the preceding chunk, advance past00.
PERFORM pg_catalog.convert_from(chunk, 'UTF8');
-- catch ONLY character_not_in_repertoire and return false;
-- otherwise propagate. All bytes kept unchanged; no concatenation.

-- Within review_coverage_json_object_utf8(value bytea):
RETURN pg_catalog.json_typeof(pg_catalog.convert_from(value, 'UTF8')::json)='object';
-- catch ONLY character_not_in_repertoire / invalid_text_representation.
```

Use qualified fixed search_path and SECURITY INVOKER. Scan bytes by index/get_byte and decode slices between00 positions without repeatedly copying the full remaining suffix (avoid quadratic work on long all-NUL content). Resolve any observed JSON syntax error state concretely, do not catch every SQL error into false. Test every byte boundary with expected bool directly as database owner, then runtime EXECUTE privilege after grants:

```python
valid = [b"", b"\x00", b"a\x00b", "е\u0301🚀".encode(), b"\xf4\x8f\xbf\xbf"]
invalid = [b"\x80", b"\xc2", b"\xc0\x80", b"\xed\xa0\x80", b"\xf4\x90\x80\x80", b"\xc2\x00\xa0"]
for payload in valid:
    assert utf8_predicate(connection, payload) is True
for payload in invalid:
    assert utf8_predicate(connection, payload) is False
```

`utf8_predicate` is a local test adapter issuing only parameterized SELECT public.review_strict_utf8(:payload). Coverage JSON predicate must accept object containing escapedNUL/combining/emoji; reject malformed UTF8/JSON/raw00 and array/string/number/boolean/null roots. Verify this actual PostgreSQL JSON behavior, never substitute a Python-only parser test.

- [ ] Step3 add exact eight spec BYTEA columns, required/optional pair CHECKs, scalar UTF8/nonempty checks and coverage object predicate. Drop old NOT NULL only for required pairs, retain all old columns/checks/data. Old scalar pair nullability/empty distinctions from spec are binding. Existing CHECK validation can scan data and locks can wait; do not claim zero-cost online migration.

```sql
CHECK ((external_review_id IS NOT NULL)::integer +
       (external_review_id_utf8 IS NOT NULL)::integer = 1);
CHECK (text IS NULL OR text_utf8 IS NULL);
CHECK (text_utf8 IS NULL OR public.review_strict_utf8(text_utf8));
```

New source versions/run/external-review required bytes and optional product/status present bytes are nonempty; text permitsb''. Coverage requires one valid object representation. Test all eight actual inserts and strict decoded semantic equality, not just column existence or helper results. Keep UUID/FK/checksum/revision identities unchanged.

- [ ] Step4 replace both exact identity branches to compare `COALESCE(new_bytes,convert_to(old_text,'UTF8'))` after actual account lock and fresh READ COMMITTED statement. Preserve fact/run branches and logical23505 `uq_review_fact_external`/`uq_review_run_source`. Include new identity columns in fact/run immutable guards; all existing other fields/version/status checks remain. Test old/new equal strings conflict; different Unicode combining sequences stay distinct; NUL and long incompressible byte keys work; same external key in a different account remains isolated. Two real sessions must prove loser waits then sees winner for both key types. No ON CONFLICT/full-byte B-tree/hash shortcut.

```python
# After a committed legacy-text key, bytes spelling of the same key is duplicate.
with pytest.raises(IntegrityError) as raised:
    insert_new_bytes_identity(connection, legacy_key.encode("utf-8"))
assert raised.value.orig.diag.constraint_name == "uq_review_fact_external"
```

The local fixture adapter inserts a complete legitimate fact/observation/run unit satisfying deferred pointer invariants; it must not disable triggers, stamp schema or treat an incomplete fake root as success. Mirror the assertion for run key. Identity representation swapping after INSERT, terminal run mutations and observation updates fail. New bytea is not mutable metadata.

- [ ] Step5 expand only existing permitted writers' run column rights, add helper EXECUTE for required existing writers, PUBLIC denied. Runtime script updates column allowlist (still excludes run_sequence) and helper grants after broad grants within one transaction. Enumerate table+column ACL existing grantees/inheritance as needed, no global/default/old-table role rewrite. Test exact old-column writer gains only paired new field, SELECT-only role gains no write, current non-owner runtime can insert every new field but cannot delete/truncate/override sequence/setval or alter triggers/functions/policies. No-context/wrong-org/cross-tenant FK/RLS behavior remains denied. Do not claim org RLS alone implements same-org allowed-account authorization.

Before any DDL, preflight relacl and attacl for PUBLIC INSERT/UPDATE on all three affected relations. Actual0063 preserves such grants; preserving arbitrary PUBLIC writers conflicts with mandatory PUBLIC helper EXECUTE denial. Refuse with fixed SQLSTATE55000 `review_lossless_acl_unsupported`, leaving schema/revision/data/ACLs unchanged. PUBLIC SELECT-only remains compatible. Do not broaden helper rights, silently revoke old PUBLIC writes or enumerate current roles in lieu of PUBLIC. Prove each table/column writer case and the SELECT-only positive control; real ACL remediation is a separate owner decision, not part of this migration.
Acquire all three ACCESS EXCLUSIVE locks first, in downgrade's fixed order; LOCK is not DDL. Prove any asserted concurrent-GRANT fencing with an actual two-session test, not an assumed lock mode. If no such fence is proven, state exclusive privileged DDL/ACL maintenance as the operating precondition; no global/catalog-lock scope expansion.

- [ ] Step6 migration/downgrade proof in own disposable schemas: empty bootstrap without stamp, actual0064 synthetic old data upgrade unchanged, old-only downgrade→upgrade preserves values and identity, anybyte-backed row blocks downgrade before DDL. Lock all three affected tables and inspect all new columns with genuine visibility; non-BYPASS owner under FORCE RLS with hidden byte data must fail, not see empty. Restore original three function definitions/NOT NULL and drop only new checks/columns/helpers, no CASCADE. Original0063 function text restoration must match actual revision, not approximated snippets.

Separate pinnedfeature/currentruntime-script fixtures; graph tests onehead+0065ancestor plus synthetic in-memory successor, never edit actual graph to test futurehead. Run actual existing runtime script against latest schema; historical0063 tests must remain historically scoped. Record grants before/after and absence of unrelated ACL drift, not just superuser access.

- [ ] Step7 focused+adjacent GREEN, static checks and exact cleanup. Run under plan-owned ignored copy of the verified Unix-only profile; no ambient environment/test auto-plugins.

```sh
.venv/bin/python -m pytest -q -s --tb=short tests/test_review_lossless_migration.py tests/test_review_lossless_rls.py tests/test_review_facts_schema.py tests/test_orders_exact_text_migration.py tests/test_orders_schema_candidate.py tests/test_orders_schema_integration.py tests/test_orders_contract.py
.venv/bin/python -m compileall -q alembic/versions/20260909_0065_review_lossless.py tests/test_review_lossless_migration.py tests/test_review_lossless_rls.py
git diff --check
```

Scoped Ruff may use `/tmp/satorna-backend-verify-20260908/bin/python -m ruff check` only. Exact command/exits/counts, no skips/natural completion, allocated resources and cleanup absence are required. Synthetic-secret/error canaries must not reflect payload fragments in new custom errors. Existing `/dev/null` passfile warning is disclosed, not suppressed as success/noise-free claim.

- [ ] Step8 self-review and bounded commit `feat: preserve Review Facts Unicode content losslessly`. Handoff exact fields/helper/trigger/conflict/grants/decoder-first/rollback contract and test evidence; independent controller review before readySHA toT4. T4 separately consumes it withactualrepository/checksum/manifest/replay anduserguard; no fullparity oractivationclaim.

## Preflight and next dependency

All eight pairs must migrate together because source-run/fact identities and
coverage otherwise keep rejecting acceptedNUL evidence. One task is atomic and
independently reviewable; it produces no runnable provider/HTTP workflow. Exact
T4Unicode boundary268ea1d closes pre-SQLsurrogate semantics without banning NUL.
HelperCHECK syntax is intentionally narrower than typedsemanticcoverage validation,
preserving0063objectadmission but not inventingnewdomainchecks.

After this newactualhead is accepted, controller amends the queued approvals
plan's revision/downrevision and only then dispatches its migration. No revision
number is silently recycled or reserved against committed reality.
