# Review Facts Schema Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Install the accepted four-table account-owned Review Facts prerequisite with real disposable PostgreSQL evidence, without changing collectors or domain code.

**Architecture:** Add one self-contained Alembic revision after the actual sole head. Composite organization/account/provider FKs and forced organization RLS protect ownership; immutable evidence and deferred final-row validation preserve transactional facts. T4 remains owner of ORM/repository and semantic ingestion acceptance.

**Tech Stack:** Existing PostgreSQL16, Alembic, SQLAlchemy, psycopg, pytest; no new dependencies or services.

**Spec:** Immutable `4a7d66999e8d8deedbe6ac5d44a23d9a960c9044:backend/docs/superpowers/reports/2026-09-08-t4-reviews-stage-2-schema-request.md`, accepted in `d38e923:backend/docs/superpowers/reports/2026-09-08-t1-schema-contract-feedback.md`. Read both fully with git show. Later T4 amendments do not replace these four relations.

## Global Constraints

- Work only in `codex/arch-t1-platform` at `/Users/bratishka/Downloads/satornawb-main/.worktrees/arch-t1-platform`; preserve other paths/worktrees/dirty files. Never use GitHub, production, application DB data, Redis, providers, real credentials, .env or host configuration.
- T1 owns Alembic/shared grants. T4 owns `app/reviews/canonical_orm.py`, repository, collectors, review_tasks, frontend/serverless. Do not create competing ORM/service implementation.
- Exactly four new relations: `review_sync_runs_v2`, `review_facts`, `review_observations`, `review_sync_run_items`. No policy/draft/send/notification tables, Catalog mapping guess, legacy writes/backfill or rollout flags.
- Organization/account IDs INTEGER; internal IDs UUID; versions/revisions/order BIGINT; opaque external IDs TEXT; provider VARCHAR(16) wb|avito; times TIMESTAMPTZ; lowercase SHA256 checksums. All owner and internal references include organization/account/provider; ON DELETE RESTRICT.
- All four tables ENABLE/FORCE RLS using transaction-local `app.organization_id`, both USING and WITH CHECK. Missing/wrong org must fail. Organization RLS is not account authorization.
- Runtime must be non-owner NOSUPERUSER/NOBYPASSRLS, without grant options, DDL, DELETE/TRUNCATE, evidence UPDATE or sequence UPDATE. Protect expand-time inherited broad default ACL before migration commit, not only after grant-script rerun.
- Server issues run_sequence; runtime cannot choose it via explicit column, identity override, UPDATE or setval. The accepted spec's BY DEFAULT identity spelling is subordinate to this security requirement.
- Disposable DB authorization: scrub environment, OS deny IP networking and secret files, allow ONLY local Unix `/private/tmp/.s.PGSQL.5432`; maintenance `postgres` only for exact random own database/role creation, removal and absence checks. No existing application data/roles/server settings. Use only own backend/.venv. Do not attempt native initdb host repair.
- Downgrade refuses any nonempty canonical relation, including rows hidden by RLS; no CASCADE. No weakening tests/skips/baseline expansion.

## Task 1: Add Review Facts DDL, runtime privilege boundary and acceptance

**Files:**
- Create `backend/alembic/versions/20260909_0063_review_facts.py` only if immediately checked actual sole head remains0062 and0063 is unused; if head differs, report actual graph and obtain controller-assigned path, never silently renumber.
- Modify `backend/ops/runtime-db-role.sql`: four FORCE RLS requirements plus explicit narrowly scoped grants inside existing atomic transaction.
- Create `backend/tests/test_review_facts_schema.py`: real DDL/RLS/FK/immutability/ACL/downgrade/concurrency tests.
- Modify `backend/tests/test_orders_schema_integration.py`: separate latest-head actual-grant-script fixture from bounded0062 feature fixture, preserving every existing Orders assertion.
- Create `backend/docs/superpowers/reports/2026-09-09-t1-review-facts-schema-handoff.md`: exact keys/grants/limits/commands and T4 service obligations.

