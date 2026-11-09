# T1 — retained legacy WB resolver lock order

Date: 2026-09-09. BASE: `1aaf19961736b91a41cab047a16e630214c1ae1b`.
Worktree: `/Users/bratishka/Downloads/satornawb-main/.worktrees/arch-t1-platform`.
Scope: confirmed local legacy locking resolver defect; independent controller review remains required before maintenance design relies on this result.

## Change and contract

Only `resolve_bound_wb_credential` changed in production (16 additions / 5 deletions). The nonlocking account lookup supplies a frozen account ID, raw reference and external identity. For locking calls, the existing `_binding_candidate(lock=True)` freezes the actual token owner before acquiring user SHARE, revalidates user organization/active state after waiting, refreshes the exact connected WB account under UPDATE, and acquires token-only UPDATE with the frozen owner and organization predicates.

The resolver checks the locked account reference/external identity and repeats singleton selection against the original locator. The existing exact caller account/reference/token checks and normalization remain. No latest-organization-token selection, public parameter, selected-account expansion, retry, commit, rollback or consumer switch was added. The shared primitive itself is unchanged.

Actual funnel and advertising raw-backfill call sites were inspected and left unchanged. No cabinet-store, schema, migrations, grants, helpers, dependencies, provider, production, Redis, key, flag, push or deploy changes.

## Reproducible safety envelope and exact commands

Every project Python invocation used the worktree's `backend/.venv/bin/python`, from the worktree's `backend` directory. The following is the exact common argument prefix (shown over lines for readability; the actual executions used the same arguments on one line):

```sh
/usr/bin/env -i \
  PATH=/usr/local/bin:/usr/bin:/bin \
  PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 ORDERS_TEST_USE_LOCAL_CLUSTER=1 \
  VELLA_DATABASE_URL=postgresql+psycopg://synthetic@127.0.0.1:1/unreachable \
  VELLA_REDIS_URL=redis://127.0.0.1:1/0 \
  VELLA_CELERY_BROKER_URL=redis://127.0.0.1:1/0 \
  VELLA_CELERY_RESULT_BACKEND=redis://127.0.0.1:1/0 \
  VELLA_WB_API_MODE=fake VELLA_AVITO_API_MODE=fake \
  /usr/bin/sandbox-exec -f \
  ../.superpowers/sdd/2026-09-09-wb-legacy-resolver-lock-order/task-1-sandbox.sb
```

The full plan-owned sandbox was read before execution. It denies network access except the exact `/private/tmp/.s.PGSQL.5432` Unix socket, and denies the specified known secret paths. The inherited allocator created fresh random `orders_test_*` databases and `fetch_authority_*` roles, migrated only each disposable database to 0061, and verified exact owned cleanup. No application connection settings or credentials were read. Synthetic provider methods remain fixture stubs; no bind CLI or real seller-info call occurred.

Append each following suffix to the exact prefix above:

| Stage | Exact suffix | Outcome |
| --- | --- | --- |
| Original RED, before production edit | `.venv/bin/python -m pytest -q -s --tb=short tests/test_wb_legacy_resolver_lock_order.py` | 1 failed in 3.82s, natural exit 1; desired both-success assertion failed with publisher SQLSTATE 40P01, resolver success |
| Minimal GREEN | Same suffix | 1 passed in 3.13s, natural exit 0 |
| Expanded race GREEN | Same suffix | 26 passed in 3.65s, natural exit 0 |
| First full scoped gate | `.venv/bin/python -m pytest -q -s --tb=short tests/test_wb_legacy_resolver_lock_order.py tests/test_marketplace_credential_fetch_postgres.py` | 69 passed in 8.35s, natural exit 0 |
| Final full scoped gate, after test lint/SQL assertions | Same full scoped suffix | 69 passed in 8.51s, natural exit 0 |
| Separate original compatibility boundary | `.venv/bin/python -m pytest -q --tb=short tests/test_wb_credential_binding.py` | 5 passed in 0.61s, natural exit 0 |
| Compile | `.venv/bin/python -m compileall -q app/platform/integrations/wb_credentials.py tests/test_wb_legacy_resolver_lock_order.py` | Exit 0 |
| Scoped lint, final | `/tmp/satorna-backend-verify-20260908/bin/python -m ruff check app/platform/integrations/wb_credentials.py tests/test_wb_legacy_resolver_lock_order.py` | Exit 1, exactly one inherited BLE001 at production line 40 |

The alternate Python was explicitly approved for lint only and ran no project code. Initial lint also found new-test I001/C408; both were corrected. BASE lint was established by the exact command below from backend (exit 1, identical single BLE001 at line 40):

```sh
git show 1aaf19961736b91a41cab047a16e630214c1ae1b:backend/app/platform/integrations/wb_credentials.py | /usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-wb-legacy-resolver-lock-order/task-1-sandbox.sb /tmp/satorna-backend-verify-20260908/bin/python -m ruff check --stdin-filename app/platform/integrations/wb_credentials.py -
```

`git diff --check` from the worktree root exited 0. No skip, xfail, baseline expansion, timeout-as-success or process kill was used. PostgreSQL emits expected warnings that `/dev/null` is not a plain password file; tests still finish naturally.

## Actual race and locking evidence

