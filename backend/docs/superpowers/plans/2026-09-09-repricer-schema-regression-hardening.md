# Repricer schema regression hardening implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans; TDD and verification-before-completion.

**Goal:** Close two independently reviewed proof gaps without changing accepted0066 DDL.
**Architecture:** Add representative old-history preservation and precise v1 optional-numeric regression checks to the existing PostgreSQL suites.
**Tech Stack:** Existing own backend/.venv, pytest/SQLAlchemy/PostgreSQL16; no dependencies.
**Spec:** Bounded Design below and accepted `backend/docs/superpowers/specs/2026-09-09-repricer-approvals-storage-design.md`; task2 of its plan remains authoritative for schema semantics.

## Design / evidence

Independent review of dbf8d31 found 0Critical/0Important but two test weaknesses:
M1 `test_empty_roundtrip_preserves_previous_data` seeds no previous data; the ACL
test checks only organization row contents. M2 optional size/min fractional or
nonfinite negatives create legacy rows whose separate NULL-only shape can reject
them even if numeric validation is absent. Current DDL correctness was accepted;
this is test strengthening, not a claim of an observed runtime vulnerability.

Populate synthetic0065 with valid Orders and Reviews facts BEFORE0066, including
lossless Review bytes. Compare actual driver-returned row values and old ACLs after
every forward/reverse step while new approvals tables stay empty. Numeric tests
must use otherwise valid v1 input with matching independently built canonical
bytes/hash and assert a specific numeric diagnostic, not any DBAPIError or a
legacy-format failure. Positive optional values demonstrate the fixture is usable.

## Global constraints

- T1 worktree only; modify only listed tests/docs. No migration/runtime/ORM/domain edits, old revision rewrite, schema number allocation, flags or consumers.
- Only exact own random disposable DB/runtime roles over authorized Unix socket via existing candidate allocator; never application DB/roles/host config. No working Redis, provider/network, production/real credentials/.env, GitHub/push/deploy or installs.
- Scrubbed ownvenv process and plan-owned all-IP/known-secret-deny profile allowing only `/private/tmp/.s.PGSQL.5432`; lint-only approved alternate Ruff. Preserve finally cleanup/absence proof, no skips/xfails/baseline or timeout-as-success.
- M3 inherited passfile warning is separately tracked; do not silence it or read a real passfile here.
- Keep0066 historical fixtures pinned; latest-head runtime-script checks remain separate. This task can run after another accepted successor without renumbering anything.

## Task 1: Strengthen the two actual database gates

**Files:**
- Modify `backend/tests/test_repricer_approvals_schema.py` only preservation tests/helpers/imports.
- Modify `backend/tests/test_repricer_approvals_lifecycle.py` only numeric test/helper precision.
- Modify `backend/docs/superpowers/reports/2026-09-09-t1-repricer-approvals-schema-handoff.md` only verification wording/new exact evidence.
- Create `backend/docs/superpowers/reports/2026-09-09-t1-repricer-schema-regression-hardening.md`.

**Consumes:** Existing `candidate.disposable_database`, `candidate.migrate`, `candidate.order/item/observation`, exact-text `make_run`; `tests.test_review_facts_schema.seed` and `tests.test_review_lossless_migration.unit`. Read these actual helpers and models/guards before composing valid fixtures. Reuse them without changing their files or importing domain implementation from T2/T3/T4.
**Produces:** Same runtime/schema interfaces, stronger test-only contract and honest preservation scope.

- [ ] Read full affected tests and accepted review M1/M2 from same-program ledger/review, relevant typed numeric checks and serializer in0066. Record current assertions and mask; capture ordinary positive v1 creation on actual disposable0066.
- [ ] Preserve a separately named empty roundtrip. Add populated0065→0066→0065→0066 case: create own DB with actual chain/no stamp, seed synthetic organizations/accounts, one Orders run/order/item/observation and one complete Review unit with valid lossless UTF8 bytes containing NUL, watermark and run-item relation. Commit valid seed with guards enabled. Assert all selected old business relations are nonempty before taking the snapshot; snapshot exact column values, bytes, IDs, timestamps and relevant row counts plus existing relation/column/default ACLs. Never use only a checksum of a reconstructed subset.

