# Canonical ingestion parity and fresh-process retry plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the concrete missing synthetic parity/restart evidence for the
approved canonical-only Review ingestion mechanism, without claiming full stage2.

**Architecture:** Exercise the existing HTTP→paired credential→reservation→single
fake WB request→normalization→guarded0068 repository chain. Compare a received DTO
directly and through persistence; legacy conversion is pure comparison only.
Fresh spawned processes share only the disposable database and explicit synthetic
inputs, never application globals or a live provider.

**Tech Stack:** Python, pytest, FastAPI TestClient, multiprocessing spawn,
SQLAlchemy, existing disposable PostgreSQL fixture and FakeWbApiClient.

**Spec:** `../reports/2026-09-09-t4-canonical-review-http-handoff.md`, current approved
amendment; original T4 stage2 and root process-audit direction2026-09-09.

## Global constraints

- No production, provider, real credential, schema, shared registration or flag change.
- All tests use existing owned disposable PostgreSQL and explicitly admitted heavy queue.
- No legacy store writes, memory fallback or second provider fetch for comparison.
- Local retry is explicit; no automatic restart daemon or ambiguous-COMMIT rule invented.
- Frozen platform milestone input remains77abab0; this evidence is a separate follow-up.

### Task1: Synthetic same-input parity and process-boundary acceptance

**Files:** Create `tests/test_review_canonical_parity.py`; update the existing
canonical HTTP handoff and single current requirements matrix with actual results.

**Interfaces:** Consume `sync_canonical_wb_page` through existing test HTTP client,
`normalize_wb_review`, pure legacy `_to_feedback_row`, `FakeWbApiClient`, existing
`test_review_canonical_http` fixtures (`cluster`, `db`, `principal`, `context`).
Produce no runtime API; only an acceptance test module and recorded evidence.

- [x] Add a common-lossless-input case. Capture the tuple returned by the actual
  canonical adapter, normalize that exact received DTO with the actual account/run,
  then compare its fields and checksum to the repository-decoded observation
  (`get_fact` returns only a snapshot, `_observation` returns the verified mapping).
  Normalize the legacy
  pure conversion of the same raw item and compare common fields while explicitly
  retaining its different source-schema version/checksum. Assertions include:

```python
assert len(provider.requests) == 1
assert persisted["content_checksum"] == normalized.content_checksum
assert (persisted["text"], persisted["rating"], persisted["answered"]) == ("Synthetic parity", 5, False)
assert legacy_fact.source_schema_version != normalized.source_schema_version
```

- [x] Add null/whitespace difference cases with literal expected legacy/canonical
  text, not coercing canonical input into legacy shape. No legacy fetch/store call.
- [x] Add a top-level spawn worker that creates its own runtime engine, synthetic
  keyring/session factory and fake provider. Send only safe response/count/PID
  metadata to the parent; dispose its engine and exit normally. Join with a bounded
  timeout, cleaning only the exact child created by the test if it hangs.
- [x] Use separate children for failed fetch, explicit retry and exact replay of
  one request key; then a denied-account attempt. Check committed running reservation
  after failure, one immutable fact/revision after retry+replay, and no fetch for
  denied scope. Expected boundary assertions:

```python
assert failed["status"] == 502 and failed["calls"] == 1
assert retried["status"] == replayed["status"] == 200
assert retried["body"] == replayed["body"]
assert retried["calls"] == replayed["calls"] == 1
assert denied["status"] == 403 and denied["calls"] == 0
assert current.revision == 1
```

- [x] Run only the new module first in the approved scrubbed Unix-PG sandbox.
  Treat fixture/setup failures separately from behavioral failures; never change
  runtime merely to manufacture a failure in this characterization task.
- [x] If behavior differs from the accepted contract, capture the exact failing
  assertion before any bounded implementation fix; do not weaken binding/auth.
- [x] Run adjacent canonical HTTP, shadow composition and binding-repository tests
  in one serial admitted gate. Check child exits and owned database/role cleanup.
- [x] Perform an isolated main critical pass, scoped Ruff/AST/diff checks,
  and update the one current status table with exact evidence/remaining boundaries.
  Commit only after acceptance; independent integration review remains with root.

Actual acceptance:4PASS13.69s then61PASS2warnings26.86s, owned cleanup verified.
No runtime discrepancy or implementation fix was needed; no artificial RED was
manufactured for this missing characterization evidence. Framework warnings and
precise scope are recorded in the existing HTTP handoff.
