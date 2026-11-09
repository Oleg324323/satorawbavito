# Review immutable run binding implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans; test-driven-development and verification-before-completion.

**Goal:** Persist exact external-cabinet provenance on new Review runs without relabeling history.
**Architecture:** Five additive nullable fields become immutable from INSERT, with exact Review object bytes/hash, account-first READ COMMITTED validation and narrow existing-writer grants. T4 owns every consumer.
**Tech Stack:** Existing Alembic/PostgreSQL16/SQLAlchemy/pytest, own backend/.venv and stdlib; no dependencies.
**Spec:** backend/docs/superpowers/specs/2026-09-09-review-run-binding-design.md at1aaf199, fully read. T4 reviewed direction against request1a3d344/codec45e2ccb; that is not DDL readiness.

## Global constraints

- T1 worktree only /Users/bratishka/Downloads/satornawb-main/.worktrees/arch-t1-platform; seven tracked paths below. No domain/ORM/router/frontend changes, old migration rewrite, source copying beyond exact fixture.
- No production/app DB/working Redis, real credentials/.env, providers/network services, keys/flags/backfill/push/deploy/host changes. Only exact random own DB/runtime roles via candidate allocator over /private/tmp/.s.PGSQL.5432; maintenance postgres only own create/drop/absence.
- Own backend/.venv, env-i, explicit ORDERS_TEST_USE_LOCAL_CLUSTER=1, PGPASSFILE/PGSERVICEFILE/NETRC=/dev/null, fake providers/unreachable app+Redis URLs/all send flagsfalse, plan-owned secret/IP deny sandbox. Alternate Ruff lint-only. No installs/skips/xfails/baseline widening/timeout-as-success or SQLite lock/RLS substitute.
- Observed sole0067. Candidate0068 is conditional NOT reserved: recheck sole20260909_0067/no0068 file before edits. If another schema lands, STOP and obtain explicit controller plan amendment, never silently renumber. P1 shares this conditional candidate; no concurrent schema implementation.
- Exact sorted six-key ASCII object with schemaVersion INSIDE bytes. Nonempty external is not nonblank; preserve whitespace/Unicode/NULL-versus-empty ref. No Orders positional/strip/length grammar, epoch or generation invented.
- Five fields immutable from INSERT, including running/empty/unbound promotion. Existing org-only FORCE RLS remains, not same-org account authorization. No cleanup/relabeling to permit rollback.

## Task 1: Forward binding schema and actual database gates

**Files:**
- Create backend/alembic/versions/20260909_0068_review_run_binding.py.
- Modify backend/ops/runtime-db-role.sql only Review new-column INSERT/new pure helper EXECUTE.
- Create backend/tests/test_review_run_binding_migration.py.
- Create backend/tests/test_review_run_binding_rls.py.
- Create backend/tests/test_review_run_binding_acl.py.
- Create backend/tests/fixtures/reviews/account_binding_descriptor_v1.json verbatim from45e2ccbb165e6ddcc383311789b84b5d93ef2933:backend/tests/fixtures/review_binding_descriptor_v1.json.
- Create backend/docs/superpowers/reports/2026-09-09-t1-review-run-binding-schema-handoff.md.

**Consumes:** Actual0063/0065 Review guards and0067 immediate missing-lock fix; candidate.cluster/disposable_database/migrate/scope, test_review_facts_schema.seed/run, test_review_lossless_migration.unit; Orders run-binding race/ACL patterns. Read exactT4 historical_binding.py/fixture/handoff but never copy domain implementation.
**Produces:** review_sync_runs_v2 fields account_binding_schema_version SMALLINT, account_binding_external_account_id TEXT COLLATE C, account_binding_credential_ref TEXT COLLATE C, account_binding_payload BYTEA, account_binding_checksum TEXT. All nullable/no backfill defaults. Dedicated immutable SECURITY INVOKER fixed-search-path helpers:

