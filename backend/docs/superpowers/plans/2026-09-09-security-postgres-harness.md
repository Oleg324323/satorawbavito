# Security PostgreSQL harness implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans, TDD and verification-before-completion.

**Goal:** Run existing credential-RLS and ingestion-token gates on proven own Unix-disposable DBs without removing native mode.
**Architecture:** Reuse existing allocator, factor each fixture's bootstrap once, route explicitly by existing test flag. Preserve historical0061 cycles and add independent empty bootstrap.
**Tech Stack:** Existing pytest/SQLAlchemy/Alembic/psycopg, no runtime changes/dependencies.
**Spec:** backend/docs/superpowers/specs/2026-09-09-security-postgres-harness-design.md (read fully).

## Global constraints

- Only T1 worktree `/Users/bratishka/Downloads/satornawb-main/.worktrees/arch-t1-platform`.
- Test/docs only. No runtime/model/schema/grants/helper/dependency changes, otherworktree
  edits, production/realcredentials/.env/provider/IP/workingRedis/hostflags or push/deploy.
- Own backend/.venv; scrubbed env-i, secret/IP-denying sandbox with only exact UnixPG
  allowed. Lint-only approved alternate Ruff interpreter; no installs.
- Only fresh random owned DB/roles from existing tracked candidate allocator via
  authorized maintenance postgres. No existing app data/role/server changes.
- No skips/xfails/new baseline, no SQLite/mocks as RLS proof; native limitation remains
  explicitly separate from local-mode GREEN. Exact cleanup/absence on success/failure.

## Task 1: Route both historical fixtures through isolated local allocation

**Files:**
- Modify backend/tests/test_marketplace_credential_rls.py only fixture/bootstrap and origin/role assertions.
- Modify backend/tests/test_avito_ingestion_token_store.py only fixture/bootstrap, origin assertion and existing startup unit test target.
- Create backend/tests/test_security_postgres_harness.py.
- Create backend/docs/superpowers/reports/2026-09-09-t1-security-postgres-harness.md.

**Consumes:** both full test files, `tests.test_orders_schema_candidate.cluster`,
`disposable_database(cluster, roles=())` and `migrate(url, action, revision)`;
actual fixtures retain0060-shaped synthetic DDL + stamp/cycle0061.
**Produces:** same owner/runtime data used by existing tests, plus explicit provenance
fields, without changing crypto/token/RLS expectations.

- [ ] Step1 read both files fully and helper definitions. Write tests for new private
`_native_postgres(tmp_path_factory)` and `_local_postgres(cluster)` routing in each
module; wrapper fixture remains `disposable_postgres`. Import candidate `cluster`
fixture via module alias, fetch it lazily only for explicit local mode:

```python
@pytest.fixture(scope="module")
def disposable_postgres(tmp_path_factory, request):
    if os.environ.get("ORDERS_TEST_USE_LOCAL_CLUSTER") == "1":
        yield from _local_postgres(request.getfixturevalue("cluster"))
    else:
        yield from _native_postgres(tmp_path_factory)
```

No cluster argument that eagerly starts an unused second native cluster. Unit
tests monkeypatch the two private generators with context-recording sentinels,
exercise flag absent/0/true/1 and generator close/error cleanup, assert one route
and exact finally. RED before helpers exist, expected missing interface. No actual
native cluster attempt is necessary to reproduce absent local routing again.
- [ ] Step2 factor original generator under `_native_postgres`, preserve native
init/start/stop and original ingestion startup-failure test (call renamed generator,
not weaken expectations). Factor each original schema+cycle+seed/grant setup once
into a private bootstrap function consumed by both paths; avoid duplicated SQL.
Replace fixed runtime names with each module's prefix+uuid4().hex, bound as exact
identifier, length<=63. Native alone creates its role; local allocator creates it.

```python
with candidate.disposable_database(cluster, (RUNTIME_ROLE,)) as database:
    owner_engine = create_engine(database.url, hide_parameters=True)
    runtime_url = owner_engine.url.set(username=RUNTIME_ROLE)
    try:
        # bootstrap uses already-created role; original synthetic DDL/cycle/grants
        yield {"mode": "local", "owner": database.url,
               "runtime": runtime_url.render_as_string(hide_password=False),
               "database": database.name, "runtime_role": RUNTIME_ROLE,
               "host": database.host, "port": str(database.port), "root": None}
    finally:
        owner_engine.dispose()
```

Only synthetic passwordless URLs allocated here; no ambient URL inspection/logging.
Dispose any additional allocated runtime engine before yielding allocator cleanup.
Preserve native fields and set mode=native; startup failures must still retain the
original exception and stop only exact owned cluster.
- [ ] Step3 actual origin tests branch by explicit mode. Local uses parsed URL plus
actual SELECT inet_server_addr() IS NULL,current_database(),current_user; names
equal owned allocator output and random grammar. Native retains root tempdir, exact
loopback/random port!=5432 check. Actual role nonowner/no superuser/no bypass remains.
Check historical alembic version exactly20260908_0061, FORCERLS both relevant tables.
No localmode assertion that5432belongs to a newly launched server.
- [ ] Step4 add fresh empty bootstrap test using separate candidate allocation,
no stamp and `migrate(url,"upgrade","20260908_0061")`, check exactversion and both
FORCERLS tables. Inject bootstrap failure after allocation through local generator,
confirm engine disposal and tracked finally/absence output; metadata test doubles
only for routing, actual allocated resource cleanup tested with real PostgreSQL.
Do not inject failure into another thread's DB or modify helper global implementation.
- [ ] Step5 GREEN routing/startup unit subset, then full actual group once:

```sh
.venv/bin/python -m pytest -q -s --tb=short tests/test_security_postgres_harness.py tests/test_marketplace_credential_rls.py tests/test_avito_ingestion_token_store.py
.venv/bin/python -m compileall -q tests/test_security_postgres_harness.py tests/test_marketplace_credential_rls.py tests/test_avito_ingestion_token_store.py
git diff --check
```

All runtime commands prefixed with env-i, explicit local mode1, synthetic fakeprovider
defaults, PGPASSFILE/PGSERVICEFILE/NETRC=/dev/null and this plan's known-secret/IP deny
sandbox. Preserve every original functional assertion; no waiver if a previously
unexecuted test now fails. Report exact failures separately before any scoped fix.
Ruff newfile+changedfiles; report existing lint separately, no unrelated reformat.
Native full runtime NOT_RUN remains explicit, startup unit tests still pass.
- [ ] Step6 self-review/commit `test: run credential security gates on isolated PostgreSQL`;
handoff exact RED/GREEN, resources, historical-vs-empty/native-vs-local distinctions,
natural exit and limits. Independent review before accepting full suite prerequisite.

## Preflight

| Shared interface | Checked consequence |
| --- | --- |
| lazy cluster fixture → allocator | No duplicate native bootstrap; explicit existing Unix authority only |
| original bootstrap → two modes | Same0061tables/seeds/security tests, role created exactly once |
| startup test → native generator | Exact old interruption cleanup assertions retained |
| provenance → actual runtime | Native and local proof distinct; no fake root/port or reused role |
| shaped cycle → empty bootstrap | Both separately required; stamp is never empty bootstrap evidence |

No revision allocated and no current consumer/operational flag touched.
