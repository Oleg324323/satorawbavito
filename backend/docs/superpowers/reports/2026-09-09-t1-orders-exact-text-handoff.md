# T1 Orders exact TEXT handoff

2026-09-09. Amendment `20260909_0064` follows the inspected sole head
`20260909_0063`; the revision was absent before editing. Implementation baseline
`e396b08556b6a47bdf4bbf6ddf6eb9008c34c5a7`, branch `codex/arch-t1-platform`.
Only the new migration, its PostgreSQL acceptance tests and this handoff belong
to this change. Historical 0062/0063, feature fixtures, runtime grant script and
T3 repositories are unchanged.

## Database contract

All comparisons are full-value `COLLATE "C"`, inside the existing organization /
marketplace account ownership boundary. No digest identity, normalization,
truncation, invented byte cap, SECURITY DEFINER or account-data mutation is used.
The guard requires actual READ COMMITTED, takes the canonical account row
`FOR UPDATE`, then checks the complete key in a separate volatile PL/pgSQL
statement with a fresh post-wait snapshot. Existing identity/history UPDATE
guards remain in place and are exercised by tests.

Each exact duplicate raises SQLSTATE `23505`, message
`orders_identity_conflict`, and the logical constraint name below. These names
are the original 0062 names, including PostgreSQL's generated/truncated names;
they are diagnostics, **not usable physical conflict targets** after 0064.
Other isolation levels fail before insertion with SQLSTATE `25000`, message
`orders_isolation_invalid`.

| Relation | Exact key after owner columns | Removed arbiter / retained logical diagnostic |
| --- | --- | --- |
| order_sync_runs | source_kind, source_run_key | order_sync_runs_organization_id_marketplace_account_id_sour_key |
| marketplace_orders | external_order_id | marketplace_orders_organization_id_marketplace_account_id_e_key |
| marketplace_order_items | order_id, source_line_key | marketplace_order_items_organization_id_marketplace_account_key |
| order_observations, NULL item | order_id, source_kind, adapter_version, payload_checksum | uq_orders_observation_order_replay |
| order_observations, non-NULL item | order_id, order_item_id, source_kind, adapter_version, payload_checksum | uq_orders_observation_item_replay |
| order_lifecycle_events | order_id, observation_id, source_event_key | order_lifecycle_events_organization_id_marketplace_account__key |
| order_deadlines | observation_id, deadline_kind | order_deadlines_organization_id_marketplace_account_id_obse_key |
| order_sync_coverage | sync_run_id, coverage_kind | order_sync_coverage_organization_id_marketplace_account_id__key |

Also removed: the redundant full-TEXT run unique constraint
`order_sync_runs_organization_id_marketplace_account_id_sync_key` on owner,
sync_run_id, source_kind, adapter_version, and dependent observation FK
`order_observations_organization_id_marketplace_account_id__fkey`.
The replacement FK `fk_orders_observation_owner_run` references the existing
bounded `(organization_id, marketplace_account_id, sync_run_id)` unique target.
The observation guard separately compares the same owner's run's exact
source_kind and adapter_version. A mismatch raises safe `23503`, message
`orders_source_binding_invalid`, diagnostic
`fk_orders_observation_source_adapter`. Parent source/adapter updates remain
forbidden by the old immutable run trigger.

The retained source/provider anchor on owner, marketplace, source_kind,
sync_run_id is bounded: source_kind has exactly the three existing permitted
literals (`wb-statistics-supplier-orders`, `avito-order-management`,
`avito-browser`) under the provider CHECK. Catalog and owner/internal-ID FKs,
membership partial indexes, snapshot keys and queue indexes remain unchanged.
The only remaining indexed TEXT field among all eleven Orders relations is this
finite source_kind. Other indexed string fields are bounded VARCHAR state,
canonical_status and resolution_state with finite CHECK sets; checksums remain
fixed 64 lowercase hex and are compared exactly for observation identity.
No expression/hash accelerator or new raw-TEXT index is introduced.

Upgrade holds ACCESS EXCLUSIVE locks on all eleven Orders relations and validates
all replaced named constraint/index definitions and the retained bounded run
anchor before changing schema. Unexpected dependencies fail transactionally;
there is no CASCADE. Existing rows are not rewritten. Downgrade first locks and
checks all eleven relations with `row_security=off`; an actor subject to FORCE
RLS gets an error, never a false-empty result. Only empty state can restore the
original named unique constraints, partial indexes and FK. Populated or hidden
state leaves schema and Alembic version unchanged. No stamp/backfill/deletion.

## T3 account-first RC replay protocol (verbatim design requirements)

