# Orders immutable run binding implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans; TDD and verification-before-completion. Steps use checkboxes.

**Goal:** Persist exact account provenance on new Orders runs without relabeling old history.
**Architecture:** Five additive nullable fields on order_sync_runs, canonical byte reconstruction and immutable-from-INSERT triggers. Existing organization RLS and multiaccount read semantics remain unchanged; live authorization belongs to the accepted publication guard.
**Tech Stack:** Existing Alembic/SQLAlchemy/PostgreSQL16, Python stdlib and own backend/.venv; no dependencies.
**Spec:** `backend/docs/superpowers/specs/2026-09-09-orders-run-binding-design.md` (read fully).

## Global Constraints

- T1 worktree only: `/Users/bratishka/Downloads/satornawb-main/.worktrees/arch-t1-platform`.
- No production/app DB, working Redis, provider/network calls, real credentials/.env, flags, keys, router activation, backfill, push/deploy or other terminal's implementation changes.
- PostgreSQL proof uses only authorized random disposable DB/runtime roles via existing `tests.test_orders_schema_candidate` allocator over local Unix socket. Maintenance postgres is only for exact own-resource create/drop/absence checks. Never query existing application databases or change server/roles/config.
- Own backend/.venv, scrubbed env and plan-owned sandbox denying IP and known secret files; allow only `/private/tmp/.s.PGSQL.5432`. Lint-only `/tmp/satorna-backend-verify-20260908/bin/python -m ruff` allowed. No skips/xfails, baseline expansion or SQLite RLS substitute.
- Current observed head at dbf8d31 is sole `20260909_0066`, with no0067 file. Execution requires 0066 accepted and exact same sole head/0067 absence rechecked before edits. If changed, stop this task and report actual graph to controller; do not silently renumber. No claim that another future schema task has a reserved number.
- Version is outside canonical bytes. Exact per-run ASCII is `[organization_id,[[marketplace_account_id,provider,external_account_id,credential_ref]]]`. Preserve edge whitespace/Unicode and NULL reference; never JSONB serialization or hash-only equality.
- New binding frozen FROM INSERT, including staging and NULL→bound. Legacy all-NULL rows stay unbound. No audit-derived or current-account backfill.
- Existing FORCE organization RLS remains unchanged. Same-org unrelated-account read authorization is a separate live guard proof, not a new single-GUC database SELECT policy.

## Task 1: Add immutable binding persistence and prove actual PostgreSQL semantics

**Files:**
- Create `backend/alembic/versions/20260909_0067_orders_run_binding.py`.
- Modify `backend/ops/runtime-db-role.sql` only new helper execution ACLs.
- Create `backend/tests/test_orders_run_binding_migration.py` (schema, literal parity, old/new migration preservation, downgrade guards).
- Create `backend/tests/test_orders_run_binding_rls.py` (runtime ACL/RLS, immutable writes, actual rebind races).
- Create `backend/tests/fixtures/orders/account_binding_golden_v1.json` verbatim from exact source below.
- Create `backend/docs/superpowers/reports/2026-09-09-t1-orders-run-binding-schema-handoff.md`.

**Consumes:** Existing order_sync_runs/account composite ownership, 0062 lifecycle, 0064 account-first insert uniqueness, guard8338ef7; exact source `1e25e71eaa2a3ac00115759ca9a93ab3d9bbe8e3:backend/tests/fixtures/orders/account_binding_golden_v1.json` and `app/orders/bindings.py` (read-only source authority, never copy domain Python).

**Produces:** Columns `account_binding_schema_version SMALLINT`, `account_binding_external_account_id TEXT COLLATE "C"`, `account_binding_credential_ref TEXT COLLATE "C"`, `account_binding_payload BYTEA`, `account_binding_checksum TEXT`, all nullable without backfill DEFAULT; three immutable SECURITY INVOKER fixed-search-path helpers:

