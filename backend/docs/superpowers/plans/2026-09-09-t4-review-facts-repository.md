# Review Facts repository implementation plan

> **For agentic workers:** Use superpowers:executing-plans with test-driven-development. Continue inline under the user's existing authorization.

**Goal:** Implement dormant PostgreSQL persistence over accepted Review Facts 0063, without enabling ingestion or send endpoints.

**Architecture:** Caller owns the transaction and live publication authorization. Repository requires explicit tenant context and exact connected account binding, obtains account lock before identity lookup, and performs each command under a savepoint. It never commits, fetches providers, selects a default account, or falls back to files/memory.

**Tech stack:** SQLAlchemy 2, PostgreSQL, existing normalized Review DTOs, pytest.

**Spec:** `../reports/2026-09-08-t4-reviews-stage-2-schema-request.md`, amended by accepted `../reports/2026-09-09-t1-review-facts-schema-handoff.md` at 56b5fa50b2de41a4f3277392148012233952188c.

## Constraints and boundaries

- Existing T4 worktree only. Exact prerequisite lineage merged without editing shared files.
- No shared DDL, auth/config changes, provider calls, production, backfill or activation.
- Repository binding checks are NOT membership/session/credential authorization. Runtime integration waits for T1's real publication guard.
- Sequence is server-owned allocation token, never provider chronology. Text identity uses exact comparison after account lock, not ON CONFLICT.
- SQLAlchemy mappings describe columns for migrated tables; Alembic remains the only supported schema installer, including all SQL triggers/RLS/grants.

## Tasks and current matrix

- [x] Read exact committed schema and domain request; preserve predecessor lineage.
- [x] Add `tests/test_review_facts_repository.py` using existing disposable PostgreSQL fixture, actual runtime grants, environment scrubbed and Unix-only sandbox. First test reserves/replays one run and proves exact request conflict does not alter it.
- [x] Add `app/reviews/canonical_orm.py`: four typed mappings. Add `canonical_repository.py`: `ReviewFactsRepository(connection, owner)` with `reserve_run`, `ingest`, and scoped `get_fact`. Each public command uses savepoint rollback; no commit authority.
- [x] Implement strict safe coverage/manifest admission in `ingestion_contract.py`: exact keys, duplicate rejection, verified DTO checksums, deterministic sorted identities. Test malformed and changed terminal manifests.
- [x] Add real DB tests for same identity across accounts, unchanged source, A→B→A, old source/late sequence, durable ambiguity and stricter clearing, CAS rollback, partial coverage, restart, invalid context/binding, long exact keys and two-session serialization.
- [x] Run bounded existing Reviews tests, new PostgreSQL tests, lint/compile/diff checks; independent critic and fix findings. Record exact evidence and unimplemented guard/service gates in one final handoff.

## Acceptance examples

```python
first = repo.reserve_run(source_run_id='source-1', request_checksum='a' * 64, started_at=now)
assert repo.reserve_run(source_run_id='source-1', request_checksum='a' * 64, started_at=now) == first
# Same source key and different request must raise a safe conflict.
```

```python
# Version is an explicit caller precondition for every existing ingested identity;
# missing/new identity uses zero. A failed precondition rolls back all run items.
repo.ingest(first.sync_run_id, facts=(normalized,), coverage=coverage,
            completeness='complete', completed_at=now,
            expected_versions={normalized.identity.external_review_id: 0})
```

Terminal replay compares the entire typed manifest/count/completeness/coverage and exact run request binding before returning without touching facts. New runs retain late/stale evidence but never regress current pointers or watermarks. Partial ingestion never tombstones absent identities. Ambiguity clears only with a later allocated run and provider timestamp strictly newer than every unresolved conflicting observation; if any conflicting timestamp is unknown, automatic clearance is refused.
