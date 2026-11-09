# WB financial-report completion

Scope: local WB financial reads and presentation only. Preserve existing dirty
changes. No production writes, deployment, restart, token changes, price actions,
or edits to Avito/authentication/extensions.

## Source and consumer map

| Consumer | Financial source | Operational sources retained |
| --- | --- | --- |
| Repricer / Statistics | Shared posting-date realization projection | Prices, orders, stock, tariffs, campaign performance |
| ABC / P&L | Same ledger, explicit product attribution and account residual | Funnel, catalogue, dated economics, stock observations |
| RNP buyouts | Financial quantity/amount; keep cohort conversion separate | Funnel traffic/orders/conversions and campaign metrics |
| Product financial chart | Persisted finance by posting day | Order/price observations retain their own definitions |
| Week-over-Week | Same source contract independently for both periods | Existing current-first funnel/stock enrichment |
| Canonical/v2 | Explicit posting membership and merchandise classification | Original business date remains available to economics |

## Implementation sequence

1. Reproduce source/coverage defects with isolated regressions. Unify field and
   version checks; retain partial data; reject incompatible legacy fallbacks.
2. Preserve unallocated financial charges and signed classifications without
   altering legacy price-planning defaults. Repair expense-only daily rows.
3. Complete RNP/drawer mappings, ABC money/advertising displays and exports.
4. Remove unnecessary financial-report dependencies, reuse projections, and
   invalidate only affected derived payloads. Keep canonical rollout disabled.
5. Run targeted and expanded regressions and comparable DB-only benchmarks.

Audit reference values (not hard-coded totals): the prior saved 05–11 October
snapshot reconciled to 292,025,355 kopecks and 1,859 net units. Production access
and a fresh WB reconciliation must not be inferred from local fixture tests.

## Result and boundaries

Implemented locally on `codex/fix-funnel-read-20261016`. The checkout already
contained substantial uncommitted work. This delivery does not claim ownership
of the unrelated Avito, extension, authentication, or earlier WB changes shown
by `git status`.

No push, deployment, service restart, price execution, production migration,
token/settings change, or deletion of raw history was performed. The working
SQLite database was opened read-only for measurements. Browser tests used
synthetic responses and installed headless Chrome, not a production session or
Codex browser panel.

## Financial definitions and verified documentation