```text
public.orders_binding_text(text,integer) -> boolean
public.orders_binding_ascii_string(text) -> text
public.orders_run_binding_bytes(integer,integer,text,text,text) -> bytea
# organization_id, marketplace_account_id, provider, external_id, nullable ref
```

New BEFORE INSERT bound-row guard and BEFORE UPDATE five-field immutability guard use fixed safe errors. No callable mutation helper, new table, sequence, extension, ownership policy or domain import.

- [ ] Read full spec, actual0062/0064 migrations, relevant table model/FKs, existing allocator/migrate/runtime-script fixtures and grant patterns. Verify sole0066/0067 absence and clean owned paths. Copy literal fixture byte-for-byte through apply_patch and verify its contents equal `git show` source; do not regenerate expected bytes with the SQL implementation.
- [ ] Add tests before DDL. Reuse `cluster = candidate.cluster`, `candidate.disposable_database`, `candidate.migrate`, existing synthetic parent setup from exact-text tests; own generated role and hide_parameters engines. First migrate0066 and invoke the new helper/insert fields to observe real UndefinedFunction/UndefinedColumn failure, not missing fixture/import/setup. Add ordinary old unbound positive control.

```python
def test_literal_single_account_vectors(db):
    owner, _ = db
    for vector in golden_vectors()["vectors"][:4]:
        account, provider, external, ref = vector["accounts"][0]
        with owner.begin() as c:
            payload = bytes(c.execute(text("""
                SELECT public.orders_run_binding_bytes(:org,:account,:provider,:external,:ref)
            """), dict(org=vector["organization_id"], account=account,
                        provider=provider, external=external, ref=ref)).scalar_one())
        assert payload == vector["canonical_ascii"].encode("ascii")
        assert hashlib.sha256(payload).hexdigest() == vector["sha256"]
```

`golden_vectors()` reads only the new synthetic literal fixture using pathlib/json. `db` is a real disposable fixture pinned0067 after implementation; the RED command may use existing0066 source before creation. Record both invocation and why failure proves the absent contract.

- [ ] Implement exact reconstruction without copying serializer blocks from unrelated migrations: dedicated ASCII string encoder with lower-case escapes and UTF16 pairs, integer ownership encoding and explicit JSON null. Validate positive INT4, wb/avito, nonblank exact external1..128/refNULLor1..255 Unicode codepoints with Python-strip-equivalent whitespace. Reject NUL/surrogates at supported input boundary; disclose PostgreSQL/client casts that reject before helper. No normalization, trimming or additional invented cap.
- [ ] Implement mutually exclusive CHECK shapes: all five NULL OR schema1/external+payload+checksum required/ref nullable plus strict text, reconstructed payload equality and lowercase SHA256 equality. Check all malformed partial NULL combinations, wrong schema and bytes with recomputed checksum, named-object/string-ID/duplicate/extra/float lexical forms, whitespace and valid two_accounts vector as negative per-run INSERT.
- [ ] Bound INSERT takes canonical account FOR UPDATE by exact existing owner under READ COMMITTED, then reads fresh provider/external/ref and compares exact values/NULL. Fixed code on absent/mismatch. Check current0064 trigger interaction without rewriting it. Binding UPDATE compares OLD/NEW five fields and refuses any change, irrespective of state; do not acquire account from this UPDATE trigger (avoid domain→account inversion).

```sql
IF ROW(OLD.account_binding_schema_version, OLD.account_binding_external_account_id,
       OLD.account_binding_credential_ref, OLD.account_binding_payload,
       OLD.account_binding_checksum)
   IS DISTINCT FROM
   ROW(NEW.account_binding_schema_version, NEW.account_binding_external_account_id,
       NEW.account_binding_credential_ref, NEW.account_binding_payload,
       NEW.account_binding_checksum) THEN
  RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='orders_run_binding_immutable';
END IF;
```

