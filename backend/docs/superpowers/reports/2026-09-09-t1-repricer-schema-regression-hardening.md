# T1 repricer schema regression hardening

2026-09-09. Test-only follow-up from exact baseline
`99b295e1946314f312d1a1c1fc46fd21a213724f` on
`codex/arch-t1-platform`. The accepted revision0066 DDL remains unchanged.

## Result and scope

This patch strengthens evidence for review findings M1 and M2 without changing
any migration, runtime role SQL, ORM/domain implementation, feature flag or
consumer. The only tracked paths are the two affected PostgreSQL test modules,
this report and the existing0066 handoff.

M1 now has a populated0065→0066→0065→0066 proof. M2 now exercises optional
NUMERIC values through otherwise-valid v1 requests and asserts the exact numeric
diagnostic instead of accepting any DBAPI failure. The evidence is ready for the
controller's independent scoped review; that review, not this implementation
report, decides final finding closure.

## Populated historical fixture and comparison

The fixture uses the existing `candidate.disposable_database`, migration runner,
scope and Orders helpers, `tests.test_orders_exact_text_migration.make_run`,
`tests.test_review_facts_schema.seed`, and
`tests.test_review_lossless_migration.unit`. It commits under normal guards:

- organizations91001/91002 and Avito/WB accounts91101/91102/91103/91201;
- one Avito `order_sync_runs` staging row with exact run key
  `repricer-preservation-orders-run`;
- one `marketplace_orders` row, one `marketplace_order_items` row and one
  order-level `order_observations` row with exact source revision/event and
  checksum `1` repeated64;
- one complete Review run/fact/observation/run-item unit whose fact watermark
  points to the exact run sequence and observation;
- the Review text representation is `text IS NULL` plus exact lossless UTF-8
  bytes
  `b"preserved\x00\xd0\xbe\xd1\x82\xd0\xb7\xd1\x8b\xd0\xb2\xf0\x9f\x9a\x80"`.

The fixed snapshot table/key map contains `lk_organizations`,
`marketplace_accounts`, `order_sync_runs`, `marketplace_orders`,
`marketplace_order_items`, `order_observations`, `review_sync_runs_v2`,
`review_facts`, `review_observations` and `review_sync_run_items`. Each relation
must be nonempty. `SELECT *` rows are ordered by the declared primary key and
compared as actual driver-returned values; this covers all columns,
row counts, bytes, IDs and timestamps rather than a checksum of a reconstructed
subset. Preexisting public relation ACLs, column ACLs and default ACLs are
snapshotted separately and compared at each migration step. The three new0066
relations must be empty whenever present and absent at0065.

Observed row counts before the migration cycle were exactly2 organizations,
4 accounts, and1 row in each of the eight Orders/Review history relations.

A transaction-local positive control increments the permitted staging
`order_sync_runs.page_count`, invokes the same row comparison and observes its
`AssertionError`, then rolls back. The unchanged comparison passes before the
migration cycle.

## Exact numeric contract

The lifecycle `create` fixture now accepts exact optional `size_id` and
`min_price_kopecks` values and independently builds the Python canonical JSON
bytes, SHA256 request checksum and action key using only stdlib. Positive pairs
`(1,50)` and `(2**70,2**72)` commit, retain one `approval.created` audit and read
back as exact `Decimal` values.

For both optional fields, each of `1.5`, `NaN`, `Infinity` and `-Infinity` must
produce exactly SQLSTATE `P0001`, primary message `repricer_numeric_invalid` and
no constraint name. This is the explicit diagnostic raised by
`repricer_integer_decimal` through numeric validation. Separate otherwise-valid
v1 range denials bind size0 and minimum49 to SQLSTATE `23514` and respectively
`wb_repricer_price_approvals_size_id_check` and
`wb_repricer_price_approvals_min_price_kopecks_check`. Existing required
`nm_id`, `recommended_price_kopecks` and `version` fraction/nonfinite cases are
tightened to SQLSTATE `23514` plus their exact column CHECK names.

The focused disposable mutation replaces only `repricer_integral_finite` in its
own database with a non-null predicate. The same optional fractional diagnostic
assertion then fails because PostgreSQL reaches the unrelated
`repricer_request_exact` CHECK (`23514`) instead of the expected safe numeric
diagnostic. No accepted migration file is edited; teardown destroys the relaxed
helper with its database.

## RED and GREEN evidence

All PostgreSQL commands ran from `backend/` under
`.superpowers/sdd/2026-09-09-repricer-schema-regression-hardening/task-1-sandbox.sb`
using scrubbed `env -i`, the repository `.venv`,
`ORDERS_TEST_USE_LOCAL_CLUSTER=1`, local Unix PostgreSQL only, fake provider
modes, unreachable TCP application/Redis URLs and all real-send flags false.

