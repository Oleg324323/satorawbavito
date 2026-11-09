# T4 dormant WB received-DTO shadow acceptance

Baseline c125a54, accepted guard4860c53 and lossless0065/writer0ce0517.
This is domain composition only: no existing route, scheduler, provider or flag
is activated. Avito is not implemented by this WB-only slice.

`begin_review_shadow` owns an Engine-bound root, captures live membership for a
trusted authenticated ActorContext, checks fixed reviews:write through the shared
guard and commits the run reservation before the caller fetches. The internal
ticket binds principal, exact account/credential authority, source request and run.
It is not a bearer token or a client-deserializable authentication request.

`publish_received_review_rows` accepts the already received tuple of WbFeedbackRow,
uses another guarded root, normalizes without another provider request and writes
through command_savepoints=False. Receipt is returned after successful commit.
Partial page evidence never removes absent reviews. Reservation order fences late
unknown-time responses. Repeated identical publication is idempotent; a changed
same-run payload conflicts. Lossless Unicode/NUL and semantic hashes remain intact.

New 19 actual disposable PostgreSQL cases passed in4.74s. Combined Review/adjacent
and shared-guard set: **641 passed in48.76s**, no skips, natural exit0. This is not
the entire backend suite. Scoped Ruff, compileall and git diff --check exit0.
Tests use the existing scrubbed Unix-only sandbox harness and fresh random owned
databases/roles with fixture cleanup verification, not an application database.

Evidence includes real paired synthetic credential resolution; committed reservation
before a single fake fetch; unchanged raw DTO; new transaction reads/replay; partial
and late response; post-fetch session/membership/scope/account/credential revocation;
stale actor permission denial; malformed ticket/payload rejection; caller Connection
rejection without committing its root; and both actual session-revocation lock-winner
orders checked through PostgreSQL blocking PIDs. No network/provider/LLM is invoked.

Independent scoped critic found no important defect. The commit-error test injects
an exception in SQLAlchemy's pre-physical-COMMIT event: it proves sanitized error and
rollback for that injection, NOT unknown-result network/driver commit recovery.
Running reservations remain after failed fetch/publication; automatic recovery and
failed-run finalization are not implemented. A process restart is not simulated.

Remaining wiring gates: accepted T1 physical-transaction guard follow-up; approved
Review-specific default-off exact org/account selector; actual trusted manual HTTP
principal and explicit account selection; paired secret used by the actual fetch;
same received DTO also consumed by legacy without extra fetch. Do not reuse collector
flags or fabricate the scheduler principal. No public canonical read API in this slice.
Local policies/send/notifications still await their accepted DDL and domain services.

Rollback: this dormant module can be removed without changing existing routes.
The underlying lossless writer rollback must retain decoder-capable dab11ec and
expanded0065; old TEXT-only binaries/schema downgrade are not a valid rollback.

## Physical-guard integration checkpoint

T1 controller-approved8338ef7 lineage merged as2dc09de. Static independent T4
integration review found no introduced incompatibility: own Engine-bound roots,
no savepoints, safe error mapping and return-after-commit remain aligned.

The expanded eight-file run is **not green**:314PASS/20setupERROR37.28s.
Credential SQLite fixture test_marketplace_credential_store.store_db uses global
Base.metadata.create_all; collection of Review ORM adds PostgreSQL JSONB coverage,
which SQLite cannot compile. Minimal collection-only witness (shadow test file plus
credential-fetch file, selecting only test_paired_fetch_exact_identity_and_closed_session)
gives1ERROR/38deselected0.74s; that same credential test alone gives1PASS0.48s.
This is evidence of fixture metadata contamination, not a waived baseline or a
proven runtime guard defect. Exact reproduction sent to T1, who owns that fixture;
no SQLite type weakening, skip or shared-file patch by T4. Combined acceptance
must be rerun after the owner-supplied fix. Review selector still pending/off.

## Fixture blocker closed with actual Review collection

Exact T1 controller-approved34b5f62 was cherry-picked as79f83b9, preserving its
test-only patch and avoiding unrelated pending approvals migration ancestry.
The same runtime fixture patch-id is0bfa989ee0226f69b82f8392985e22902e406849.
All eight original files were retained and credential-store/fixture-isolation
files added: **349 passed in27.69s** with Review files first, and **349 passed
in31.46s** with the entire file list reversed. Both exited0 with no skips and
the actual Review ORM loaded. The original20setup errors are resolved for this
mixed gate; no exclusion or JSONB/schema weakening. This is not a full backend run.

Ordered file list (second run exactly reversed), from backend:
test_review_shadow_service.py, test_review_publication_repository.py,
test_publication_guard.py, test_publication_guard_postgres.py,
test_marketplace_account_context.py, test_marketplace_account_context_postgres.py,
test_marketplace_credential_fetch.py, test_marketplace_credential_fetch_postgres.py,
test_marketplace_credential_fixture_isolation.py, test_marketplace_credential_store.py.
All under tests/, using the same scrubbed Unix-only sandbox/disposable DB harness
as the failed run. Fixture cleanup checks ran on natural completion.

New-fixture Ruff, both fixture compileall and diff-check exit0. T1's four unchanged
legacy-file Ruff findings remain disclosed in its handoff, not a whole-lint PASS.
T1 independent review approved the exact patch; T4 checked the actual diff and
independently ran both mixed orders. Review default-off selector is still pending;
neither HTTP integration nor live/provider/production work has been enabled.

## Rollout prerequisite accepted; legacy ownership wiring gate

Exact accepted T1 selector434ff5f cherry-picked9981e72. Independent selector plus
scheduler/canonical-shadow/infra compatibility checks:94PASS2warnings2.81s, exit0;
new-test Ruff/compile/diff0. Two dependency warnings and existing configI001 remain
disclosed, not a whole-lint claim. Both policy fields remain default-off/empty.

Before actual manual HTTP wiring, read-only tracing confirmed a separate boundary:
ReviewFeedbackSyncRequest has no marketplace account selector; existing route fetches
using the user WB token. Legacy ReviewFeedbackRow (app/reviews/orm.py) has only
feedback_id primary key and no organization/account columns. _store_feedback gets
that global key; list_feedbacks queries all rows. The memory map is also global and
_run_db converts SQLAlchemyError into fallback. Canonical guard does not authorize
or isolate these later legacy operations.

Therefore a newly selectable canonical account must not silently feed its DTOs
into the existing global legacy store. No ownership can be inferred from matching
feedback IDs, tokens, current account metadata or the rollout allowlist. Sent exact
evidence to coordinator for a bounded integration decision: either approved trusted
legacy-source compatibility binding without broadened legacy writes, or scoped
legacy/canonical read-write migration before dual-consumer account expansion.
Original legacy route remains unchanged; no HTTP wiring has been implemented.
Received-DTO extraction/tests can be prepared independently, but are not proof of
safe multi-account legacy parity. The selector/physical guard dependencies themselves
are accepted; this remaining issue is domain ownership, not another flag dependency.
