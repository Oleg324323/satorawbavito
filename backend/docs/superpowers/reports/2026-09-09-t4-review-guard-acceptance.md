# T4 — Review repository / publication guard acceptance

## Exact prerequisite and scope

Accepted T1 paired-fetch7bfb631, publication0900f8e plus required4860c53 finalizer
fix merged as83ef4a4. The merge retains accepted ancestor0064 and shared history;
T4 did not edit shared guard, credentials, migrations or runtime grants.

Found a concrete consumer incompatibility: the dormant repository always opened
Connection SAVEPOINTs, whereas the guard protocol excludes them. Do not exploit the
fact that Connection savepoints are invisible to Session nested-transaction hooks.

`ReviewFactsRepository(connection, owner, *, command_savepoints=False)` explicitly
uses the existing root; defaultTrue preserves old isolated command semantics.
The argument accepts only real bool. Root mode rejects an already nested connection
before repository SQL. Account binding, tenant, READ COMMITTED, identity/CAS and
safe-error checks remain unchanged. Repository still has no commit or auth power.

## Mandatory caller protocol

Acquire the real guard in a fresh clean Session root before repository/domain locks.
Use fixed service permissions and every fetched authority; revalidate before writes.
Let EVERY failure leave the root context, or explicitly roll back that root before
returning any error. Never catch a command failure and commit prior/partial writes.
Root mode does not implement automatic rollback-only poisoning. Repository reads
do not replace current user/membership/session/account authorization.

```python
with Session(runtime_engine) as session, session.begin():
    guard = acquire_publication_guard(session, **trusted_arguments)
    repo = ReviewFactsRepository(session.connection(), owner, command_savepoints=False)
    guard.revalidate_before_write()
    run = repo.reserve_run(source_run_id=source, request_checksum=checksum, started_at=now)
    repo.ingest(run.sync_run_id, facts=facts, coverage=coverage, completeness="complete",
                completed_at=now, expected_versions=expected_versions)
# Caller also sanitizes physical COMMIT failures and never commits Connection directly.
```

## Verification

New test_review_publication_repository.py uses the existing fresh disposable Review
PostgreSQL DB/runtime-role fixture at actual latest migrated head. Synthetic users,
canonical memberships, login sessions and a metadata-only credential are inserted
in that owned DB. Credential bytes are never decrypted; no provider fetch is claimed.

- Initial realPG RED2FAIL1PASS: missing explicit mode API; invalid-mode RED5FAIL.
- First GREEN91PASS (new8 + existing repository35/schema48).
- Additional realPG RED1FAIL: existing connection SAVEPOINT was not rejected; fixed.
- Final **253PASS34.26s**, exit0, no skips: new9 + repository35 + schema48 + shared
  guard contract/PostgreSQL161. Exact inherited disposable cleanup checks run at
  fixture teardown. Existing /dev/null passfile warnings remain disclosed.
- Scoped Ruff, compileall and git diff checks exit0. Independent scoped review and
  follow-up nested-connection review found no important defects.

Actual evidence covers reserve/ingest/read commit without savepoints, new-session
guarded persisted read, whole-root rollback on later replay conflict, sequential
membership revocation denial, strict mode admission and pre-existing nested refusal.

Command from backend, scrubbed environment and authorized Unix-only sandbox:

```sh
env -i PATH=/usr/local/bin:/usr/bin:/bin PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 ORDERS_TEST_USE_LOCAL_CLUSTER=1 /usr/bin/sandbox-exec -f /Users/bratishka/Downloads/satornawb-main/.worktrees/arch-t1-platform/.superpowers/sdd/2026-09-09-review-facts-schema/local-postgres.sb /Users/bratishka/Downloads/satornawb-main/.worktrees/wave1-integration/backend/.venv/bin/python -m pytest -q --tb=short tests/test_review_publication_repository.py tests/test_review_facts_repository.py tests/test_review_facts_schema.py tests/test_publication_guard.py tests/test_publication_guard_postgres.py
```

## Still incomplete

No authenticated request composition, actual paired-fetch caller/shadow hook, domain
audit, provider action, worker principal, HTTP cutover or activation. Shared guard
race tests pass, but the new Review-specific revocation case is sequential, not a
new two-session Review race. Schema0065 and lossless repository decoder/roundtrip
remain prerequisites; do not silently drop NUL or claim current0063 stores it.
No production/working DB/Redis/provider/print/export/push/flag action was performed.
