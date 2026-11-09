# Orders schema integration — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Follow TDD and verification-before-completion.

**Goal:** Принять проверенный Orders DDL в единственную активную Alembic chain T1, с Catalog anchors, ограниченными grants и реальными PostgreSQL acceptance tests.

**Architecture:** Кандидат принят только после двух проверок отсутствия собственной схемы T1. SQL становится self-contained migration; кандидат остаётся историческим источником, не runtime dependency. T3 сохраняет владение domain ORM/repository/API.

**Tech Stack:** PostgreSQL, Alembic/SQLAlchemy, psycopg, pytest, локальная backend/.venv.

**Spec:** `backend/schema_candidates/orders_v1/HANDOFF.md`, `upgrade.sql`, `downgrade.sql`; поручение пользователя `/Users/bratishka/.codex/memories/projects/satorna-orders-coordination-20260909.md`, «Текст задания T1».

## Global Constraints

- Только worktree arch-t1-platform; не менять чужие worktrees/domain implementation.
- No production, application DB/tables, provider calls, real credentials, deploy/push/flags activation/collector activation.
- Maintenance fallback явно разрешён: Unix `/tmp`, maintenance DB `postgres`, только создание/удаление собственных случайно именованных disposable DB/runtime roles. Не читать application data, не изменять работающий сервер, auth/host config или прежние DB/roles.
- Не брать inherited DB URLs, libpq credentials или .env. Env scrubbed; PGPASSFILE/PGSERVICEFILE/NETRC=/dev/null. IPv4/IPv6 external networking deny, Unix test socket разрешён явно.
- Head перед реализацией: `20260908_0061`; назначить `20260909_0062` только если это всё ещё единственная head и номер0062 свободен. Иначе остановиться и сообщить.
- Никаких прав ownership/BYPASSRLS/DDL/DELETE/TRUNCATE/sequence UPDATE для runtime Orders. Org RLS не заменяет account authorization.
- Orders не заменяет T2 approvals/attempts/audit и T3 atomic publication/service acceptance.

### Task 1: Active migration, Catalog anchors and runtime acceptance

**Files:**
- Create: `backend/alembic/versions/20260909_0062_orders.py` (self-contained candidate SQL constants + upgrade/downgrade functions).
- Modify: `backend/app/platform/catalog/orm.py` (только три constraints кандидата).
- Modify: `backend/ops/runtime-db-role.sql` (Orders FORCE RLS guard and explicit least-privilege overrides after existing broad grants).
- Modify: `backend/tests/test_orders_schema_candidate.py` (сохранить acceptance scenarios, перенести их на actual revision, безопасный fixture cleanup).
- Modify: `backend/tests/test_marketplace_credential_rls.py` (только два isolated-fixture upgrade targets: moving head → точный0061).
- Create: `backend/tests/test_orders_schema_integration.py` (integration/ORM/grants/actual chain and populated synthetic Catalog tests).
- Create: `backend/docs/superpowers/reports/2026-09-09-t1-orders-schema-handoff.md` (точные зависимости/доказательства/limits).

**Interfaces:**
- Consumes 11 tables and all constraints/triggers from candidate SQL. Immutable source candidate commit09cf383, T1 cherry-pick c4c78fe. Do not redesign domain vocabulary.
- Produces actual revision0062 over0061, account-qualified Catalog keys and runtime ACL, T3 SQL handoff. No domain ORM registration added.

- [ ] Step 1: Write RED for actual head/three ORM anchors/least-privilege grants. Example:

```python
scripts = ScriptDirectory.from_config(config)
assert len(scripts.get_heads()) == 1
assert scripts.get_revision('20260909_0062').down_revision == '20260908_0061'
assert '20260909_0062' in {r.revision for r in scripts.walk_revisions()}
assert 'uq_orders_product_account' in {x.name for x in MarketplaceProductRow.__table__.constraints}
assert 'fk_orders_offer_product_account' in {x.name for x in MarketplaceOfferRow.__table__.constraints}
assert 'uq_orders_offer_product_account' in {x.name for x in MarketplaceOfferRow.__table__.constraints}
```

For runtime privilege assertions execute actual psql script on disposable DB with random runtime_role/runtime_database, exact authenticated migration owner_role and synthetic runtime_password, not a hand-written approximation. Owner default ACL changes are database-local to this disposable DB; never ALTER an existing owner or create a privileged test role. Assert history SELECT/INSERT, current order/item and staging sync run UPDATE; deny DELETE/TRUNCATE/DDL/sequence UPDATE; deny no-context/wrong-org writes and reads of seeded rows. Separately deliberate broad grants exercise history triggers; restore least privileges or isolate tests.

