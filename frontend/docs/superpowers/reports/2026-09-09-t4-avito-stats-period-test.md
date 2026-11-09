# T4: actual Avito statistics period test

Test-only successor to33e0ef6. `avitoStatsPeriodBrowser.test.ts` mounts the actual
Vella React page at `/avito/stats` with the committed synthetic AuthContext fixture,
generated snapshot and real effects. No runtime, backend or schema changes.

## Behavior

Initial empty backend response is rendered with matching date inputs/applied
period. Filling the actual accessible date inputs changes only draft state:
the displayed applied period and request count stay unchanged. Clicking the
real Apply button results in one additional exact `dateFrom/dateTo` GET without
forceRefresh. Applied-period copy changes and the empty state returns. Clearing
the synthetic token renders session error without another request.

Only two obsolete source assertions requiring `avitoStatsDateFrom/To` DOM IDs
are removed from avitoLiveIntegration.test.ts. All other guards are retained.
No inferred canonical Avito contract, multi-account safety or activation claim.

## Safety and evidence

All requests are locally fulfilled or aborted, including offline image/font
stubs. Only the scoped stats GET is permitted; no provider refresh, messages,
export, print, credentials, production or live backend. Vite fixture compilation
is in-memory with write=false, configFile=false and envFile=false.

- Initial actual-page witness:1PASS, naturalexit0.
- Focused test plus retained Avito source guards:15PASS/5 known sourceFAIL,
  naturalexit1; the stats-period case is closed, other five left unchanged.
- In-memory mutation `SATORNA_AVITO_STATS_TEST_MUTATION=apply` inserts an immediate
  return into the unique Apply handler only in the test bundle. Unknown modes
  or missing/duplicate targets throw. Actual mutation:0PASS/1FAIL, naturalexit1,
  at the second-request assertion after Apply (15s bounded timeout), as intended.
- Final full suite:358PASS/16FAIL/0pending, naturalexit1, versus356/17. Exact
  failed-ID comparison closes only the old stats-period case; zero new failures.
  New actual-page case passes with mutation disabled. Final TypeScript and
  git diff checks exit0. The remaining16failures are unwaived.
- Independent read-only critic found no important issue. No CodeRabbit; critic
  did not independently execute tests.

Local test artifacts: `/tmp/satorna-t4-avito-period-{witness,focused,mutation,full}.json`.

## Limits

Empty response only. Logout checks no new request and an unavailable KPI; it does
not prove clearing previously populated KPI data. No delayed/debounced-request,
invalid date, refresh/cooldown, populated row, backend auth/tenant isolation or
production parity proof. Other source-test failures are not waived. Production
application build was not repeated for this test-only change.
