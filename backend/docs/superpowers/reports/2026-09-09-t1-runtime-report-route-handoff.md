# T1 runtime report route contract handoff

## Result

The runtime adapter now excludes only the four exact standalone `GET` stubs superseded by the real `app.routers.wb_reports_sprint_d` endpoints:

- `/api/v1/wb-reports/pnl`
- `/api/v1/wb-reports/ads/performance`
- `/api/v1/wb-reports/rnp`
- `/api/v1/wb-reports/abc`

It constructs a private `APIRouter` from a new filtered list of `reference_router.routes` before inclusion. The filter matches both exact path and exact `{"GET"}` methods, so it does not silently delete a future mixed-method route. The standalone reference router and its original seven direct route objects/endpoints remain untouched; its source-registry, repricer, review, and four report stub operations remain available when mounted independently.

## Contract proof

`backend/tests/test_runtime_report_route_contract.py` verifies against a full `create_app()` runtime:

- one effective GET per bounded path using installed FastAPI's public `iter_route_contexts` test traversal;
- exact endpoint object and module identity for the real report functions;
- complete OpenAPI operation equality with an app that mounts only the real report router;
- PNL `source` enum `operative | preliminary | final`, default `preliminary`;
- absence of the four previously observed duplicate operation IDs;
- exact pre-runtime-import standalone route-list/object/endpoint preservation;
- retained reference operation metadata and both local source-registry endpoints;
- unauthorized PNL denial before data access and authorized propagation of `dateFrom`, `dateTo`, `groupBy`, and `source` through the real handler using synthetic actor/permission/data/audit boundary fakes.

No private FastAPI implementation is used in runtime code. The runtime code only reads public route attributes while constructing an `APIRouter` from existing direct route objects.

## TDD and verification evidence

Sandbox syntax check:

```sh
/usr/bin/sandbox-exec -f .superpowers/sdd/2026-09-09-runtime-report-route-contract/task-1-sandbox.sb /usr/bin/true
```

Exit `0`.

Focused RED/GREEN command, from `backend/`:

```sh
/usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-runtime-report-route-contract/task-1-sandbox.sb /usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin CI=1 PYTHONUNBUFFERED=1 LC_ALL=C PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null VELLA_ENV=test VELLA_DATABASE_URL=postgresql+psycopg://synthetic:synthetic@127.0.0.1:1/unreachable VELLA_REDIS_URL=redis://127.0.0.1:1/0 VELLA_CELERY_BROKER_URL=redis://127.0.0.1:1/0 VELLA_CELERY_RESULT_BACKEND=redis://127.0.0.1:1/0 VELLA_WB_API_MODE=fake VELLA_AVITO_API_MODE=fake VELLA_REAL_PRICE_APPLY_ENABLED=false VELLA_REPRICER_LOCAL_PRICE_APPLY_ENABLED=false VELLA_REPRICER_SCHEDULER_ENABLED=false VELLA_REPRICER_WB_SYNC_ENABLED=false VELLA_FINANCE_SHADOW_INGEST_ENABLED=false VELLA_ADVERTISING_SHADOW_INGEST_ENABLED=false VELLA_CANONICAL_SHADOW_COLLECTION_ENABLED=false VELLA_REVIEW_SHADOW_ENABLED=false VELLA_MARKETPLACE_CREDENTIALS_ENABLED=false VELLA_AVITO_REPRICER_WORKER_ENABLED=false VELLA_AVITO_REPRICER_PRICE_APPLY_ENABLED=false VELLA_AVITO_RETURNS_SYNC_ENABLED=false VELLA_WB_FEEDBACKS_SEND_ENABLED=false VELLA_PROCESS_HEARTBEAT_ENABLED=false VELLA_1C_ENABLED=false .venv/bin/python -m pytest -q --tb=short tests/test_runtime_report_route_contract.py
```

- RED exit `1`: `2 failed, 3 passed, 6 warnings in 3.28s`; failures were the effective count of two and the exact four duplicate IDs. Four defect warnings plus two inherited dependency deprecations were observed.
- GREEN exit `0`: `5 passed, 2 warnings in 3.53s`; only the inherited FastAPI/Starlette deprecations remained.

Adjacent pure-reference gate, from `backend/`:

