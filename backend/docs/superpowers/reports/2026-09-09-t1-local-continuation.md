# T1 local continuation — verified slices and remaining gates

Date: 2026-09-09. Branch `codex/arch-t1-platform`.
Repository `/Users/bratishka/Downloads/satornawb-git`; isolated worktree
`/Users/bratishka/Downloads/satornawb-main/.worktrees/arch-t1-platform`.
Original base `c88a474695569af905b294906ba604d61f3de62f`.
The token/wrapper/scheduler/heartbeat continuation starts after
`edd9d4b927252148b7e25d6933b9834a307d9493`; earlier Stage 1 history is retained
in the commit table for lineage, not relabeled as newly executed here.
This is not a declaration that Stages 2–6 or the architecture are complete.

## Completed bounded changes

- Account-owned ingestion-token lifecycle boundary reuses 0061. Exact canonical
  Avito owner, self-locating bounded bearer, one-way verifier, one-time reveal,
  safe uniform errors, expiry/revoke/rotation, fixed row-lock order and post-lock
  freshness checks. Issue/verify require connected account; revoke/status remain
  usable after disconnect. No HTTP ingestion consumer was switched.
- DecryptedCredential now refuses generic serialization/copy/pickle/schema paths,
  has no instance dictionary, and reveals only through its explicit method.
  Framework-wrapped refusal types are tested, not surfaced as public error text.
- The legacy onboarding test now fences the actual runtime entrypoint and uses
  a synchronous test-only Thread double. The sentinel subprocess counts setup
  and teardown errors. This closes one demonstrated escaping background task;
  it does not establish that every full-suite shutdown cause is fixed.
- Python repricer scheduler defaults now match default-off deployment intent.
  Explicit true/false overrides work. Only positive scheduling-test setup changed;
  no task implementation or deployed flag was changed.
- Four missing runtime packages are included in wheel metadata. Installed-origin
  checks cover the new packages, security, integrations, Reviews namespace and
  the backend_contracts alias. Packaging verification does not imply complete CI.
- Default-off heartbeat is implemented through exact worker lifecycle/Heart/PID
  fencing and completed PersistentScheduler ticks. API performs one bounded
  expected-slot MGET and reports redacted freshness independently of readiness.
  Enabled operation requires explicit policy; no production TTL/identity/timeout
  was selected. No external scheduler is silently replaced. Real Redis TTL,
  deployed lifecycle, all worker modes, clock synchronization, hard DNS deadlines
  and exclusive leadership remain unverified operational acceptance conditions.
- Final review fixes declare wheel build tools in the test extra (not runtime),
  establish disposable-cluster cleanup before startup, reject unknown credential
  JSON keys without reflecting their names, and correct API-vs-audit metadata
  wording. The global error handler and existing consumers are unchanged.

## Evidence already recorded

All tests use this worktree's own backend/.venv. Synthetic non-DB checks use
env -i and macOS sandbox-exec with `(version 1) (allow default) (deny network*)`.
That profile denies network entirely; no claim of a working loopback exception.

