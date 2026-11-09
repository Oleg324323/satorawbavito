# T4 Review Facts repository handoff

Date: 2026-09-09. Status: verified **dormant repository**, not completed Stage 2 runtime integration, not send authorization or production readiness.

## Exact prerequisite and owned changes

T1 `56b5fa50b2de41a4f3277392148012233952188c` was merged into T4 as `64f256f`. T1's fixture-only `b9cb131c0fd234ca12941f46b4ade5899c20f50e` and import-only `e396b08556b6a47bdf4bbf6ddf6eb9008c34c5a7` were consumed as `825326d` and `5f1f539`. No shared SQL semantics were authored/changed by T4. These prerequisite commits are not additional T4 work to duplicate in integration.

Own files:

- `app/reviews/canonical_orm.py`: typed column mappings for the four migrated tables. Alembic remains the only schema installer; mappings do not reproduce SQL triggers, RLS, grants or provide an alternate create_all schema.
- `app/reviews/ingestion_contract.py`: strict snapshot admission and deterministic manifest.
- `app/reviews/canonical_repository.py`: synchronous SQLAlchemy Connection repository, explicit `ReviewOwner`, `reserve_run`, `ingest`, `get_fact`.
- `tests/test_review_facts_repository.py`: 35 actual PostgreSQL scenarios/parameter cases.

No router, task, collector, legacy store, default flag, production configuration, provider call or frontend runtime was changed by this slice.

## Command contract

Caller supplies an already-open READ COMMITTED transaction and explicit transaction-local organization context. Each command acquires a savepoint, validates context, locks the exact canonical account before identity decisions, checks connected status and original external-account binding. All writes in a failed command roll back to the savepoint; only the caller may commit the enclosing transaction. No DB transaction is intended to span provider I/O.

`reserve_run(source_run_id, request_checksum, started_at)` returns UUID plus server-issued sequence. It explicitly omits `run_sequence` from INSERT and uses RETURNING. Exact source identity replay compares request checksum; a changed request conflicts. No raw TEXT ON CONFLICT target or hash identity exists. Long incompressible synthetic keys remain accepted.

`ingest(sync_run_id, facts, coverage, completeness, completed_at, expected_versions)` accepts an immutable tuple of already normalized DTOs. Every DTO must match the run/organization/account/provider and recomputed checksum. Coverage has an exact key set and typed fields; complete coverage requires terminal stream evidence. Duplicate IDs and malformed versions fail before writes. The repository derives count and manifest from the full admitted snapshot, rather than trusting caller counts. Terminal replay compares entire manifest, count, completeness and normalized coverage; timestamps of delivery and stale expected versions do not make an exact terminal replay mutate current facts.

Each existing identity requires its exact expected version; a new identity requires zero. Unchanged current content reuses observation evidence, changed content creates the next revision including A→B→A. Run items retain exact observation/checksum ownership. Complete/partial finalization happens after the exact item count agrees. Partial or empty responses never delete/tombstone absent reviews.

`get_fact` is a narrow domain snapshot reader, not the public list/pagination/approval API. Its returned source-order state is mandatory input to future decision wiring; no send is authorized from its data alone.

## Freshness and ambiguity

- SQL sequence is an allocation/watermark token, never provider chronology or completion order.
- Late runs retain immutable evidence without regressing the current pointer or watermark.
- Same-provider-timestamp disagreement with the current observation sets durable ambiguity even when the run finishes late. In that late case only the ambiguity/version changes; watermark/pointer stay unchanged.
- Known older evidence cannot replace current data. The maximum known provider timestamp is recovered from immutable history, so A@T2→B@unknown→C@T1 cannot erase the known T2 fence.
- Historical timestamp checks cannot hide a disagreement with the current observation at the same timestamp.
- Automatic ambiguity clearance requires a later allocated run and a timestamp strictly greater than current/conflict/prior known-history evidence. Unknown conflicting timestamps cannot be cleared automatically. Operator resolution remains a separate audited command.

