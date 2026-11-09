# Review0068 repository consumer implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans task-by-task. User requested autonomous execution; no additional execution-choice pause.

**Goal:** Reject historical Reviews data that cannot prove the exact guarded account binding, and stamp every new reservation at INSERT.

**Architecture:** Consume T1's immutable five-column0068 binding using the existing strict T4 codec. Keep caller-owned publication guards and root rollback; provenance is additional validation, never authorization. Reject the whole requested unit rather than silently filtering or relabeling history.

**Tech Stack:** Python, SQLAlchemy Core/ORM mappings, PostgreSQL, pytest; existing offline disposable fixtures.

**Spec:** `backend/docs/superpowers/reports/2026-09-09-t4-review-historical-binding-request.md` and exact T1 `2026-09-09-t1-review-run-binding-schema-handoff.md` at `4d95b24b549f0b1f773095ec583a85f18172fdbc`.

## Global constraints

- Only local dormant Reviews consumer work. No migration edits, production, provider calls, credentials, flags, registration, scheduler, push or backfill.
- T1 schema prerequisites only: dbf8d31 (0066), d87e575 plus mandatory c4e3e373 fix (0067), 4d95b24 (0068). Do not import unrelated ancestry.
- Existing unbound rows stay all-NULL and are rejected on consumption. NULL, empty and whitespace credential references differ. No invented lifecycle epoch or ABA guarantee.
- Preserve0065 lossless text, original identities, late/ambiguous ordering, request replay and CAS. Schema rollback cannot remove bound history; binary rollback requires retaining expanded schema and a compatible safe consumer.
- One team heavy gate at a time, direct admission and explicit natural-exit/cleanup release. No timeout widening, baseline waiver, app database or shared service use.

## Task1: Accept prerequisites and prove current leak

Checkpoint: Task1 actual RED completed on exact imported0068, two behavioral
failures6.23s with own2DB2rolecleanup. Task2 implementation passes all18 new
binding cases; targeted adjacent gate80PASS1FAIL12.53s because one JSON-invalid
fixture was unbound. That fixture is corrected at INSERT; final gate and Task3
race/rollback acceptance remain pending. No activation or final completion claim.

Files: exact T1 prerequisite commits; `backend/tests/test_review_historical_binding_gap.py`; new `backend/tests/test_review_run_binding_repository.py`.

- [x] Cherry-pick the four exact approved prerequisite commits, preserving their SQL unchanged. Check sole Alembic0068 and diff.
- [ ] Convert the existing completed-rebind characterization into rejection acceptance, retaining the old synthetic flow and evidence in the report. The expectation must surround the entire guarded transaction, not catch the error inside `session.begin()`:

```python
with pytest.raises(ReviewRepositoryError, match="^REVIEW_HISTORY_BINDING_CONFLICT$"):
    with Session(db[1]) as session, session.begin():
        guard = acquire_publication_guard(
            session,
            principal=ticket.principal,
            required_permissions=frozenset({"reviews:read"}),
            accounts=(ExpectedAccountBinding(91103, "wb", "synthetic-new-cabinet", None),),
            authorities=(),
        )
        guard.revalidate_before_write()
        repo = ReviewFactsRepository(
            session.connection(),
            ReviewOwner(91001, 91103, "wb", "synthetic-new-cabinet"),
            command_savepoints=False,
        )
        repo.get_fact(key)
```

- [ ] Add actual reservation assertion: read the stored run via runtime SELECT and decode with `decode_review_run_binding`; compare with literal `ReviewBindingDescriptor(91001, 91103, "wb", "synthetic-c", None)`. Expect current implementation to store unbound NULLs.
- [x] Run those focused tests against own freshly migrated disposable PostgreSQL after resource admission. Require actual behavioral RED, not fixture/setup failure.

## Task2: Bind reservation and historical consumption

Files: `backend/app/reviews/canonical_orm.py`, `canonical_repository.py`, `shadow_service.py`; repository/shadow/lossless tests.

