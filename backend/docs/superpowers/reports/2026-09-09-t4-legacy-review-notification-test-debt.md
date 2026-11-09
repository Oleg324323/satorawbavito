# T4: four historical Review/Notification test failures

T1 requested current reproduction of four IDs still in `ops/legacy-test-failures.txt`.
They were not assumed to fail merely from that list. On parent176d243 a fresh exact
four-test run reproduced all four failures, naturalexit1,2warnings,3.05s, under
scrubbed environment, fake WB/Avito and OS all-network denial. No actual DB/Redis,
provider request, message, scheduler, print/export or working JSON file was used.

## Actual causes and bounded corrections

1. Three Review tests mocked generation but omitted its now-required configured
   `aiPrompt`. Actual service rejects before generation with409
   `REVIEW_AI_PROMPT_REQUIRED`; the permission test then indexed absent `data`.
   Tests now explicitly configure a synthetic prompt through the existing settings
   endpoint with sync `enabled=false`. No runtime prompt guard is removed.
2. After that preparation, two tests exposed outdated automation expectations:
   default `lowStarsAction=alert-block` keeps the two-star draft blocked even after
   approval; default `automationMode=draft-first` requires approval even for a clean
   five-star reply. Assertions now preserve these stronger existing restrictions.
   Neither auto-send nor an activation flag is enabled to satisfy old expectations.
   Intermediate run:2PASS/2FAIL,2warnings,2.48s. A new negative test explicitly proves
   empty prompt409 before generation; the fake generation boundary fails if called.
3. Notification test expected persistence without provisioning its SQL cache.
   Existing `save_source_cache` catches DB failure and returns a transient payload;
   its later read does not recover that payload, so the test raised StopIteration.
   Test now owns an isolated SQLite in-memory engine/StaticPool, exact organization
   and legacy source-cache tables, enabled FK checking and a real session factory.
   Only Redis get/set/delete infrastructure is stubbed. It exercises actual cache
   SQL/commit and HTTP list/read-mark, then verifies payload in separate sessions.
   Engine is disposed after each test; no runtime memory fallback is introduced.

Only two test files change. Existing four test IDs remain, none renamed/skipped or
weakened to accept failure. Missing prompt/no-send/auth behavior is still asserted;
externalRequestPath remains null and forbidden caller still receives403. Test
generation is synthetic; live providers are not called. Runtime source, schemas,
main registration, canonical services and global baseline file remain unchanged.

## Acceptance and limits

Final entire `test_wb_reviews.py` plus `test_notifications.py`: **11PASS,2known framework
warnings,3.12s**, naturalexit0. Scoped Ruff/diff0. All-network-denied sandbox, env-i,
bytecode/cache/plugins off, fake WB/Avito; application DB and Redis set to unreachable
synthetic loopback URLs. TestClient is in-process, no server opened. No heavy PG gate
or shared data resource was used. Main critical pass checked no runtime guard changes,
actual legacy SQL roundtrip rather than dictionary-storage mock, retained IDs and
explicit distinction between defaults and authorizations.

This does NOT fix legacy architecture: source-cache persistence errors still have
the existing unsafe success/empty behavior; legacy Reviews still use their global
store/fallback, and notification read flags are not canonical per-member receipts.
SQLite roundtrip is not PostgreSQL/RLS/CAS/restart parity. The new canonical service
work remains necessary. No full backend result or subtraction-based baseline count
is inferred from this focused run. T1/root own baseline reconciliation after shared
integration; remove these four historical IDs only in the verified integrated gate.
Root final independent review remains; no production or publication approval.

Exact historical IDs checked:

- tests/test_wb_reviews.py::test_reviews_send_requires_reviews_send_permission
- tests/test_wb_reviews.py::test_reviews_sync_generate_approve_and_send_workflow
- tests/test_wb_reviews.py::test_safe_review_generation_stays_draft_only_without_live_send
- tests/test_notifications.py::test_notifications_api_returns_and_marks_wb_sync_event_read
