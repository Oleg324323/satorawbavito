# WB legacy resolver lock-order repair

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans, test-driven-development and verification-before-completion.

**Goal:** Remove the confirmed account→user lock inversion from the existing locking legacy WB resolver without switching consumers.
**Architecture:** Retain public signature/singleton/reference/token rules; align the locking path with the existing user SHARE→account UPDATE→token UPDATE discipline, using the correctly ordered binding primitive where valid.
**Tech Stack:** Existing SQLAlchemy/pytest/PostgreSQL16, own backend/.venv, no dependencies.
**Spec:** Bounded design below; coordinator explicitly approved this confirmed local defect repair and required post-wait identity validation. Evidence `.superpowers/sdd/2026-09-09-orders-schema-integration/credential-backfill-lock-authority.md` is an inspection record, not a replacement regression test.

## Bounded design / evidence

Current resolve_bound_wb_credential(lock=True) locks organization WB accounts, then token JOINuser with unrestricted FOR UPDATE. Actual funnel/raw_backfill.py:125 and advertising/raw_backfill.py:114 invoke this path. Independent main diagnostic under real nonowner runtime role: ordinary synthetic resolver positive control PASS; A holds userSHARE, actual resolverB locks account and waits on A's user, A then requests account → PostgreSQL40P01. Own0061 DB/role cleaned; no provider call. Existing _binding_candidate(lock=True) already locks user SHARE, exact account UPDATE and token-only UPDATE, refreshing after waits.

Choose a bounded locking-path repair, not retries, weakened user locks or another resolver. No API changes, account cardinality expansion, writer activation or domain rewrite. Existing singleton semantics remain: missing/multiple connected WB accounts fail; supplied account/reference/token must match. Preserve nonlocking behavior. Never choose latest org token. Locking lookup may read a locator first, but must lock and revalidate actual user/org/active state, canonical account/provider/status/ref and token owner/org/value before returning. A changed binding/owner after wait fails safe; it cannot silently choose a new source. Explicit FOR UPDATE OF token avoids accidental user lock promotion.

## Global constraints

- Only T1 worktree `/Users/bratishka/Downloads/satornawb-main/.worktrees/arch-t1-platform`, three listed write paths. No domain/raw-backfill callers, cabinet store, schema/migrations/grants, configs/dependencies/tasks/frontend changes.
- No providers/GitHub/production/app DB/real credentials/.env/working Redis/flags/backfill/key/push/deploy/host changes. Synthetic fixtures only; do not call bind CLI or actual seller-info.
- Own backend/.venv, env-i PATH=/usr/local/bin:/usr/bin:/bin, PGPASSFILE/PGSERVICEFILE/NETRC=/dev/null, PYTEST_DISABLE_PLUGIN_AUTOLOAD=1, explicit ORDERS_TEST_USE_LOCAL_CLUSTER=1. Plan-owned known-secret/IP-denying sandbox permitting only `/private/tmp/.s.PGSQL.5432`; exact own candidate allocation/cleanup only. Approved alternate Ruff lint-only, no installs.
- No skip/xfail/baseline expansion or timeout/kill-as-success. Preserve real two-session evidence and cleanup before executor shutdown. No SQLITE substitute for lock proof.

## Task 1: Repair locking resolver and prove actual race/compatibility

**Files:**
- Modify `backend/app/platform/integrations/wb_credentials.py` only resolve_bound_wb_credential and directly needed helper reuse.
- Create `backend/tests/test_wb_legacy_resolver_lock_order.py`.
- Create `backend/docs/superpowers/reports/2026-09-09-t1-wb-legacy-resolver-lock-order.md`.

**Consumes:** Read full wb_credentials.py; existing fetch PostgreSQL fixture cluster/pg_database/pg_store/wait_blocked in tests/test_marketplace_credential_fetch_postgres.py; actual five tests in test_wb_credential_binding.py. Main standalone baseline of those five:5PASS0.59s/natural0 under all-network-denied sandbox. Read actual raw-backfill call sites only, do not change them.
**Produces:** Same resolve_bound_wb_credential signature and tuple result; corrected retained lock order and post-wait identity checks. No own-session commit/rollback inside this caller-transaction function.