Interfaces: keep public repository methods; add `credential_ref: str | None = None` to internal `ReviewOwner`. Add five nullable mappings exactly matching0068. Use existing `encode_review_run_binding(ReviewBindingDescriptor) -> dict[str, object]` and `decode_review_run_binding(Mapping) -> ReviewBindingDescriptor | None`.

- [ ] Validate ReviewOwner through existing strict descriptor rules (INT4, scalar exactness). `_command` locks and compares current external ID AND credential reference, with no normalization.
- [ ] Pass the captured `ExpectedAccountBinding.credential_ref` from shadow service, never user JSON or fresh unpaired lookup.
- [ ] Add a private repository check equivalent to:

```python
def _require_run_binding(self, row):
    try:
        valid = decode_review_run_binding(row) == self._binding
    except ReviewBindingDescriptorError:
        valid = False
    if not valid:
        raise ReviewRepositoryError("REVIEW_HISTORY_BINDING_CONFLICT")
```

`self._binding` is a validated ReviewBindingDescriptor constructed from all five owner values. The helper exposes neither payload nor reference in errors.

- [ ] New reserve INSERT includes `**encode_review_run_binding(self._binding)`. Existing reservation replay and ingest validate stored run before returning or writing; never stamp with UPDATE.
- [ ] Validate provenance before returning current observations, resolving ambiguous observations or consulting historical source_updated_at watermarks. Validate every distinct contributing run, including fact last-source run and terminal replay ITEM observation runs; missing joins/metadata must reject, not disappear through an inner join. Keep checks scoped to requested facts/runs, not entire organization or unrelated histories.
- [ ] Preserve source/body checksum checks and CAS; same bound unchanged-body reuse must remain valid. No silent rewind to older matching records when newer/current/ambiguous sources conflict.
- [ ] Map the new internal conflict through existing SHADOW_CONFLICT → SYNC_CONFLICT409 path. No raw SQL or credential references in ErrorEnvelope. Do not claim an unregistered GET route is implemented.
- [ ] Update positive direct-seed lossless fixtures to stamp binding at INSERT; preserve separate intentional unbound negatives. Old scalar-format fixtures are not automatically old account-binding fixtures.
- [ ] Focused RED→GREEN, then adjacent repository/shadow/publication/lossless tests in the admitted sequential gate.

## Task3: Isolation, replay and transaction acceptance

Files: new binding repository tests; `test_review_historical_binding_gap.py`, `test_review_shadow_service.py`, `test_review_publication_repository.py`; one verification report and local coordinator matrix.

- [ ] Cover external-account rebind and valid nonempty credential-reference changes with fresh valid guards. Preserve None/empty/space storage distinctions separately: the platform rejects empty/whitespace references before authorizing a read, so assert that early rejection rather than weakening its policy. Current live guard cannot authorize old history.
- [ ] Cover legacy unbound reservation/current source, mixed provenance, ambiguous source, watermark-only old source, empty terminal replay and nonempty terminal replay. Same-binding positive replay is unchanged; one invalid contributing run rejects the whole command.
- [ ] Verify no persistent changes after root failure: run/items/observations/fact heads/versions unchanged except separately committed original reservation. Include a two-fact command where the first fact is actually changed before the second fact fails provenance; a fresh transaction must observe none of the first fact's attempted changes. Never catch-and-commit a failed guarded root; exception expectations surround the entire transaction context.
- [ ] Exercise both lock winners: completed rebind before publication rejects; held account publication before rebind commits its matching original binding then rebind proceeds. Retain inherited second-run-UPDATE FK wait characterization; do not claim general deadlock freedom.
- [ ] Re-run0068 migration/RLS/ACL and targeted Reviews adjacent tests against actual runtime grants; run scoped Ruff, compile and git diff check. Record actual counts, failures and cleanup, not inferred broad-suite totals.
- [ ] Independent read-only critic reviews implementation and coverage. Resolve Important findings with new RED tests, then commit exact consumer/test/report paths. Send T1 exact acceptance SHA and remaining activation boundaries; do not merge other terminal code or activate routes.
