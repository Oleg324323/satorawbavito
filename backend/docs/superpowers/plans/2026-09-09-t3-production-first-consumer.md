# T3 Production First Consumer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans. Follow TDD and verification-before-completion; execute inline in T3, not in a competing writer.

**Goal:** Provide dormant, authenticated account-scoped Production creation/read/manual-assignment services using accepted P1 storage.

**Architecture:** Own clean root transactions with shared physical publication guard and account-local RLS context. Reuse existing Orders immutable provenance and pure Production command/result codecs. No router, worker, scheduler or Catalog copy.

**Tech Stack:** Existing SQLAlchemy Session/text, PostgreSQL, dataclasses and pytest; no dependency changes.

**Spec:** `backend/docs/superpowers/specs/2026-09-09-production-assignment-storage-design.md`; `backend/docs/superpowers/reports/2026-09-09-production-first-writer-amendment.md` including root-approved permission section; exact T1 READY handoff must be read before implementation.

## Global Constraints

- T3 worktree only; leave `app/modules/orders.py` pure and unchanged. New persistence goes in `app/orders/`.
- T1 owns migrations/shared ORM/config/permission recognition/registration. Do not infer READY from a candidate file or reserve a revision number.
- No providers, production, actual exports/printing, user JSON changes, KIZ, flags, push or deployment.
- Heavy tests require explicit single-owner resource admission; natural cleanup, no timeout relaxation or hidden skips.
- No unassign, source refresh, planning, batches or automatic readiness in P1.
- Fixed server-selected read/create/assign capabilities; read also required for both commands and all replays. No profile/alias substitution, automatic grants or body actor.
- Source binding is not provider completeness or A-to-B-to-A epoch proof. Unbound/changed source is denied, never repaired from current metadata.

## Admission

- [ ] Read exact committed T1 READY feature/fix handoff and shared permission implementation. Verify lineage/sole head and preserve existing dirty files. Merge accepted lineage only.
- [ ] Compare actual column names, generated IDs/remaining quantity, request/result schema versions, reciprocal witness constraints, RLS helper API, runtime ACLs and downgrade guards against the accepted design. Stop on a contract difference, not by editing T1 schema.
- [ ] Select current-schema disposable fixture with actual runtime-role script. Keep historical migration fixtures historical; no current-role script against an older incompatible schema.

## Task 1: Guarded Read and Initial Creation

**Files:** Create `backend/app/orders/production_service.py`, `backend/tests/test_production_service.py`; update T3 progress report.

**Consumes:** `UserSessionPrincipal`, `ExpectedAccountBinding`, `acquire_publication_guard`, `set_marketplace_account_context`, `validate_run_binding`, normalized observation decoder and immutable P1 source fields.

T1 proposed shared imports, consume only after its exact READY commit:
`from app.cabinet.permissions import PRODUCTION_READ_PERMISSIONS,
PRODUCTION_CREATE_PERMISSIONS, PRODUCTION_ASSIGN_PERMISSIONS`. Do not duplicate
the registry locally. Shared guard additionally rejects create/assign requirement
sets missing read before SQL; actual profile grants remain unchanged.

**Produces:** `read_production_work_item(session, *, principal, account, work_item_id)` and `create_production_work_item(session, *, principal, account, order_item_id, expected_source_item_version)`. Define frozen internal outcomes in this file; no browser numeric contract is implied. Creation returns stable work-item ID plus replay indication, not a fabricated original receipt (P1 has no creation-receipt table).