- [ ] Step 2: Run focused RED before migration/ORM/grants changes; record expected missing head/anchor/privilege assertions. Prove authorized Unix-only connection before DB writes. Register cleanup before setup can partially create resources; exact random DB/role targets only. Fix inherited candidate fixture start-before-finally issue as part of safe harness, cover it with mocked failure tests. No SQLite substitution.

- [ ] Step 3: Implement minimal migration, consuming SQL as literal constants, not paths outside Alembic:

```python
revision = '20260909_0062'
down_revision = '20260908_0061'
branch_labels = depends_on = None
def upgrade():
    op.execute(sa.text(UPGRADE_SQL))
def downgrade():
    op.execute(sa.text(DOWNGRADE_SQL))
```

UPGRADE_SQL/DOWNGRADE_SQL contain exactly the candidate content (comment normalization allowed); no percent interpolation. Reflect its three named Catalog constraints verbatim in ORM. Append Orders-specific REVOKE ALL + explicit table and sequence grants after broad runtime script defaults. Do not silently widen unrelated ACLs or alter schema values.

Expand-time amendment: after candidate DDL in the same migration transaction,
normalize ACLs on only these 11 newly created tables and their identity sequences.
Discover their actual non-owner ACL grantees (including PUBLIC); revoke grants and
restore only the intersection of existing rights with SELECT/INSERT for history,
SELECT/INSERT/UPDATE for marketplace_orders, marketplace_order_items and
order_sync_runs, and USAGE for sequences. Do not restore grant options or widen a
read-only grantee. Preserve owner authority; do not change global default ACLs,
unrelated relations or guess deployed role names. Verify effective rights with
preexisting broad owner defaults immediately after upgrade, before runtime script;
add a selective-role no-widening regression. This is additive ACL hardening; the
candidate structural DDL is unchanged.

Runtime grant script also runs as one explicit transaction: BEGIN after
ON_ERROR_STOP and COMMIT after all guards/grants/narrow revokes. This prevents
intermediate broad grants becoming externally visible. Add a failure-rollback or
two-session visibility regression using the actual script on a disposable DB.
Document standalone psql invocation, not nesting in another transaction or -1.

- [ ] Step 4: Adapt candidate tests from unregistered wrapper/standalone SQL to actual migration. Preserve all 39 original acceptance cases and cover actual active head from empty DB (no stamp), upgrade0061→0062 with populated synthetic Catalog, empty0062→0061→0062, nonempty+hidden-RLS downgrade refusal leaving version/schema/data intact, invalid preexisting Catalog pairing atomic upgrade failure, source/adapter/run and org/account FKs, immutable history, sealed snapshot payloads, two-session replay unique/CAS. Actual runtime role NOSUPERUSER/NOBYPASSRLS/NOINHERIT. Snapshot/provider semantics remain T3 obligations.

Adjacent regression: isolated credential RLS fixture creates only two parent tables
and stamps0060. Its two `command.upgrade(config, 'head')` calls must target0061,
not new Orders/Catalog DDL. Preserve all credential assertions; focused target
regression belongs in test_orders_schema_integration.py. This does not claim a
native credential-cluster execution. Actual empty full-chain proof remains separate.

Orders fixtures and feature roundtrips target the registered0062 revision, not
moving head, so later independent migrations do not corrupt this bounded fixture.
Graph test checks one actual head and0062 in its ancestry; record exact head at
verification time (initially0062). Central release gate owns future moving-head
empty chain testing. No temporary unregistered wrapper or stamp substitute.

- [ ] Step 5: GREEN full scoped `tests/test_orders_schema_candidate.py tests/test_orders_schema_integration.py tests/test_orders_contract.py`, then compileall changed source/tests/alembic and diff check. Report exact invocation/env isolation, counts/exits, DB engine/server version (no application inspection), cleanup proof and limits. Do not run unrelated full suite with services or pretend package-wide baseline parity.

- [ ] Step 6: Self-review and commit migration+anchors+grants+tests+handoff as a bounded schema commit. Controller independently reviews and reruns. T3 waits for coordinator acceptance. T2 exact durable reserve/dispatch amendment is now received at `ccaed3410e32c50b3604f02e5b1171fd7243ce49`; its separate DDL is not enabled by Orders.