Official source checked in this task:
[WB Finance OpenAPI](https://dev.wildberries.ru/api/swagger/yaml/ru/13-finances.yaml).
Reuse the existing `POST /api/finance/v1/sales-reports/detailed`, report-list,
and exact-report methods. This is field/endpoint migration, not token rotation.

| Metric | Definition used |
| --- | --- |
| Realization revenue | Signed operation-total `retailAmount`, never multiplied by quantity |
| Gross sales / returns / net quantity | Qualifying merchandise `quantity`; return document reverses units and revenue |
| Period | Inclusive Moscow posting dates `rrDate`; timestamp boundaries are `[start, next day)` |
| Dated economics | Original sale/business date retained independently of posting membership |
| Commission | Existing retail settlement bridge `retailAmount - forPay - acquiringFee`, signed for returns; raw `ppvzSalesCommission` remains distinct evidence |
| Logistics / storage / acceptance | `deliveryService` / `paidStorage` / `paidAcceptance`, signed rubles converted to exact integer kopecks |
| Penalty / deduction | Signed `penalty` / `deduction`; reversals stay reversals, not fabricated compensation |
| Financial advertising | Deduction explicitly classified as WB Promotion by `bonusTypeName`; missing classification with a nonzero deduction is unavailable |
| Compensation / adjustments | Explicit compensation operation and its `forPay`, separate from additional payments, penalty/deduction reversals, acquiring, loyalty and other adjustments |
| Tax / cost | Existing dated configured rules; no new regime, rate or WB-supplied tax assumption |

Orders, traffic, baskets, cohort conversions, physical stock, current prices,
tariffs and campaign analytics retain their operational sources. Realization,
seller-price turnover, payable and bank receipts are not interchangeable.

## Finding-by-finding status

| Finding | Status | Implementation / evidence |
| --- | --- | --- |
| RNP financial buyouts | Fixed locally | Shared Finance projection for both periods; financial-only products retained; funnel cohort quantities and percentage remain separate; no finance read without permission |
| Drawer financial series | Fixed locally | DB posting-day revenue, returns and net units; explicitly selected table dates; order/price series retained; organization-scoped drawer no longer downloads Statistics sales |
| ABC ruble expense cells | Fixed locally | Commission/logistics/storage amounts and percentages shown separately from comparison deltas; legacy and canonical adapters and exports covered |
| Financial ads versus campaigns | Fixed locally | Settlement deductions determine financial expense and profit; campaign spend retained separately; unknown classification is not zero or a campaign fallback |
| Unallocated cabinet charges disappearing | Fixed locally | Account residuals retained in totals, metadata and visible non-product ABC/P&L/Week rows; RNP coverage carries residual evidence; 100 RUB storage regression has no sales weights |
| Storage allocation | Fixed locally, evidence-limited | Only unattributed storage allocated from physical stock on observed historical days; directly attributed charges untouched; uncovered-day balance remains unallocated and history dates are exposed |
| Refunds labelled compensation | Fixed locally | Signed penalty/deduction refunds keep their monetary effect once; explicit 100 RUB penalty reversal regression |
| Reader/planner disagreement | Fixed locally | Shared full-field/currency/connection/date/pagination contract; revenue-only and count-mismatched receipts cannot certify fee coverage |
| Partial periods and old aggregates | Fixed locally | Known values remain visible; missing dates explicit; profit/final deltas withheld where required inputs are incomplete; legacy seller-price aggregates cannot replace realization |
| Artificial P&L rows / daily mismatch | Fixed locally | Zero-impact service fields do not create products; legitimate expense-only rows retain posting-day zero-trade basis; original business dates survive late postings |
| Week derived caches | Fixed locally | Version bumped; DB-only rebuild; selected-period preview preserved; financial ads no longer overwritten by campaign spend; final deltas require both periods |
| Reserved source collector | Verified | Settlement costs use Finance; Analytics penalties/storage/acceptance retained only as explicitly operational drill-down; callers covered by source-runtime tests |
| Canonical/v2 | Fixed locally, rollout blocked pending approved migration | Posting membership and merchandise classification aligned; explicit financial ads and compensation stored; old formula rejected; rollout/allowlists unchanged and not enabled |
| Loading performance | Partially verified | Local DB-only assembly and JSON/SQL measurements below; removed optional shadow campaign download from the ABC/P&L worker while retaining its DB catalogue/economics bridge; no authenticated production latency, queue-wait or live collection measurement |

No finding is labelled “not reproduced” merely because a historical amount may
have changed. The historical report totals are reconciliation references, not
constants in application code.

### Attribution limits

- No revenue-proportional allocation of account charges in public realization.
  Legacy planning defaults are intentionally preserved and separately tested.
- Stock-based storage allocation never substitutes current stock for a missing
  historical day. Allocation metadata identifies observed/missing days and the
  residual; it is an estimate, not a direct WB product charge.
- Unattributed merchandise money/units remain account evidence, not an invented
  product. Unknown product cost/tax blocks a confirmed account profit.
- An ambiguous correction operation is not assumed to create merchandise units.
- Optional missing financial advertising descriptions are not a reason to
  download the same report forever; the amount remains unclassified/unavailable.

## Changed implementation areas

Paths are relative to the repository root; this is the scope of this task, not
the entire pre-existing dirty diff.

- Shared source/coverage: `backend/app/wb_api/financial_reports.py`,
  `backend/app/repricer_page_finance.py`, `backend/app/wb_report_finance.py`,
  `backend/app/wb_abc_loading.py`.
- Reports/readiness/cache integration: `backend/app/wb_reports_sprint_d.py`,
  `backend/app/wb_pnl_expenses.py`, `backend/app/routers/wb_reports_bff.py`,
  `backend/app/repricer_tasks.py`.
- Remaining financial consumers: new `backend/app/wb_rnp_finance.py`, new
  `backend/app/wb_finance_timeseries.py`, `backend/app/repricer_bff.py`,
  `backend/app/routers/wb_repricer_bff.py`, `backend/app/wb_week_finance.py`,
  `backend/app/wb_week_sources.py`.
- Gated canonical path: `backend/app/platform/finance/service.py`,
  `backend/app/platform/finance/orm.py`,
  `backend/app/modules/wb_reports/abc_pnl.py`,
  `backend/app/modules/wb_reports/schemas.py`,
  `backend/app/routers/wb_reports_v2.py`.
- Public compatibility DTOs:
  `backend/backend_contracts/vella_wb_19_05/models.py`.
- Presentation and exports:
  `frontend/src/features/vella-parity/VellaHtmlParityPage.tsx`,
  `frontend/src/features/wb-finance/canonicalAbcPnl.ts`,
  `frontend/src/features/wb-finance/reportTableExport.ts`.
- New focused regressions: `backend/tests/test_wb_finance_completion.py`,
  `backend/tests/test_finance_classified_migration.py`; nearby financial,
  source-lifecycle, canonical, ABC, Week and export fixtures updated to the
  verified source contract.

Derived versions: repricer Finance 4; product report 3; P&L fees 3; Week financial
columns 5; ABC payload 33; P&L payload 19; RNP payload 12; Week payload 14;
SKU list snapshot 22. Canonical formulas are `wb-finance-v4-rrdate` and
`wb-abc-pnl-finance-rrdate-v4`. Old derived results rebuild from compatible raw
operations; raw source history is not rewritten or deleted.

### Additive migration, not applied to the working database

`backend/alembic/versions/20261020_0094_finance_classified_credits.py` follows
0093. It adds nullable advertising and explicit compensation amounts to the
three canonical operation/rollup tables. No existing migration is rewritten.
Old formula snapshots are not relabelled as new evidence. Downgrade refuses to
discard classified evidence and checks across tenants before restoring forced
RLS. Apply this migration only after separate approval before using the new v2
path or Finance shadow ingestion. The deployment preflight must check shadow
settings before starting code that expects these columns. No automatic rollout
or migration occurred.

## Regression verification

- Final expanded financial backend set: **778 passed**, 3 warnings, 28.85 s. Includes
  source selection, reader/planner parity, exact money, dates, both periods,
  deduplication/corrections, account residuals, expense-only rows, history gaps,
  cache revisions, bounded retries and queued-job lifecycle.
  The final run includes both enabled-shadow-mode regressions added after the
  earlier 776-test run.
- Follow-up cache/permission subset: **252 passed**; overlaps the expanded set,
  so these counts must not be added as distinct tests.
- Migration / empty-database checks: **6 passed** on disposable PostgreSQL,
  including upgrade/downgrade and populated-evidence protection. Not a test on
  the working SQLite or production database.
- Frontend focused set: **126 passed**, including canonical nulls/source
  versions, signed fees, ABC ruble cells, sticky headers, polling, source
  selection and export mapping.
- Browser export checks: 15 passed in the full export run, then the remaining
  ABC test passed in a focused rerun after synchronizing its render/header
  assertions. All 16 scenarios therefore passed across these runs; this is not
  represented as a single all-green full-file run. Includes visible account
  fees/export, selected-row totals, empty results and cancellation on
  account/token/period changes. Synthetic transport only.
- TypeScript `tsc -b --noEmit`, `git diff --check`, and targeted Python
  compilation passed. No extension build or large rendering job was launched.

### Remaining extended-suite failures (not hidden)

An additional repricer selection, outside the financial completion set, found:

1. `test_repricer_stats_metrics_accept_ads_alias_fields` expects older aliases.
2. `test_repricer_stats_summary_tracks_coverage_and_weights_rates` expects DRR
   from general financial revenue rather than explicit advertising revenue.
3. `test_repricer_stats_total_metrics_and_period_price_do_not_use_ad_or_current_fallbacks`
   has the same older DRR denominator expectation.

All three were rerun with `_repricer_stats_metrics` and
`_repricer_stats_summary` loaded from **HEAD** and failed identically (3 failed).
These campaign-statistics expectations were not changed to make this migration
green. The dedicated `test_wb_statistics.py` contract uses explicit ad revenue.

`test_repricer_simulator_allows_input_overrides_in_real_apply_mode_without_applying[True]`
could not obtain its expected execution state while persistence attempted the
unavailable PostgreSQL `127.0.0.1:5433`. The earlier broader run also reported
`REPRICER_DISPATCH_STORAGE_UNAVAILABLE` in the pending-price-approval test.
Price execution/safeguards were not changed to bypass missing storage. These
are not successful live price tests or evidence of production readiness.

A later **wider worker selection** (`test_repricer_tasks.py` plus three report
files) had **229 passed / 10 failed**. The two new opt-in shadow-mode regressions
passed: ABC and P&L fetched only the required Finance interval and kept the
existing DB economics bridge without Promotion backfill. The ten remaining
failures are retained explicitly, not counted as a green full worker suite:

- `test_onboarding_readiness_requires_daily_baskets_detail`,
  `test_report_snapshot_source_ready_accepts_covering_daily_detail`,
  `test_scheduler_execute_loads_full_cached_goods_list`: fixtures stamp
  `retailAmount` as the legacy planning `revenueBasis`, while its unchanged
  legacy validator requires `retailPriceWithDisc*quantity`. No scheduler safety
  or planned-price calculation was loosened to accept incompatible evidence.
- `test_pnl_report_task_waits_for_1c_before_building`,
  `test_stock_report_task_presyncs_exact_range_before_building`,
  `test_ads_report_task_presyncs_exact_range_before_building`,
  `test_rnp_report_task_presyncs_funnel_sources_before_building`: mocks patch
  the older readiness helper, whereas the worker now checks the shared router
  daily-source contract. They returned `waiting_daily_detail` before reaching
  their intended assertions. These need isolated fixture/readiness follow-up.
- `test_report_source_refresh_task_refreshes_sources_then_builds_report` and
  `test_report_source_refresh_task_fails_on_partial_wb_refresh`: attempted
  unisolated catalogue locking on unavailable Redis `localhost:6379`, with
  persistence also logging unavailable PostgreSQL `127.0.0.1:5433`.
- `test_digest_task_presyncs_report_sources_before_building`: returned `failed`
  rather than the fixture's expected `completed`; digest was not changed to
  bypass absent financial source evidence.

Unlike the three Statistics assertions above, this whole worker selection was
**not reproduced on a clean baseline**. Its failures must not be labelled
confirmed pre-existing or silently omitted. They remain extended-suite debt,
not successful integration verification. The source-isolated financial worker
regressions are part of the green financial completion set.

The old month-cache test was updated to verify reuse of operational history
while rejecting a financial aggregate without connection/realization evidence;
its focused rerun passed. A legacy unscoped synthetic drawer test also passed.

## Local performance and available source evidence

Measurement method: same read-only local SQLite, organization scope, three
requested periods, fresh Python process and then immediate repeat; Redis and
network disabled, source-cache writes disabled. “First” is application-cold,
not an OS page-cache flush. Reports are assembled directly, not through the
HTTP gateway. Week measures **selected-period preview only**. RNP now performs
additional financial work that the baseline did not do, so a slower RNP with
real rows must not be presented as a regression-free speedup.

The baseline was recorded after the initial reader-coverage patch but before
most migration fixes. It is not an unmodified Git HEAD benchmark. Baseline JSON
decode counters were not collected; no before/after claim is made for them.

### Comparable assembly results

Seconds below are first / immediate repeat. SQL columns are query counts in
the same order. Payload size is serialized UTF-8 JSON, not compressed transfer.

| Period | Report | Baseline seconds | Final seconds | Baseline SQL | Final SQL | Final bytes / rows |
| --- | --- | --- | --- | --- | --- | --- |
| 01–06 Oct | ABC | 3.315 / 1.306 | 3.590 / 1.190 | 63 / 55 | 65 / 55 | 9,870,560 / 3,563 |
| 01–06 Oct | P&L | 1.950 / 0.657 | 1.979 / 0.635 | 44 / 36 | 39 / 29 | 591,811 / 779 |
| 01–06 Oct | RNP | 0.057 / 0.054 | 1.090 / 0.581 | 22 / 22 | 47 / 38 | 1,325,282 / 778 |
| 01–06 Oct | Week selected | 4.354 / 2.410 | 3.730 / 2.157 | 161 / 146 | 160 / 144 | 2,868,912 / 2,789 |
| 05–11 Oct | ABC | 2.236 / 1.150 | 2.311 / 1.171 | 48 / 40 | 50 / 40 | 10,336,626 / 3,691 |
| 05–11 Oct | P&L | 1.883 / 0.659 | 1.949 / 0.639 | 33 / 25 | 35 / 25 | 583,532 / 768 |
| 05–11 Oct | RNP | 0.629 / 0.532 | 2.022 / 0.969 | 19 / 19 | 38 / 27 | 5,608,345 / 3,292 |
| 05–11 Oct | Week selected | 3.689 / 2.229 | 3.423 / 2.252 | 129 / 115 | 136 / 121 | 3,089,831 / 3,143 |
| 11–14 Oct | ABC | 1.318 / 1.068 | 1.253 / 0.946 | 58 / 51 | 60 / 51 | 10,160,742 / 3,564 |
| 11–14 Oct | P&L | 0.925 / 0.551 | 0.963 / 0.516 | 39 / 32 | 42 / 33 | 272,950 / 357 |
| 11–14 Oct | RNP | 0.032 / 0.035 | 1.681 / 0.558 | 18 / 18 | 44 / 34 | 586,802 / 344 |
| 11–14 Oct | Week selected | 2.541 / 2.211 | 2.742 / 2.255 | 150 / 137 | 160 / 146 | 865,372 / 522 |

RNP's former fast empty reports for 01–06 and 11–14 are not a useful speed
target: they now retain available financial products even when the operational
funnel is blocked. No blanket performance improvement is claimed. ABC remains
large (about 10 MB); full operational/catalogue JSON reads are still material.
Reducing that unrelated payload requires its own measured contract change.

Additional final repeat-run instrumentation:

| Period / report | SQL execution seconds | JSON decodes | JSON bytes decoded | JSON decode seconds | Raw Finance JSON decodes |
| --- | --- | --- | --- | --- | --- |
| 01–06 ABC | 0.130 | 39 | 104,732,145 | 0.499 | 0 |
| 01–06 P&L | 0.063 | 14 | 60,363,432 | 0.195 | 0 |
| 01–06 RNP | 0.111 | 18 | 60,688,695 | 0.195 | 0 |
| 01–06 Week | 0.606 | 71 | 187,253,097 | 0.645 | 0 |
| 05–11 ABC | 0.117 | 31 | 104,393,529 | 0.473 | 0 |
| 05–11 P&L | 0.060 | 14 | 60,651,433 | 0.199 | 0 |
| 05–11 RNP | 0.098 | 14 | 60,836,161 | 0.215 | 0 |
| 05–11 Week | 0.618 | 71 | 187,062,341 | 0.650 | 0 |
| 11–14 ABC | 0.140 | 31 | 83,301,279 | 0.351 | 0 |
| 11–14 P&L | 0.078 | 16 | 56,715,956 | 0.188 | 0 |
| 11–14 RNP | 0.111 | 12 | 56,711,872 | 0.182 | 0 |
| 11–14 Week | 0.664 | 93 | 240,034,117 | 0.828 | 2 |

JSON instrumentation counts SQLAlchemy JSON result decoding, not every small
manual metadata `json.loads`. SQL execution excludes result decoding. Partial
Week still inspects two stored financial receipts in its readiness path; this
is not an API download. **All 24 assembly runs made zero network attempts.**
No API concurrency, rate-limit or queue-wait claim follows from a DB-only run;
those concerns have isolated lifecycle/retry tests, not a production timing.

Saved-report read compatibility and additional source evidence are recorded
below separately; no working-DB writes were used to prime new derived caches.

### Saved payload reads and required rebuilds

The representative latest persisted report in each available family was read
twice, unchanged, with writes and network disabled:

| Report | Stored version | Read seconds first / repeat | JSON bytes returned | Compatible with this delivery |
| --- | --- | --- | --- | --- |
| ABC | v32 | 0.0473 / 0.0490 | 8,411,262 | No: v33 rebuild required |
| P&L | v18 | 0.0089 / 0.0074 | 1,217,985 | No: v19 rebuild required |
| RNP | none | unavailable | — | No saved payload to time |
| Week | v12 | 0.0156 / 0.0167 | 2,869,155 | No: v14 rebuild required |

Compatibility validation added approximately 0.0024–0.0147 s per sample. The
unchanged reads do not establish an improvement over baseline or a usable new
report response: these old versions were correctly rejected. They must be
rebuilt through the existing worker using stored raw data after an approved
release. Measuring a new persisted report's HTTP latency requires that release;
it was not simulated by overwriting working caches here.

### Current saved Finance evidence (not a fresh WB API fetch)

| Inclusive period | Known realization | Net units | Completeness |
| --- | --- | --- | --- |
| 01–06 Oct 2026 | 2,982,341.47 RUB | 2,061 | Complete financial interval |
| 05–11 Oct 2026 | 2,920,253.55 RUB | 1,859 | Complete financial interval |
| 11–14 Oct 2026 | 439,779.55 RUB | 267 | Only known portion; 12, 13 and 14 October missing |

Stored product `867213853` for 05–11 October has 55 net financial units and
170,220.90 RUB, independent of its operational funnel cohort. These values were
read from the current persisted operations, not hard-coded in application code.

No supportable stock-allocation observations were present for these stored
periods. Unattributed storage therefore correctly remains account-level:
11,106.58 RUB for 01–06, 13,492.15 RUB for 05–11, and 1,910.63 RUB for the known
11–14 portion. Historical gaps were not filled with present-day stock.

Other visible source/configuration limits in this snapshot include missing
operational funnel coverage in the first/third periods, unavailable KTR input,
example rather than verified user costs, and incomplete dated economics for
the partial period. The implementation does not fabricate them or claim a
confirmed profit. They are not solved by repeatedly fetching Finance.

### Production / live WB limitations

Read-only unauthenticated checks in this task:

- Production root returned HTTP 200 in approximately 0.411 s.
- Production P&L API without a session returned HTTP 401 in approximately 0.280 s.

401 is an access limitation, not proof of a loading defect. No authenticated
production report, production SQL/queue timing, WB Finance account request, or
same-product live seller-cabinet reconciliation was performed. Local stored
evidence must not be described as a fresh API result. A separately approved
restart/deployment and authorized production verification remain necessary.
