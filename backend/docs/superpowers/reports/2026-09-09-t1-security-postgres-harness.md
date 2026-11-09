# T1 credential security PostgreSQL harness handoff

Date: 2026-09-09
Base: `c4e3e373f97be19171f80ebdad2ce919dd7c84dc`
Branch: `codex/arch-t1-platform`

## Delivered behavior

Both historical security fixtures retain native PostgreSQL as their default and
select the existing candidate allocator only when
`ORDERS_TEST_USE_LOCAL_CLUSTER == "1"`. The wrapper resolves `cluster` lazily,
so absent, `0`, and `true` do not start or claim the shared local server.

Each module now has one bootstrap consumed by its native and local generators.
The original synthetic 0060-shaped DDL, stamp 0060, upgrade 0061, downgrade 0060,
upgrade 0061, seeds, grants, and all functional/RLS/race assertions are preserved.
Runtime roles are module-prefixed UUID4 hex identifiers and are created exactly
once: native creates its own; local delegates creation/removal to
`candidate.disposable_database`.

Local provenance is actual, not native-shaped: mode, allocator database, random
runtime role, Unix host, port, and `root=None`. Tests parse the runtime URL and
query `inet_server_addr() IS NULL`, `current_database()`, and `current_user`, then
check NOSUPERUSER/NOBYPASSRLS, exact 0061, and forced RLS on both credential tables.

The new harness separately proves a truly empty database has no Alembic version
table before direct upgrade to 0061. It also injects failure after each real local
allocation and proves owner-engine disposal plus exact database/role absence.

## RED / focused GREEN

All commands ran from `backend/` under the task Unix-only sandbox and scrubbed
environment.

```sh
/usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-security-postgres-harness/task-1-sandbox.sb /usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin CI=1 PYTHONUNBUFFERED=1 LC_ALL=C PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null VELLA_DATABASE_URL=postgresql+psycopg://satorna_gate:satorna_gate@127.0.0.1:1/unreachable VELLA_REDIS_URL=redis://127.0.0.1:1/0 VELLA_CELERY_BROKER_URL=redis://127.0.0.1:1/0 VELLA_CELERY_RESULT_BACKEND=redis://127.0.0.1:1/0 VELLA_WB_API_MODE=fake VELLA_AVITO_API_MODE=fake VELLA_REAL_PRICE_APPLY_ENABLED=false VELLA_WB_FEEDBACKS_SEND_ENABLED=false VELLA_AVITO_REPRICER_PRICE_APPLY_ENABLED=false .venv/bin/python -m pytest -q --tb=short tests/test_security_postgres_harness.py
```

RED exit `1`: all 16 cases failed on the intended missing interface:
`TypeError: disposable_postgres() takes 1 positional argument but 2 were given`.
No native cluster was attempted. After minimal implementation the same 16 routing
cases passed, exit `0`, in 0.72s.

```sh
/usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-security-postgres-harness/task-1-sandbox.sb /usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin CI=1 PYTHONUNBUFFERED=1 LC_ALL=C PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null VELLA_DATABASE_URL=postgresql+psycopg://satorna_gate:satorna_gate@127.0.0.1:1/unreachable VELLA_REDIS_URL=redis://127.0.0.1:1/0 VELLA_CELERY_BROKER_URL=redis://127.0.0.1:1/0 VELLA_CELERY_RESULT_BACKEND=redis://127.0.0.1:1/0 VELLA_WB_API_MODE=fake VELLA_AVITO_API_MODE=fake VELLA_REAL_PRICE_APPLY_ENABLED=false VELLA_WB_FEEDBACKS_SEND_ENABLED=false VELLA_AVITO_REPRICER_PRICE_APPLY_ENABLED=false .venv/bin/python -m pytest -q --tb=short 'tests/test_security_postgres_harness.py::test_disposable_postgres_routes_exactly_once_and_finalizes_selected_generator' 'tests/test_avito_ingestion_token_store.py::test_disposable_postgres_cleans_up_failed_start_without_progress'
```

Focused GREEN exit `0`: `19 passed in 0.55s`. This covers absent/`0`/`true`/`1`,
selected-generator close/error `finally`, and all three original native startup
failure/interrupt/cleanup outcomes.

## Actual local PostgreSQL gate

