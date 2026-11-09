# Production assignment storage implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. TDD and verification-before-completion apply; steps use checkboxes.

**Goal:** Add dormant, account-scoped work-item creation and provable manual SKU assignment storage.
**Architecture:** Three relations with captured OLD/NEW deferred witnesses, inverse continuous-history validation and exact account-serialized idempotency. No domain service or public writer is introduced.
**Tech Stack:** Existing PostgreSQL16/Alembic/SQLAlchemy/pytest and stdlib; own backend/.venv, no dependencies.
**Spec:** `backend/docs/superpowers/specs/2026-09-09-production-assignment-storage-design.md`, read fully. Exact T3 authority8a8722f+0aa2c48 and codec72dfacc are cited there.

## Global Constraints

- Only `/Users/bratishka/Downloads/satornawb-main/.worktrees/arch-t1-platform`; no domain Python/ORM/router/task/frontend edits or copying T3 implementation.
- No production/app DB, existing application roles/data, working Redis, providers/network/real credentials/.env, keys/flags, backfill, push/deploy or host configuration changes.
- Only fresh random own databases/runtime roles via existing `tests.test_orders_schema_candidate` allocator. Maintenance postgres only creates/drops/checks exact owned resources over `/private/tmp/.s.PGSQL.5432`; no existing app queries.
- Own backend/.venv; env-i PATH=/usr/local/bin:/usr/bin:/bin, PGPASSFILE/PGSERVICEFILE/NETRC=/dev/null, PYTEST_DISABLE_PLUGIN_AUTOLOAD=1, ORDERS_TEST_USE_LOCAL_CLUSTER=1, plan-owned secret/IP-denying Unix-only sandbox. Alternate Ruff interpreter is lint-only. No skips/xfails/baseline expansion or SQLite RLS substitute.
- Controller amendment after accepted Review binding4d95b24b549f0b1f773095ec583a85f18172fdbc: actual sole head is20260909_0068. Allocate20260909_0069 ONLY if that remains the sole head and0069 is absent at dispatch. If either differs, stop this task for controller graph reconciliation; no silent renumbering. This explicitly supersedes the original conditional0068 candidate, not a reservation against another accepted schema request.
- Heavy PostgreSQL/test/build/browser gates now use the coordinator's single team slot. Read/plan work may proceed; before running RED or any later heavy gate obtain controller confirmation of slot ownership. Do not start a concurrent heavy gate, change timeouts, silently retry a failed full gate, or signal another task's process. Preserve natural exit and owned-resource cleanup evidence.
- No older migration edits. All three new tables FORCE org+account RLS; context is not authorization. Production permission remains an explicit activation gate, never invented here.
- Exactly creation and manual assignment; no source refresh/unassign/planned-quantity changes, automatic mapping, print/artifact/calendar/provider or scheduler behavior.
- Runtime service must separately prove immutable Orders item→parent current run binding, exact live owner and source-version coherence before creation/assignment/replay. Unbound/mismatched history fails closed; this physical schema is not that authentication/provenance service.

## Task 1: Implement the three-relation storage prerequisite

**Files:**
- Create `backend/alembic/versions/20260909_0069_production_assignment.py`.
- Modify `backend/ops/runtime-db-role.sql` only new table/sequence/helper overrides and FORCE-RLS preflight entries.
- Create `backend/tests/test_production_assignment_schema.py`.
- Create `backend/tests/test_production_assignment_lifecycle.py`.
- Create `backend/tests/test_production_assignment_rls.py`.
- Create `backend/docs/superpowers/reports/2026-09-09-t1-production-assignment-schema-handoff.md`.
- Modify `backend/tests/test_orders_schema_integration.py` only the existing credential-fixture revision regression and its focused negative controls, as the controller amendment below specifies.

### Controller amendment: repair inherited fixture-admission regression

The mandatory adjacent gate produced438PASS/1FAIL on this candidate. Both the failing Orders test and credential fixture are byte-identical to review BASEf563d8a. Earlier accepted53e0549 extracted the actual0060→0061→0060→0061 bootstrap into `_bootstrap_postgres`; the old AST assertion still walks only `disposable_postgres` and sees zero upgrade calls. This is a pre-existing test regression, not permission to waive the required gate.

