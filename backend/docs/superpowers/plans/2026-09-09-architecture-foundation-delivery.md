# Architecture foundation delivery plan

> For agentic workers: execute the assigned bounded package against the existing original specs and current owner handoffs. The latest user instruction overrides per-change TDD/review loops: finish implementation, then consolidated important gates. No additional planning or speculative features.

**Goal:** Deliver an integrated, verified foundation for existing functions, with an explicit deferred backlog; do not claim the entire original product specification is complete.

**Architecture:** Reuse the implemented T1–T4 account-scoped storage, immutable facts, authorization and transaction boundaries. Keep one migration owner and explicit ownership for shared files. Unapproved operations and external actions remain disabled; a storage participant is not a completed authorized user workflow.

**Tech stack:** Existing Python/FastAPI/PostgreSQL/Alembic and TypeScript frontend; no new services or dependencies merely for this milestone.

**Spec:** Original T1–T4 assignments and owner handoffs, amended by the user's latest architecture-first/foundation-first instruction. This plan supersedes the active-scope section of architecture-integration-acceptance.md for the current delivery only; the original incomplete requirements remain recorded.

## Priority and ownership — finite current delivery

P0 = blocks safe foundation delivery. P1 = recorded for the next implementation phase. P2 = explicitly excluded/deferred by user. “Existing source” is not verification evidence.

| Priority / urgency | Task and completion boundary | Owner | Dependency / state |
|---|---|---|---|
| P0 / now | Finish the in-progress 0079 migration and inert provision/deprovision contracts; one coherent Alembic graph, no new migration family for this milestone | T1 | Integrated; actual empty bootstrap to 0079 and downgrade/reupgrade passed |
| P0 / now | Preserve credential encryption, paired account scope, dedicated executor resolver, guarded management and persisted readback; fix concrete integration defects in these existing changes | T1 | Existing implementations; final verification pending |
| P0 / now | Consolidate actual repricer approval/worker, versioned settings/account-state, current source and daily participants against the exact T1 schemas; retain immutable calculation inputs and transaction/error boundaries | T2 | Existing source; do not invent public permission mappings or financial rules |
| P0 / now | Finish/repair already implemented Orders user/job/token publication and scoped reads; preserve partial evidence, replay and commit semantics | T3 | f007da2 plus required T1 ancestry; do not invent completeness/fulfillment/filter rules |
| P0 / now | Consolidate existing Reviews local/send/notification participants and typed clients; maintain account/session invalidation, decimal IDs and safe error contracts | T4 | Original domain branch a331a1d; delegated shared changes go through T1 |
| P0 / parallel | Installed package/import readiness, existing release gate and natural test-process shutdown; fix concrete failures only, no new observability subsystem | T3 | Temporary ownership of backend/ops/release_gate.py, backend/tests/test_release_gate.py, backend/tests/test_installed_wheel.py; other shared files require explicit transfer |
| P0 / parallel | Frontend type compatibility with actual implemented HTTP contracts, existing default-off behavior and preservation of legacy functions | T4 | No new inbox/list/filter UI, no provider activation |
| P0 / after source packages | Merge immutable terminal source packages and prerequisites, resolve overlaps once, connect existing entry points only where authorization/configuration contracts are defined | root | Integration checkout architecture-integration-filipp; never merge active files |
| P0 / final | Disposable migration bootstrap/upgrade and restricted-role isolation; credential boundaries; atomic writes/CAS/replay and representative crash/revocation/concurrency paths for changed domains | root with T1/T2/T3 | One consolidated PostgreSQL run; heavy queue exclusive |
| P0 / final | Integrated backend suite, frontend suite, typecheck/build, installed-wheel entry points and natural exit | root with T3/T4 | One final run per combined tree; failures classified, no skips/timeout inflation to claim success |
| P0 / after failures | Fix actual integration defects in owner files, rerun affected gates and the relevant release gate; no redundant repeats of unchanged independent gates | responsible owner + root | Evidence tied to final commit |
| P0 / last | Final requirement-to-result review, remaining-input/deferred list, fresh fetch, normal verified filipp push and remote SHA confirmation | root | No force push; preserve others' commits; no production rollout |
| P1 / later | Operational maintenance CLI/runbooks/provisioning rollout, real backfill/cutover/restore exercises and key custody decisions beyond the minimal safe inert foundation | T1 future | Never enable migration/destructive cleanup without verified recovery; current 0079 retained, not claimed an operational migration solution |
| P1 / later | Live heartbeat/monitoring expansion, broader CI/infra improvements not needed for current import/build/release correctness | future | Existing behavior preserved; fix an actual current release blocker as P0 |
| P1 / needs decisions | Org-wide settings authority; assignment/liquidation/daily review mutation permissions, negative-margin confirmation and financial/KTR/freshness rules | user + future owners | Keep unsupported mutations closed and profit fields null; not completed workflows |
| P1 / needs contracts | Notification discovery/preferences/fair scheduling; provider send/recovery semantics and new browser capture provenance/limits; complete account traversal and source ordering/fulfillment/deadline/filter rules | future owners | Existing dormant safe participants remain; no fabricated source or new product policy |
| P1 / later | Actual provider adapters, full UI switch, legacy writer retirement and operational cutover where accepted provider/policy evidence is absent | future owners | Foundation must preserve one effective writer; dormant alternative must not become a competing active writer |
| P2 / user deferred | Production assembly/printing: assignments/quantities/batches/calendar/sheets/XLSX/A4/labels/archive/reprint/delivery | deferred | Preserve existing code/schema and necessary compatibility, do not implement now |
| P2 / user excluded | KIZ and standalone matcher | excluded | No new work; preserve unrelated returns matching, Catalog and historical compatibility |

