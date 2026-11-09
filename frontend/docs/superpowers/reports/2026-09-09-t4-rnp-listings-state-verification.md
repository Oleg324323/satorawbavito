# T4 RNP cache proof and Avito Listings stale-response fix

## Bounded changes

RNP test mounts the actual React page with fully intercepted synthetic traffic.
It proves the exact cache GET for Aug1–7, loading without rows, populated/empty/503
outcomes and row removal on session loss without another GET. The one obsolete
explanatory-copy source assertion is removed; backend/no-mock/diagnostic guards
remain. The populated fixture deliberately does not claim formula or KPI parity.

Avito Listings had a real stale-response bug: Apply issued the new-period GET,
but retained the previous response. Rows, seven KPI values and the selected detail
remained visible while that request was held. Main independently reproduced
0PASS/2FAIL before changing runtime. Load start now clears the previous response
and selected key; existing abort checks are unchanged. Selected key is not a load
effect dependency, so clearing it cannot itself start another request.

Two browser cases prove initial data, close button/Escape, draft-only changes,
exact Apply GET, held loading/empty rows/no old detail/seven unavailable KPIs,
then empty or HTTP503 response and session clearing. To exercise an already-open
detail at Apply, the real button's React handler is activated with DOM click:
the overlay blocks pointer access. This is explicitly not a normal pointer-flow
claim. Main GREEN:2PASS/0FAIL. Obsolete listing date DOM-ID and unavailable daily
copy assertions are removed; route/load/no-fake-data/close-state guards remain.

## Verification and limits

- RNP focused main run:6PASS/0FAIL/25unselected using the RNP name filter; agent
  exact new-file run:3PASS. This is not a full-suite count.
- Listings main RED:0PASS/2FAIL; after the runtime change:2PASS/0FAIL.
- TypeScript build-check exits0. Vite build exits0, existing large-chunk warning.
- Independent critic found no important issue in either bounded slice; critic
  inspected evidence and code, did not independently execute these browser tests.
- First broad run (excluding the separate in-progress Orders test):376PASS/9FAIL.
  Five additional failed IDs are test timeouts, not silently accepted as baseline.
  One-worker rerun without timeout changes:380PASS/5FAIL; those five timeout IDs
  pass, four old failures remain, and an overview logout assertion exposes a
  pre-existing-heading readiness race. It observed the old chats zero before
  session clearing. The test now waits for the same exact seven unavailable values
  instead of treating an already-visible error heading as logout completion.
  The overview adjustment subsequently passes the assigned focused batch and final
  broad run; exact seven values are still required.

The separate in-progress avitoOrdersBrowser.test.ts was explicitly excluded from
both initial broad commands, not skipped or counted as green. Later full run
includes Orders/Stock new files:405PASS/4FAIL/0pending. Three old source IDs remain;
the other failure exposes the same pre-existing-heading race in reportLoading's
session assertion. It now waits for actual session text; focused4PASS/0FAIL,
not a green whole suite. All admitted gates ended naturally, T1 directly
notified of cleanup/release. No timeout relaxation or skipped tests.

No provider request, production change, schema change, flag activation, actual
printing/export, database mutation or credential operation occurred. Browser
requests are locally fulfilled or aborted. This does not prove render-before-effect
isolation, reversed response ordering, refresh, complete auth/account isolation,
backend authorization or production canary. Rollback is the scoped Listings
runtime diff; RNP additions and all new browser tests are test-only.
