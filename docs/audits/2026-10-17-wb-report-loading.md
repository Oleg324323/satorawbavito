# WB report loading: local implementation and verification

Date: 2026-10-17 (Europe/Moscow).

Scope: Sales Funnel, ABC, RNP, P&L and Week-over-Week only. Starting revision:
`1e6345f65ff9a9893bfdb26a8d172677dc51430b`, branch
`codex/fix-funnel-read-20261016`, PR #7 checkout. The existing funnel JSON-query
bounding fix in that revision is preserved. Implementation and tests are saved
locally; no commit, push, deployment, production restart or migration was run.

## Evidence boundary

Requests to the production origin failed during TLS negotiation, before an HTTP
response (`SSL_ERROR_SYSCALL`, HTTP 000). The deployed SHA, production query plans,
database coverage, authenticated responses, queue timings and proxy/API logs
were therefore not accessible. The user's screenshots establish symptoms, not
their precise production causes. Optional sanitized diagnostics were requested;
they were not required for the isolated local work below.

The checked-in deployment README describes Nginx for app.comcookie.store, with
Caddy as a separate deployment option. `deploy/nginx-app.conf` contains 300-second
upstream read/send timeouts. Neither configuration nor its deployment was changed
or verified live. A screenshot saying Bad Gateway does not prove a timeout, an
OOM, a worker failure, or which reverse proxy produced it. Increasing timeouts
is not the implemented remedy.

All tests use disposable data, synthetic transports and/or intercepted browser
requests. No WB prices, discounts, seller settings or real product data were
written. No live financial reconciliation or campaign-throughput claim is made.

## Confirmed code paths and changes

### Sales Funnel

- The initial saved-data GET is a database read, not a direct WB download. The
  frontend waited for it before starting collection and had no explicit deadline.
- `wb_sales_funnel.read_page` repeatedly materialized the same catalog/projection
  for counts, sums, coverage, timestamps and conversion cohorts.
- Combined those aggregates into one SELECT and reused one identity/ready result
  for unresolved and provider-omitted products. The bounded PostgreSQL CTE,
  filters, server pagination, account scoping and CR cohort rules remain intact.
- Added report-local request deadlines and cancellation. Even a stalled shared
  authentication-refresh wait cannot leave the report indefinitely loading.
  Timeout is an explicit recoverable error, not a successful empty report.
- Did not reintroduce overall CTR, mark omitted products as zero, or change CR.

### Heavy saved reports, ABC and P&L

- A cache miss previously allowed expensive DB preview/report assembly inside
  `latest-cache`, including raw financial ledgers and both comparison periods.
  A source correction could also reject a displayable same-period snapshot and
  lead back to this path.
- For ABC/P&L/RNP/Week, a cold saved-report GET now returns the existing missing
  cache response (404). The existing jobs endpoint builds in the worker. Valid
  same-period stale snapshots remain displayable while being refreshed; schema,
  permission and economics/settings revision checks remain enforced.
- The worker publishes clearly provisional DB-only snapshots before collection,
  after relevant completed windows and at throttled progress intervals. Status
  GETs do not themselves assemble financial reports. Preview failure does not
  cancel collection, and a preview is never a completed source receipt.
- Financial coverage checks previously hydrated the full saved operations JSON
  even when only completion flags were needed. They now query native JSON boolean
  flags only, scoped to the organization/source. A missing flag, numeric value
  or string `"true"` is not a completed cursor.
- Existing advertising batch persistence, exact-request checkpoints, missing
  ranges, credential scoping and WB rate-budget coordination are reused. No
  endpoints, batch sizes, provider concurrency or WB retry limits were increased.
  The observed 500/705 and 600/705 progress cannot be timed or reconciled locally.
- Preserve the settings revision for a partial tax result, but never accept a
  transient `tax_policy_unavailable` calculation as fresh. Unknown costs, tax
  and advertising remain unknown; a background retry can recover a transient
  resolver failure. No tax formula/rate/regime changed.

### Week-over-Week

- The worker previously merged source windows across the selected and previous
  periods and sorted chronologically. Older comparison data could run first.
  The canonical frontend separately requested both financial periods using
  `Promise.all`.
- Collection now plans independent periods and runs all selected-period windows
  before new comparison collection. Current-only worker previews skip previous
  financial, funnel, ad and stock-history reads. The current stock snapshot is
  collected once, not mistaken for historical stock.
- A terminal current funnel/finance error is recorded per source and does not
  skip independent current sources or permanently starve the previous period.
  Authentication failures, demand cancellation and rate-limit retries still
  stop/retry appropriately; throttled current work does not jump ahead to the
  previous period. Missing coverage stays explicit.
- Complete cached comparison coverage produces no new WB requests. Previously
  materialized same-period snapshots remain usable while collection proceeds.
- The canonical frontend displays current operational rows immediately, then
  finishes the selected financial request before starting the previous request.
  Comparison failure retains current values. Deltas remain unavailable until
  both operands are valid. Financial/storage definitions and period rules are
  unchanged.

### RNP and report coordination

- Removed the complete manual refresh row and its duplicate polling effect.
  The existing automatic saved-data/job loader is the sole loading owner on
  entry/date selection. A contextual cold-error retry remains available.
- Added an organization/period/source/finance-permission-scoped Redis start lock
  for the four affected background reports. Check-and-dispatch is serialized
  across API workers; Redis failure fails closed rather than dispatching blindly.
- Build-only tasks persist their queued UUID before dispatch, so a fast worker's
  completed state cannot be overwritten by a late HTTP handler write.