Keep migration0069, runtime grants and credential fixture unchanged. In the existing Orders test, validate the actual fixture dispatch to local/native generators, their calls to the shared bootstrap, and the bootstrap's exact pinned upgrade/downgrade sequence. Preserve failure when a reachable upgrade target becomes `head` or a wrong revision, or when either generator bypasses the bootstrap; do not pass by finding disconnected literals anywhere in the file. Reproduce the current focused failure before editing, then add focused negative controls and run the repaired pure test group without PostgreSQL. This is the sole additional write path; no broad test cleanup. The original failed adjacent run remains evidence, and the full seven-file adjacent gate must pass in a later explicitly allocated heavy slot before READY. One bounded implementation commit includes this test repair; controller review BASE remainsf563d8a and includes this visible docs amendment.

**Consumes:** accepted spec; existing0062 Orders scoped FK/quantity/version and Catalog/member anchors,0064 account-first discipline, accepted account context2408839 and physical guard8338ef7 as consumer requirements; candidate.cluster/disposable_database/migrate; latest-head runtime script fixture from test_orders_schema_integration. Source codec and exact literal test:
`72dfaccb9e6190234c025f3c96bcfef400ce43b0:backend/app/modules/production.py` and `backend/tests/test_production_assignment_serialization.py` (read only).

**Produces:** production_work_items, production_assignment_receipts, production_assignment_history with exact spec columns/constraints and no extra business states. Immutable fixed-search-path SECURITY INVOKER helpers:

```text
public.production_exact_text(text)->boolean
public.production_ascii_json_string(text)->text
public.production_assignment_bytes(bigint,bigint,integer,text,text)->bytea
# work_item_id,expected_version,catalog_sku_id,idempotency_key,reason
```

- [ ] Read full spec and exact source request/amendment/codec, actual Orders0062 quantity/version/identity guards and parent FK, existing allocator and ACL/downgrade examples. Confirm clean owned paths, exact head and0069 absence before writing. Tests must not import/copy T3 domain code. T3 actual0067 consumer33c95c17+decoder8010112 is accepted as a domain handoff, not a reason to import/copy its implementation here; P1 source-chain/permission acceptance stays separate.
- [ ] Write initial tests against previous0068 before any new DDL. Real PostgreSQL invocation of new helper and new table INSERT must fail UndefinedFunction/UndefinedTable, with an existing Orders unbound positive control. Record command/exit and setup success separately; fixture/import failures are not RED. Then write the remaining invariant tests before implementation.

```python
GOLDEN = b'{"command":{"catalog_sku_id":3,"expected_version":2,"idempotency_key":"synthetic-key","reason":"synthetic-reason","work_item_id":1},"schema_version":1}'
with owner.connect() as c:
    actual, sql_checksum = c.execute(text("SELECT payload, encode(sha256(payload),'hex') FROM (SELECT public.production_assignment_bytes(1,2,3,'synthetic-key','synthetic-reason') AS payload) AS generated")).one()
assert bytes(actual) == GOLDEN
assert sql_checksum == hashlib.sha256(GOLDEN).hexdigest()
```

Additional expectations use Python stdlib JSON independently, not the SQL serializer. Cover edge whitespace (full Python strip set), interior controls, quote/backslash, combining/supplementary Unicode, DEL and exact long distinct/equal text. NUL/unpaired surrogates reject at supported boundary with native client/TEXT restrictions disclosed.
- [ ] Create exact model from spec. Positive generated-always BIGINT physical IDs; existing org/account/member/SKU positive INTEGER; version/source versions BIGINT; quantity INTEGER. TEXT C exact nonempty key/reason with no invented length cap; no text-bearing B-tree unique. Generated remaining quantity, scoped FK targets and nullable org-owned SKU preserved. JSONB request exactly five fields and result exactly seven; BYTEA canonical metadata request with lower-case SHA256 and exact reconstructed-byte equality.

```sql
-- These are different invariants, not aliases for a digest comparison:
CHECK (request_schema_version=1 AND result_schema_version=1),
CHECK (canonical_request_bytes = public.production_assignment_bytes(
  work_item_id, result_version-1, (request_payload->>'catalog_sku_id')::integer,
  idempotency_key, request_payload->>'reason'))
```

