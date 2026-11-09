# T4: actual Stock / Week report state tests

## Scope

Test-only follow-up to the frontend source-test debt at 45e2ccb. No runtime,
generated HTML, backend, schema, rollout or production changes.

The two old source assertions prohibited the name `ReportProgressPanelIsland`
even though the current component renders a human-readable loading state.
Only these two assertions are replaced; all other source guards remain.
`reportLoadingBrowser.test.ts` mounts the actual `VellaHtmlParityPage`, its
generated snapshot and effects, using a synthetic AuthContext and MemoryRouter.

## Behavioral evidence

Four browser cases: Stock and Week-over-week, each with empty or HTTP503 response.
The exact latest-cache GET is held until visible loading is asserted. Releasing
it renders the corresponding empty/error heading, removes progress and leaves no
report rows. Clearing the synthetic access token renders the expired-session
error without another GET. Tests assert exactly one scoped GET, query selection,
Russian page title and zero unexpected requests/page errors.

All browser requests are locally fulfilled or aborted. Image/font requests use
offline stubs; no provider, export, print, build POST or existing server is used.
Vite builds the fixture in memory with config/env files disabled and write=false.
Stock loading and Week empty screenshots were visually inspected: scoped human
progress, then explicit empty state, no fallback report table.

## Verification log

- Initial real-page witness: 4 passed.
- First combined run: 8 passed / 1 failed because default expect.poll allowed
  only 1000ms for the initial React effect. The concurrent broad run reproduced
  the same startup timeout in two cases, not an incorrect response/render.
- Changed only that readiness timeout to explicit 15 seconds, matching the
  existing actual-page test pattern. No retries, skips or application changes.
- Final focused browser + retained source tests: 9 passed, natural exit0.
- TypeScript `tsc -b --noEmit`: exit0. Git diff check: exit0.
- Full-suite rerun after the timeout correction: 356 passed / 17 failed /
  0 pending, natural exit1. Compared exact failed test IDs with the previous
  350/19 baseline: only the two obsolete Stock/Week source cases are closed;
  zero new failed IDs. All four new browser cases pass in this broad run.
- Independent read-only critic found no important issues in the scoped diff.
  No CodeRabbit. Critic did not independently execute the suite.

Artifacts are local `/tmp/satorna-t4-report-states-{witness,focused-final,full-final}.json`
and `/tmp/satorna-t4-{stock,week}-{loading,empty}.png`, not committed runtime data.

## Limits

This is not populated-row, polling, backend job metadata, real authentication,
server permission, performance SLA or production/canary acceptance. Build is
test-fixture-only; an application production bundle was not rebuilt for this
test-only change. The remaining full-suite failures are not waived.

## T1 coordination

The complete T1 `2026-09-09-review-run-binding-design.md` was compared against
T4 request1a3d344 and ready codec45e2ccbb165e6ddcc383311789b84b5d93ef2933.
No important domain mismatch found; direction acceptance was delivered directly
to T1. This does not approve missing DDL or enable public reads/sync. Immutable
historical binding and consumer isolation acceptance remain required.