## Execution and check boundaries

- [x] T1 completes the finite current source package and releases shared ownership. If 0079 needs a larger unimplemented operational subsystem, keep it inert and explicitly incomplete; do not silently turn it into the next large project.
- [x] T2, T3 and T4 complete the assigned existing architecture packages concurrently. Missing a business rule is a recorded limitation, not permission to invent one.
- [x] Root integrates exact immutable commits. Keep original T4 domain a331a1d separate from codex/t4-credential-management shared commits already imported by T1.
- [x] Run the consolidated important gates after source completion. Terminal owners may finish implementation and prepare the existing gate inputs, but do not each launch duplicate full suites.
- [x] Correct failures, recheck affected behavior, record final source SHA and actual gate result.
- [x] Publish the verified foundation with the explicit P1/P2 backlog. Never call source-only, skipped or policy-blocked behavior fully implemented.

Heavy resource order: root/T1 migration and combined PostgreSQL, then root/T3 full backend, then root/T4 frontend/build. Only one active heavy owner; no reservation while planning and no interruption of running processes. Lightweight source work can proceed in parallel.

No repeated per-commit critics, no broad new security audit, no extra codec/fixture project while delivery work remains, no invented completion percentages or unsupported ETA. A final independent review of the nearly finished result remains required.

## Current final acceptance checkpoint

All four original source packages merged at 6939bc797662655fe1708e9b8c724f5d39e7eca1. Final runtime source is 0fb2d7a, including the API/worker followups. Inputs: T1 79e4621, T2 40213ff, T3 bf6aa9a, T4 original domain a331a1d. This is foundation scope only.

- Initial combined backend on c4d1b9a: 5426 passed, 135 failed, 448 errors, no skips; natural exit in 1316.23 s. Do not claim the backend is green. Of 105 historical failing IDs, 35 now passed and 70 still failed; 65 failures were new. Errors grouped into 297 current-schema fixture errors, 146 shared ORM metadata errors and 5 migration harness errors.
- Concrete causes repaired: SQL migration parsing (c4d1b9a), isolated Review ORM metadata (ff6ca4a), historical immutable-storage fixtures (38956d4), Orders harness and readback contracts (de94e55/9eb5ca8), platform fixture/error boundaries (23bba88), executor account-version read privilege (117c14b).
- T1 related platform gate: 358 passed, no failures/errors/skips, 178.36 s, approved Python 3.11, natural exit and owned disposable cleanup. API fixture followup is complete (see final result below).
- T3: 15 related Orders/legacy migration cases passed; corrected empty-database head roundtrip then passed separately (6.96 s). No repeated full group. Actual upgrade to 0079, downgrade to 0078 and reupgrade passed.
- T4: all 12 residual Review cases passed on project-compatible Python 3.11 / Unicode 14, 20.97 s, natural exit and owned cleanup. No Unicode guard weakening.
- Frontend: unchanged original suite 494 passed; subsequently added notification-client tests 14 passed on integrated tree. Actual npm build (snapshot generation, TypeScript project build, Vite) passed. Only generated timestamp changed and was restored; source snapshot hash identical. Existing chunk-size warning is deferred optimization, not a build failure.
- Main/wheel/release entrypoints: 26 passed; later registered existing Review read/local routes separately passed 17 registration/auth/default-off cases. Final delta review is bounded to actual fixes.
- Final residual results: worker15 PASS; combined metadata139 PASS; credential API/WB14 PASS plus corrected legacy UTC case1 PASS, then integrated same case1 PASS7.65s. T3 complete ID reconciliation found no unclassified new failure/error family. Root reviewed the final runtime diff. All implementation and important verification P0 items are complete; ordinary publication completed; remote SHA confirmed.

Foundation acceptance is complete with 70 explicit unchanged legacy failures; a new full green suite is not claimed. Published to origin/filipp; initial publication SHA 167bd0c0919908d4012f1123a2819a85447f7ae6 matched ls-remote. Heavy gates are sequential; source fixes run concurrently. Existing passed independent gates are not rerun without an affected dependency. No new product features, maintenance CLI or excluded modules are being implemented.