| Check | Observed result |
| --- | --- |
| Token initial RED | Module/API absence reproduced before implementation |
| Token stale-lock/disconnected/integer regressions | 2/5/2 expected failures before respective fixes |
| Token/store/RLS adjacent at b430c35 | 53 passed in 5.67s, exit 0 |
| Token focused before ff855ec commit | 35 passed in 3.80s, exit 0 |
| Final token DB gate | BLOCKED by fresh PostgreSQL bootstrap ENOMEM, not green |
| Pure lock cleanup guard after commit | 1 passed, 34 deselected, exit 0 |
| Wrapper initial RED | 12 failed, 29 passed before hardening |
| Wrapper exact-error mutation | 14 tests failed with wrong RuntimeError, restored |
| Wrapper crypto/store/API | 69 passed, 6 existing warnings in 2.50s, exit 0 |
| Background isolation mutation | Sentinel reached when entrypoint fence removed; expected failure |
| Background isolation after fdd26ec | 1 passed in 1.82s, exit 0, natural exit |
| Separate legacy onboarding case | 1 failed, 2 warnings in 1.22s, exit 1, natural exit |
| Scheduler RED | 3 failed, 3 passed, exit 1 |
| Scheduler focused after daace4c | 8 passed, 2 existing warnings in 1.02s, exit 0 |
| Actual wheel RED | Four missing package entries, exit 1 |
| Stale-build RED | Temporary package removal masked by stale build/lib; 1 expected failure |
| Clean wheel build/install/origins after 7331494 | 2 passed in 2.72s, exit 0 |
| Generated-model compile + backend_contracts/tests | 15 passed, 2 existing warnings in 0.27s, exit 0 |
| Heartbeat behavioral RED | 13 failed/1 passed; later missing hooks/health 34 failed/99 passed |
| Heartbeat/health/scheduler after 696d4be | 163 passed, 2 existing warnings in 1.28s, exit 0 |
| Combined safe T1 subset at 696d4be | 260 passed, 6 existing warnings in 7.21s, exit 0; natural exit |
| Final review regression RED | 6 expected assertion failures, 2 warnings in 2.70s, exit 1 |
| Final fix focused GREEN | 77 passed, 6 existing warnings in 5.73s, exit 0 |
| Fresh combined safe subset at 50d0d54 | 265 passed, 6 existing warnings in 7.62s, exit 0; natural exit |
| Fresh compileall app/tests/alembic/ops release gate at 50d0d54 | exit 0 |
| Alembic heads (no DB connection) | 20260908_0061 only, exit 0 |
| Synthetic API secret markers in app/docs | no matches, rg exit 1 as expected |

Wheel builds now stage only pyproject.toml and known Python source roots into a
fresh temporary tree, excluding environments/build/dist/caches/egg-info and non-Python
snapshots/.env. Existing ignored build artifacts were not deleted. Build tools were
setuptools 84.0.0 and wheel 0.48.0; runtime dependency constraints are unchanged.

## Bounded commit sequence after original base

| Full SHA | Change |
| --- | --- |
| 2a2b765457f603d62235231dfb6dec63235cd307 | Account-owned credential API, inherited Stage 1 |
| edd9d4b927252148b7e25d6933b9834a307d9493 | Release subprocess environment isolation |
| 9f299058d86904f75769a0f9e971563255859057 | Ingestion-token lifecycle |
| b430c35bcb85af71bc67154862511e67df05e5e7 | Post-lock expiry, revoke and integer invariants |
| ff855ecac689548c2757051f33f3d2e7919a0680 | Lock-safe concurrency-test cleanup |
| d38e923d608d8de5f25855c86b04d55bc3ea60cc | Committed domain schema feedback |
| 2e29b820723567c5eed1733bca1a90f9dfd59a7c | Secret wrapper serialization refusal |
| 39fc59fad867de8c4ca86fe593e08408fe3dc9e0 | Exact serializer and schema contract tests |
| 5ba31a0725bee6610997fb09efd3d7ebb5f4b2d1 | Actual onboarding test entrypoint fence |
| fdd26ecf49aaa0687c734dcf7343744d2fcceea9 | Deterministic test threading and error accounting |
| daace4cdc7e202023cb06f44128fc111dd826fd8 | Scheduler default-off |
| d861eb2b201689db3abf0c6f532948944d16bdf6 | Runtime wheel package completeness |
| 73314946f71961229dd68e6722fc41f7a65cf85e | Clean wheel source staging regression |
| ba08f02e448514d2200bcc6720406cb8543ea8da | Default-off heartbeat design/plan |
| 696d4beece05fba9d28783d07d23852a7e6ca2e7 | Owner-published heartbeat and separate freshness |
| 50d0d542d23d08a87b82e29599a2f6962c59833e | Consolidated final review fixes |

