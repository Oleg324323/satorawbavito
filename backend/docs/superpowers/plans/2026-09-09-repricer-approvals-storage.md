# Repricer approvals storage implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Supply the exact account-context and PostgreSQL storage prerequisite for T2's accepted reserve/dispatch contract without changing its domain implementation or activating a writer.

**Architecture:** Add a transaction-local account context helper independently; then install the three mutually dependent relations, immutable byte reconstruction, lifecycle/audit witness constraints and narrow runtime privileges in one atomic forward migration. User authorization remains the separate publication guard; context is never a permission.

**Tech Stack:** Existing Python/SQLAlchemy/pytest, PostgreSQL UTF8/READ COMMITTED/Alembic; built-in `sha256(bytea)`, no new extension or dependency.

**Spec:** `backend/docs/superpowers/specs/2026-09-09-repricer-approvals-storage-design.md`, read fully. Exact domain authority `45f94a39627ebe2916903e9e5e0930844f3daaf5:backend/docs/superpowers/reports/2026-09-09-t2-reserve-dispatch-contract-amendment.md` and its named domain modules, read from local Git only. Golden fixture authority `e15495a89c16422b96811e1ba0ffa06580191a9e:backend/tests/fixtures/wb_repricing_sql_golden_vectors_v1.json`.

## Global constraints

- Only `/Users/bratishka/Downloads/satornawb-main/.worktrees/arch-t1-platform`, branch `codex/arch-t1-platform`. No production, application DBs, real Redis/providers/credentials, secret files, external network, GitHub/push/deploy, flags, keys or host configuration changes.
- T2 retains every domain module/router/task. Do not copy those implementations or their test modules into this branch. The exact synthetic fixture may be copied byte-for-byte via apply_patch and must retain its provenance.
- Tests use own `backend/.venv`, scrubbed environment, secret-file/IP-denying sandbox and authorized Unix-only maintenance connection solely to allocate/clean exact own random disposable DBs/runtime roles. No existing app data/role inspection or server changes. Reuse tracked disposable helpers; native `initdb` limitation must not become a false native-cluster proof or skipped RLS gate.
- Lint-only approved exception: `/tmp/satorna-backend-verify-20260908/bin/python -m ruff check`; never use that environment for application/tests. No dependency install required.
- All owner/catalog/member references are canonical positive INTEGER IDs. Business values/approval versions are finite integral unconstrained NUMERIC, not scale-rounding NUMERIC(p,0), BIGINT or invented caps. Physical references are bounded UUIDs. Free text remains exact C-collated text, never a hash-only identity or B-tree key for unbounded values.
- Exactly three relations: `wb_repricer_price_approvals`, `wb_repricer_price_apply_attempts`, `wb_repricer_price_approval_audit`. Zero/one lifetime attempt; marker write-once; no retry/lease/outbox/cleanup schema. Immutable witnesses and captured transition plus inverse audit validation prove no omitted/ghost events.
- Every mutation locks account before domain tuple locks at READ COMMITTED, checks fresh exact equality after waits, and enforces organization AND account FORCE RLS. Missing/invalid context denies. Auth user/membership/session locks precede this helper's account/domain work in the trusted service, not fabricated by schema.
- New runtime writes are v1 only; privileged legacy import preserves bytes=NULL, checksum/key/status/version/actors with imported audit and no attempts. Runtime cannot grant itself owner/import authority. No real import is performed.
- Runtime/default ACL defense is atomic and confined to the new relations/functions; never rewrite unrelated old/default privileges. No PUBLIC rights on new relations or mutating helpers, no grant options, DELETE/TRUNCATE/audit UPDATE/trigger bypass.
- Fresh sanitized ScriptDirectory currently reports sole head `20260909_0065`. Task2 targets new `20260909_0066`; recheck at dispatch. If another revision exists or head differs, stop that task for controller revision/plan amendment, never silently renumber or rewrite an old revision.
- Historical feature fixture pins0066; current runtime-script fixture independently migrates latest head. Graph gate requires one head and0066 ancestry, not permanent head equality. Downgrade requires all-empty proof under genuine visibility and locks before destructive DDL; no CASCADE or data deletion.