The new fixture establishes runtime NOSUPERUSER/NOBYPASSRLS, nonownership of marketplace_accounts and Unix origin, then sets the exact synthetic legacy reference. Every case first passes actual nonlocking resolution with the expected account/reference and token equality assertion.

The regression has exactly two business sessions and a read-only observer. Publisher A holds user SHARE. Actual resolver B pauses only after its account UPDATE query has executed. A requests the same account lock. The observer proves A is blocked by the exact B backend using `pg_blocking_pids`, then releases B. BASE produces 40P01; fixed code returns successful publisher and resolver transactions with the exact account/reference/token result. All release events and transaction rollbacks execute in finally before executor shutdown.

Captured emitted locking SQL proves precisely three locking statements in order:

1. Projection from `lk_users` ending `FOR SHARE`.
2. Query from `marketplace_accounts` containing the exact `marketplace_account_id =` predicate and `FOR UPDATE`.
3. Token JOIN query ending `FOR UPDATE OF lk_user_wb_tokens`.

No user password/refresh-token hash columns are selected by these locking statements. Ordinary nonlocking resolution emits no row locks.

## Coverage and observed freshness

60 new tests comprise:

- One actual deadlock-to-success regression.
- Thirteen mutation-wins waits: token rotation, deletion, binding revocation, changed reference, disconnected account, inactive user, moved user organization, reassigned token owner, moved token organization, changed external identity, changed provider, moved account organization, second connected WB account.
- Twelve resolver-wins retained-lock proofs for those existing user/account/token rows, proving the exact writer backend is blocked until caller rollback, unchanged committed rows before release, then exact expected rows afterward.
- Thirty-two denial cases across locking/nonlocking calls: missing/ambiguous account, wrong explicit account/reference/token/org, missing token, inactive user, zero/leading-zero/Unicode token ID, missing reference, wrong prefix, blank token, padded caller token/reference.
- Two success/normalization/transaction tests proving stored surrounding whitespace still normalizes, caller inputs remain exact, no resolver DML/audit changes occur, no caller commit/rollback occurs, preexisting caller work stays in its transaction and remains rollbackable.

Freshness tests retain a stale account ORM instance before waits, exercising the shared primitive's refresh. Cross-organization adversarial metadata mutations use only the disposable DB owner; the resolver always uses the verified unprivileged runtime factory. Snapshots compare actual account/user/token metadata, account count, audit count, and secret equality flags without printing token values. Expected errors are the typed `WbCredentialBindingError` and its safe code, not matched driver exception strings. Failed calls leave the transaction open for caller rollback.

## Exact cleanup evidence

Every allocation below reported verified absence of its exact database and role after the run:

| Run | Database | Role |
| --- | --- | --- |
| RED | orders_test_f1b6816c388c4ba088f61ce26cb101db | fetch_authority_a2407b33a82341a38b6618661718d30d |
| Minimal GREEN | orders_test_b8207b15c804477fa76e565bba7ee590 | fetch_authority_62c2a7d0fc224d29aa71c9353c9c785c |
| Expanded races | orders_test_6220f9ff3188470a9d28874c6c2f1292 | fetch_authority_d1a677057b784ef18e7d8247bf2356cf |
| First scoped, legacy | orders_test_068a8356fb9b4d86a7c4050cc2a48570 | fetch_authority_2dad83e2b63d4e02844e6b1aa48421ad |
| First scoped, inherited | orders_test_ee4a3e0af8a043778a5fc1ff8b5075c8 | fetch_authority_abf37c1a60b14adcaa1fa3ea9e77cd78 |
| Final scoped, legacy | orders_test_9d524de10e8d49ee859f9766d1534c31 | fetch_authority_0d67614d2893452cb0d6f3534a458813 |
| Final scoped, inherited | orders_test_0d2d8a0b3ed5497c92d2cdb569c45cbb | fetch_authority_6ec3ac4f92b14d1b93a4755771fe530f |

## Isolated critic pass and limits

Executing-plans, TDD and verification-before-completion instructions were read and applied. The requested `/Users/bratishka/.codex/skills/preflight-critic/SKILL.md` was absent; a local search found no replacement. The controller accepted an isolated self-critic rather than an invented skill procedure. No subagents were used.

The self-critic re-read the production diff, full new tests, original requirements, actual callers and verification outputs. Checks covered frozen token ownership through waits, refreshed account/source identity, token-only lock targeting, retained caller transaction ownership, singleton semantics, secret-safe failure evidence, deterministic exact-backend waits and finally cleanup before executor shutdown. No unresolved blocker or important issue was found within this repair.

Limits: this is the explicitly scoped PostgreSQL16/synthetic fixture proof and five original compatibility tests, not full backend, raw-backfill end-to-end, provider or production acceptance. The existing optional caller token remains the compatibility boundary; no new historical identity parameter was invented. Singleton is checked again after locks; this does not claim a serializable organization-wide phantom-insert lock. The inherited lint finding remains unchanged. Independent controller review is still required.

Only the approved production file, new test file and this tracked report belong to the repair commit. Controller changes to the two queued rekey documents and the new review-run-binding plan must remain unstaged and preserved. The ignored task report records the full resulting commit SHA after the one scoped commit.
