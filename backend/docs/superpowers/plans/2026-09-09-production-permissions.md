# Production permission boundary implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans. TDD and verification-before-completion apply.

**Goal:** Register explicit-only Production capabilities and enforce the shared read-plus-command contract without changing profiles or enabling consumers.
**Architecture:** Four immutable constants in the existing permission module, one pre-I/O requirement validation in the existing publication guard, focused pure/disposable-PG acceptance. T3 continues to own service and domain audit.
**Tech Stack:** Existing Python/SQLAlchemy/pytest/PostgreSQL16; no dependency changes.
**Spec:** `backend/docs/superpowers/specs/2026-09-09-production-permissions-design.md`.

## Global Constraints

- Only arch-t1-platform worktree. No domain/router/task/frontend/migration/runtime-grant edits or copying T3 service implementation.
- No changes to `PROFILE_PERMISSIONS`, `LEGACY_PROFILE_ALIASES`, existing permission strings or profile normalization. Admin/owner/settings_editor/legacy production aliases receive no implicit Production capability.
- Existing explicit membership grants are unioned with profile grants exactly as today. Registration is separate from assignment; no user rows, defaults, migrations, provisioning or grant-management endpoints change.
- No actual unassign, background principal, production/provider/real credentials/.env/network/Redis/keys/flags/backfill/push/deploy or host changes.
- Own backend/.venv, env-i PATH=/usr/local/bin:/usr/bin:/bin, PGPASSFILE/PGSERVICEFILE/NETRC=/dev/null, PYTEST_DISABLE_PLUGIN_AUTOLOAD=1, ORDERS_TEST_USE_LOCAL_CLUSTER=1, reviewed secret/IP-denying Unix-only sandbox. Lint-only interpreter exception remains lint only.
- Only fresh random own DB/runtime roles via existing allocator, exact own cleanup. Heavy PostgreSQL gates require explicit team-slot admission and natural completion before release; no skips/xfails/baseline expansion/timeout widening.
- One implementation agent at a time. Do not begin until preceding P1 schema task passes review or controller explicitly parks its remaining work; this is a queued plan, not READY.

## Task 1: Register and verify fixed Production requirements

**Files:** modify `backend/app/cabinet/permissions.py` (add four constants only); modify `backend/app/platform/integrations/publication_guard.py` (imports and pre-I/O read prerequisite only); create `backend/tests/test_production_permissions.py`; create `backend/tests/test_production_permissions_postgres.py`; create `backend/docs/superpowers/reports/2026-09-09-t1-production-permissions-handoff.md`.

**Consumes:** existing UserSessionPrincipal, ExpectedAccountBinding, acquire_publication_guard, actual profile+explicit-grant membership fence; existing test_publication_guard_postgres data/arguments/proof/count_proof/wait_blocked fixtures and examples. Read those exact fixtures before reuse; local helpers must not import domain service or silently upgrade global schema.

**Produces:** exact imports and constants:

```python
PRODUCTION_PERMISSION_KEYS = frozenset({"production:read", "production:create", "production:assign"})
PRODUCTION_READ_PERMISSIONS = frozenset({"production:read"})
PRODUCTION_CREATE_PERMISSIONS = frozenset({"production:read", "production:create"})
PRODUCTION_ASSIGN_PERMISSIONS = frozenset({"production:read", "production:assign"})
```

- [ ] Read full spec, existing permission module and guard. Record clean exact owned paths/BASE. Snapshot the current profile+alias mapping in test expectations; do not compute expected profiles from the changed code itself.
- [ ] Write pure tests first. Dynamic attribute lookup must fail for absent constants. Use a real Session with connection/SQL methods instrumented to reject any access; for valid principal/account and requirements containing create or assign without read, assert `publication_context_invalid` before those methods. Existing guard reaches session/SQL admission instead, producing actual RED. Never count an unrelated import/setup failure as behavior RED.
- [ ] Add exact immutable constant/union tests and unchanged profile/alias tests (including admin, owner, production, custom). Test write-without-read for create, assign, both, and mixtures with unrelated permissions. Valid full sets retain existing guard behavior, not new blanket rejection. Unknown-input errors never reflect the canary.
- [ ] Implement constants and one bounded conditional after current required_permissions shape validation:

