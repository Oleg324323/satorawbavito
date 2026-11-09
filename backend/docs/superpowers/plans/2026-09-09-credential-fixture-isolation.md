# Credential fixture isolation implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans; TDD and verification-before-completion.

**Goal:** Keep the SQLite credential unit fixture independent of unrelated global ORM imports.
**Architecture:** Explicit existing five-table boundary, without changing ORM types or PostgreSQL tests.
**Tech Stack:** Existing SQLAlchemy/pytest/SQLite, own backend/.venv; no dependencies.
**Spec:** This plan's bounded Design section; actual T4 regression and T1 diagnostic below.

## Design and evidence

T4 mixed collection imports new Review JSONB metadata before paired credential tests:
314PASS/20setupERROR; fetch alone passes. T1 independently registered one synthetic
unrelated JSONB Table in the same Base before running the exact existing paired-fetch
test: 1setupERROR0.71s, SQLiteTypeCompiler missing visit_JSONB at store fixture line44.
No implementation change was made for this diagnostic; earlier six-file304PASS is
the ordinary-control baseline. T1 has only legacy Review ORM, not T4's new facts
ORM; do not copy T4 code or claim that importing legacy ORM proves its actual gate.

Root cause: Base.metadata.create_all(engine) creates all imported tables, although
this fixture owns only credential/account/audit rows. Select exactly org, user
(audit FK), account, credential and audit tables. Do not catch CompileError, mutate
JSONB, remove another module's metadata or select tables by supported dialect type.
No runtime/schema/RLS behavior changes. Actual mixed Review acceptance remains T4.

## Global constraints

- Only T1 worktree `/Users/bratishka/Downloads/satornawb-main/.worktrees/arch-t1-platform`.
- Test/docs paths only; no production, network/providers/DB/Redis, .env/real secrets,
  flags/keys, GitHub/push/deploy, dependency installs or other-worktree modifications.
- Own backend/.venv under env-i and plan-owned all-network/known-secret-file denial;
  lint-only `/tmp/satorna-backend-verify-20260908/bin/python -m ruff` allowed.
- No skips/xfails/baseline expansion. PostgreSQL JSONB and runtime models unchanged.
- Active approvals agent owns its schema/test paths; do not stage or overwrite them.

## Task 1: Bound the credential unit fixture schema

**Files:**
- Modify `backend/tests/test_marketplace_credential_store.py` only explicit model import/table list in fixture.
- Create `backend/tests/test_marketplace_credential_fixture_isolation.py`.
- Create `backend/docs/superpowers/reports/2026-09-09-t1-credential-fixture-isolation.md`.

**Consumes:** existing store_db fixture used by test_marketplace_credential_fetch;
actual existing model FKs and put/resolve/audit behaviors. **Produces:** same fixture
return and test interfaces, no runtime exports.

- [ ] Read full existing fixture and referenced table definitions. Write regression
before fixing: register a unique synthetic unrelated PostgreSQL-only JSONB Table
in Base.metadata, request the real store_db fixture AFTER registration, execute
real put/resolve and audit read. Remove only that exact synthetic table in finally;
never alter production metadata or catch an expected JSONB exception as success.

```python
foreign = Table("credential_fixture_unrelated_pg_only", Base.metadata,
                Column("id", Integer, primary_key=True), Column("coverage", JSONB))
try:
    factory, keyring = request.getfixturevalue("store_db")
    # real credential put/resolve and audit assertions using existing synthetic helpers
    assert "credential_fixture_unrelated_pg_only" not in inspect(factory.kw["bind"]).get_table_names()
    assert Base.metadata.tables[foreign.name] is foreign
    assert isinstance(foreign.c.coverage.type, JSONB)
finally:
    Base.metadata.remove(foreign)
```

Test also asserts exactly the five expected physical tables exist (not a reduced
schema that silently dropped account/credential/audit integrity). Import fixture
explicitly into the new test module; no pytester/plugin dependency. Include a
separate ordinary positive case. Run new file RED with expected actual CompileError,
not absent fixture/import error; all-network OS sandbox permits no PG connection.
- [ ] Minimal fix retains Base metadata and all definitions, with explicit:

```python
Base.metadata.create_all(engine, tables=[
    LkOrganizationRow.__table__, LkUserRow.__table__, MarketplaceAccountRow.__table__,
    MarketplaceAccountCredentialRow.__table__, LkAuditEventRow.__table__,
])
```

No dynamic registry scan, cloned model, dialect conversion or unrelated fixture refactor.
Keep current fixture return/lifetime behavior unchanged in this root-cause fix.
- [ ] GREEN own-runtime commands under scrubbed profile, both collection orders:

```sh
.venv/bin/python -m pytest -q tests/test_marketplace_credential_fixture_isolation.py tests/test_marketplace_credential_store.py tests/test_marketplace_credential_fetch.py
.venv/bin/python -m pytest -q tests/test_marketplace_credential_fetch.py tests/test_marketplace_credential_store.py tests/test_marketplace_credential_fixture_isolation.py
.venv/bin/python -m compileall -q tests/test_marketplace_credential_fixture_isolation.py tests/test_marketplace_credential_store.py
git diff --check
```

Also repeat controller's synthetic pre-collection JSONB registration + existing
paired-fetch test after fix: unlike test-local registration this exercises collection
order directly. Capture exact natural results. Scoped Ruff newfile and changedfile;
separate pre-existing lint findings, no broad reformat to gain unrelated green.
- [ ] Self-review, bounded commit `test: isolate credential fixture from unrelated ORM tables`.
Handoff exact files/RED/GREEN/limitations; independent reviewer before ready T4.

## Preflight

Single task fixture→fetch import shares exact same fixture object; no conftest-wide
global override. Registered synthetic table is cleaned in finally even on RED;
that cleanup does not destroy any DB/data. Real T4 JSONB import/334 mixed gate is a
separate consumer acceptance, never inferred from this surrogate regression.