T3 must acquire fresh authorization/account locks before its run/projection locks,
read exact existing key under the held account lock, compare every immutable replay
field/semantic payload, then insert only when absent. An exact no-op returns stored
identity/evidence; same invocation/key with changed immutable payload is a conflict,
not overwrite, ambiguous merge or new authority. Under concurrency the waiter
refreshes after lock acquisition, sees the committed winner and validates replay.

Observation replay lookup includes owner/order/item-nullability/source/adapter/
checksum and compares the exact normalized semantic evidence as already required.
Receipt/manifest membership still binds a new run to existing evidence. Bounded
membership and internal-ID arbiters not changed by this migration may retain their
ON CONFLICT usage. Generic DO NOTHING without the exact comparison is not a replay
contract. No service implementation is copied into T1.

Database triggers are a last defense; they cannot retroactively reorder domain
locks acquired by an incorrect caller. Session guard and T3 service tests remain
separate obligations. RR/SERIALIZABLE are explicitly unsupported for the new key
inserts, not silently accepted based on RC tests. Creation writers/importers must
use the documented fresh RC protocol. Provider network stays outside locks.

## Verification evidence

Commands run from the T1 `backend` directory. Runtime is that worktree's own
`.venv`; no inherited application environment or URL. Existing disposable helpers
create random fresh DBs/roles only, use Unix `/tmp`, and explicitly check exact
resource absence after cleanup. The OS profile denies network other than the
authorized Unix maintenance socket and denies secret-file reads.

RED, before adding 0064, with latest head still 0063:

```sh
env -i PATH=/usr/local/bin:/usr/bin:/bin PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 ORDERS_TEST_USE_LOCAL_CLUSTER=1 /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-orders-exact-text-amendment/local-postgres.sb .venv/bin/python -m pytest -q -s --tb=short tests/test_orders_exact_text_migration.py
```

Exit **1**, **8 failed in 3.13s**, every case actual SQLSTATE **54000**.
Input is a label plus 64 distinct SHA-256 hex outputs (4096 deterministic
incompressible suffix bytes); SHA is only the synthetic test-data generator,
never database identity. Fields: source_run_key, external_order_id,
source_line_key, run/observation adapter_version (both null-item branches),
source_event_key, deadline_kind, coverage_kind. The adapter regression reaches
the old run adapter index first. SQLAlchemy hides bound parameters; diagnostics
record safe SQLSTATE rather than formatting input values. Cleanup verified
`orders_test_2f48c4aca4a04e22bc56d3a5473668d7` and role
`orders_exact_e4fc8495739646d7adb4754543519652` absent.

Initial focused GREEN: **60 passed in 13.44s**, exit 0. Expanded suite exposed
one test-only psycopg `%` placeholder mistake (**67 passed, 1 failed**); changed
the catalog LIKE pattern to a bound parameter. Subsequent combined acceptance:
**217 passed in 115.85s**, exit 0. Ten additional logical parent/source scope
cases were then added for the final acceptance run below.

Final combined command:

```sh
env -i PATH=/usr/local/bin:/usr/bin:/bin PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 ORDERS_TEST_USE_LOCAL_CLUSTER=1 /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-orders-exact-text-amendment/local-postgres.sb .venv/bin/python -m pytest -q -s --tb=short tests/test_orders_exact_text_migration.py tests/test_orders_schema_candidate.py tests/test_orders_schema_integration.py tests/test_orders_contract.py tests/test_review_facts_schema.py
.venv/bin/python -m compileall -q alembic tests/test_orders_exact_text_migration.py
/tmp/satorna-backend-verify-20260908/bin/python -m ruff check alembic/versions/20260909_0064_orders_exact_text.py tests/test_orders_exact_text_migration.py
git diff --check
```

Final combined result: **227 passed in 64.56s**, exit **0**, including **78** new
exact-text cases and **149** adjacent cases. Compileall, scoped Ruff and diff
checks each exited **0**. Alembic graph inspection (without env loading) confirms
sole head **20260909_0064**, directly after **20260909_0063**. Natural completion
reported PostgreSQL **16.15 (Homebrew)** over local Unix socket, with no skips,
SQLite substitutes or killed-process success. The expected libpq
warning that `/dev/null` is not a plain password file is a consequence of the
explicit credential isolation. Static Ruff uses the explicitly approved external
lint-only environment, never the application runtime.