```python
if (required_permissions & (PRODUCTION_PERMISSION_KEYS - PRODUCTION_READ_PERMISSIONS)
        and not PRODUCTION_READ_PERMISSIONS <= required_permissions):
    raise PublicationGuardError("publication_context_invalid")
```

Do not alter role lookup, scope predicates, physical-root admission, lock ordering, SQL projections, callbacks, auth fallback or public error codes. Keep existing literal/read-size checks. No operation selector from request data.

- [ ] Write actual PG acceptance using the existing own disposable guard fixture and synthetic explicit membership grants. Example compose without credential dependency:

```python
args = arguments(d, read=True)
args["required_permissions"] = PRODUCTION_CREATE_PERMISSIONS
with d.factory() as session, session.begin():
    guard = acquire_publication_guard(session, **args)
    guard.revalidate_before_write()
    proof(session, d)
assert count_proof(d) == 2
```

Before that operation, the test setup explicitly sets membership permissions to both required keys, never profile auto-grants. Parameterize read/create/assign, both WB/Avito exact account bindings, custom/viewer/admin explicit grants; all ungranted profiles and legacy production alias deny. Read-only and wrong write-operation grants deny create/assign. Same-org wrong selected account and different-org principal/account deny. Inactive membership, revoked/expired session and stale identity map deny. Denial uses exact safe codes and zero committed proof rows. This is shared-fence proof, not actual Production receipt audit.
- [ ] For create and assign, remove read or command grant before acquire and in the same transaction before commit, asserting denial and rollback. Use real two-session concurrency with observed `wait_blocked`: revoker-first commits then requester denies; requester-first holds guard, revoker blocks until commit, next operation denies. Use existing bounded wait/error discipline; no timing-only sleep as proof. Recheck operation requirements on the synthetic replay-shaped path before returning existing proof data; label it a shared contract test, not T3 domain replay acceptance.
- [ ] Run pure RED then GREEN, followed by both new files plus existing publication_guard pure/PG files in one admitted interval; natural exit and exact cleanup. No new DDL needed, underlying fixture retains its accepted historical revision. Run compileall for the four Python paths, scoped Ruff, git diff --check and exact changed-path check. Existing legacy Ruff findings must be compared to exact BASE rather than hidden by blanket formatting.

```sh
.venv/bin/python -m pytest -q --tb=short tests/test_production_permissions.py
.venv/bin/python -m pytest -q -s --tb=short tests/test_production_permissions.py tests/test_production_permissions_postgres.py tests/test_publication_guard.py tests/test_publication_guard_postgres.py
.venv/bin/python -m compileall -q app/cabinet/permissions.py app/platform/integrations/publication_guard.py tests/test_production_permissions.py tests/test_production_permissions_postgres.py
git diff --check
```

- [ ] Self-review exact scope and spec. Commit `feat: add explicit Production permission contract`. Handoff exact constants/guard arguments/safe codes, consumer replay-before-receipt and final-commit obligations, test evidence, unchanged profiles and disabled activation. Do not claim schema or domain readiness. Independent review before exact READY T3.

## Preflight

| Interface | Check |
| --- | --- |
| registry→profile grant union | Constants independent of profiles; no automatic grant |
| operation constants→guard | Write implies read pre-I/O; existing unrelated permission sets preserved |
| guard→service/replay | Real user/session/account, same fixed operation set; T3 actual audit/receipt acceptance separate |
| permissions→0069 | No DDL or unassign expansion |
| fixture→race evidence | Owned historical PG fixture, observed locks and final rollback; no provider transport |
