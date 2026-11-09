# T4 — Repricer statistics response isolation

## Scope and implementation

Independent frontend-only slice, based on `47ef31e`. No API contract,
backend/schema, account rollout, source HTML, generated snapshot, operational
flag, provider action or production change.

The effect's existing statistics loader is extracted into
`installRepricerStatsLiveBridge`. Each installed instance owns a disposed flag
and invocation generation. Only its current invocation may apply cache-period
metadata, render a response/page, or display/rethrow a request error. A stale
completion returns null and cannot launch additional pages. Cleanup invalidates
pending work and removes the global callback only if it still owns it.

Requests already sent are not cancelled. This is stale-result suppression, not
provider request deduplication, credential revocation or server authorization.

## Evidence

- Actual old loader behavior after mechanical extraction: one browser test
  failed because the newer SKU disappeared after release of the older response
  (`/tmp/satorna-t4-stats-race-witness.json`). The earlier missing-export failure
  was setup RED, not evidence of the race.
- Five browser cases pass, none skipped: late first-page success, late error,
  late success after disposal/logout, late second-page success and error
  (`/tmp/satorna-t4-stats-race-green.json`). Requests are deliberately held,
  released after newer rendering, and awaited to settlement before assertions.
- Tests bundle the actual bridge and rendering functions into an in-memory
  synthetic document. Every request is intercepted; only synthetic-origin GETs
  are accepted. No backend or external provider is contacted.
- Screenshot inspected: newer SKU visible, old SKU absent. This unstyled fixture
  verifies DOM behavior, not production visual parity. Title and page errors are
  checked. Screenshot: `/tmp/satorna-t4-repricer-stats-race.png`.
- TypeScript initially detected an optional-global call after extraction;
  corrected to optional invocation. Final `tsc -b --noEmit` exits 0.
- Vite build exits 0, existing large-chunk warning remains.

Full frontend suite: 287 passed / 21 failed / 0 pending, versus previous
282 / 21 / 0. Exact failed-test identities match, no new or removed failures
(`/tmp/satorna-t4-stats-race-full.json` compared with
`/tmp/satorna-t4-rnp-period-full.json`). These 21 failures remain unwaived debt.
Independent read-only critic: scoped PASS, including the pagination extension;
the final test also asserts exact query/page pairs. No CodeRabbit used.

## Limits and follow-up

The fixture invokes the real bridge but does not mount the entire React page.
It proves stale response/error and row isolation, not complete auth-screen
clearing. Existing loading/error renderers retain previously rendered KPI and
filter-summary values; that is a separate frontend follow-up, not fixed here.
No claim of complete architecture readiness or production canary acceptance.

Rollback: revert this slice; it has no persistent data effects. That restores
the demonstrated stale-response race.