## Changed files against original base through 50d0d54

- `backend/app/cabinet/schemas.py`
- `backend/app/config.py`
- `backend/app/infra/celery_app.py`
- `backend/app/infra/health.py`
- `backend/app/infra/heartbeat.py`
- `backend/app/main.py`
- `backend/app/platform/integrations/access.py`
- `backend/app/platform/integrations/credential_store.py`
- `backend/app/platform/integrations/ingestion_tokens.py`
- `backend/app/routers/cabinet.py`
- `backend/app/security/marketplace_credentials.py`
- `backend/docs/superpowers/plans/2026-09-09-process-heartbeat.md`
- `backend/docs/superpowers/reports/2026-09-08-arch-t1-continuation-gates.md`
- `backend/docs/superpowers/reports/2026-09-08-arch-t1-stage-1-credential-api-handoff.md`
- `backend/docs/superpowers/reports/2026-09-08-t1-schema-contract-feedback.md`
- `backend/docs/superpowers/specs/2026-09-09-process-heartbeat-design.md`
- `backend/ops/release_gate.py`
- `backend/pyproject.toml`
- `backend/tests/test_avito_ingestion_token_store.py`
- `backend/tests/test_background_test_isolation.py`
- `backend/tests/test_infra_baseline.py`
- `backend/tests/test_installed_wheel.py`
- `backend/tests/test_marketplace_credential_api.py`
- `backend/tests/test_marketplace_credential_crypto.py`
- `backend/tests/test_marketplace_credential_store.py`
- `backend/tests/test_process_heartbeat.py`
- `backend/tests/test_release_gate.py`
- `backend/tests/test_review_tasks.py`
- `backend/tests/test_scheduler_default_off.py`
- `backend/tests/test_wb_repricer_bff.py`

The separate documentation handoff commit adds this report and its two companion
reports named below. Its SHA is obtained from Git history; no self-referential
commit SHA is embedded here. No Alembic, grants, frontend or serverless file
changed in this branch range.

## Fresh combined verification command

Run from backend at `50d0d542d23d08a87b82e29599a2f6962c59833e`:

```text
env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin LC_ALL=C PIP_CONFIG_FILE=/dev/null NETRC=/dev/null /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-process-heartbeat/offline-local.sb .venv/bin/python -m pytest -q tests/test_marketplace_credential_crypto.py tests/test_marketplace_credential_store.py tests/test_marketplace_credential_api.py tests/test_release_gate.py tests/test_background_test_isolation.py tests/test_installed_wheel.py tests/test_process_heartbeat.py tests/test_health.py tests/test_scheduler_default_off.py tests/test_infra_baseline.py::test_celery_app_is_configured_from_settings tests/test_review_tasks.py::test_celery_beat_includes_reviews_sync_feedbacks tests/test_avito_ingestion_token_store.py::test_lock_guard_rolls_back_before_pool_exit_when_sync_assertion_fails tests/test_avito_ingestion_token_store.py::test_disposable_postgres_cleans_up_failed_start_without_progress tests/test_openapi_generated_models.py backend_contracts/tests
```

```text
env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin LC_ALL=C /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-process-heartbeat/offline-local.sb .venv/bin/python -m compileall -q app tests alembic ops/release_gate.py
env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin LC_ALL=C /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-process-heartbeat/offline-local.sb .venv/bin/python -m alembic heads
git diff --check
git diff --name-only c88a474695569af905b294906ba604d61f3de62f HEAD
git show --stat --name-status HEAD
```

The network-deny profile is ignored test scratch, not deployment configuration;
its complete content is stated above. These commands require installed project
test dependencies in the isolated venv. The test extra now declares the existing
build-system tools; TOML and actual wheel Requires-Dist/extra-marker checks prove
the declaration and exclusion from runtime. Fresh online resolution/install and
fully locked reproducible release CI remain unverified. Ruff was absent; no Ruff
pass is claimed. Three startup-cleanup cases in the combined command replace
binary discovery, port allocation and subprocess calls: no real PG starts there.