## Task 1: Add exact transaction-local account context

**Files:**
- Modify `backend/app/infra/db.py` only additive helper/error/state cleanup; preserve `set_tenant_context` behavior.
- Create `backend/tests/test_marketplace_account_context.py`.
- Create `backend/tests/test_marketplace_account_context_postgres.py`.
- Create `backend/docs/superpowers/reports/2026-09-09-t1-account-context-handoff.md`.

**Consumes:** SQLAlchemy Session root transaction and current org context; read infra/db fully, actual publication guard contract if now present, and existing disposable test helper before edits.

**Produces:**

```python
class MarketplaceAccountContextError(RuntimeError):
    # str/repr expose only account_context_invalid or account_context_failed.
    code: str

def set_marketplace_account_context(
    session: Session, *, organization_id: int, marketplace_account_id: int
) -> None: ...
```

Require actual Session, active clean root transaction (no nested, pending new/dirty/deleted or ended transaction), PostgreSQL READ COMMITTED, strict positive INT4 IDs excluding bool. Before changing anything read both existing settings without autoflush. Each must be unset/empty or match the requested canonical decimal value; a different/malformed value fails closed. An existing own marker must belong to that root and exact pair; never use a marker to skip actual setting verification. Set both values with parameterized transaction-local set_config, record root/pair under a dedicated session.info key, remove only this helper's state when the root ends. Same-pair repeat is legal; scope switching within the same transaction is denied. SQL failures become a safe error with no chained SQL/value fragments. Caller must roll back after failure. It does not lock account rows, grant permission, commit work or guard arbitrary future caller SQL; publication guard remains responsible for final authorization/context checks.

Inspect both Session nested state and the actual bound Connection's nested state
before context SQL on every call. `Connection.begin_nested()` is not visible to
Session nested hooks; an existing Connection SAVEPOINT must also deny. This is
initial/repeated-call admission, not a claim to prevent arbitrary later raw SQL.
Add a real PostgreSQL negative case plus positive normal Session root control.

Supported lifetime is an Engine-bound Session, as produced by get_session_factory.
Require isinstance(session.get_bind(), Engine) before any context SQL; all
Connection-bound Sessions are unsupported, even if a particular join mode could
commit their external transaction. Never inspect private Session transaction maps
or force-commit/rollback external caller work. Add real PostgreSQL negatives for
an already active external Connection with default conditional_savepoint and
explicit rollback_only: denial occurs before settings/marker mutation, external
transaction and preexisting control data/settings stay unchanged until its owner
rolls it back. Retain ordinary Engine Session commit/rollback/pool positive cases.
Document this conservative public-binding limitation and caller cost.

- [ ] Step1 write focused missing-interface RED and type/transaction canaries.

```python
with Session(engine) as session, session.begin():
    set_marketplace_account_context(session, organization_id=1, marketplace_account_id=11)
    set_marketplace_account_context(session, organization_id=1, marketplace_account_id=11)
    with pytest.raises(MarketplaceAccountContextError, match="account_context_invalid"):
        set_marketplace_account_context(session, organization_id=1, marketplace_account_id=12)
```

Cover booleans, zero/negative/out-of-INT4/string IDs, absent/ended/nested transaction, wrong dialect/isolation, dirty ORM state, conflicting preexisting org/account settings and raw setting tamper between helper calls. Do not interpret missing fixture/setup/import dependency as a behavior regression; record the expected missing helper separately.

- [ ] Step2 run RED under offline sandbox. Implement the additive helper with explicit safe codes and per-session root cleanup; do not modify existing tenant helper or use a global hook to affect sessions without this helper.

```python
session.execute(text("SELECT set_config('app.organization_id', :org, true), "
                     "set_config('app.marketplace_account_id', :account, true)"),
                {"org": str(organization_id), "account": str(marketplace_account_id)})
```

