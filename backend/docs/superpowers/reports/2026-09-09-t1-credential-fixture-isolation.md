# T1 credential fixture isolation handoff

Date: 2026-09-09. Task 1 only, implemented from base
`beef478a38a456a0a2e2c00b62f6b4447c786b96` on branch
`codex/arch-t1-platform`. The controller's intervening docs-only commit
`54d5f48` was preserved and is not part of this task's staged set.

## Result

The credential SQLite fixture now creates exactly its five owned tables:
`lk_organizations`, `lk_users`, `marketplace_accounts`,
`marketplace_account_credentials`, and `lk_audit_events`. It retains the shared
`Base.metadata` and existing fixture return/lifetime behavior, but no longer
attempts to compile unrelated imported ORM tables.

The regression registers a synthetic PostgreSQL-only `JSONB` table in the real
shared metadata before requesting the real `store_db` fixture. It exercises real
credential put/resolve and audit persistence, verifies the exact five physical
SQLite tables, proves the synthetic definition remains intact in metadata, and
removes only that definition in `finally`. A separate positive case preserves the
ordinary put/resolve/audit control.

No production/runtime model, schema, RLS, provider, PostgreSQL test, dependency,
or external service behavior changed.

## TDD evidence

All Python/pytest execution used this worktree's `backend/.venv`, `env -i`, and
the plan-owned macOS sandbox denying all network access and selected secret-file
reads. Commands exited naturally; no failing status was masked.

Initial test-shape RED, before production-fixture changes:

```sh
env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin LC_ALL=C PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PIP_CONFIG_FILE=/dev/null NETRC=/dev/null PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-credential-fixture-isolation/task-1-sandbox.sb .venv/bin/python -m pytest -q tests/test_marketplace_credential_fixture_isolation.py
```

Natural result: exit 1, `2 failed in 0.77s`. The isolation test produced the
required real `sqlalchemy.exc.CompileError` at
`tests/test_marketplace_credential_store.py:44`: SQLite could not render the
synthetic unrelated `JSONB` column. The ordinary control also failed only because
its first draft duplicated the new exact-table-boundary assertion.

The control was narrowed to existing behavior without touching implementation,
then the same command was repeated. Authoritative RED: exit 1,
`1 failed, 1 passed in 0.79s`; the only failure was the expected real JSONB
`CompileError` from global `Base.metadata.create_all(engine)`.

Minimal GREEN changed only the fixture import and `create_all(..., tables=[...])`
list. The same focused command then returned exit 0, `2 passed in 0.34s`.

## Final verification evidence

Both required collection orders:

```sh
env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin LC_ALL=C PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PIP_CONFIG_FILE=/dev/null NETRC=/dev/null PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-credential-fixture-isolation/task-1-sandbox.sb .venv/bin/python -m pytest -q tests/test_marketplace_credential_fixture_isolation.py tests/test_marketplace_credential_store.py tests/test_marketplace_credential_fetch.py
```

Fresh final natural result: exit 0, `35 passed in 1.25s`.

```sh
env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin LC_ALL=C PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PIP_CONFIG_FILE=/dev/null NETRC=/dev/null PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-credential-fixture-isolation/task-1-sandbox.sb .venv/bin/python -m pytest -q tests/test_marketplace_credential_fetch.py tests/test_marketplace_credential_store.py tests/test_marketplace_credential_fixture_isolation.py
```

Fresh final natural result: exit 0, `35 passed in 1.22s`.

Synthetic pre-collection registration before importing the existing paired-fetch
test:

```sh
env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin LC_ALL=C PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PIP_CONFIG_FILE=/dev/null NETRC=/dev/null PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-credential-fixture-isolation/task-1-sandbox.sb .venv/bin/python -c 'from sqlalchemy import Column, Integer, Table; from sqlalchemy.dialects.postgresql import JSONB; from app.infra.models import Base; import pytest; foreign = Table("credential_fixture_unrelated_pg_only", Base.metadata, Column("id", Integer, primary_key=True), Column("coverage", JSONB)); result = pytest.main(["-q", "tests/test_marketplace_credential_fetch.py::test_paired_fetch_exact_identity_and_closed_session"]); Base.metadata.remove(foreign); raise SystemExit(result)'
```

Natural result: exit 0, `1 passed in 0.28s`.

Compile and whitespace checks:

```sh
env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin LC_ALL=C PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PIP_CONFIG_FILE=/dev/null NETRC=/dev/null PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-credential-fixture-isolation/task-1-sandbox.sb .venv/bin/python -m compileall -q tests/test_marketplace_credential_fixture_isolation.py tests/test_marketplace_credential_store.py
git diff --check
```

Natural result: both exit 0 with no output.

Scoped Ruff used only the controller-approved lint interpreter:

```sh
env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin LC_ALL=C PIP_CONFIG_FILE=/dev/null NETRC=/dev/null PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-credential-fixture-isolation/task-1-sandbox.sb /tmp/satorna-backend-verify-20260908/bin/python -m ruff check tests/test_marketplace_credential_fixture_isolation.py
```

Natural result: exit 0, `All checks passed!`.

```sh
env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin LC_ALL=C PIP_CONFIG_FILE=/dev/null NETRC=/dev/null PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-credential-fixture-isolation/task-1-sandbox.sb /tmp/satorna-backend-verify-20260908/bin/python -m ruff check tests/test_marketplace_credential_store.py
```

Natural result: exit 1 with four findings: `I001`, two `UP017`, and `F841`.
The exact-base comparison below returned the same four findings, so they are
pre-existing and were not reformatted in this bounded fix:

```sh
git show beef478a38a456a0a2e2c00b62f6b4447c786b96:backend/tests/test_marketplace_credential_store.py | env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin LC_ALL=C /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-credential-fixture-isolation/task-1-sandbox.sb /tmp/satorna-backend-verify-20260908/bin/python -m ruff check --stdin-filename tests/test_marketplace_credential_store.py -
```

Natural result: exit 1 with the same `I001`, two `UP017`, and `F841` findings.

## Files and self-review

Commit scope is exactly:

- `backend/tests/test_marketplace_credential_store.py`
- `backend/tests/test_marketplace_credential_fixture_isolation.py`
- `backend/docs/superpowers/reports/2026-09-09-t1-credential-fixture-isolation.md`

Self-review checked the bounded design against the diff. The captured pre-fix RED
proves that restoring global metadata creation recreates the JSONB failure. As a
reasoning-only mutation check, omitting any owned table would be caught by the
exact physical-table assertion and/or the real put/resolve/audit path; that deletion
was not separately executed. The synthetic table is neither compiled nor mutated
and is removed in `finally`. There is no exception-catching success path, dynamic
registry scan, cloned model, dialect conversion, or fixture interface change.

## Limits and next gate

This surrogate establishes T1 fixture isolation only. It does not import the new
T4 Review facts ORM, does not replace the actual T4 mixed collection, and makes no
claim about the separate `314 passed / 20 setup errors` acceptance gate. Full
application, external/PostgreSQL database, provider, Redis, and network-backed
suites were outside scope and were not run; only the in-memory SQLite fixture was
used. The requested local `preflight-critic` skill file was not present; the
required critic pass was therefore performed as an isolated manual self-review,
with independent controller review still required before T4.
