# Orders exact TEXT amendment implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Execute the bounded task with TDD and independent review.

**Goal:** Store exact accepted Orders text identities without PostgreSQL B-tree row-size failures.

**Architecture:** New migration replaces only oversized arbiters with account-serialized exact comparisons under READ COMMITTED. Preserve existing data, bounded foreign keys, RLS, grants and immutable evidence. T3 separately replaces obsolete ON CONFLICT usage.

**Tech Stack:** PostgreSQL16, SQLAlchemy/Alembic, pytest, own isolated Python venv.

**Spec:** `backend/docs/superpowers/specs/2026-09-09-orders-exact-text-amendment-design.md`.

## Global Constraints

- Only T1 worktree `codex/arch-t1-platform`; no other owner code, secrets/env, production, external network/provider, push/deploy or flags activation.
- New migration only. Actual sole head must be `20260909_0063`; `20260909_0064` must be absent. Otherwise stop with evidence; never renumber or rewrite0062/0063.
- No hash identity, normalization, text truncation or invented business byte limit.
- Exact `COLLATE "C"` comparison; canonical account `FOR UPDATE`, fresh post-wait statement snapshot, actual READ COMMITTED only; other isolation raises safe25000.
- Duplicate logical identity raises23505 with stable logical constraint name and no input fragments. SECURITY DEFINER and account-data mutation prohibited.
- Use only own random disposable PostgreSQL databases/runtime roles via authorized Unix `/tmp`; maintenance `postgres` only create/drop exact own names and absence checks. Never inspect existing app data or alter host. OS network denial and env isolation required.
- Downgrade fails closed before mutation if affected data exist, including hidden-by-RLS data. No CASCADE, stamp, backfill or data deletion.
- Existing feature fixtures remain bounded0062/0063; actual current grant script uses latest-head fixture only.
- This new feature's fixture/index inventory and downgrade helper remain bounded0064 too. Its graph assertion is one current head with0064 in ancestry, never that0064 stays head forever. A separate latest-head fixture exercises the actual current runtime script; do not invoke that script on a pinned0064 database.
- No subagents from implementer. Controller performs review. Preserve unrelated controller docs; stage only task files.

### Task 1: Exact-text migration and PostgreSQL acceptance

**Files:**
- Create `backend/alembic/versions/20260909_0064_orders_exact_text.py`.
- Create `backend/tests/test_orders_exact_text_migration.py`.
- Create `backend/docs/superpowers/reports/2026-09-09-t1-orders-exact-text-handoff.md`.
- Existing migrations, runtime script and T3 repository unchanged. Report any concrete prerequisite rather than broadening files.

**Interfaces:** Consume0062 immutable Orders tables and0063 current head. Produce same table/column ownership contract with eight exact logical uniqueness rules and bounded observation/run source binding. Handoff lists removed physical arbiters, replacement logical names and account-first RC replay protocol verbatim for T3.

- [ ] Read full design,0062 migration and tests' disposable helpers; inventory each TEXT-index/FK dependency. Query Alembic graph without environment loading; assert sole0063 and free0064.
- [ ] Write failing regression on0063: deterministic incompressible text, not repeated compressible filler. Use helper shape:

```python
def long_key(label):
    return label + ''.join(hashlib.sha256(str(i).encode('ascii')).hexdigest()
                           for i in range(64))

def test_long_external_order_identity(db):
    expected = long_key('synthetic-order:')
    with db.begin() as c:
        # Use inspected existing minimal valid owner/run fixtures, exact input.
        order_id = insert_order(c, external_order_id=expected)
        assert c.execute(text('SELECT external_order_id FROM marketplace_orders '
                              'WHERE order_id=:id'), {'id': order_id}).scalar_one() == expected
```