**Interfaces:**
- Consume canonical MarketplaceAccount owner target from0061 and local disposable fixture helpers in tests/test_orders_schema_candidate.py. Existing Orders feature roundtrips remain pinned0062.
- Produce exact four SQL tables/column names from spec. Add scoped target UNIQUEs for observation checksum binding and run sequence reference.
- Physical amendment for unbounded `source_run_id` and `external_review_id`: do not create a B-tree UNIQUE over full TEXT, which rejects valid incompressible long IDs. Enforce exact scoped uniqueness with a BEFORE INSERT trigger taking the canonical account FOR UPDATE, then comparing exact text with C collation. Existing owner-prefix indexes may support lookup; an optional bounded nonunique digest lookup may accelerate but never replace exact comparison or reject a collision. UUID-scoped FK UNIQUE targets remain. Exact duplicate raises safe23505; no raw value. This is not an ON CONFLICT constraint target: T4 future repository must use account-serialized exact replay lookup/insert, described in handoff. No existing Orders constraint/repository change in this slice.
- `review_facts` committed row must have non-null current_observation_id and version>0. Deferred constraint trigger queries the final row by identity, not event NEW. Valid insert identity→observation→pointer in one transaction must pass; half-created identity must fail at commit.
- Facts identity/owner/external ID immutable; mutable pointer/watermark update must advance version by one. Run identity/owner/request/sequence/start immutable; running→terminal guarded, terminal rows cannot change. Append-only observation and run-item UPDATE/DELETE/TRUNCATE are denied by privileges and protective triggers. This does not claim service complete-manifest or freshness proof.
- Runtime run INSERT uses an explicit column allowlist excluding run_sequence, with identity server default; no table-level INSERT on this table. Runtime UPDATE cannot change sequence/identity (guarded). Only USAGE on its identity sequence. `INSERT ... OVERRIDING SYSTEM VALUE` that supplies run_sequence must be denied. Document T4 must omit run_sequence from INSERT and use RETURNING.
- Preserve an existing grantee's intersection of permitted rights for new objects; when inherited table INSERT exists on run table, replace with allowed column INSERT, not privilege widening for SELECT-only roles. New explicit runtime script grants the same contract. Do not touch old object ACLs or global defaults inside migration.

- [ ] **Step 1: Capture graph/scope, write RED structural and PostgreSQL tests.**

```python
scripts = ScriptDirectory.from_config(config)
assert len(scripts.get_heads()) == 1
revision = scripts.get_revision('20260909_0063')
assert revision.down_revision == '20260909_0062'
# Deferred final-row invariant: this must COMMIT after pointer update.
with runtime.begin() as c:
    scope(c, org)
    insert_identity(c, review_id, current_observation_id=None, version=0)
    insert_observation(c, observation_id, review_id, run_id)
    advance_fact(c, review_id, observation_id, expected_version=0)
# Explicit caller-issued ordering is denied, even with identity override.
with pytest.raises(DBAPIError), runtime.begin() as c:
    scope(c, org)
    c.exec_driver_sql(explicit_run_sequence_insert_with_override)
```

Write focused tests for all four forced-RLS flags and no/wrong-org CRUD, owner/provider/FK substitutions including same-org other account, exact leading-zero external IDs, nullable can_answer/text, rating/checksum/version/paired metadata constraints, observation checksum substitution, immutable evidence/run identity/terminal metadata, duplicate scopedrun/externalreview/ordinal/revision, A→B→A three evidence revisions, incomplete fact commit refusal and final-row success, two-session expected-version update winner. Service authorization/manifest/replay ordering are T4 gates, not replaced by SQL-only tests.

Additional RED→GREEN: canonical normalizer accepts long incompressible valid ASCII/Unicode IDs but ordinary B-tree UNIQUE fails. Both run-source and external-review identity must survive exact insert/read after amendment; two concurrent exact duplicates leave one row and no partial facts. Distinct values must remain distinct. If a digest optimization is used, force equal lookup buckets in a disposable-only test and prove exact comparison permits distinct values; no production collision shortcut or identity normalization.

Chosen minimal physical implementation uses no hash: exact owner-local scan with existing owner-leading indexes and canonical account lock. The two INSERT triggers explicitly require `transaction_isolation = 'read committed'`, otherwise safeSQLSTATE25000 before write; a REPEATABLE READ snapshot could otherwise miss the winner committed during the lock wait. Test RR refusal and RC two-session uniqueness. Document this transaction contract, including synthetic imports; do not mutate account data to manufacture serialization conflicts. No additional isolation restriction on other tables.

- [ ] **Step 2: Run RED using the explicitly safe local PostgreSQL profile.**