```sh
/usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-security-postgres-harness/task-1-sandbox.sb /usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin CI=1 PYTHONUNBUFFERED=1 LC_ALL=C PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null ORDERS_TEST_USE_LOCAL_CLUSTER=1 VELLA_DATABASE_URL=postgresql+psycopg://satorna_gate:satorna_gate@127.0.0.1:1/unreachable VELLA_REDIS_URL=redis://127.0.0.1:1/0 VELLA_CELERY_BROKER_URL=redis://127.0.0.1:1/0 VELLA_CELERY_RESULT_BACKEND=redis://127.0.0.1:1/0 VELLA_WB_API_MODE=fake VELLA_AVITO_API_MODE=fake VELLA_REAL_PRICE_APPLY_ENABLED=false VELLA_WB_FEEDBACKS_SEND_ENABLED=false VELLA_AVITO_REPRICER_PRICE_APPLY_ENABLED=false .venv/bin/python -m pytest -q -s --tb=short tests/test_security_postgres_harness.py tests/test_marketplace_credential_rls.py tests/test_avito_ingestion_token_store.py
```

Exit `0`, natural exit: `63 passed in 6.84s`, no skips/xfails and no previously
unrun functional failures. Actual RLS, ACL, verifier, redaction, lifecycle,
canaries, and two-session races executed.

Combined-run resources, all followed by allocator cleanup confirmation:

- Empty unstamped: `orders_test_4c18d8e4a4614e09a80b8067fed2b29a`, no role.
- Historical credential: `orders_test_d68d2d2f4b494656a79a9cc7a496599e` /
  `satorna_credential_runtime_27ab587b8c8b487a89a749ee94a7f7c9`.
- Historical ingestion: `orders_test_5d9d67c949e741abba638dfe3575730a` /
  `satorna_ingestion_runtime_0fed21c5ba8947efbebaa34b8730c9a7`.

Focused failure-injection cleanup ran with the same local sandbox prefix and test
node `test_local_bootstrap_failure_disposes_engine_and_removes_owned_resources`.
Exit `0`: `2 passed in 1.72s`. Exact pairs queried absent after cleanup:

- `orders_test_27ee38b3a0fe4594b7d38712e1bb5f83` /
  `satorna_credential_runtime_db4b4ed1512244c79da1531c0fdba14a`.
- `orders_test_114c526f565b4702b366d1f7eb8a5ed3` /
  `satorna_ingestion_runtime_3e078bad72164cc99c71e78260aa1da0`.

## Static checks and lint baseline

```sh
/usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-security-postgres-harness/task-1-sandbox.sb /usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin CI=1 PYTHONUNBUFFERED=1 LC_ALL=C PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null .venv/bin/python -m compileall -q tests/test_security_postgres_harness.py tests/test_marketplace_credential_rls.py tests/test_avito_ingestion_token_store.py
```

Exit `0`. `git diff --check` also exited `0`.

Own `.venv` Ruff invocation exited `1` with `No module named ruff`. The approved
lint-only interpreter was used:

```sh
/usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-security-postgres-harness/task-1-sandbox.sb /usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin CI=1 PYTHONUNBUFFERED=1 LC_ALL=C PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null /tmp/satorna-backend-verify-20260908/bin/python -m ruff check tests/test_security_postgres_harness.py tests/test_marketplace_credential_rls.py tests/test_avito_ingestion_token_store.py
```

The new harness alone passes Ruff. Read-only baseline at `c4e3e37` had eight
credential findings (`I001` plus seven `SIM117`) and three ingestion findings
(`I001`, `UP017`, `SIM117`). Current combined lint has exactly nine inherited
findings on unchanged historical assertions: one `UP017` and eight `SIM117`.
An I-only pass fixed the two baseline import blocks and the new harness import
block; no unrelated functional formatting was performed.

## Limits

- Native full runtime is `NOT_RUN` because of the recorded host ENOMEM limitation.
  Native remains default; its owned temp-root/random-loopback behavior and startup
  cleanup unit tests remain. Local GREEN is not reported as native GREEN.
- Local mode proves Unix-socket origin and exact allocator-owned names. Port 5432
  is not claimed as a newly launched server.
- `/dev/null` passfile warnings remain visible and intentionally unsuppressed.
- No production/app database or data, real credential, provider, Redis, IP network,
  host setting, dependency, helper, runtime, migration/schema/grant, flag,
  deployment, push, or merge action occurred.