`insert_order` is test-local wrapper adapted from existing candidate fixture, never a new production helper. Cover source_run_key, external_order_id, source_line_key, adapter_version in run+observation, source_event_key, deadline_kind and coverage_kind. Capture safe SQLSTATE54000 RED without formatting DB parameter values. Missing new revision RED is supplementary, not substitute for behavior reproduction.
- [ ] Add target tests: distinct and exact duplicates for all eight design keys; order/item observation null semantics; exact source+adapter mismatch rejection; owner/provider and wrong-context isolation; changed checksum distinct evidence; transaction rollback preserves all relevant rows; duplicate error messages contain no long synthetic identity. Two real connections establish snapshots BEFORE direct guarded INSERT, observe lock wait, commit winner, then waiter sees duplicate23505; precreate parent run/orders so no earlier helper pre-acquires account lock. Test RR and SERIALIZABLE25000/no partial rows.
- [ ] Implement migration upgrade with owner transaction table locks and old-constraint/FK shape validation before mutation. Remove full TEXT arbiters enumerated in spec. Replace unbounded run-adapter FK with bounded owner/run FK plus exact immutable source/adapter check. Follow this actual guard pattern with per-table complete predicates:

```sql
IF current_setting('transaction_isolation') <> 'read committed' THEN
  RAISE EXCEPTION USING ERRCODE='25000', MESSAGE='orders_isolation_invalid';
END IF;
PERFORM 1 FROM marketplace_accounts
 WHERE organization_id=NEW.organization_id
   AND marketplace_account_id=NEW.marketplace_account_id
 FOR UPDATE;
-- Existing ownership FK remains authoritative; exact predicate follows in a
-- separate volatile PL/pgSQL statement after the acquired account lock.
IF EXISTS (SELECT 1 FROM marketplace_orders
 WHERE organization_id=NEW.organization_id
   AND marketplace_account_id=NEW.marketplace_account_id
   AND external_order_id COLLATE "C" = NEW.external_order_id COLLATE "C") THEN
  RAISE EXCEPTION USING ERRCODE='23505', MESSAGE='orders_identity_conflict',
    CONSTRAINT='uq_order_external';
END IF;
```

Confirm canonical account column names from ORM/0062 before rendering SQL. Preserve exact old logical names when available; document any new safe name. Existing key immutability must prevent UPDATE bypass; prove rather than assume. No new raw-TEXT index introduced.
- [ ] Add populated0063→0064 parity (synthetic complete fixtures and immutable counts/values), empty0063→0064→0063→0064, nonempty/hidden-RLS guarded refusal with schema/version preserved. New downgrade validates all affected state while holding locks, then restores original named constraints/indexes/FK exactly only when empty. Check nonowner ACLs/RLS remain unchanged through upgrade and no-context/wrong-org denial.
- [ ] Run focused and adjacent acceptance with own `.venv`, sanitized env and copied network-denial profile. Example from backend:

```sh
env -i PATH=/usr/local/bin:/usr/bin:/bin PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 ORDERS_TEST_USE_LOCAL_CLUSTER=1 /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-orders-exact-text-amendment/local-postgres.sb .venv/bin/python -m pytest -q -s --tb=short tests/test_orders_exact_text_migration.py tests/test_orders_schema_candidate.py tests/test_orders_schema_integration.py tests/test_orders_contract.py tests/test_review_facts_schema.py
.venv/bin/python -m compileall -q alembic tests/test_orders_exact_text_migration.py
/tmp/satorna-backend-verify-20260908/bin/python -m ruff check alembic/versions/20260909_0064_orders_exact_text.py tests/test_orders_exact_text_migration.py
git diff --check
```

Natural completion required; no skip, SQLite RLS substitute or killed test success. Verify exact own DB/role absence cleanup and graph sole0064. Static Ruff external path is explicitly approved lint-only, not app runtime.
- [ ] Write handoff with exact RED/GREEN commands/exits, removed arbiters, logical names, RC/account-lock requirements, performance limits and T3 release dependency. Commit only three allowed files as `fix: preserve exact long Orders identities`. Report SHA and concerns to controller; no claim full architecture/consumer readiness.
