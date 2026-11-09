# WB Week-over-Week: progressive-data fixes — 25 October 2026

## Scope

Only `/wb/reports/week-over-week` and its Week-specific assembly, cache dependencies, source-preview calls and tests were changed in this task. Existing unrelated working-tree changes were preserved.

No changes to financial formulas, other WB pages, Avito, extensions, authentication, marketplace prices/discounts/strategies, saved costs, tax settings, dependencies, schema or production infrastructure. No deployment or Git push. Existing collectors, pagination, throttling, retries, cancellation and provider-job locks remain in place.

## Corrections

1. **Profit after the final product union.** `_week_financial_columns` attaches the existing dated P&L results after both financial and operational row attachment. It handles both periods independently, including fee-only rows, signed/zero results, absent results and finance permissions. Chart entries receive the same values. There is no second financial calculation.
2. **Attention membership.** The Week card includes known negative profit, even when revenue is zero and margin is unavailable. Existing negative-margin, falling-buyout and confirmed-OOS conditions remain. Product identifiers are normalized/deduplicated; account/unallocated rows are excluded.
3. **Available comparison is independent of collection order.** Worker previews read saved data for both periods before collecting missing sources. Actual WB collection remains selected-period-first. A DB-only Week preview also uses the API's existing background-task mechanism, outside the long provider worker. An expiring per-organization Redis lease bounds/deduplicates reconstruction; ordinary status polls retry after contention/interruption. There is no new scheduler, worker queue or provider concurrency. Input revisions are checked before publication; a newer worker result wins. Assembly status/failure is exposed in the existing Week job response.
4. **Bounded revisions.** Week dependency checks use the selected/comparison interval, WB connection, catalog receipt and actual seven-day stock context. Financial projection reuse filters raw receipt metadata by overlap. Undated receipts remain conservative. Per-fact revisions detect a correction to an older existing observation even when row count and latest timestamp do not change. Cost/mapping and quarterly-tax revisions remain mandatory, including for loading snapshots. Only Week payload/projection versions were advanced.
5. **Storage state.** At the Week boundary, an absent/incomplete financial source with no observed account storage amount stays `null`. Confirmed zero stays zero; a known partial charge stays visible with incomplete coverage. The shared allocator and allocation formula were not changed.
6. **Tooltips.** Commission now describes `retailAmount − forPay − acquiringFee`, posting dates, return signs and ambiguous corrections. Advertising describes classified financial `deduction`/`bonusTypeName` on `rrDate`; separate campaign spending is not substituted or added.

## Verification of the original defects

The new isolated tests first reproduced disappearing/obsolete profit, the false storage zero and the excluded zero-revenue loss. After correction they pass.

Comparing the first actual Chrome response to the original audit:

- All **173** formerly missing calculated results are present immediately: **−2,443,340 kopecks / −24,433.40 RUB**.
- The attention count is **554**, not 330, on the unchanged audited full-week inputs.
- Previous storage is **1,349,215 kopecks / 13,492.15 RUB** when available; a missing source is tested as unavailable, not zero.
- One-day/four-day results and their saved comparisons appeared while the provider job was still `running / refreshing_sources / 10%`; campaign completion was not a prerequisite.

## Actual frontend and arithmetic

Used real headless Chrome against the local application, only the Week page. Captured DOM cells and dated API responses; tested search, sorting, complete XLSX export, horizontal/page scrolling, rapid date changes and a deliberately late response. Credentials remained in memory and were not saved in screenshots/traces/auth-state files.

Independent arithmetic reads saved raw WB operations directly with SQLite in read-only mode, without importing application calculation code. Available values, signed amounts, quantities and deltas had **zero numerical mismatches** in both browser passes. This is not a new live reconciliation against the WB seller cabinet.

| Selected inclusive dates | Previous dates | Operations | Net units | Realization, RUB |
|---|---|---:|---:|---:|
| 12–18.10.2026 | 05–11.10.2026 | 21,185 | 1,924 | 3,111,294.14 |
| 05–11.10.2026 | 28.09–04.10.2026 | 19,853 | 1,859 | 2,920,253.55 |
| 12–15.10.2026 | 08–11.10.2026 | 11,693 | 1,038 | 1,654,260.09 |
| 12.10.2026 | 11.10.2026 | 2,802 | 260 | 401,446.33 |