- [ ] Confirm clean three-path scope and record BASE. Write actual regression before implementation. Use inherited synthetic fixture but actual runtime factory, set exact legacy credential_ref, assert nonlocking positive control. Runtime nonowner/NOSUPERUSER/NOBYPASSRLS/Unix origin must be established.
- [ ] Construct two business sessions plus an observer: publisherA holds userSHARE; resolverB pauses after obtaining accountUPDATE; A attempts account lock, observer proves exact A→B blocking and releases B. Old B then requests userUPDATE through the JOIN, forming the confirmed cycle. Desired assertion is both operations complete successfully, so old40P01 fails the test. Do not instead assert that the buggy exception is expected and call that a RED. Release signals and rollback failed sessions in finally before waiting on executor shutdown. Observer is not a third business mutation.

```python
# Fixed result must prove successful compatible transactions, not only no timeout:
assert outcomes == {"publisher": "success", "resolver": "success"}
assert observed_publisher_wait is True
assert resolved[0:2] == (owner.marketplace_account_id, reference)
assert resolved[2] == CANARY  # assertion only; never print secret
```

Record exact RED command/exit/SQLSTATE and ownedcleanup; setup failures are not regression evidence. Existing account/user fixtures need no provider API.
- [ ] Implement minimal path: a nonlocking locator lookup can precede user lock; use _binding_candidate(lock=True) or equivalent shared ordered primitive, then verify locked account ref and frozen owner/source locator match the pre-lock/caller binding. Preserve exact token compare and singleton selection; don't make _binding_candidate take account before user. Token lock explicitly targets LkUserWbTokenRow only. No new outer commit, retry or fallback.
- [ ] Add actual wait/revalidation tests for token rotation, token deletion/revocation, account ref change, disconnected account, user inactive/moved org and token reassignment/moved org. If A's mutation won first, B denies rather than returning changed/stale caller binding; if resolver wins first, retained locks block mutation until caller transaction release. Prove exact before/after rows and expected safe errors, not driver exception-string matching. Do not invent a new public parameter to freeze unrelated data; existing inputs are the compatibility boundary.
- [ ] Verify emitted locking SQL user SHARE→exact account UPDATE→token UPDATE OF only; ordinary nonlocking calls emit no row locks. Missing/ambiguous WB account, wrong explicit account/ref/token, wrong org, missing token and inactive user deny; keep same argument normalization behavior outside the locking freshness check. Successful locking resolve must not mutate rows/audit, commit or rollback caller work. A failed call leaves rollback to caller.
- [ ] Run focused GREEN then complete scoped actual gate:

```sh
.venv/bin/python -m pytest -q -s --tb=short tests/test_wb_legacy_resolver_lock_order.py tests/test_marketplace_credential_fetch_postgres.py
.venv/bin/python -m pytest -q --tb=short tests/test_wb_credential_binding.py
.venv/bin/python -m compileall -q app/platform/integrations/wb_credentials.py tests/test_wb_legacy_resolver_lock_order.py
git diff --check
```

Use specified scrubbed Unix-only sandbox and fakeprovider/unreachable settings. Run existing five compatibility tests separately to retain their original collection boundary; do not edit their global SQLite fixture as part of this fix. Scoped Ruff with inherited findings identified against BASE; no unrelated formatting. No full backend or real provider acceptance claim.
- [ ] Self-review and commit `fix: align legacy WB resolver lock order`. Report original40P01 RED→GREEN, exact tests/exits/cleanup/actual lock evidence and compatibility limits, safe error/secret-canary results, no domain/caller/operational changes. Independent review required before maintenance-helper design relies on this lock order.

## Preflight

| Interface | Obligation |
| --- | --- |
| Nonlocking locator → ordered primitive | No source identity switch after waiting; user/account/token revalidated |
| Existing public signature → locking fix | Preserve singleton/reference/token/nonlocking semantics, no selected-account expansion |
| Shared locking protocol → raw callers | Actual two-session40P01 regression removed, caller retains transaction ownership |
| Runtime tests → maintenance prerequisite | Own role/retained locks, no helper/grants/backfill prematurely introduced |

One task, independent of repricer test hardening; no implementation may overlap another implementation agent. Root approval is local only, not production authorization.