- [ ] Step3 realPG GREEN: actual local settings, same-pair idempotence, conflicting context not overwritten, commit/rollback clear values and marker, pooled connection reborrow has no authority, second root can choose another pair only with fresh valid context. Verify preexisting old set_tenant_context semantics remain unchanged and compatibility with the new publication guard (if it exists) without claiming context alone authenticates a caller.

```sh
.venv/bin/python -m pytest -q tests/test_marketplace_account_context.py tests/test_marketplace_account_context_postgres.py
.venv/bin/python -m compileall -q app/infra/db.py tests/test_marketplace_account_context.py tests/test_marketplace_account_context_postgres.py
git diff --check
```

Run adjacent tenant/publication tests touching the exact helper once, not the whole legacy suite repeatedly. Record commands, exit codes, natural exit, own resources cleanup and redaction canary results.

- [ ] Step4 self-review, bounded commit `feat: add transaction-local marketplace account context`, exact handoff; independent controller review before Task2.

## Task 2: Install approvals, attempt and audit storage atomically

**Files:**
- Create `backend/alembic/versions/20260909_0066_repricer_approvals.py`.
- Modify `backend/ops/runtime-db-role.sql` only new relation/function overrides after broad grants within its existing transaction.
- Create `backend/tests/test_repricer_approvals_schema.py` (disposable fixtures, migration/serialization/numeric/graph gates).
- Create `backend/tests/test_repricer_approvals_lifecycle.py` (exact lifecycle/audit/CAS/race gates, import shared fixtures).
- Create `backend/tests/test_repricer_approvals_rls.py` (actual runtime/default/column/PUBLIC ACL, context/scope/downgrade gates).
- Create `backend/tests/fixtures/wb_repricing_sql_golden_vectors_v1.json` exactly from e15495a, no fixture editing to match SQL.
- Create `backend/docs/superpowers/reports/2026-09-09-t1-repricer-approvals-schema-handoff.md`.

**Consumes:** accepted three-relation spec and exact T2 request plus literal fixture; Task1 `set_marketplace_account_context`; canonical account/catalog/membership tables, actual head0065 and runtime grants. Existing domain code is read-only local Git evidence. No repository implementation is included.

**Produces:** forward0066 after0065, all spec tables/fields/FKs/checks/indexes/triggers/ACLs; installed helper names and exact SQL parameter order documented in handoff for T2. Fixed helper interfaces:

```text
repricer_exact_text(text) -> boolean
repricer_integral_finite(numeric) -> boolean
repricer_safe_code(text) -> boolean
repricer_ascii_json_string(text) -> text
repricer_integer_decimal(numeric) -> text
repricer_request_bytes(integer, integer, text, integer, numeric, text,
                       numeric, smallint, numeric, numeric) -> bytea
# positional: org, account, approval ID, catalog SKU, nm ID, article ID,
# recommended price, discount, size ID, minimum price.
repricer_action_key(integer, integer, text, text) -> text
# org, account, approval ID, request checksum; account serialized as JSON STRING.
repricer_dispatch_key(integer, integer, text, text, uuid) -> text
# org, account, approval ID, action key, attempt ID; account is JSON NUMBER.
```

Pure validation/serialization helpers may be invoked by scoped runtime checks; no mutation helper is SECURITY DEFINER. Use immutable helpers with qualified fixed search_path, VOLATILE trigger routines. A safe context helper for RLS can be dedicated to this migration and must turn absent/empty/noncanonical/out-of-INT4 settings into denial, never default scope. Runtime role grants only the required helper execution; PUBLIC mutating/trigger execution is revoked. Privileged import is restricted to the current table owner (catalog role identity), not a caller-controlled GUC; it still requires exact scoped context and all witnesses/constraints. No new global role, inherited-owner membership or production grant is created.

