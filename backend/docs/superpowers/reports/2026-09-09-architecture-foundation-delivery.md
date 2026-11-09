# Architecture foundation delivery

Status: foundation accepted within the finite scope below and published to filipp. Initial publication 167bd0c0919908d4012f1123a2819a85447f7ae6 was confirmed by ls-remote; this followup changes publication bookkeeping only. The latest foundation-first instruction controls scope. The original complete product specification is not claimed finished.

## Delivered source and boundaries

| Owner | Foundation included | Explicit remaining boundary |
|---|---|---|
| T1 | Coherent Alembic chain through 0079; encrypted credentials; account/session authorization; publication guards; management and readback; inert maintenance schema/provision artifacts | No operational maintenance CLI/backfill/cutover or invented permission policy |
| T2 | Repricer approval/executor persistence, replay and guarded transitions; versioned settings/account-state and source/daily transaction participants | Unapproved financial formulas, organization-wide permissions and missing mutation policies remain closed |
| T3 | Account-scoped Orders reads and existing user/job/token publication; partial evidence, replay, uncertain-commit readback; packaging/release harness | Completeness, fulfillment, ordering, capture provenance and provider policies require actual contracts |
| T4 | Canonical Review facts/local workflow/send/notification persistence and guarded composition; typed clients and existing defined read/local entry points | Provider adapters, notification discovery/preferences, new UI and operational activation remain deferred |
| Root | Immutable source integration, shared-file conflict resolution, important acceptance gates and actual failure repair | Ordinary publication to filipp; no production deployment |

Production assembly/printing and KIZ/standalone matcher remain excluded from current work. Production here does not mean all Avito functionality. Existing compatibility is preserved. The detailed priority/urgency matrix is in `../plans/2026-09-09-architecture-foundation-delivery.md`.

## Evidence

All original packages combined at 6939bc797662655fe1708e9b8c724f5d39e7eca1 (T1 79e4621, T2 40213ff, T3 bf6aa9a, T4 a331a1d). Final fixes are recorded in subsequent commits; latest source SHA must be taken from final publication.

| Gate | Actual result | Scope / evidence |
|---|---|---|
| First full backend | 5426 passed / 135 failed / 448 errors / no skips, natural exit, 1316.23 s | c4d1b9a, `/tmp/satorna-foundation-backend-20260909.xml`; diagnostic initial run, not final green acceptance |
| Platform related correction | 358 passed, 178.36 s, natural exit and owned DB/role cleanup | d2a9cc1 (integrated as 23bba88), `/tmp/satorna-t1-platform-common-final-green.log` |
| Orders publication / legacy migration | 15 passed; corrected head roundtrip separately 1 passed, 6.96 s | b12cb99/89f008c; actual empty upgrade to 0079, downgrade to 0078 and reupgrade; cleanup verified |
| Review residuals | 12 passed, 20.97 s, natural exit and cleanup | e001b54 (38956d4); immutable TRUNCATE/downgrade/overload and physical-session send/CAS/rollback cases |
| Repricer executor | 15 passed, 16.00 s, natural exit and cleanup | Exact one-column SELECT fix 77fc30e (117c14b); no worker algorithm or guard weakening |
| Combined legacy metadata regression | 139 passed, 18.18 s, natural exit | bedc71d; 137 originally failing/error IDs plus two isolation cases, `/tmp/satorna-foundation-metadata-residual-20260909.xml` |
| Credential API / WB fixtures | 14 passed in initial bounded gate; corrected legacy time case separately passed in 7.29 s; integrated case passed again in 7.65 s, natural exit | 6a3f832 (0fb2d7a), actual restricted PG schema/live session; non-UTC DB timestamp converted to UTC preserving instant; no fixture timezone masking |
| Frontend suite | Original 494 passed; subsequently added 14 passed | Combined frontend; `/tmp/satorna-foundation-frontend-20260909.json`, `/tmp/satorna-foundation-frontend-notifications-final.log` |
| Frontend actual build | Passed, natural exit | Snapshot generation → TypeScript build → Vite; `/tmp/satorna-foundation-frontend-build-final.log`; generated hash unchanged, timestamp-only edit restored |
| Main / wheel / release entry points | 26 passed; later Review read/local registration/auth/default-off checks 17 passed | Existing defined routers only; provider/send activation not inferred |
| Independent bounded review | No important findings | Immutable merge/interface review and final runtime deltas; `/tmp/satorna-foundation-final-review-20260909.md`; review does not replace tests |

The PostgreSQL gates used the existing isolated Unix-socket allocator and approved OS sandbox, with no real providers, credentials, production databases or activation. Final compatibility runtime was isolated CPython 3.11.16 / Unicode 14, matching the project's Python 3.11 container contract. Python 3.13 / Unicode 15.1 caused the initial Review send compatibility failures; the guard was retained.

## Failure accounting and limitations

The initial complete XML contained 65 new failure IDs, 70 old failure IDs and 448 errors. Explicit XML pass records confirm 35 of the original 105 baseline failures passed in that run. A bounded independent ID reconciliation found no unclassified new failure/error family. Corrected groups were rechecked individually and on the combined tree where shared metadata was involved; no repeated whole-suite run is represented as having occurred.

The expected legacy failure list was reduced from 105 to 70, removing only the 35 explicit PASS IDs in the complete initial JUnit. No new failure ID was accepted, and strict release-gate comparison/exit semantics were unchanged. The 70 remaining IDs were unaffected by the fixes; a redundant legacy-only rerun was not performed. This is initial full-suite evidence plus subsequent affected regression gates, not a new complete green run. Do not claim all legacy backend tests pass or that the complete product is operationally ready.

Initial complete JUnit SHA-256: `47e138c22f450bb57fc8b86e03e53fe14b153e4fd5db90bc62e8cc8e2508547e`.

## Deferred decisions

- Operational credential maintenance, key custody, rollout/backfill/restore and deployment configuration.
- Organization-wide settings authority; assignment/liquidation/daily-review mutation permissions and negative-margin confirmation.
- Approved financial sources, allocation, KTR/freshness and final-profit rules; unsupported final values remain null.
- Provider send/recovery contracts, complete source traversal/ordering/fulfillment/deadline/filter rules and capture provenance/limits.
- Notification discovery/preferences/scheduling, full UI switch and retirement of legacy writers after approved operational cutover.
- User-deferred Production assembly/printing, KIZ and standalone matcher.

No extra product behavior was introduced to resolve missing decisions. Publishing this foundation is not a production deployment or authorization to enable dormant external actions.

## Final integration acceptance

Final runtime source: `0fb2d7a` (subsequent report/plan commits change documentation only). The only final runtime delta normalizes an aware legacy credential timestamp to UTC without guessing the zone of a naive value. Root reviewed the complete two-file diff and the integrated non-UTC regression passed naturally: `/tmp/satorna-foundation-final-integrated-api-20260909.log`, 1 passed in 7.65 s.

All identified new failure/error families from the initial full run have corresponding corrected, passing affected gates. The 70 unchanged legacy failures remain explicit debt. No claim of a fresh entirely green backend suite, complete original product delivery or operational activation is made. Final priority and deferred-input boundaries remain unchanged.