- [ ] Add missing-service RED with real disposable storage and synthetic explicit grants. Exercise production:read only, create+read, admin/profile without explicit production grants, wrong org, second account in same org and revoked session.
- [ ] Require a clean Engine-bound root READ COMMITTED Session. Acquire fixed capabilities/live guard before any receipt/work-item lookup; set exact org/account transaction-local context via shared helper. Reject non-integer/out-of-range identifiers before SQL without coercion.
- [ ] Under retained account lock, read scoped source item, its parent current run, membership and normalized observation. Lock source item/parent/run in consistent order. Require complete current manifest, exact immutable run binding, coherent source identity/quantity and expected source version. Missing membership, unbound run, rebind and changed source versions deny without creating rows.
- [ ] For creation, exact `(org,account,order_item_id)` lookup is the existing identity. New row uses DB defaults, version1, planned0, null SKU/witness and current positive source quantity/version. Existing identity must still pass live source checks before returning its ID; no quantity refresh or resetting assignment state.
- [ ] Read checks fixed read capability and scope before returning stored state; never treats current account metadata alone as historical source authority. Fields stay Python integers; later HTTP must encode BIGINT safely.
- [ ] Record authenticated create/replay audit actions in the same guarded transaction without mutating existing work-item/assignment history on replay. Audit logging is not a replacement for P1 witnesses.
- [ ] Test rollback after final authorization failure, account rebind before request, source change, unbound history and two actual sessions racing creation. Assert one work item, no guessed status eligibility and no source mutation.

Expected public transaction shape:

```python
with session.begin():
    guard = acquire_publication_guard(
        session, principal=principal, accounts=(account,), authorities=(),
        required_permissions=PRODUCTION_CREATE_PERMISSIONS,
    )
    # Shared context helper, exact source proof and scoped DML occur here.
    guard.revalidate_before_write()
# Return only after successful physical commit.
```

This sketch fixes transaction ownership, not a substitute for actual source proof.

## Task 2: Assignment, Exact Replay and Atomic Witnesses

**Files:** Extend `backend/app/orders/production_service.py` and `backend/tests/test_production_service.py`.

**Consumes:** `AssignmentCommand`, `serialize_assignment_command`, `assignment_command_checksum`, `deserialize_assignment_result`; actual P1 receipt/history DDL.

**Produces:** `assign_production_work_item(session, *, principal, account, command)` returning an `AssignmentResult` plus replay indication only after commit. Actor is current authenticated membership; no supplied actor/timestamp/permission fields.

- [ ] RED: first valid assignment with expected version1, original-result replay after a second assignment, same-key changed payload, stale version, wrong-account target/SKU and revoked rights. Test direct helper with fake request privileges cannot broaden fixed requirements.
- [ ] Acquire live read+assign guard, exact account context and current source proof before receipt lookup. Lock work item and compare current source version with its immutable source version; mismatch denies even on replay.
- [ ] Lookup exact full idempotency text scoped by org/account/work item. For an existing receipt, compare canonical bytes/checksum and strict result version/payload; same key/different command raises conflict. Return original immutable receipt result, not current work-item state. Replay still passes current authorization and source proof.
- [ ] For a new command, enforce CAS version, positive scoped Catalog SKU and physical version bounds. Obtain one DB clock after locks. Allocate receipt identity using the accepted generated-ID protocol, build exact seven-field result and capture actor/reason consistently across receipt, history and work-item witness update. Follow actual deferred FK ordering, never disable constraints.
- [ ] Use exact equal command time across transition/receipt/history. Commit all effects atomically with final shared guard revalidation. On SQL/deferred/commit error, rollback and return sanitized domain error without raw key/reason or SQL details.
- [ ] Actual two-connection/PID tests: same expected version/different keys yields one success; same key/same payload yields one transition and original replay; same key/different payload conflicts. Observe lock waiting rather than using sequential calls as concurrency proof.
- [ ] Verify no history/witness mutation on replay; separate authenticated replay audit must not become an assignment event. Denial writes nothing. Final callback revocation and physical pre-commit fault roll back all new effects.

## Verification and Handoff

- [ ] After resource admission run only `tests/test_production_service.py` first, stop on real failure, preserve natural cleanup.
- [ ] Run focused pure Production codecs plus affected Orders publication/assembly and exact T1 P1 schema tests serially in separately approved interval. No full-backend expansion.
- [ ] Scoped Ruff, compileall, `git diff --check`, sole Alembic head and Git integrity. Review actual source/auth/replay paths independently before commit.
- [ ] Commit own files; update current matrix with exact commits, tests/exit codes, source/permission gates and expanded-schema rollback limits. Send T1/T4 coherent handoff, not per-edit updates.

No complete Production workflow, browser token-only ingestion, operator readiness,
unassign, frozen sheet/artifact or cutover claim follows from these two tasks.