- [ ] Step1 before DDL, copy only literal golden fixture through apply_patch and verify byte parity with local e15495a. Write tests that call the new SQL helpers and query new relations, then run RED on a disposable0065 DB. Distinguish expected undefined_table/function failures from real boundary characterization: plain PostgreSQL INTEGER/BIGINT cannot store the accepted >BIGINT values, raw full-text B-tree indexes cannot store incompressible long accepted IDs. Existing0065 is not rewritten to manufacture a regression.

```python
fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
for vector in fixture["vectors"]:
    expected = vector["canonical_request_ascii"].encode("ascii")
    actual = connection.execute(text("SELECT public.repricer_request_bytes("
        ":org,:account,:approval,:catalog,:nm,:article,:price,"
        "CAST(:discount AS smallint),:size,:minimum)"), params(vector)).scalar_one()
    assert bytes(actual) == expected
    assert sql_request_hash(actual) == vector["request_checksum"]
    assert sql_action_key(vector) == vector["action_key"]
    assert sql_dispatch_key(vector) == vector["dispatch_key"]
```

Test adapters `params`, `sql_request_hash`, `sql_action_key`, `sql_dispatch_key` belong only to test_repricer_approvals_schema.py and implement the exact positional signatures above. Do not import absent T2 modules to recompute the expected bytes; these are independently produced literal vectors, not a claim that T2 serializer executed in T1. Additional test expectations may use Python standard json with explicit exact input and must not overwrite golden literals.

- [ ] Step2 implement pure SQL helpers then exact relations/checks/scoped FKs. Add cyclic deferred witness FKs only after all tables exist. Enforce spec matrices, numeric finite/integral semantics, immutable creation/kernel intent and required NULLs, exact reconstructed v1 bytes1..4096, SHA/checksum/action/dispatch equality. Do not compare jsonb::text or merely trust a supplied hash. Keep unbounded text/NUMERIC out of B-tree keys; retain UNIQUE(org,account,action_key) as the requested intent key and bounded UUID targets.

Eight new attempt error codes are exactly `INTERNAL_APPLY_ERROR`, `WB_APPLY_AUTHORIZATION_FAILED`, `WB_APPLY_RATE_LIMITED`, `WB_APPLY_REJECTED`, `WB_APPLY_TIMEOUT`, `WB_APPLY_TRANSPORT_ERROR`, `WB_APPLY_VALIDATION_FAILED`, `WB_RESULT_UNAVAILABLE`. Legacy reason/error/result grammar remains `[A-Za-z][A-Za-z0-9_]{0,127}`; do not narrow accepted imported codes. Post-marker failed allows only WB_APPLY_REJECTED, not every eight-code value.

- [ ] Step3 implement statement-before-tuple account locking and fresh exact logical uniqueness, BEFORE immutable/lifecycle checks, deferred captured OLD/NEW witness validation, inverse audit validation and final-state alignment. Test concrete committed prefixes and multi-step transactions:

```text
new pending V0 + created audit
pending V0 -> applying V1 + claimed audit (no attempt yet is valid)
reserve A0 with frozen claim V1 + reserved audit (approval unchanged)
dispatch A1 + dispatched audit (approval unchanged)
terminal A2 + approval V2 + one SHARED outcome audit
```

Each omitted/ghost/misbound audit, metadata-only witness fill, actor/time/version/result mismatch and duplicate event must reject at commit. A valid claim+reserve+dispatch+outcome in one transaction must validate captured intermediate changes, not reinterpret every deferred event as the final row. Legacy imports preserve terminal/applying states with no attempt and imported witness; runtime cannot use that route.

- [ ] Step4 realPG two-session race proof: equal long approval ID, two claim CAS, two reserves, marker versus marker, pre-marker fail versus marker. Use events/barriers and actual lock observations limited to the disposable DB. Account wait must precede tuple mutation and subsequent exact check must see the committed winner at READ COMMITTED. Different long IDs and >BIGINT business/version values round-trip exactly; non-RC admission denies. Zero-row CAS/exact replay creates no version or audit; marker replay never yields send permission. No actual provider call is made.

