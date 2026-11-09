# T4 — Replace obsolete Repricer source checks with React behavior

Test-only slice following `c62b71d`; production TypeScript, source HTML and the
generated snapshot are unchanged. Two existing source-string assertions were
obsolete: the route now uses effectiveActiveParityTab and period filtering uses
reportPeriodEventMatches. Other existing source guards remain unchanged.

The replacement browser test mounts actual VellaHtmlParityPage, its dynamic
generated HTML and React effects, using synthetic AuthContext and MemoryRouter.
It does not call the extracted loader directly. It proves:

- One stats GET on route entry, visible synthetic SKU and KPI902.
- A different report's period event does not reload stats; its own event emits
  exactly one additional GET, with exact selected dateFrom/dateTo and new rows.
- Session-token loss clears rows and all four KPI slots without another GET.
- The expected existing page title, no page errors or unexpected API requests.

All requests are intercepted. Observed Google font CSS is fulfilled with empty
offline CSS, images with a synthetic GIF; no remote font/image/provider request
is forwarded. Screenshot `/tmp/satorna-t4-stats-actual-react-page.png` inspected:
real page shell and active statistics table are visible. Fonts use system fallback.

## Mutation evidence

The test's optional SATORNA_STATS_TEST_MUTATION is confined to the in-memory Vite
test transform. It never edits a runtime file or production build configuration.
Unknown mutation names or missing targets fail closed.

- route mutation removes initial-load invocation: actual test fails waiting for
  the live SKU (0 PASS /1 FAIL).
- period mutation removes event-scope guard: actual test fails with3 requests
  instead of2 (0 PASS /1 FAIL).
- No mutation: focused3 tests pass, including remaining source guards.

Initial fixture-only failures (unhandled font fixture, overly specific guessed
title) were corrected after inspecting the actual source/title. These were not
application regressions and are not claimed as bug-fix RED evidence.

Independent critique: scoped PASS, mutation sensitivity reviewed, no important
defects. This establishes real page-effect behavior, not server authentication,
complete login/logout workflow, live API integration or production canary.
No backend, schema, provider, production, print/export or flag actions.

Final broad frontend:349PASS/20FAIL/0pending versus345/21 before this test and
the two final Orders whitespace tests. Exact removed failure is the Repricer
stats source case; zero new failures. Remaining20 are unwaived debt.
`tsc -b --noEmit` and `git diff --check` exit0. No runtime/build inputs changed
in this test-only slice; prior Vite build remains unchanged, not rerun here.
Full run report `/tmp/satorna-t4-stats-page-full.json`.

## Follow-up: inactive strategy loading

The next test-only extension removes one obsolete dependency-array literal from
the performance source test; its checks against pathname dependency and product
reload tick remain. Actual stats-page test verifies no strategies request on a
non-product page. An exact multiline in-memory mutation removes productsTabActive
only from the loadLiveRepricerStrategies effect; the test detects an unexpected
request. Normal adjacent6 tests pass. Independent critic approves this replacement.
This does not prove reload counts across all product-route transitions; no broad
performance completion claim. Runtime files remain unchanged.

Fresh full:350PASS/19FAIL/0pending versus349/20; only the named performance
source case removed from failure set, zero new IDs. Final TypeScript/diff0.
Mutation evidence names unexpected GET `/api/v1/wb-repricer/strategies/catalog`;
it was aborted locally. Full `/tmp/satorna-t4-strategies-scope-full.json`, mutation
`/tmp/satorna-t4-stats-page-strategies-mutation-evidence.json`.