The wrapper/API/store tests using SQLite are not RLS proof. PostgreSQL RLS tests
are separate actual-server tests. The inherited full baseline remains historical:
Stage 1 recorded 894 passed / 105 failed / 0 errors / 0 skipped with the same
failure IDs and a post-summary shutdown hang. No full baseline delta is claimed
here and the failure allowlist was not changed. The isolated legacy failure now
reaches the preserved `payload["state"]` assertion; it was not converted to pass,
skip or xfail. Six adjacent warnings comprise two framework deprecations and four
duplicate OpenAPI operation IDs in unchanged contract routes; not suppressed.

## Review and execution process

The independent whole-branch review of c88a474..696d4be found three Important
issues and one Minor documentation issue. A single six-file fix commit 50d0d54
addressed all four. Its independent scoped re-review passed with no new findings;
this is not approval of the unresolved DB/full-release/integration gates.
The requested preflight-critic skill file was unavailable, so an independent
source/evidence critic served as the fallback; no unavailable skill use is claimed.

Using-git-worktrees preserved the existing isolated branch and user changes;
executing-plans and subagent-driven-development kept slices and review boundaries
explicit; test-driven-development supplied the RED→GREEN regressions;
verification-before-completion required the fresh commands above. No additional
implementation was inferred from a green subset or a neighboring branch head.

## PostgreSQL environment and blocker

Successful token/RLS runs used fresh initdb clusters under pytest temporary roots,
loopback-only dynamically allocated ports, synthetic tenants/accounts and an
explicit NOSUPERUSER/NOINHERIT/NOBYPASSRLS runtime role. Token fixture is pinned to
0061, never an unreviewed future head; clusters were stopped by fixture cleanup.

Later fresh fixtures and independent initdb attempts fail before acceptance tests:
`could not create shared memory segment: Cannot allocate memory`,
`shmget(... size=56 ...)`. A reduced-memory mmap/shared_buffers/max_connections
attempt also failed. The exact cause beyond PostgreSQL bootstrap ENOMEM has not
been proven. No sysctl, existing shared-memory segment, working PostgreSQL process
or FoundHub workload was changed. Read-only resource recheck showed no material
improvement, so repeated startup attempts were not used as progress.
The local container alternative was checked read-only: docker/podman/colima were
not found in PATH, and Docker/Podman executables were absent from checked standard
Docker.app/Homebrew/local-bin paths. No host service/tool installation or remote
Docker context was attempted. This is a bounded availability check, not a claim
about every possible container runtime installation on the machine.

Consequently no new migration, empty/production-shaped schema roundtrip, account
RLS-context helper proof, credential backfill/rotation CAS acceptance or restore
rehearsal is claimed. Head remains 20260908_0061; 0062 was not reserved.

## Consumer and policy gates

Source defaults verified after heartbeat: credentials, process heartbeat,
repricer scheduler, real price apply, Avito price apply, WB feedback send,
canonical collection and finance/advertising shadow ingestion remain false.
Legacy WB sync, Avito returns collection and Avito repricer worker defaults remain
true; this is not a statement that every beat job is disabled. No actual environment
or allowlist was read or changed to activate any feature.

New API boundary and resolver handoff remain in the Stage 1 report. No existing
WB/Avito/extension consumer has been switched by these continuation slices.
Legacy plaintext stays untouched; new encryption writers remain encrypted-only.

Extension HTTP cutover additionally needs account-owned durable Orders ingestion:
the existing avito_orders browser-snapshot writer uses org-only cache, AI enrichment
and legacy returns persistence. A scoped token must not feed that sink. Production
TTL, pre-IP/per-token quotas/windows, body limits, reissue UX and mixed-version
queue drain/fence policy still require explicit approval; synthetic examples do not
approve values. Runtime key custody/mount/readers, recovery and retention policies
remain owner decisions. No actual key mount, backfill or cleanup is authorized.