```python
before = snapshot_old_rows(owner)  # fixed explicit table list, SELECT * ordered by PK
assert all(before[table] for table in OLD_BUSINESS_TABLES)
for action, revision in (("upgrade", "20260909_0066"),
                         ("downgrade", "20260909_0065"),
                         ("upgrade", "20260909_0066")):
    result = candidate.migrate(database.url, action, revision)
    assert result.returncode == 0, result.stderr
    assert snapshot_old_rows(owner) == before
    assert snapshot_old_acls(owner) == acl_before
```

Use `candidate.scope` for guarded Orders/Review writes where required. Main helpers
use compatible91001/91101 Avito seed. No truncate/disable-trigger/clear-plaintext
fixture trick. Existing owner engine disposes before exact DB teardown.
- [ ] Demonstrate new preservation assertion can fail: on a separate own synthetic disposable transaction change a permitted old staging-run field, invoke the same comparison and observe AssertionError, then rollback. This is a mutation positive-control of new regression, not a production bug claim. Do not mutate migration source or use weakened guard. Also run ordinary unchanged comparison PASS. Record exact RED/control and GREEN commands.
- [ ] Replace optional size/min negative coverage with otherwise valid v1 values. A small helper may accept exact optional fields and reconstruct the existing canonical Python JSON body/checksum/action key with stdlib, preserving existing helper defaults. Positive `size_id=1,min_price_kopecks=50` and >BIGINT integral values must successfully commit with created audit and resolve exact NUMERIC values. Actual0066 requires size>0 and minimum>=50; test minimum49 and size0 as separate range denials, not accepted values.
- [ ] For each optional field test fraction, NaN, +Infinity, -Infinity under v1. Assert exact fixed numeric safe error/SQLSTATE or intended numeric CHECK name as the actual contract establishes; NOT any error and NOT legacy NULL-only rejection. Keep existing required numeric and version coverage, tightening if same diagnostic issue applies. Confirm payload/checksum setup cannot itself accidentally fail earlier for unrelated reasons. Add focused mutation/control evidence that a removed numeric guard would no longer pass the intended diagnostic assertion, using only an isolated disposable helper/check experiment if needed; do not edit accepted migration or persist any relaxation.

```python
with pytest.raises(DBAPIError) as captured, db.begin() as c:
    scope(c)
    create_v1_numeric_boundary(c, field=field, value=value)
assert captured.value.orig.sqlstate == expected_numeric_sqlstate
assert captured.value.orig.diag.message_primary == expected_numeric_safe_message
```

Bind actual expected fixed diagnostic explicitly per observed constraint/function;
record why it comes from numeric validation. A broad set including unrelated
request-format/hash errors is not an acceptable replacement.
- [ ] Run under scrubbed Unix-only sandbox, natural results and cleanup:

```sh
.venv/bin/python -m pytest -q -s --tb=short tests/test_repricer_approvals_schema.py tests/test_repricer_approvals_lifecycle.py tests/test_repricer_approvals_rls.py
.venv/bin/python -m compileall -q tests/test_repricer_approvals_schema.py tests/test_repricer_approvals_lifecycle.py
git diff --check
```

Scoped Ruff affected tests. No new full-suite green claim. Report exact seeded old
tables/fields and observed bytes, not a universal production-shaped parity claim.
- [ ] Self-review no test mask/migration change, commit `test: strengthen repricer migration preservation and numeric checks`; full task report and independent scoped review before marking M1/M2 addressed. Updated handoff must distinguish original evidence from this later strengthening; M3 remains disclosed.

## Preflight

| Interface | Checked boundary |
| --- | --- |
| old0065 fixtures→0066 lifecycle | Only new tables empty; valid oldhistory survives downgrade and re-upgrade |
| numeric fixture→v1 reconstruction | Positive values commit; invalid numbers fail at specific numeric guard, no legacy mask |
| tests→T2 ready schema | Runtime andDDL unchanged; findings close only after actual proof/review, no activation |

These two minor findings stay visible until this task is executed; documentation is not test evidence.