```sh
/usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-runtime-report-route-contract/task-1-sandbox.sb /usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin CI=1 PYTHONUNBUFFERED=1 LC_ALL=C PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null VELLA_ENV=test VELLA_DATABASE_URL=postgresql+psycopg://synthetic:synthetic@127.0.0.1:1/unreachable VELLA_REDIS_URL=redis://127.0.0.1:1/0 VELLA_CELERY_BROKER_URL=redis://127.0.0.1:1/0 VELLA_CELERY_RESULT_BACKEND=redis://127.0.0.1:1/0 VELLA_WB_API_MODE=fake VELLA_AVITO_API_MODE=fake VELLA_REAL_PRICE_APPLY_ENABLED=false VELLA_REPRICER_LOCAL_PRICE_APPLY_ENABLED=false VELLA_REPRICER_SCHEDULER_ENABLED=false VELLA_REPRICER_WB_SYNC_ENABLED=false VELLA_FINANCE_SHADOW_INGEST_ENABLED=false VELLA_ADVERTISING_SHADOW_INGEST_ENABLED=false VELLA_CANONICAL_SHADOW_COLLECTION_ENABLED=false VELLA_REVIEW_SHADOW_ENABLED=false VELLA_MARKETPLACE_CREDENTIALS_ENABLED=false VELLA_AVITO_REPRICER_WORKER_ENABLED=false VELLA_AVITO_REPRICER_PRICE_APPLY_ENABLED=false VELLA_AVITO_RETURNS_SYNC_ENABLED=false VELLA_WB_FEEDBACKS_SEND_ENABLED=false VELLA_PROCESS_HEARTBEAT_ENABLED=false VELLA_1C_ENABLED=false .venv/bin/python -m pytest -q --tb=short backend_contracts/tests/test_router_smoke.py backend_contracts/tests/test_19_05_invariants.py
```

Initial exit `0`: `14 passed, 2 warnings in 0.36s`, with the same inherited dependency deprecations.

Fresh pre-commit verification:

- Focused contract: exit `0`, `5 passed, 2 warnings in 2.91s`.
- Adjacent reference gate: exit `0`, `14 passed, 2 warnings in 0.26s`.
- Compile: exit `0`, no output.
- Scoped Ruff: exit `0`, `All checks passed!`.
- `git diff --check`: exit `0`, no output.

Static commands, from `backend/` unless noted:

```sh
/usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-runtime-report-route-contract/task-1-sandbox.sb /usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin LC_ALL=C PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null .venv/bin/python -m compileall -q app/routers/wb_19_05.py tests/test_runtime_report_route_contract.py
/usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-runtime-report-route-contract/task-1-sandbox.sb /usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin LC_ALL=C PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null /tmp/satorna-backend-verify-20260908/bin/python -m ruff check app/routers/wb_19_05.py tests/test_runtime_report_route_contract.py --output-format concise
cd .. && git diff --check
```

An initial scoped Ruff pass exited `1` with seven auto-fixable import-order/unused-noqa findings; they were corrected manually with `apply_patch`. Compile and `git diff --check` were already exit `0` in that pass.

## Changed paths

- `backend/app/routers/wb_19_05.py`
- `backend/tests/test_runtime_report_route_contract.py`
- `backend/docs/superpowers/reports/2026-09-09-t1-runtime-report-route-handoff.md`

The detailed ignored execution record is `.superpowers/sdd/2026-09-09-runtime-report-route-contract/task-1-report.md`.

## Boundaries and limitations

No lifespan, auth helper, provider, DB, Redis, Celery, default business store/cache, real send/apply, production, credential, `.env`, frontend, domain-math, dependency, flag, deploy, push, or remote-service activity was used. HTTP exercised the real route handler only after synthetic boundary fakes were installed.

This closes only the four runtime registration/OpenAPI collisions. It does not claim generated frontend contract parity, report-domain correctness, a broad backend baseline, or production readiness. The configured `preflight-critic` skill path was unavailable, so the required critical review is an isolated local Critic Pass; controller-owned independent review follows this commit.

## Isolated Critic Pass

The critic pass re-read the brief, production diff, complete test file, source router, and fresh verification output. It checked exact path+method filtering, original route identity, future mixed-method behavior, OpenAPI/dispatch identity, permission-before-data ordering, side-effect isolation, test mutation coverage, and scope. No Blocker or Important issue remained. The two inherited dependency deprecations are disclosed above.