Backfill must use an insert-if-absent/expected-generation operation inside the sole
credential store; current put replaces a generation. Existing re-encryption opens
its own session without tenant context; runtime-role invocation must be proven
before adopting it in maintenance CLI. Do not duplicate crypto SQL in a CLI.
Account-owned OAuth refresh also requires concurrency protection across the client
credential generation and derived access credential; the legacy user-cache resolver
must not be relabeled account-owned or wired to a blind replacing writer. That
transactional acceptance belongs with the restored disposable PostgreSQL gate.

## Decisions taken within local scope

1. Split token lifecycle from HTTP ingestion cutover: the existing encrypted
   schema permits independent lifecycle proof, while the durable sink/policy
   gates remain separate. Cost if wrong: adjust the boundary before activation.
2. Restrict issue/verify to connected accounts, but allow exact-owner status/revoke
   after disconnect so revocation cannot be undone by reconnect. Cost if wrong:
   adjust lifecycle API semantics before activation, without expanding ingestion.
3. Replace the inherited assertion that a decrypted wrapper exposes its instance
   dictionary with serialization-refusal tests. Cost if wrong: private-introspection
   callers must move to explicit reveal; no public secret serialization is retained.
4. Own domain/HTTP errors remain typed safe codes; FastAPI/Pydantic serializer
   refusals have their exact framework wrapper types. Cost if wrong: future HTTP
   callers need explicit translation, never framework str(exc) as the API contract.
5. Heartbeat uses lifecycle-fenced Heart signals and completed persistent ticks,
   with a dedicated client supporting only explicitly selected zero retries.
   Cost if wrong: framework-hook compatibility or total-deadline retry redesign
   before activation. No production TTL/topology/timeout values were selected.

## Coordination and integration status

Companion reports: `2026-09-09-t1-avito-integration-inventory.md` records the
source-scoped Avito inventory; `2026-09-09-t1-schema-amendment-feedback.md` records
the committed T4 amendment assessment. The existing Stage 1 handoff remains the
exact credential API/resolver and shared ownership contract.

Shared ownership remains the Stage 1 manifest: T2 repricer_tasks, T3 Avito order
client/domain, T4 review_tasks/frontend/serverless; T1 shared migrations/config/
registrations/grants/build/integration and avito_orders router during cutover.
Only named scheduling test setup and the demonstrated test isolation case were
changed here, not neighboring domain implementations.

Read-only committed review found T4 8f4a6c4 resolves most prior send lifecycle,
canonical bytes, idempotency and in-app notification questions. Review Facts from
4a7d669 remain separately accepted. Remaining exact send lease/result/evidence/
audit and head-initialization details need a committed owner contract; external
notifications additionally depend on platform binding/receipt/policy contracts.
T2 ee7bd6e still needs exact durable attempt/dispatch repository semantics. T3
f4d6d55 documents pure ingestion and DB acceptance, not installed persistence.

Observed branch heads are not a stopped/ready acceptance set. No integration branch,
merge/cherry-pick or common handoff rewrite occurred. Owner resource remediation,
exact schema contracts and ready commits precede the remaining database/integration
gates; independent local default-off work is separate from production activation.

No GitHub, CodeRabbit, production, working database/Redis or real credentials were
used in this continuation, and no provider calls occurred after the offline
boundary was established. Preserve the historical distinction: the earlier Stage 1
handoff records one unexpected read-only accounts/self request with a synthetic
bearer receiving HTTP 403; no message or price mutation occurred. This report does
not rewrite that incident into a whole-branch no-network claim.
The only network operation in packaging was unauthenticated
public PyPI retrieval of declared build tools into the isolated worktree venv;
subsequent build/install/import tests denied network. No push, deploy, real backfill,
key rotation, flag activation, plaintext cleanup or legacy retirement occurred.