The new suite's final run verified absence of all six exact disposable databases:
`orders_test_4cbe5dd3af5e4ef5be8d22055d7672b7`,
`orders_test_18071e1e5102481aadbff09de2be2a94`,
`orders_test_476aaf6ea8014da98d9bd1e70e404eb6`,
`orders_test_cf7c6e667fec44d6b89bde8094d9efca`,
`orders_test_2a51dccd21f44e8aaaef9ad35bbb1151`,
`orders_test_ad85b00b6dbc454daa9c7821ca09dc6b`; runtime roles
`orders_roundtrip_a090a7351f73444e873136c336ab95a4`,
`orders_parity_56af3ae436f84775b3a2cf18941d67b0`,
`orders_exact_4f2ecba7d5bf458ab6b47011c8a83c77` also verified absent.
All adjacent cleanup assertions completed successfully as well.

Coverage includes every long/distinct/duplicate key; exact null-item semantics;
source/provider/adapter/owner and RLS rejection; changed checksum preserving new
semantic evidence; immutable key/source/adapter UPDATE denial; all eleven table
rollback and populated upgrade parity; ACL, policies, sequence ACL and exact empty
schema roundtrip; nonempty/hidden-RLS downgrade refusal; old unique/FK/partial
index drift rollback; all eight direct-insert account waits with both sessions'
snapshots established before insert, observed physical blocking and safe waiter
23505 after winner commit; all eight keys at RR and SERIALIZABLE rejecting 25000;
actual latest-head runtime grant script, with canonical account values unchanged.

## Limits and release dependency

Per-account serialization and exact scans use existing owner-leading indexes.
Contention and growing lookup cost are explicit tradeoffs; no production
throughput claim or benchmark is made. PostgreSQL's inherent TEXT/storage limits
still apply; this removes B-tree identity-size rejection, not physical storage
limits. Upgrade/downgrade require owner maintenance transactions and table locks.

T3's repository amendment and independent migration/repository verification are
still release dependencies. Existing raw-column `ON CONFLICT` targets must be
removed by their owner before relying on this schema. This commit does not prove
full architecture readiness, consumer readiness, authorization/service lock
ordering, provider behavior, or production rollout. No real DB, env values,
credentials, external network/provider calls, host changes, push or deploy.

An isolated self-review checked the eight predicates against the design, null
semantics, immutable parent binding, migration atomicity, scope, safe diagnostics
and evidence limits. The configured `preflight-critic/SKILL.md` was absent from
the local skills/plugin trees; controller independent review remains required.

## Review correction: historical fixture lifetime

Independent review found one P2 acceptance defect: the historical downgrade
helper asserted that 0064 remained the literal latest head, and the feature's
catalog checks shared a latest-head database with the current runtime script.
A legitimate successor therefore failed the helper before PostgreSQL could
exercise its hidden-RLS downgrade check. No migration SQL defect was identified.

Feature tests and exact index inventory now use a database pinned to 0064; the
historical helper resolves 0064 directly. A separate latest-head fixture is used
only by the actual runtime-script acceptance test. The graph gate still requires
one current head and 0064 in its ancestry. The earlier sole-head statements in
this handoff are recorded verification-time facts, not permanent graph rules.
All PostgreSQL assertions remain in place; no migration or runtime SQL changed.

An in-memory Alembic successor regression changes only a private revision map,
without writing migration files, loading env.py or connecting to PostgreSQL.
Before the correction it failed at the old literal head assertion: **1 failed,
78 deselected in 0.45s**, exit **1**. After correction, the successor/helper and
current-ancestry gates passed: **2 passed, 78 deselected in 0.39s**, exit **0**.

The focused commands use the same sanitized Unix-only prefix documented above:

```sh
env -i PATH=/usr/local/bin:/usr/bin:/bin PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 ORDERS_TEST_USE_LOCAL_CLUSTER=1 /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-orders-exact-text-amendment/local-postgres.sb .venv/bin/python -m pytest -q -s --tb=short tests/test_orders_exact_text_migration.py -k historical_revision_accepts_synthetic_successor
env -i PATH=/usr/local/bin:/usr/bin:/bin PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 ORDERS_TEST_USE_LOCAL_CLUSTER=1 /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-orders-exact-text-amendment/local-postgres.sb .venv/bin/python -m pytest -q -s --tb=short tests/test_orders_exact_text_migration.py -k 'historical_revision_accepts_synthetic_successor or current_chain_includes_orders_amendment'
```

The full five-file command above was rerun after the correction: **229 passed in
69.15s**, exit **0** (80 amendment cases, including the two pure graph gates,
plus 149 adjacent cases). PostgreSQL 16.15, natural completion, no skipped tests.
All exact disposable DB/role absence assertions passed, including the now separate
0064 feature and latest runtime-script databases. Compileall, scoped Ruff,
`git diff --check` and the migration/runtime-script unchanged check each exited
0. The P2 is addressed; independent follow-up review and T3 consumer acceptance
remain separate requirements.