The history fence is intentionally conservative: late evidence with a future provider timestamp can delay later current advancement. It is not a throughput/freshness SLA. Account locking and scoped history scans can contend; no pagination or performance acceptance is claimed here.

## Verification and criticism

Final combined run: **314 passed, zero skipped, exit 0, 16.55 seconds**: 35 repository + 48 schema + 231 existing Reviews/Notifications tests. PostgreSQL uses only freshly allocated disposable databases and NOSUPERUSER/NOBYPASSRLS runtime roles, with the actual shared privilege script; fixtures verify cleanup of exactly allocated resources. Environment is scrubbed and OS sandbox permits only local PostgreSQL Unix socket while denying IP network and secret-file reads. Expected libpq /dev/null password-file warnings remain fixture diagnostics, not test failures.

Command from `backend` (Python and sandbox paths are local runner prerequisites, not application dependencies):

```sh
env -i PATH=/usr/local/bin:/usr/bin:/bin PGPASSFILE=/dev/null PGSERVICEFILE=/dev/null NETRC=/dev/null PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 ORDERS_TEST_USE_LOCAL_CLUSTER=1 /usr/bin/sandbox-exec -f /Users/bratishka/Downloads/satornawb-main/.worktrees/arch-t1-platform/.superpowers/sdd/2026-09-09-review-facts-schema/local-postgres.sb /Users/bratishka/Downloads/satornawb-main/.worktrees/wave1-integration/backend/.venv/bin/python -m pytest -q --tb=short tests/test_review_facts_repository.py tests/test_review_facts_schema.py tests/test_review_storage_payloads.py tests/test_notification_preferences_read.py tests/test_review_send_recovery.py tests/test_review_provider_lifecycle.py tests/test_review_decision_contract.py tests/test_notification_contract.py tests/test_review_canonical_contract.py
```

Initial RED demonstrated missing repository, then missing ingestion. Freshness regressions independently found by the critic reproduced 2 failures, then a combined history/equality regression reproduced another failure; all were fixed and rerun. Final critic reports scoped PASS, with no independent claim to having run the tests. No CodeRabbit was used. Requested local preflight-critic skill file was unavailable; the available requesting-code-review skill and existing independent critic supplied the review pass.

Ruff on all four own Python files, compileall and diff check exit 0. This is not a full backend/frontend suite or wheel-install certification. Previous frontend baseline failures remain unwaived; no new frontend verification is attributed to this backend slice.

## Remaining dependency gates (do not infer activation)

| Surface | State / necessary next step |
|---|---|
| Live user/session/membership/allowed-account/credential authority | **Blocked on T1 publication guard.** `ReviewOwner` and organization RLS are not authorization; no guessed helper or integrations:write escalation was introduced. User locks must precede account locks and survive commit. |
| Shadow ingestion service | Await guard; reserve before the existing fetch, share that single fetched DTO, revalidate original credential/account binding before publication. No second provider call. |
| Runtime failure lifecycle | Service-level safe failed-run recording, retries of reads only, endpoint safe-error translation and cancellation handling are not wired. No automatic business-command retry. |
| Membership revocation / selected-account denial / credential rebind race | Must be tested against the real shared guard, not simulated as repository authorization. Current tests prove tenant/account binding, disconnect, repository CAS and SQL isolation only. |
| Policies/drafts/decisions and send command storage | Exact T4 requests/serializers already supplied; still await T1-owned schema. Pure domain behavior is not persistent runtime integration. |
| In-app and external Notifications | Typed preferences read exists; event/receipt/delivery persistence and platform destination/policy contracts remain separate gates. |
| Frontend canary, production, legacy removal | Remain off / not authorized by this repository acceptance. Preserve existing owners until parity and release gates. |

No credential values, customer data, production database/Redis, scheduler, printing/export actions or GitHub push were used.