The exact common prefix was:

```sh
/usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-repricer-schema-regression-hardening/task-1-sandbox.sb /usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin CI=1 PYTHONUNBUFFERED=1 LC_ALL=C PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null ORDERS_TEST_USE_LOCAL_CLUSTER=1 VELLA_DATABASE_URL=postgresql+psycopg://satorna_gate:satorna_gate@127.0.0.1:1/unreachable VELLA_REDIS_URL=redis://127.0.0.1:1/0 VELLA_CELERY_BROKER_URL=redis://127.0.0.1:1/0 VELLA_CELERY_RESULT_BACKEND=redis://127.0.0.1:1/0 VELLA_WB_API_MODE=fake VELLA_AVITO_API_MODE=fake VELLA_REAL_PRICE_APPLY_ENABLED=false VELLA_WB_FEEDBACKS_SEND_ENABLED=false VELLA_AVITO_REPRICER_PRICE_APPLY_ENABLED=false
```

For RED, focused GREEN and the prescribed group, the shown pytest argv followed
this prefix directly.

Initial RED command:

```sh
.venv/bin/python -m pytest -q -s --tb=short \
  tests/test_repricer_approvals_schema.py::test_populated_old_rows_and_acls_survive_0066_roundtrip \
  tests/test_repricer_approvals_lifecycle.py::test_optional_numeric_diagnostic_detects_relaxed_helper
```

Natural exit1: exactly two expected failures. The old-row comparison showed only
`order_sync_runs.page_count` changing0→1. The numeric comparison expected
`P0001/repricer_numeric_invalid/NULL` but observed
`23514/repricer_request_exact`, proving both new assertions can fail. Both
disposable databases emitted cleanup/absence success.

Focused GREEN command selected `populated_old_rows or optional_numeric or
required_numeric` across the two changed tests. Natural exit0:

```text
26 passed, 142 deselected in 12.98s
```

Prescribed GREEN command:

```sh
.venv/bin/python -m pytest -q -s --tb=short \
  tests/test_repricer_approvals_schema.py \
  tests/test_repricer_approvals_lifecycle.py \
  tests/test_repricer_approvals_rls.py
```

Natural exit0:

```text
199 passed in 124.46s (0:02:04)
```

No skips, xfails, retries, baseline suppression or timeout-as-success occurred.
All eight printed random database/role cleanup checks passed. The exact disposable
names are intentionally ephemeral and contain no credentials.

Static checks:

```sh
.venv/bin/python -m compileall -q \
  tests/test_repricer_approvals_schema.py \
  tests/test_repricer_approvals_lifecycle.py
/tmp/satorna-backend-verify-20260908/bin/python -m ruff check \
  tests/test_repricer_approvals_schema.py \
  tests/test_repricer_approvals_lifecycle.py
git diff --check
```

Compileall and diff check exited0 with no output. The repository `.venv` does not
contain Ruff; the previously approved lint-only runtime reported
`All checks passed!`, exit0. The first attempted compile command contained a
misspelled generated path and was discarded; the exact command above was rerun
successfully.

## Baseline, cleanup and limitations

- Baseline/branch were verified before edits: exact HEAD
  `99b295e1946314f312d1a1c1fc46fd21a213724f`, branch
  `codex/arch-t1-platform`.
- The controller-owned untracked Review binding design and legacy-resolver
  lock-order plan were present during work and were neither read for this
  implementation nor edited/staged.
- No migration/runtime/ORM/domain, old revision, schema number, flag, provider,
  network service, real database/role, credential, `.env`, GitHub, push, deploy
  or dependency installation was touched.
- This is representative synthetic Orders/Review history, not universal
  production-shaped parity and not a full-backend green claim.
- Inherited libpq warnings that `/dev/null` is not a plain passfile remain visible
  as separately tracked M3. They were not silenced and no real passfile was read.
- The named global `preflight-critic` skill file was unavailable. An isolated
  manual critic pass was used, and independent review remains controller-owned.

## Critic pass

Isolated manual Critic Pass: PASS,0 Blocker and0 Important. It rechecked the
brief against the actual diff and fresh DB/static evidence, confirmed the
expected values are independent literals/stdlib serialization, both mutation
controls exercise real PostgreSQL and roll back or destroy their disposable DB,
and no skip/xfail/broad-error mask or DDL/runtime change was introduced. The
named local `preflight-critic` skill remained unavailable; independent review is
still the controller's separate gate.