```sh
env -i PATH=/usr/local/bin:/usr/bin:/bin PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 ORDERS_TEST_USE_LOCAL_CLUSTER=1 /usr/bin/sandbox-exec -f ../.superpowers/sdd/2026-09-09-review-facts-schema/local-postgres.sb .venv/bin/python -m pytest -q -s tests/test_review_facts_schema.py
```

Record functional failures (missing revision/tables/contracts), not just import errors. Profile denies network except exact Unix maintenance socket and denies .env/.pgpass/.pg_service.conf/.netrc; use freshly allocated random DB/roles with try/finally registered before creation and absence checks. Do not inspect old app data. Retain exact evidence in ignored task report; never print payload text/credentials.

- [ ] **Step 3: Implement minimal self-contained DDL and grants.**

Translate exact spec columns/constraints into op.execute SQL. Use deterministic bounded constraint/index names. Typical owner policy:

```sql
ALTER TABLE review_facts ENABLE ROW LEVEL SECURITY;
ALTER TABLE review_facts FORCE ROW LEVEL SECURITY;
CREATE POLICY review_facts_org ON review_facts
USING (organization_id = NULLIF(current_setting('app.organization_id', true), '')::integer)
WITH CHECK (organization_id = NULLIF(current_setting('app.organization_id', true), '')::integer);
```

Cover all four tables, scoped canonical ownership, scoped circular pointer FKs, immutable guards and final-row deferred invariant. Use generic safe trigger errors without payloads. coverage is JSON object at DB layer; exact application keys/types and safe error-code vocabulary stay T4 typed contract, not invented enumerations. Default source_order_state current is acceptable only for exact explicit owner identity; no placeholder observations. Guard empty-only downgrade under locks and row_security=off, drop circular FKs/triggers in dependency order without CASCADE. No runtime import of candidate/report files.

- [ ] **Step 4: Prove expand and steady-state privileges and full chain.**

Use actual broad owner default grants before upgrading0062→0063 in own disposable DB. Verify broad grantee narrowed and reader not widened, no grant options, sequence setval denied, explicit/override sequence insertion denied, default INSERT returns positive server sequence. Prove same invariants after actual atomic runtime script; missing FORCE RLS aborts without partial privileges. Upgrade empty→latest without stamp, empty0063→0062→0063, populate synthetic0062 Catalog/Orders thenupgrade (old rows unchanged), nonempty+hidden-RLS downgrade refusal leaves version/schema/data. Native credential fixture remains0061 and is not represented as fresh proof.

Actual runtime script now requires0063 objects: give existing Orders integration tests a latest-head fixture for script tests while retaining registered0062 candidate tests/roundtrips. Do not weaken guard/assertions, copy old script or addskip. Test Orders assertions and Review ACLs together under actualscript.

Review graph checks one actual head and registered0063 in its ancestry, not a frozenhead literal. Review feature upgrade→downgrade→upgrade targets0063 explicitly, including version-preservation assertions; actual runtime-script fixture alone follows head. This preserves the same bounded-feature/latest-script separation when later independent migrations arrive.

- [ ] **Step 5: Run GREEN, compile, graph and diff checks; self-review.**

```sh
# Same env scrub and Unix-only sandbox prefix as Step2:
.venv/bin/python -m pytest -q -s tests/test_review_facts_schema.py tests/test_orders_schema_candidate.py tests/test_orders_schema_integration.py tests/test_orders_contract.py
.venv/bin/python -m compileall -q alembic tests/test_review_facts_schema.py tests/test_orders_schema_integration.py
git diff --check
```

Report exact counts/exits, engine/socket and allocated-resource cleanup, no skip, actual head and native/full-suite limits. Verify handoff separates SQL invariants from T4 semantic service gates and no activation.

- [ ] **Step 6: Commit bounded prerequisite and return handoff.**

```sh
git add backend/alembic/versions/20260909_0063_review_facts.py backend/ops/runtime-db-role.sql backend/tests/test_review_facts_schema.py backend/tests/test_orders_schema_integration.py backend/docs/superpowers/reports/2026-09-09-t1-review-facts-schema-handoff.md
git commit -m 'feat: add account-owned review facts schema'
```

Only exact owned paths; leave controller docs/scratch untouched. Controller independently reviews committed diff and verifies. Send T4 exact revision/SHA/runtime INSERT/RETURNING contract immediately after ready; production flags, collectors and send remain unchanged.
