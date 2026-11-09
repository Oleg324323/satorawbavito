# T2 — price quota admission before dispatch marker

2026-09-10; branch `codex/t2-account-discovery`, following `efbf0a2027899fa2a6fd8f5bb56b4900deb77855`.
ROOT explicitly assigned this bounded correction after the Avito statistics slice.

## Root cause and change

Previously the actual `WbPriceHttpAdapter` requested pacing inside `post_price_once`,
after `DurableApprovalWorker` had committed its dispatch marker. A quota refusal
could therefore become a fabricated transport-uncertainty/ambiguous outcome even
though no provider request was possible. No formula or approval kernel change.

Only existing T2 transport/worker files, two new focused test files and this
report change. T1 executor, quota implementation, schema, migrations, config,
shared wiring, task registration and current legacy repricer are untouched.

```text
readback → claim/reserve if needed → resolve_fetch physical root closes
    → validate body / canonical reserved attempt / credential / adapter scope
    → durable admission callback exactly once
        denial/error: raise safe error; attempt remains reserved; no marker/POST/outcome
        success: opaque process-local prepared handle
    → existing _dispatch physical commit
        unknown commit: READBACK_REQUIRED; no POST; acquired capacity may be lost
    → existing before_provider_io live authorization + marker consumption
        denial: existing conservative ambiguous behavior; no HTTP
    → consume prepared handle once → one HTTP POST → original receipt publication
```

The opaque handle is **not** authorization, a DB transaction, a queue payload,
a persisted send permit or an exactly-once external-effect guarantee.

## Exact adapter contract

```python
prepared = adapter.prepare_price_once(
    body=request.provider_bytes,
    credential=resolved_credential,
    locator=job_locator,
    expected=original_reserved_expected_state,
)
# Worker commits dispatch and runs executor.before_provider_io here.
response = adapter.post_prepared_once(
    prepared=prepared,
    body=request.provider_bytes,
    credential=resolved_credential,
    locator=job_locator,
    expected=original_reserved_expected_state,
)
```

`expected` remains the original **reserved** state (`attempt_version=0`), not the
new dispatched version. It binds full approval identity/action key/checksum,
canonical bytes, attempt ID, dispatch key, claim version and optimistic versions.
Preparation verifies reconstructed canonical provider bytes and calculated dispatch
key against the stored contract before acquiring quota. Unsupported size/minPrice,
invalid money/body, wrong account or wrong attempt state cannot consume capacity.

Consumption checks exact body, same redacted credential wrapper and captured
binding, locator, full expected state, adapter identity and per-adapter seal.
The handle inherits existing redacted/nonserializable behavior; copy/deepcopy fail.
A per-handle lock burns it before HTTP; replay or even concurrent consumption
cannot produce a second POST. An adapter clone cannot reuse the handle despite
copying its seal. Failed consumption never reopens a handle.

Worker recognizes the paired prepare/post-prepared protocol. Incomplete hooks are
rejected at construction. Existing post-only synthetic transports retain the old
single-POST protocol. Direct actual `post_price_once` remains compatible and still
validates, admits once and consumes an internally prepared handle. No `skip_quota`
boolean or second admission was added. History GET retains admission → request;
header cooldown persistence, receipt bytes, no-retry behavior and safe errors remain.

## Verification

All tests are synthetic/offline. Python `/tmp/satorna-backend311-20260909/bin/python`,
working directory this worktree's `backend`; minimal `env -i` with existing
network-deny `arch-t1-platform/.superpowers/sdd/2026-09-09-publication-guard/task-1-offline.sb`.
PG additionally uses `ORDERS_TEST_USE_LOCAL_CLUSTER=1`, Unix socket only,
`PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null`.

| Gate | Result |
|---|---|
| New prepared transport API RED | 13 failed, missing `prepare_price_once` |
| Real worker + HTTP adapter sequencing RED | 7 failed / 13 passed: old marker-before-quota and false ambiguous behavior reproduced |
| Internal critic clone-binding RED | 1 failed: a copied adapter with the same seal made a POST; fixed by binding actual adapter identity too |
| `pytest -q --tb=short tests/test_wb_repricing_prepared_post.py tests/test_wb_repricing_http.py` | 47 passed, 0.57s, exit 0 (21 new offline + 26 existing HTTP) |
| `pytest -q --tb=short tests/test_wb_repricing_prepared_post_postgres.py tests/test_wb_repricing_worker_postgres.py` | 21 passed, 11.24s, exit 0 (6 new + 15 existing) |
| Ruff on four changed/new Python files | exit 0 |
| `compileall -q` on the same four files, redirected `/tmp` pycache | exit 0 |
| `git diff --check` | exit 0 |

The new offline worker tests substitute only the executor persistence boundary,
not the worker/HTTP adapter. They prove call order, not SQL authority. The PG gate
uses the actual existing dedicated executor/API-role fixture and actual adapter
with MockTransport. Quota callback is synthetic, explicitly **not** T1's SQL quota
implementation. New cases verify reserved/no marker on denial or callback error,
one admission/POST then duplicate delivery, uncertain committed marker, final
authorization failure and uncertain receipt publication without resend. Existing
15 retain real two-session claim race, restart, rollback and crash/recovery coverage.

ROOT granted exclusive `PreparedPrice21` after T1 WBACL6. Two sequential module-scope
fixture allocations migrated only owned disposable databases. All engines disposed,
exact created databases/API+worker roles removed and absence assertions passed.
Pytest process exited naturally; slot explicitly released. No PG resources retained.

## Remaining gates / nonclaims

- Actual T1 durable quota owner + dedicated price executor + this actual worker
  composed in one test remains a separate gate; these 21 do not claim it.
- Quota API has no attempt identity. Crash/re-delivery before dispatch can spend
  capacity again, and a crash after admission can lose capacity. No durable
  per-attempt exactly-once quota semantics, refund, resend, dispatch reset or new
  retry policy is introduced. The handle has no invented expiry/renewal policy;
  scheduling/long delays between admission and actual send remain integration concerns.
- One-use process-local consumption is not provider-side idempotency. An uncertain
  provider acceptance still requires reconciliation; no automatic resend.
- Bootstrap activation, durable admission callbacks and real-provider parity remain
  owner responsibilities. No real tokens, price requests, external calls, production,
  deploy, push, scheduler changes or real-price flag changes occurred.
- Independent critic pass checked scope, original expected-state binding, hidden
  second admission, mutable adapter cloning and preserved failure branches. The
  local preflight-critic skill file is absent; no external review is claimed here.

Rollback keeps the actual adapter unregistered/default off. Do not re-enable the
old marker-before-quota composition; disabling only the prepared protocol on an
active real worker would reintroduce the original bug.