- These reports tolerate the existing worker-aware 15-minute queued grace instead
  of expiring after 30 seconds. Abandoned jobs still become stale. Report-local
  status reads back off from 2 to at most 10 seconds (up to 30 during throttling),
  with a 20-minute elapsed loading budget, bounded individual requests and abort
  cleanup. Provider retry headers remain the collector's responsibility.
- Cached data survives a refresh failure and retains an update-error notice.
  Late responses cannot replace a newly selected period.

## Files

Backend implementation: `app/wb_sales_funnel.py`, `app/wb_abc_loading.py`,
`app/repricer_cache/store.py`, `app/repricer_tasks.py`,
`app/routers/wb_reports_bff.py`, `app/wb_week_sources.py`,
`app/wb_week_finance.py` (paths relative to `backend/`).

Frontend implementation: `src/features/wb-reports/reportRequest.ts`,
`WbSalesFunnel.tsx`, and `src/features/vella-parity/VellaHtmlParityPage.tsx`,
`backgroundReportPolling.ts`, `pnlReportJob.ts` (relative to `frontend/`).
The shared request/auth client is not changed. Unaffected report IDs keep their
previous request/polling behavior. Browser tests can use an explicitly supplied
Chrome executable; they do not use the user's browser profile.

## Verification

Final targeted backend run: **620 passed**, one dependency deprecation warning.
Files: `test_wb_report_loading`, `test_wb_sales_funnel`, `test_wb_abc_loading`,
`test_wb_week_sources`, `test_wb_week_finance`, `test_wb_week_buyouts`,
`test_abc_pnl_costs`, `test_wb_pnl_expense_api`, `test_report_job_cache_lifecycle`,
`test_pnl_payload_cache_permissions`, `test_disabled_cash_flow_reports`,
`test_wb_reports_bff`, `test_reports_sources_runtime`, `test_finance_normalization`,
`test_legacy_finance_tax`, `test_wb_quarterly_tax`, `test_legacy_report_tax`.
Run from `backend/` with `../.venv/bin/python -m pytest ... -q`.

These cover source reuse, flags without ledger hydration, current-first ordering,
cached previous-period reuse, current terminal failure, 429 retry headers,
concurrent duplicate starts, fast-worker completion, provisional snapshots,
permission isolation, product matching, signed corrections/returns, inclusive
dates, cost dates and tax selection. They do not reconcile the merchant's live
01–06 October financial totals.

Final targeted frontend checks: **38 passed** in 11 files, plus **3 passed** in
the existing funnel/browser row-protection smoke suites. New real-browser cases
exercise a stalled initial GET, finite error and retry, current rows while
comparison is pending, and RNP date changes without the removed refresh row.
Unit cases cover automatic cold initialization, complete cache reuse, late or
wrong-period responses, cached refresh failure and elapsed polling limits.
The source-pipeline tests exercise 01–06 and 11–14 October; Week comparison uses
07–11 versus 02–06 October. Browser network is intercepted throughout.

Commands from `frontend/`:

```sh
PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' npm run test -- --maxWorkers=1 --minWorkers=1 <targeted-files>
npm run typecheck
npx vite build
```

TypeScript and Vite production build passed. Existing Browserslist age and large
bundle warnings remain. Full `npm run build` was deliberately not used because
it also packages unrelated extensions. The generator-only snapshot change was
removed from the final diff.

### Broader-suite limitations, not hidden green claims

- An exploratory unchanged-HEAD canonical frontend suite had **63 passed /
  2 failed**, matching the current failures: the five-minute request-cache call
  count expectation and obsolete missing-data/CTR copy expectation. These were
  not repaired by unrelated rendering changes.
- The broader baseline browser run had **15 passed / 11 failed**, including old
  calendar/header selectors, legacy route mocks and expansion/detail assertions.
  The obsolete RNP header assertion was corrected in the affected RNP test;
  other baseline failures are not claimed fixed. One ABC search timing failure
  passed when rerun alone. A targeted passing subset is not a fully green suite.
- **22 Redis integration cases** could not start their disposable Redis process:
  this machine's `/usr/local/bin/redis-server` is x86_64 and returns
  `Bad CPU type in executable`. This is a test-environment limitation, not a
  passing distributed-Redis test. New start-lock concurrency tests use isolated
  lock-backed Redis fakes. Real Redis integration must run on compatible CI/VPS.
- No PostgreSQL production EXPLAIN, proxy logs, live queue wait, full WB readiness
  duration, authenticated frontend smoke, or real campaign timing was available.

## Before/after microbenchmark

Same 3,522 synthetic products, selected period 11–14 October, SQLite in memory,
five reads, page size 50. The baseline is the starting SHA above; no provider or
production database is involved.

| Measurement | Baseline | Updated |
| --- | ---: | ---: |
| SQL statements per funnel read | 11 | 5 |
| Joined catalog CTE statements | 9 | 3 |
| Median read time | 51.39 ms | 45.06 ms |
| Products / displayed rows | 3,522 / 50 | 3,522 / 50 |

Summary metrics were identical. This measures application query overhead, not
production latency or real WB collection. A temporary baseline worktree used
for comparisons was archived recoverably; the working checkout is retained.

## Remaining production validation / release boundary

No production access or deployment is implied by these results. After authorized
deployment, verify the deployed SHA and matching API/worker code, worker readiness,
Redis integration, and authenticated requests for the specified periods. Capture
request duration/status, queue wait, source progress and proxy/API errors without
tokens/cookies. Compare identical products, source date definitions and coverage
before claiming P&L reconciliation. If a gateway error remains, correlate it with
the actual proxy and upstream logs rather than changing timeouts speculatively.

The changes require the normal frontend/API/worker release; no database migration
or cache purge is required by this implementation. Production release/restart
still requires explicit authorization.