Before the CHECK's casts, a BEFORE INSERT validator must check object key sets, JSON types and retained numeric representation/width/range bounds; reject bool/string/retained fractional forms/NULL, require exact request expected=result_version-1 and exact work item/key. Canonical BYTEA requires decimal integer tokens and rejects exponent/fraction spelling even when JSONB is equivalent. JSONB normalizes some exponent tokens before triggers can inspect them: add native characterization of 1e0 versus retained 1.0, and do not claim original lexical rejection from JSONB alone (including result_payload). Raw lexical admission is the trusted decoder's obligation. Do not depend on SQL CHECK evaluation order for safe parsing. No rounding/coercion in validators. Trigger helpers cannot be runtime callable mutation APIs.
- [ ] Implement account-first BEFORE STATEMENT locks for all new-table mutations before tuple locks. Require exact canonical positive org/account context, READ COMMITTED and successful account lookup; missing account lock must reject immediately before any later fresh read (the0067 I1 lesson). Provider belongs to canonical account; both WB/Avito allowed, no fake Catalog account ownership.
- [ ] Creation BEFORE INSERT enforces version1/SKU NULL/planned0/witness NULL, obtains exact source Orders item FOR SHARE after account lock, and validates current positive quantity/version together. Captured deferred INSERT witness repeats comparison at commit; same-transaction source mutation fails, concurrent source mutation waits or yields coherent conflict. Source/identity/quantity/created fields become immutable. No invented order-status readiness predicate.
- [ ] Manual UPDATE changes only SKU/version/time/current receipt witness, version exactly OLD+1, non-NULL new SKU and distinct new witness. Same SKU with a new command is still a version transition. Validate captured OLD/NEW against exact receipt/history at deferred commit, including all seven result values, actor/reason/target and equal command time. Initial creation has no assignment receipt; update cannot commit without one.

```sql
-- Captured rows belong to each event, not only the final current row:
IF NEW.version <> OLD.version + 1
   OR NEW.catalog_sku_id IS NULL
   OR NEW.current_assignment_receipt_id IS NULL
   OR NEW.current_assignment_receipt_id IS NOT DISTINCT FROM OLD.current_assignment_receipt_id
THEN RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='production_assignment_invalid';
END IF;
```

Timestamp protocol: trusted consumer obtains DB clock once after guard/locks and uses that exact finite instant for its work-item mutation/receipt/history. Do not take a caller body timestamp or invent clock-skew tolerance. Schema proves finite/order/equality, not the provenance/authenticity of an arbitrary SQL caller's supplied time. Use DB clock defaults for creation; tests use actual DB time after acquiring locks. A rollback/clock regression may fail closed, never fabricate a future time.
- [ ] Reciprocal receipt/history validators prove no ghost history and every actual transition. For each current work item require unique result versions2..V, countV-1, exact one history per receipt and vice versa, initial previous SKU NULL, adjacent previous/new SKU chain, frozen source/quantities across results and final witness atV. Compare every captured transition separately so valid multi-command transactions pass. Parent/children queue final validation; no private xmin/GUC token shortcut or audit-only claim.
- [ ] Enforce exact scoped idempotency with retained account lock, fresh READ COMMITTED full equality and deferred duplicate rejection. Bounded owner/workitem indexes only; no digest identity or truncation. Test DB-backed test participant using SQL: authorize/context simulated only as test setup, account lock, exact key lookup, old receipt replay before current-version check, new CAS and atomic receipt/history. It is not a new application service or live permission proof.

```python
# Required test outcomes, with separate real sessions and observed blocking:
assert same_version_different_keys == (1, 1)  # one committed CAS, one safe conflict
assert same_key_same_body_counts == (1, 1, 1)  # one transition/receipt/history
assert replay_result == original_frozen_result  # even after a later valid command
assert changed_body_replay_commits == 0
```

