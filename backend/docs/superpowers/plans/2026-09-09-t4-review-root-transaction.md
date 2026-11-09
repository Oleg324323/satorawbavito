# Review repository root-transaction compatibility plan

> **For agentic workers:** Use superpowers:executing-plans and test-driven-development inline under existing authorization.

**Goal:** Make dormant ReviewFacts commands compatible with the accepted publication guard without bypassing its SAVEPOINT prohibition.

**Architecture:** Preserve default standalone command savepoints. Add explicit strict-boolean `command_savepoints=False` for a trusted caller-owned root transaction. This mode never opens a nested transaction and requires the caller to roll back the entire root on every error; it does not grant authorization or acquire the guard itself.

**Tech Stack:** Existing SQLAlchemy/PostgreSQL/pytest, no new dependencies.

**Spec:** T1 publication handoff at4860c53; T4 repository plan and eaf15c6 handoff. No API activation; lossless0065 remains required for full source parity.

## Task: explicit root mode and real guard acceptance — verified

Files: modify app/reviews/canonical_repository.py; create tests/test_review_publication_repository.py and scoped handoff report.

- [ ] Seed synthetic live user, membership, session and account authority inside the existing owned disposable Review DB fixture. Use actual runtime grants and guard, not mocked authorization. Existing data remains untouched.
- [ ] RED: call `ReviewFactsRepository(session.connection(), owner, command_savepoints=False)` after acquiring guard and reserve/ingest/get under one root. Record missing-mode failure before implementation.
- [ ] Implement strict bool validation and choose `connection.begin_nested()` only in default mode, otherwise `nullcontext()`. Keep account locks, RC/tenant checks, safe SQL errors and all existing methods unchanged. No commits/rollbacks owned by repository.
- [ ] Assert actual connection savepoint events remain zero; commit then read in a new guarded root. Use credential-independent cabinet:read only for persisted reads, and an explicit synthetic stored credential expectation with sync:run for publication. No provider/decryption action.
- [ ] On second-command replay/CAS failure, exit root with exception; verify all first-command writes are absent. Do not catch-and-commit or claim per-command recovery in root mode. Denied live membership must publish zero rows. Do not represent these sequential cases as a two-session revocation race.
- [ ] Run new acceptance plus existing repository/schema and shared guard tests in scrubbed Unix-only disposable harness. Run scoped Ruff/compile/diff. Critic then commit; no schema/shared source changes or activation.

Concrete caller protocol:

```python
with Session(runtime_engine) as session, session.begin():
    guard = acquire_publication_guard(session, **trusted_arguments)
    repo = ReviewFactsRepository(session.connection(), owner, command_savepoints=False)
    guard.revalidate_before_write()
    run = repo.reserve_run(source_run_id=source, request_checksum=checksum, started_at=now)
    repo.ingest(run.sync_run_id, facts=facts, coverage=coverage, completeness="complete",
                completed_at=now, expected_versions=expected_versions)
# Any exception escapes this root context; caller sanitizes physical COMMIT errors.
```

This acceptance does not authenticate a request, capture fetch authority, implement
the shadow service, persist domain audit, or make arbitrary caller code safe.

Completed evidence: new9 + existing repository35/schema48 + shared guard161 =
253PASS34.26s, Ruff/compile/diff0, independent scoped and follow-up review PASS.
An additional pre-existing Connection SAVEPOINT refusal was RED→GREEN. Full results
and exact caller restrictions: ../reports/2026-09-09-t4-review-guard-acceptance.md.