- [ ] Step5 install FORCE RLS org+account policies and atomic ACL intersection for all existing new-table relacl/column ACL grantees, revoke grant options/PUBLIC rights, retain only allowed inherited operations. Runtime script overrides the same new objects after broad grants; no global/default ACL change. Test actual non-owner NOSUPERUSER/NOBYPASSRLS role: missing/wrong org/account SELECT/INSERT/UPDATE/DELETE, same-org foreign-account references, foreign membership/catalog, scoped FK mismatch, arbitrary import, forbidden mutation/delete/truncate/setval/trigger/policy/schema/role privileges. Current script must run against a separate latest-head fixture; pre-script expansion proves broad defaults cannot leave a temporary bypass.

- [ ] Step6 migration acceptance: empty bootstrap without stamp;0065 synthetic production-shaped schema upgrade preserves old tables/data/defaults/ACLs;0066 empty downgrade→upgrade; sole-head ancestor gate plus synthetic future-head regression. Downgrade locks all three tables before all-row counts, sets row_security=off only as refusal defense, and requires genuine visibility. Nonempty and RLS-hidden rows refuse atomically with unchanged schema/data; no CASCADE or deletion workaround. All runtime failure paths are safe fixed codes, no body/ID/request fragments in custom exceptions or generic audit.

```sh
.venv/bin/python -m pytest -q tests/test_marketplace_account_context.py tests/test_marketplace_account_context_postgres.py tests/test_repricer_approvals_schema.py tests/test_repricer_approvals_lifecycle.py tests/test_repricer_approvals_rls.py
.venv/bin/python -m pytest -q tests/test_orders_exact_text_migration.py tests/test_orders_schema_candidate.py tests/test_orders_schema_integration.py tests/test_orders_contract.py tests/test_review_facts_schema.py tests/test_review_lossless_migration.py tests/test_review_lossless_rls.py
.venv/bin/python -m compileall -q app/infra/db.py alembic/versions/20260909_0066_repricer_approvals.py tests/test_repricer_approvals_schema.py tests/test_repricer_approvals_lifecycle.py tests/test_repricer_approvals_rls.py
git diff --check
```

All commands above run with the authorized scrubbed Unix-only test wrapper, not ambient environment. Record exact commands/exits/counts/natural shutdown and exact DB/role absence checks. Run scoped Ruff through the approved lint-only interpreter, not an unavailable tool claim. No skip/baseline expansion.

For the new-table default-ACL intersection fixture, establish valid previous0065
first, then install broad defaults before upgrading only0066. Review0065 explicitly
rejects preexisting PUBLIC writes to its old relations; that earlier documented
operating precondition is not a0066 test failure or reason to rewrite old migrations.
Empty bootstrap uses normal authorized defaults. Preserve/recheck all old ACLs;
new-table narrowing must not change old Review/Orders/default privilege state.

- [ ] Step7 self-review and commit `feat: add account-owned repricer approval storage`. Handoff physical columns/helper signatures, owner/import/context/lock/witness insert ordering, removed assumptions and exact tests. Independent controller review precedes ready delivery to T2; repository/authenticated dispatch/provider proof remains T2/shared integration work, not claimed by DDL.

## Preflight decisions and execution order

Task1 produces account context, Task2 consumes it; neither rewrites the user publication guard or permission policy. Task2 relation cycles are atomic and reviewed together, not partially committed tables lacking audit invariants. This plan follows completed publication-guard implementation; source-text forward migration remains an independent required queue entry and may cause an explicit revision amendment before Task2 dispatch. There is no reservation against another valid committed migration.

Spec coverage: sections1/3 → helpers and byte/numeric tests;2/4/5/6 → relations, witnesses/lifecycle/races;7 → statement lock and exact long keys;8 → Task1/RLS/ACL/import;9 → locked downgrade;10 → full acceptance matrix. Golden parity covers actual producer output, not provider action evidence. Context remains necessary but insufficient for authorization. The final readiness report must retain production activation, external evidence, retention and full integration limitations.