Use actual fixture/SQL helpers local to these three tests, concrete safe exception codes and exact post-transaction row assertions. Include long-key same/different-body races, stale version, other org/account/actor/SKU, missing/extra/ghost/history/result/version/time witnesses, multiple valid transitions and replays with no writes. Inject audit/deferred/physical pre-COMMIT failure and prove all three effects rollback; do not claim unknown network COMMIT recovery from an injected failure.
- [ ] FORCE org AND account RLS on all3: no-context/malformed/wrongorg/wrongaccount SELECT empty and INSERT/UPDATE/DELETE denied with positive controls. Privileges independently checked so ACL denial is not passed off as RLS proof. Runtime nonowner/NOSUPERUSER/NOBYPASSRLS; workitems SELECT/INSERT/UPDATE, receipts/history SELECT/INSERT, no DELETE/TRUNCATE/REFERENCES/TRIGGER/schemaCREATE/ownerSETROLE/grantoptions. Identity sequences USAGE only, setval denied. No authentication derived from GUC/actor FK.
- [ ] Narrow only NEW table/column/sequence/function inherited grants to allowed intersection, PUBLIC none/no grant options, limited readers not promoted to writers. Revoke trigger-only helper execution, grant only needed pure helper rights to entitled grantees. Preserve exact old/default ACL snapshots; runtime script new overrides/preflight only, actual script failure must rollback its changes. Test broad defaults, limited-column grants and sequence defaults independently after previous valid head.
- [ ] Downgrade locks all3 in fixed order ACCESS EXCLUSIVE, sets row_security=off as refusal defense and checks truly unfiltered emptiness BEFORE DDL. Visible rows and FORCE-RLS-hidden data each refuse; no CASCADE/delete/truncate to pass. Empty downgrade removes named cyclic constraints/objects in dependency order. Expanded-schema rollback with dormant old binary is documented; no erasure of history.
- [ ] Migration tests separately prove empty chain without stamp and previous-head production-shaped synthetic preservation through upgrade→empty-new-tables downgrade→upgrade. Seed representative old Orders and Reviews (including0065 lossless bytes), retain exact row/ACL snapshots across each step; every asserted seeded relation must be nonempty. Keep historical feature fixture pinned0069, runtime-script fixture follows actual latest head. Graph test accepts one synthetic future successor, not permanently0069-as-globalhead.
- [ ] Run all three files with natural exit under exact scrubbed Unix sandbox, then once adjacent Orders/Reviews/repricer migration gates (select the concrete files below, no full backend expansion):

```sh
.venv/bin/python -m pytest -q -s --tb=short tests/test_production_assignment_schema.py tests/test_production_assignment_lifecycle.py tests/test_production_assignment_rls.py
.venv/bin/python -m pytest -q -s --tb=short tests/test_orders_run_binding_migration.py tests/test_orders_run_binding_rls.py tests/test_orders_schema_integration.py tests/test_review_lossless_migration.py tests/test_repricer_approvals_schema.py tests/test_repricer_approvals_lifecycle.py tests/test_repricer_approvals_rls.py
.venv/bin/python -m compileall -q alembic/versions/20260909_0069_production_assignment.py tests/test_production_assignment_schema.py tests/test_production_assignment_lifecycle.py tests/test_production_assignment_rls.py
.venv/bin/python -m alembic heads
git diff --check
```

All listed adjacent filenames were confirmed in the current checkout before plan completion. Report later removal/rename to controller, not silent omission. Scoped Ruff new Python; exact own resource cleanup/absence for success and fault paths. `/dev/null` passfile warnings remain disclosed, no real password-file access. No app/provider/production actions.
- [ ] Self-review exact accepted spec and amended seven-path implementation scope; commit `feat: add scoped Production assignment storage`, report RED/GREEN/commands/exit/cleanup/solehead/DDL API/caller lock-clock-source obligations and unselected permission gates. Independent task review before exact READY T3. Do not implement domain writer or next migration.

## Controller preflight

| Interface | Decision and verification obligation |
| --- | --- |
| exact codec→SQL/BYTEA/JSONB | Fixed72dfacc literal + independent standard-json cases; no T3 code copy or coercion |
| creation→Orders mutable source | Account then exact source SHARE lock, captured deferred consistency; immutable historical owner remains consumer gate |
| mutation→receipt/history | Captured each transition plus inverse continuous chain, valid multi-transition accepted/no ghost |
| long key→lookup | Account-serialized fresh exact equality, no unbounded text B-tree or hash identity |
| runtime grants→RLS | Separate operation rights and dual-context predicates, old/default ACL preservation, sequences USAGE only |
| downgrade→old histories | Allnewtables locked/empty/visible, old rows and ACLs preserved, no destructive bypass |
| code consumer→clock/auth | One trusted DB instant afterlocks, schema validates coherence not user/time provenance; permission remains owner gate |

No other task shares these six write paths during execution. Current queued test-only
harness/package/report gates may run before this schema task; they do not reserve a
revision. Actual head and clean owned scope must be rechecked at dispatch.
