# Legacy report case reconciled with current RNP ownership

## Evidence and changes

The old stabilized-report test first failed at Ads `Все SKU`; the actual
standalone select reads `Все товары`, while actual React search is separately
covered by `adsSkuBrowser.test.ts`. Only this obsolete label expectation changed.
HTTP(S) traffic is now aborted and service workers blocked in this local-file
case. No provider request or export/printing action is performed.

The next run reached the 45s test timeout. Reducing the action timeout to 5s
(not increasing the test timeout) exposed the precise next failure: clicking
`#tab-rnp .report-comment-btn`, which does not exist because the old RNP demo
renderer explicitly has no body target. All earlier scenarios had progressed.

Current React RNP renders backend `reasons` in its read-only comment column,
not a demo comment-history action. The populated cache test now supplies a
distinct source explanation and checks its exact cell by the Comment header,
alongside existing loading, empty, failure and session-loss checks. No domain
rule, comment writer or fallback renderer was introduced.

Only the obsolete three-line demo history click/assertion is replaced by a
no-demo-row/action assertion. The rest of the giant case, including later
mobile fallback assertions, remains and executes.

## Actual final checks

- RNP cache browser: **3 passed, 6.92s**, natural exit 0.
- Selected legacy stabilized-report case: **1 passed, 31.21s**, natural exit 0;
  27 other cases not selected by the command, no new skip in source.
- Ads + historical marking compatibility: **2 passed, 4.57s**, natural exit 0.
- TypeScript `tsc -b --noEmit` and `git diff --check`: exit 0.

Every browser uses finally cleanup. Gates are serialized in the explicitly
admitted T4 interval. The prior 45s timeout and subsequent 5s missing-locator
failure are retained as diagnostic evidence, not hidden or called passing.
No fresh whole-suite/build/production acceptance is claimed. The remaining
generic-filter and Digest aggregation broad-test debt is still open.

Main critical pass checked that active data rendering has positive evidence,
legacy rows remain retired, later assertions remain, and no timeout was widened.
This is not independent final integration review.