```text
public.review_binding_ascii_string(text) -> text
public.review_run_binding_bytes(integer,integer,text,text,text) -> bytea
# org, account, marketplace, external, nullable ref
review_run_binding_invalid:23514
review_run_binding_mismatch:23514
review_run_binding_immutable:23514
review_run_binding_isolation_invalid:25000
review_run_binding_downgrade_bound:55000
```

- [ ] Record BASE/clean scope/actualhead and absence. Read full spec/old guards/ACL/fixture APIs/six literal vectors. Copy fixture via apply_patch and prove exact equality to immutable source. No domain imports in parity tests.
- [ ] Write RED first on actual0067: ordinary valid unbound run control, then absent helper/new-column call with desired success. Missing-function/column failure is contract RED; setup/import failure is not. After implementation feature fixtures pin0068, runtime-script fixtures consume latest head; synthetic-successor graph must not freeze future heads.
- [ ] Implement dedicated ASCII encoder preserving Python JSON escapes/UTF16 supplementary pairs and exact scalar strings. Validate positive INT4, exact wb/avito, non-NULL nonempty external, nullable exact ref. Reconstruct six-key sorted compact object; never JSONB textual output. Current canonical-account VARCHAR/TEXT cannot admit NUL, but no stripping/truncation/new source-body constraint.

```python
# Values and literal expectations come from copied fixture, not SQL serializer.
payload, checksum = c.execute(text("""
 SELECT p, encode(sha256(p),'hex') FROM
 (SELECT public.review_run_binding_bytes(:org,:account,:provider,:external,:ref) AS p) q
"""), values).one()
assert bytes(payload) == literal_ascii.encode("ascii")
assert checksum == literal_sha256
```

Four representable vectors must match SQLbytes AND SQLhash; two NUL vectors need explicit native-rejection evidence, never skip. Test whitespace-only external, maxINT4, controls, supplementary/composed/decomposed Unicode with independent stdlib sorted compact ensure_ascii JSON. Null/empty ref distinct. Recomputed hash cannot admit alternate order/duplicate/extra keys/string or float IDs/BOM/newline/changed shape.
- [ ] Add all-five-NULL OR explicitly schema1/external+payload+checksum nonNULL/refnullable CHECK. Test all partial-null masks, invalid schema/emptyexternal/hash/bytes; SQL unknown cannot admit missing required fields. Literal exact reconstruction AND SHA equality both required.
- [ ] Add dedicated bound BEFORE INSERT guard; retain old0065 guards. READ COMMITTED only. Lock exact org/account/provider canonical row FOR UPDATE, immediately reject NOT FOUND, then separate fresh metadata SELECT compares exact provider/external/ref. Dedicated UPDATE trigger rejects OLD/NEW five-field difference from INSERT, never requests account lock after run tuple.

```sql
PERFORM 1 FROM public.marketplace_accounts
 WHERE organization_id=NEW.organization_id
 AND marketplace_account_id=NEW.marketplace_account_id
 AND marketplace COLLATE "C"=NEW.marketplace COLLATE "C" FOR UPDATE;
IF NOT FOUND THEN
 RAISE EXCEPTION USING ERRCODE='23514', MESSAGE='review_run_binding_mismatch';
END IF;
-- Only now separate fresh metadata SELECT: a missing lock cannot be overwritten.
```