Binding-check SQL must explicitly reject SQL NULL where required, not rely on unknown CHECK success. Preserve all other existing run updates/unbound legacy compatibility and original terminal guard.
- [ ] Add real two-session races with event coordination and observed wait/blocking, not sleep-only assertions: account rebind winner causes bound INSERT mismatch after wait; bound INSERT winner commits old immutable stamp before rebind proceeds. Metadata re-read after waiting must not use stale statement snapshot. Test READ COMMITTED requirement. No provider I/O while holding locks. Verify old sealed stamp remains unchanged and future account values do not relabel it.
- [ ] Preserve existing table/RLS/default ACLs. Revoke PUBLIC/default grant options on the three new helpers and trigger-only functions; grant only needed pure helper EXECUTE to existing entitled non-owner table/column grantees, respecting limited readers. New runtime-script block grants only these new helper permissions and removes their PUBLIC/grant options, never broadens pre-existing tables. Actual runtime role exercises correct org, missing/malformed/wrong-org denial; same-org second-account read retains existing org policy by explicit assertion, never claim cross-account database denial. Test unknown/new role cannot execute helpers, no trigger helper runtime grant, column reader and default-function privilege cases, script transaction failure and old ACL equality.
- [ ] Downgrade locks exact order_sync_runs ACCESS EXCLUSIVE, requires actual all-row visibility (`row_security=off` as refusal defense), and rejects any row with ANY new field non-NULL before alteration. Test hidden bound row with FORCE-RLS owner, visible bound and malformed partial bound where safely constructing fixture is explicit; all must refuse without drops/deletion. Only empty/unbound-only downgrade preserves old row values/counts, then re-upgrade without stamp. Never CASCADE or clear binding data to make downgrade pass.
- [ ] Full command gate under scrubbed Unix-only sandbox, natural exit and every own DB/role absence evidence:

```sh
.venv/bin/python -m pytest -q -s --tb=short tests/test_orders_run_binding_migration.py tests/test_orders_run_binding_rls.py
.venv/bin/python -m pytest -q -s --tb=short tests/test_orders_exact_text_migration.py tests/test_orders_schema_candidate.py tests/test_orders_schema_integration.py tests/test_orders_contract.py tests/test_repricer_approvals_schema.py tests/test_repricer_approvals_lifecycle.py tests/test_repricer_approvals_rls.py
.venv/bin/python -m compileall -q alembic/versions/20260909_0067_orders_run_binding.py tests/test_orders_run_binding_migration.py tests/test_orders_run_binding_rls.py
.venv/bin/python -m alembic heads
git diff --check
```

Feature fixtures pin0067; runtime-script fixture consumes latest head. Test graph ancestry accepts one synthetic future successor rather than permanently asserting0067 is globally latest. Empty actual chain upgrade→downgrade0066→upgrade0067 (no stamp), plus production-shaped synthetic unbound data roundtrip and bound-data refusal are separate assertions. Run scoped Ruff on new Python only.
- [ ] Self-review contract, fixed errors/canary, trigger lock ordering, exact bytes versus hash, no stale binding read or scope overclaim. Commit `feat: freeze account binding on Orders runs` with only six listed paths. Report exact RED/GREEN/commands/exit codes/cleanup/sole head and limits; independent review before exact ready T3 handoff. T3 owns ORM/repository/decoder/live-guard/API acceptance. Rollback keeps expanded schema with decoder-capable binary; no return to mutable audit proof.

## Controller preflight

| Interface | Decision / cost |
| --- | --- |
| literal aggregate codec → per-run field | Four single-account vectors accepted; multiaccount vector negative, shared source codec unchanged |
| INSERT0064 lock → new binding check | Same canonical account lock, fresh read; UPDATE does not introduce account lock inversion |
| legacy rows → strict bound shape | All NULL preserved, no opportunistic backfill; old unbound histories remain unavailable to new strict reader |
| org RLS → guarded multiaccount reader | Do not impose single-account GUC SELECT; T3 must keep live per-account authorization |
| production/P1 queue → migration number | No concurrent schema implementer; recheck actual0066/0067 absence before executing this task |

No production policy is inferred. This plan does not activate routes, workers, data migration or credentials consumers.
