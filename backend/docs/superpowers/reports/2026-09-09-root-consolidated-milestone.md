# Consolidated integration milestone — bounded acceptance

This is an intermediate integration milestone, not full architecture acceptance,
deployment, or permission to enable provider actions. The user deferred order
assembly/printing; its existing migrations remain for compatibility.

## Immutable inputs

Started from root integration `a2ae397` in isolated branch
`codex/architecture-milestone-review`.

| Owner | Accepted input |
| --- | --- |
| T1 platform | `e6d97c2ee97fbc320ba98c945a768a2b9a631a0e` |
| T2 repricer consumer | `f3c61d7704813790245ec73096c753ee5dd93915` |
| T3 Orders consumer | `35ea34e77d80920b0d8779a6b888e6f1d8e7bad0` |
| T4 Reviews/frontend | `77abab0ae4a5bef1544bccfbeb92e728d8b7c4e6` |
| Explicit SKU permission addition | `47ca9513244ea29f21aba47527cbd8807d31caed` |
| Explicit current Orders fixture correction | `be0329bf211f8a3af7b087efddbc6d1a63243854` |

No moving owner branch was substituted. Later source progression, Review reads,
settings and service work are outside this candidate until separately admitted.

## Merge and registration

T4 contained older copies of four shared T1 files: runtime role SQL, repricer
schema/lifecycle tests, and the repricer schema handoff. Conflict resolution kept
the exact frozen T1 versions, preserving 0069 ACL restrictions and expanded
numeric/history preservation tests. Independent read-only review found no loss
of T4-owned behavior in these four resolutions.

T1 explicitly delegated shared registration in this candidate to root. Real
Orders GET `/api/v2/orders` and Reviews POST `/api/v2/reviews/wb/sync` are included
in `app.main`, retaining prefixes, authentication and the default-off Review gate.
No domain implementation, flag, legacy writer or provider execution is changed.

New tests exercise actual main routing/OpenAPI, unauthenticated responses,
error-envelope propagation, default-off denial and validation value redaction.
Dependency-overridden 401/403 cases prove envelope handling, not real revocation;
actual domain PostgreSQL tests provide separate authorization/race evidence.
Wheel checks now import the actual Orders and Review router/service modules from
an isolated installed wheel, not the checkout.

## Evidence and pending gates

- Before registration: 9 expected failures (missing routes/404).
- After registration: 17 PASS, 2 inherited deprecation warnings, 14.16 seconds,
  natural exit; includes runtime report compatibility and installed-wheel tests.
- Initial combined PG attempt: 1 PASS/432 setup errors, all due to missing
  PostgreSQL binaries in root's scrubbed PATH. No database was allocated. Corrected
  PATH includes `/usr/local/bin`; no product code or timeout changed for this.
- Corrected combined PG run: **433 PASS**, 2 inherited warnings, 147.24 seconds,
  natural exit 0 with allocator cleanup assertions. All 15 selected files ran;
  diagnostic `--maxfail=3` did not cut the passing run short. Log:
  `/tmp/satorna-root-milestone-pg-20260909.log`. Prior failure is not waived.
- New registration tests and wheel test Ruff check pass. Diff whitespace check
  passes. Independent scoped review found no important registration/conflict
  issues; this is not a repository-wide review.
- Combined full frontend: **430 PASS / 0 FAIL / 0 PENDING**, 132 suites,
  183.67 seconds, natural exit 0. JSON report:
  `/tmp/satorna-root-milestone-frontend-20260909.json`.
- TypeScript app and node projects both exit 0, with build-info files outside
  shared dependencies. Vite build exits 0 in 4.36 seconds, retaining chunk-size
  and plugin timing warnings. Snapshot source equals frozen T4 byte-for-byte;
  the snapshot generator was not rerun or represented as newly tested.
- Registration commit: `587967a40c0d779e5271a709701282ffb97ae8a2`.
  Root may fast-forward the original integration branch to this verified
  milestone plus documentation. No GitHub push or final whole-TZ claim.

Runtime uses the existing T1 Python environment with a scrubbed environment and
OS network denial except the approved local PostgreSQL Unix socket. Tests allocate
only synthetic disposable databases/roles with their own cleanup assertions.
Heavy checks run sequentially in the explicitly handed-off root slot.
All root processes finished; the slot was explicitly returned to T1.

## Reproduction selections

Backend interpreter: sibling `arch-t1-platform/backend/.venv/bin/python`.
All backend commands used `env -i`, PATH `/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin`,
TMPDIR `/tmp`, USER `bratishka`, PGPASSFILE/PGSERVICEFILE/NETRC `/dev/null`,
`ORDERS_TEST_USE_LOCAL_CLUSTER=1`, `PYTHONDONTWRITEBYTECODE=1`, and
`sandbox-exec -f` the existing T1 publication-guard `task-1-offline.sb`.
From this candidate's backend:

```sh
python -m pytest -q --tb=short tests/test_canonical_main_registration.py tests/test_runtime_report_route_contract.py tests/test_installed_wheel.py
python -m pytest -q --tb=short --maxfail=3 \
  tests/test_wb_repricing_postgres_repository.py tests/test_wb_repricing_approval_commands.py \
  tests/test_wb_repricing_legacy_import.py tests/test_orders_read_service.py \
  tests/test_orders_read_assembly.py tests/test_orders_http.py \
  tests/test_review_run_binding_repository.py tests/test_review_binding_publication_races.py \
  tests/test_review_canonical_http.py tests/test_review_shadow_service.py \
  tests/test_orders_run_binding_migration.py tests/test_orders_run_binding_rls.py \
  tests/test_review_run_binding_migration.py tests/test_review_run_binding_rls.py \
  tests/test_review_run_binding_acl.py
```

Frontend reused the existing locked dependency tree through a local symlink;
no package installation or dependency changes. Sequential commands from frontend,
under `env -i PATH=/usr/local/bin:/usr/bin:/bin TMPDIR=/tmp`:

```sh
node node_modules/vitest/vitest.mjs run --maxWorkers=1 --minWorkers=1 --no-cache --reporter=json --outputFile=/tmp/satorna-root-milestone-frontend-20260909.json
node node_modules/typescript/bin/tsc -p tsconfig.app.json --tsBuildInfoFile /tmp/satorna-root-milestone-app.tsbuildinfo
node node_modules/typescript/bin/tsc -p tsconfig.node.json --tsBuildInfoFile /tmp/satorna-root-milestone-node.tsbuildinfo
node node_modules/vite/bin/vite.js build --configLoader runner
```

Final acceptance still requires all remaining active T1–T4 requirements, including
newer service work, full backend/debt review, and an explicit missing-input list.