The full-week first response reconciled **799 available product profits and 506 margins**, plus 3,332 available funnel order/cart observations. For example, nmId 867213853 retains profit **4,387,710 kopecks** and margin **27.01%**; nmId 1025784485 retains **−514,011 kopecks / −10.73%**. Existing UI rounding is unchanged.

A separate DB-only check compared every available existing P&L result in **both** periods with the final Week row map: 999 / 270 / 848 / 486 known results respectively; **no missing or different known results**.

Some raw technical-operation-only products have no P&L row: the independent simplified sum gives zero, but the existing P&L does not supply a result. These were not filled with invented zero profit. Missing pre-12-October historical costs were not backdated. Zero revenue still has no margin percentage; the margin-change summary can legitimately remain unavailable when comparison economics are missing.

## Local performance

Not a production benchmark or guaranteed latency target. Source freshness and simultaneous background work varied between runs. A "cold" frontend range means no usable v15 derived Week payload, not an empty source database.

| Range | First selected rows, cold | Available comparison response | Warm full-page reload | Standalone DB assembly, first / repeat |
|---|---:|---:|---:|---:|
| 12–18 October | 16.25 s | 16.24 s | 3.16–7.72 s | 3.71 / 1.67 s |
| 5–11 October | 15.10 s | 14.98 s | 1.60 s | 2.25 / 1.65 s |
| 12–15 October | 13.87 s | 13.68 s | 2.42 s | 2.59 / 1.58 s |
| 12 October | 16.61 s | 16.38 s | 2.36 s | 2.42 / 1.50 s |

The earlier audit observed 27.8 seconds for the four-day range and over 35 seconds without one-day rows. The new cold runs render while enrichment remains pending. Initial loading is improved, not instantaneous.

First cold entry per range: **one jobs POST and two latest-cache GETs** (initial expected 404, then the assembled data). No duplicate job POST. Warm reloads reuse saved payloads; same-page reselection additionally uses the existing frontend cache. The first full-week DB background publication recorded 5.64 seconds under load; the four-day background publication recorded 8.76 seconds. The standalone timings above use a separate network-denied process and are not HTTP latencies.

Optional collection was not waited to completion for every range: leaving a range can pause its existing demand-bound task. The UI honestly retained `running`, `paused` or partial states as applicable. No campaign-completion time or production performance is claimed. No browser JavaScript errors occurred; expected cache-miss 404s were handled by normal job startup, not replaced by another date range.

Evidence (local, Git-ignored):

- `output/playwright/2026-10-25-week-fixes/results.json`, `arithmetic.json`, `database-assembly.json`, screenshots.
- `output/playwright/2026-10-25-week-fixes-warm/results.json`, `arithmetic.json`, screenshots.
- Final post-restart smoke: `output/playwright/2026-10-25-week-fixes-final/results.json`. Full-week rows appeared in **5.16 s**, one-day rows in **2.80 s**, with no browser JavaScript or HTTP errors. The local API health check confirms price application remains disabled.

## Automated checks

- **267 passed**: Week finance, source projection, buyouts, new progressive regressions, report loading, cache/job lifecycle, date/export contract and financial follow-up tests.
- **16 passed**: Week financial rendering/tooltips/attention, loading, search/sort/export, late-date responses, canonical-loading compatibility and source-status tests, including actual browser fixtures.
- `npx tsc -b`: passed.
- Direct `npx vite build`: passed; existing Browserslist/chunk-size warnings remain. No extension packaging/build command was run.
- `git diff --check`: passed.

Five tests in the broader `test_report_refresh_handoff.py` also fail with the original **HEAD version of `repricer_tasks.py`**, loaded in a separate test process without changing working files. They were not introduced here and were not repaired outside scope:

- `test_repricer_finance_job_never_mutates_legacy_reports[windows0]`
- `test_leaving_view_pauses_worker_without_losing_saved_source_batches`
- `test_refresh_hands_off_without_completing_a_waiting_report[pnl]`
- `test_abc_refresh_collects_raw_advertising_before_report_handoff`
- `test_page_demand_refresh_only_fetches_missing_source`

The remaining 18 tests in that file passed. A fully green project-wide suite and production deployment are **not** claimed.

Other pages, financial formulas, marketplace prices and saved settings were not changed in this task.