- [ ] Real rebind→insert and insert→rebind winner tests use events plus observed pg_blocking_pids. Include external/refNULL-empty changes, missing-row-then-created race with disclosed disposable instrumentation if needed, ordinary positive/noopUPDATE control. No UPDATE account lock inversion. Finally signals/rollback precede executor join. DDL preservation is not T4 completed-rebind reader acceptance.
- [ ] Snapshot old relation/column/function/default ACLs. Extend new-column INSERT only to non-PUBLIC grantees with complete old required run INSERT authority (runtime-script old INSERT allowlist, excludes generated run_sequence). Readers/update-only/partial-column roles receive no new-column INSERT. Test effective role membership deliberately without creating missing parent/account authority. Pure helpers EXECUTE only entitled old INSERT/UPDATE CHECK evaluators; trigger helpers no direct runtime/PUBLIC execute. Clear new-function default grants/grant options atomically without altering old default ACLs.
- [ ] Runtime script extends existing allowlist with five fields and pure-helper EXECUTE only. Actual runtime/reader/partial-column/full-column/update-only/inherited-role/hostile-default cases; exact old rights unchanged except approved additions, sequenceUSAGE-only/no explicit generated ID. Injected script failure rolls back grants. Exclusive DDL/ACL maintenance is precondition, not relation-lock fence for concurrent GRANT.
- [ ] Actual nonowner/NOSUPER/NOBYPASS Unix runtime proves org FORCE RLS/no-context/wrongorg denials and scoped FK/provider mismatch. Preserve same-org multiaccount read explicitly; T4 allowed-account auth is separate. Binding immutable in running/empty/terminal and allNULL→bound; permitted unchanged legacy updates remain.
- [ ] Empty unstamped0067→0068→0067→0068 and separately populated0067 cycles: valid Orders unit plus Review NUL-byte/current-watermark/run-item unit. Snapshot all selected old driver-returned columns/counts/IDs/timestamps/bytes/ACL at each step, new fieldsNULL. No triggerdisable/truncate. Downgrade exact run ACCESS EXCLUSIVE plus row_security=off refusal defense checks ANY newfieldnonnull before ALL DDL. Visible bound/partial privileged malformed/hidden FORCE-RLS bound refuse unchanged. No CASCADE/unbind/erase/current-account backfill.
- [ ] Run fresh feature and adjacent gates in specified sandbox, natural exits/own exact absence checks:

```sh
.venv/bin/python -m pytest -q -s --tb=short tests/test_review_run_binding_migration.py tests/test_review_run_binding_rls.py tests/test_review_run_binding_acl.py
.venv/bin/python -m pytest -q -s --tb=short tests/test_review_facts_schema.py tests/test_review_lossless_migration.py tests/test_review_lossless_rls.py tests/test_orders_run_binding_migration.py tests/test_orders_run_binding_rls.py
.venv/bin/python -m compileall -q alembic/versions/20260909_0068_review_run_binding.py tests/test_review_run_binding_migration.py tests/test_review_run_binding_rls.py tests/test_review_run_binding_acl.py
.venv/bin/python -m alembic heads
git diff --check
```

Scoped new-Python Ruff, exact literal fixture equality. Verify named adjacent files before dispatch and amend any incorrect path rather than silently omit coverage. No full-backend/native or production acceptance claim.
- [ ] Self-critic exact byte/hash/NULL/ACL/lock/hidden-downgrade/canary coverage; commit feat: freeze account binding on Review runs, only seven paths. Report exact RED/GREEN/exits/cleanup/head/limitations. Independent controller review/fresh acceptance precede exact READY T4 handoff; T4 still owns current+ambiguity/mixed/unbound/history/replay/ErrorEnvelope and completed-rebind acceptance. Old decoder-capable binary keeps expanded schema for rollback; no reader/worker/flag activation.

## Preflight

| Interface | Decision |
| --- | --- |
| T4 object→SQL | Schema inside, four native positives/two NUL negatives, SQLbytes AND SQLhash |
| old column ACL→expansion | Complete writer intersection, no reader/sequence promotion |
| INSERT lock→metadata | Immediate NOT FOUND, fresh statement, no UPDATE account lock |
| lossless0065→newbinding | Body/coverage untouched; exact synthetic preservation, accountnative limits separate |
| P1candidate0068→Reviewcandidate0068 | Neither reserved; controller explicitly amends second plan before dispatch |
| DDL→consumer | T4 isolation acceptance still mandatory, no false completed-rebind closure |

Critical pass covers spec shape/NULL/whitespace/Unicode/default grants/missing-lock race/orgRLS-versus-auth/hidden rollback and competing candidate. No test result inferred from plan.
