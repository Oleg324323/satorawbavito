# Account-scoped credential re-encryption implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans; TDD and verification-before-completion. Steps use checkbox syntax.

**Goal:** Prove controlled re-encryption under the real nonowner runtime role with exact account ownership.
**Architecture:** Tighten the existing store entry point, retaining one crypto/write boundary and account-first locking. No operational caller or second writer is introduced.
**Tech Stack:** Existing SQLAlchemy/pytest/PostgreSQL16/cryptography, own backend/.venv; no dependencies.
**Spec:** `backend/docs/superpowers/specs/2026-09-09-account-scoped-credential-reencryption-design.md` (read fully).

## Global constraints

- T1 worktree only `/Users/bratishka/Downloads/satornawb-main/.worktrees/arch-t1-platform`, listed six paths only. No migrations/ORM/config/grants/crypto/routers/tasks/frontend/foreign-domain edits.
- No real credentials/keyrings/.env/providers/production/app DB/working Redis/backfill/rotation/deploy/push/flags/host changes. Only in-memory synthetic keys and existing exact Unix-disposable DB/runtime roles.
- Own backend/.venv under env-i PATH=/usr/local/bin:/usr/bin:/bin, PGPASSFILE/PGSERVICEFILE/NETRC=/dev/null, PYTEST_DISABLE_PLUGIN_AUTOLOAD=1, ORDERS_TEST_USE_LOCAL_CLUSTER=1 and plan-owned known-secret/IP-denying sandbox allowing only `/private/tmp/.s.PGSQL.5432`. Approved alternate Ruff is lint-only; no installs.
- No skip/xfail/baseline expansion, no SQLite RLS proof, no DB-owner substitution for the writer. Account scope is not authentication. No operational key policy or worker principal is invented.

## Task 1: Bind the existing rekey entry point to an explicit owner

**Files:**
- Modify `backend/app/platform/integrations/credential_store.py` only reencrypt_credential and its directly needed imports/private validation.
- Modify `backend/tests/test_marketplace_credential_store.py` only the two existing rekey calls and relevant new validation assertions.
- Modify `backend/tests/test_marketplace_credential_fetch_postgres.py` only reencryption owner substitution/call and directly unused variables.
- Modify `backend/tests/test_publication_guard_postgres.py` only reencryption owner substitution/call and directly unused variables.
- Create `backend/tests/test_marketplace_credential_reencryption_postgres.py`.
- Create `backend/docs/superpowers/reports/2026-09-09-t1-account-scoped-credential-reencryption.md`.

**Consumes:** Existing MarketplaceAccountCredentialOwner, _account, _identity, _encrypted, _audit, CredentialKeyring/encrypt/decrypt and set_tenant_context. Test fixtures `tests.test_marketplace_credential_fetch_postgres.cluster`, `pg_database`, `pg_store`, `wait_blocked` may be reused explicitly; read their full definitions. Existing publication fixture/real lock-winner cases must be preserved, not reimplemented as mocks.
**Produces:** Existing three-position signature plus keyword-only account_identity default None, which denies missing owner before I/O; safe CredentialMetadata on success.

- [ ] Read full spec, current reencrypt implementation and all four call sites, existing runtime fixture/role origin and RLS definitions. Confirm no new app callers appeared; report any caller/ownership conflict before editing it. Record BASE and six-path cleanliness.
- [ ] Before implementation, reproduce actual UUID-only runtime failure: in an allocated fixture ordinary put/resolve succeeds with a nonowner NOSUPERUSER/NOBYPASSRLS role, then the old three-argument call cannot rotate its own credential. Keep the desired positive assertion so the run exits FAIL for credential_missing, not an import/setup failure. Write missing-owner no-I/O test expecting typed credential_contract_invalid; patch key/session factories to raise if invoked, observe old boundary fails the assertion. Capture both exact commands/results.

```python
created = store.put_marketplace_credential(owner, "wb_api", {"token": CANARY})
assert store.resolve_marketplace_credential(owner, "wb_api").reveal() == {"token": CANARY}
rotated = store.reencrypt_credential(created.credential_id, 1, 8)
assert rotated.generation == 2  # initial RED before adding owner keyword
```

After the RED, update that desired-success call to pass `account_identity=owner`; keep a separate missing-owner safe-denial test. Do not make an old-behavior characterization pass count as the bug's RED.
- [ ] Write scope and mutation tests before implementation. Seed exact other account in same org and another org/account with their own active credentials. For wrong owner/provider/UUID pair assert safe denial and exact before/after encrypted row plus audit snapshots, never raw secret in output. Missing owner, arbitrary object, booleans, zero/negative/out-of-INT4 owner IDs, invalid UUID/generation/key version and generation overflow deny before I/O. Target key_version must also fit existing INTEGER:1..2147483647;2147483648 denies before key/session factories. Positive maxINT4 validates input shape, but does not imply a configured key exists.
- [ ] Implement keyword-only owner validation and set tenant context in the fresh owned transaction; enforce actual PostgreSQL READ COMMITTED/nonautocommit (SQLite remains limited contract tests). Lock account before credential. Use exact predicates in SELECT and UPDATE:

```python
MarketplaceAccountCredentialRow.organization_id == account_identity.organization_id
MarketplaceAccountCredentialRow.marketplace_account_id == account_identity.marketplace_account_id
MarketplaceAccountCredentialRow.provider == account_identity.provider
MarketplaceAccountCredentialRow.credential_id == credential_id
MarketplaceAccountCredentialRow.revoked_at.is_(None)
```

CAS additionally requires generation==expected_generation. Use existing _account(lock=True) with exact owner; fresh Session avoids stale identity map. Check expiry after lock wait and before encryption. Preserve decrypt→encrypt freshnonce→decrypt exactequality→CAS→audit→commit, increment generation once and no plaintext fallback. Errors safe typed codes/from None on translated persistence failures. Validation failure must not expose key inventory or arbitrary exceptions.
- [ ] Replace the two PG test owner-only factories with the unchanged runtime factory and supply owner in all four existing rekey call sites. Do not remove original paired snapshot assertions, both publication lock winners, row blocking observation or cleanup. Existing store SQLite test still checks ciphertext/nonce/generation/single UPDATE/audit/CAS, but the report labels it non-RLS evidence.
- [ ] New actual PG tests assert role/catalog origin, decrypt equality, old+new synthetic keys, fresh nonce and ciphertext, retained UUID/owner/kind/schema/expiry, generation+1 and one safe audit; same-key rekey still freshnonce. Stale expectedgeneration, revoked/expired/auth-failed row, missing old/target key and synthetic DB failure preserve rows/audit. Inject decrypt-verify mismatch and audit failure, assert full rollback and canary absence in error/repr/logs. No real key file is opened.
- [ ] Actual account-first race: session A holds exact account UPDATE, rekey B is observed blocked by pg_blocking_pids. A can lock credential without a cycle, and may replace/revoke/expire it before releasing; B must observe committed row state and deny stale/invalid input. Separate two-rekey same-generation race yields one generation increment/audit and one safe CAS conflict. Signal releases/engine disposal occur in finally before thread executor shutdown; no test process kill/timeout-as-success. Preserve helpers' owned resource absence output.

```python
assert sorted(result_codes) == ["credential_concurrent_update", "success"]
assert after.generation == before.generation + 1
assert audit_after - audit_before == 1
```

For expiry-after-wait use the injected trusted clock advancing after observed blocking and a valid Avito access credential; never mutate real time or key policy. READ COMMITTED and nonautocommit admission each have a synthetic denied configuration plus ordinary positive control; failed calls make no business/audit changes.
- [ ] Run scoped actual gate once after focused RED→GREEN, natural exit/owned cleanup:

```sh
.venv/bin/python -m pytest -q -s --tb=short tests/test_marketplace_credential_reencryption_postgres.py tests/test_marketplace_credential_store.py tests/test_marketplace_credential_fetch_postgres.py tests/test_publication_guard_postgres.py
.venv/bin/python -m pytest -q tests/test_marketplace_credential_crypto.py tests/test_marketplace_credential_migration.py
.venv/bin/python -m compileall -q app/platform/integrations/credential_store.py tests/test_marketplace_credential_reencryption_postgres.py tests/test_marketplace_credential_store.py tests/test_marketplace_credential_fetch_postgres.py tests/test_publication_guard_postgres.py
git diff --check
```

Prefix all runtime commands with the specified scrubbed sandbox and synthetic fakeprovider/unreachable default URLs as in accepted securityPG harness. Scoped Ruff and exact baseline comparison; no unrelated reformat. No full suite/native PG/production claim.
- [ ] Self-review the six-path diff and safe diagnostics, then commit `fix: scope credential reencryption to canonical account`. Report exact RED/GREEN commands, natural exits, owned PG/runtime proof, lock order/expiry/CAS/failure coverage, removed owner-only substitutions, safe signature and explicit trusted-administration/no-activation limits. Independent scoped review before Stage3 CLI depends on it.

## Controller preflight

| Interface | Check / ruling |
| --- | --- |
| Old three-arg invocation → safe boundary | Optional keyword defaults None but denies before I/O, no silent UUID discovery |
| Store → existing runtime fixture | Explicit exactowner/tenant/accountlock fixes missing RLS context without privileged factory |
| Existing tests → strengthened implementation | All four calls updated; no snapshot/guard/concurrency assertion removed |
| New PG proof → SQLite compatibility | SQLite remains contract-only, actual runtime proves RLS/locks/configuration |
| Administrative rekey → future CLI | No public authorization implied, no real key lifecycle/backfill activated |

One implementation task; no concurrent implementation on these files. This plan allocates no schema revision and does not retire legacy readers.
