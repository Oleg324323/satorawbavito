# WB: implementation and verification, 5 October 2026

Status: local implementation with verified real WB reads and database persistence; **not a completed production release or browser acceptance**.

## Latest follow-up: report loading, dates and factual SKU margin, 6 October 2026

- Removed the React sidebar effect that recreated the deleted Simulator link. Existing Simulator URLs still redirect. Removed statistics trends from headers; product metric cells compare the last three completed days against the preceding three, showing absolute and percentage changes. Missing/partial sources remain neutral; zero baselines have no invented percentage. Existing ABC and week-over-week percentage changes now also carry direction arrows. This does not yet implement every missing comparison on every screen.
- Added saved-data previews for ABC, RNP, P&L, stocks, ads and the sales funnel. They read the requested range from the DB, mark partial coverage, do not fetch providers or mark a collection job complete. RNP/P&L no longer discard all saved days merely because the selected range is incomplete. UI refreshes these previews during collection and retains them when refresh fails. Cancelled and wrong-period responses cannot replace selected dates.
- Products now use versioned settlement profit, not current-price planned margin. Confirmed payout, observed fees, dated COGS and the user-confirmed 22% deduction are required. Daily rollups retain missing-field provenance; unknown payout/fees block profit. Observed expense-only records and unsold catalog records no longer incorrectly block every SKU. Two-axis ABC uses the same settlement calculation. **The 800 RUB costs remain user-authorized examples, not actual costs.**
- DB-only verification, with network connections denied, for 6 September–5 October: ABC 3,531 rows / 1.418 s; RNP 2,748 / 0.453 s; P&L 901 / 0.028 s; stocks 1,658 / 0.801 s; ads 414 / 0.015 s; funnel payload / 1.456 s. These are cache reads, not first browser paint or a before/after speedup claim.
- Exact-period product verification: 28 September–4 October has 893 computable SKU margins and 893 two-letter ABC codes, revenue 3,685,987.71 RUB. 29 September–5 October has 855, revenue 3,119,204.47 RUB, with incomplete coverage. The 30-day window still contains only partial saved finance data; an unconfirmed payout prevents its account-wide profit classification. Do not present those partial values as a complete 30-day result.
- One bounded real WB orders request using the connected account succeeded (HTTP 200) in 22.64 s. No new token was needed. No marketplace prices or budgets changed.
- Final focused checks: **328 backend tests passed**, **50 frontend tests passed**, TypeScript and Vite production build passed. One Starlette/httpx warning and existing frontend chunk-size warnings remain. Independent read-only review found no remaining blockers in the latest changes. No new extension/browser/production acceptance claim.
- Final startup check exposed a pre-existing SQLite health-probe bug: PostgreSQL-only `SET LOCAL` caused false HTTP503. The timeout is now applied only to PostgreSQL. The running local `/health/ready` returns HTTP200 with DB/Redis/broker/backend checks `ok`; the actual report worker is running but this endpoint does not monitor its heartbeat.
- Additional health suite: **19 tests passed**. The PostgreSQL connection test double was updated to include its dialect; the timeout assertion remains unchanged.
- Still open: full selected-period collection from WB; overall card impressions (not provided by the currently used API/price-only extension); all-screen metric comparisons where previous observations are absent; reconciliation of the older management P&L formula with the new product settlement formula; acceptance in the user's existing Chrome and deployment. The original full task is **not complete**.


## Latest follow-up: missing WB photos and RNP, 6 October 2026

This section supersedes earlier missing-photo counts. Cost/tax decisions below remain unchanged.

- Root cause: content refresh updated the legacy source snapshot, but did not fill missing photo URLs in the shared catalog after initial backfill. Added scoped, conditional, missing-only persistence; rejects ambiguous account mapping and unsafe URLs, never overwrites an existing photo or acknowledges a failed commit.
- Successful content pages now save photos immediately. A later timeout preserves those photos and the previous complete content snapshot. Malformed payloads/cards, repeated full pages and invalid/non-advancing pagination cursors now fail explicitly instead of publishing an incomplete catalog as complete. Critic findings covered by regressions.
- Real local org2/account1 catalog: **3,522 products; 3,428 with saved photo URLs; 94 without**. Recovered **14** formerly missing photos. Of the remaining 94, **81 exact WB cards return no photos**, and **13 return no exact card in the queried Content endpoint**. The latter is not evidence of deletion or absence from every WB source. No image URLs were guessed.
- Targeted five-worker collection: **107 products checked in 118.6 seconds**, 11 photos saved; five temporary failures retried once in **20.4 seconds**, all HTTP200, two more photos saved. One photo was recovered in the preceding targeted check. Full-catalog scan was interrupted after slow pages; it is not a completed baseline and no comparable 10x speedup is claimed.
- Content reads now use their own documented 100/minute, 600ms, burst5 budget, with existing shared throttling/checkpoints. Source: https://dev.wildberries.ru/en/openapi/work-with-products . No WB prices, budgets or business settings changed.
- RNP had a second issue: enrichment preferred stale content snapshots and never read newly saved canonical photos. It now explicitly overlays shared catalog photos for RNP, without adding photo queries to financial reports or empty RNP results. RNP payload v6 checks catalog revision; the revision includes filled-photo count so consecutive missing-photo batches invalidate even with identical SQLite timestamps.
- Real DB-only RNP for **2026-09-28—2026-10-04**: **2,884 rows**, **2,842 photo URLs**, all **2,842** exactly match canonical saved URLs; **0.856 seconds** to build. This verifies DB-to-report payload, not actual image rendering in Chrome.
- Final gate: **411 backend tests passed**, 0 failed, one Starlette/httpx warning, **27.71 seconds**, across 16 focused modules. WB extension **44 tests passed** (unchanged source). Final backend wheel built successfully. Frontend source unchanged this follow-up; its earlier build is not claimed as a new browser check. Intermediate broad photo overlay caused 31 isolated financial fixture failures; it was restricted to the requested RNP path. Three empty-RNP failures exposed an unnecessary photo lookup, now removed. No assertions were weakened or tests skipped.
- Local API and read-only report worker restarted after final runtime edits; frontend5177 and API58017 health checks HTTP200. No Safari/new browser windows. No production deployment or GitHub push.
- Publishing remains unverified: currently connected Vercel account returned no teams and project listing was denied without a team. User has been asked for the owning Vercel project/team and backend hosting location, not credentials. Existing-Chrome/installed-extension E2E, full legacy multi-cabinet cache migration, queue priority and checkpoint cleanup remain open. The entire original task is **not** marked complete.

## Latest follow-up: user-authorized example costs, 6 October 2026

This section supersedes the older statements below about zero costs and missing distributed read throttling.

- User explicitly authorized 800 RUB for every WB product across all history. Saved 3,522 scoped SKU versions in the existing local org2/account1 DB, effective 1970-01-01, source `user-example`, state `assumed`. Verified all 3,522 through the shared catalog read. No marketplace prices changed. The seeding script is explicit/local-only, not startup behaviour.
- XLSX import now has an opt-in **Replace example costs for all history** checkbox. Preview identifies affected rows; only marked examples can be superseded historically, retaining audit history. Ordinary imports remain effective from save time. Tests cover replay/idempotency, changed preview mode, existing actual costs and concurrent edits (SQLite immediate transaction and PostgreSQL row locking).
- Reports allow the specifically authorized sample math but remain preliminary and visibly marked. Arbitrary unverified costs remain unknown. With disabled 1C, authorized finance viewers may see sample math with an explicit excluded-expenses warning; the result remains unconfirmed. Users without finance permission still have profit/KPI/chart masked.
- Fixed ABC authoritative finance advertising, signed P&L fee corrections, ads-only products, actual storage vs other expenses, and missing sales rows from a complete exact ledger. ABC/P&L now share expense math; every P&L row reconciles visible columns. Explicitly incomplete finance sources cannot be final/fresh. Signed fields are aligned in backend, OpenAPI and frontend contracts.
- Real saved WB period **2026-09-28—2026-10-04**: revenue **3,685,987.71 RUB**, example profit **−497,326.27 RUB**, margin **−13.49%**. ABC (3,534 rows including catalog/ads) and P&L (901 financial/ads rows) agree exactly. This is illustrative WB/settings math, not verified actual business profit; external 1C expenses are excluded. The mapped frontend payload keeps the number and both sample/1C blockers.
- Added Redis token-bucket read coordination across workers, scoped organization/credential/host/category; shared 429 cooldown, response remaining cap, 409 penalty and cancellable waits without future reservation. No new real whole-collection speed comparison was measured.
- Read-only live stocks refresh completed: 1,658 stock rows and refreshed 3,522-item snapshot. Local API/UI restarted with latest code and returned HTTP200. Existing DB preserved.
- Latest tests: **156 backend passed** (six focused modules, 9.58s), **110 non-browser frontend passed**. TypeScript/Vite build passed. Backend wheel passed with declared isolated build dependencies (non-isolated attempt lacked setuptools). Browser test selection was interrupted and is not counted as verification; no real Chrome acceptance claimed. Existing large-chunk/Browserslist warnings remain.
- Read-only critic findings (SQLite race, source completeness, authoritative ads and sample-profit permission) were fixed with regression coverage.
- Still not done: existing-Chrome visual/interaction acceptance, production deployment/installed-extension E2E, recovery of the remaining 108 missing catalog photos, full legacy multi-account cache migration, queue priority/checkpoint cleanup. No claim that the entire original task is complete.

## Follow-up, 6 October 2026

**Final combined gate: 518 backend tests passed, 0 failed, 4 warnings, 334.69 seconds.** Includes all thirteen files in the expanded follow-up selection, including the previously failing legacy repricer module. No skips/xfails added. Warnings: Starlette/httpx deprecation and three legacy test cases with a null organization context. This is a selected subsystem suite, not the full repository suite. Final frontend combined gate: **52 passed**; extension **44 passed**; TypeScript, Vite production build and backend wheel passed. The served extension ZIP was compared entry-by-entry with `wb-prices-extension/dist`: identical.

- Restarted frontend `http://127.0.0.1:5177`, API `127.0.0.1:58017`, dedicated Redis `59999` and the read-only report worker. API/worker restarted after the final runtime changes; both HTTP health checks returned 200. Existing DB retained; no provider price/budget changes, production deployment or new browser windows.
- Added browser view/date demand leases. Route changes/unload release the old view; delayed old starts/polls cannot revive it. The worker stops before its next provider request, keeping already saved responses. Multiple jobs on the same view and other consumers remain independent. Unmanaged legacy clients remain compatible. Interrupted HTTP itself is not forcibly terminated; expiry is 120 seconds if release delivery fails.
- Added durable exact-request checkpoints scoped by organization, actual host and credential digest, cross-process Redis locks, and bounded 401/403/429 cooldown. Successful responses must reach the DB before reuse. Async report-status polling is not cached. Different requests are not globally serialized across multiple workers; the local report worker uses concurrency one.
- Added source freshness: recent days revalidate hourly, older days daily; existing data remain visible during refresh. Newer sources invalidate derived reports. Stock readiness requires a stock snapshot. ABC/week-over-week refresh includes the preceding comparison period. These changes do not complete migration of all legacy organization-only caches to cabinet keys.
- Removed the DEV-only products-period autoload gate. The existing products table now requests the managed statistics-source job in the background, retains cached rows, and rereads saved batches every 30 seconds and on completion. Query/route/signal checks stop obsolete polling as well as obsolete publication. This uses an already available product catalog; initial cold-catalog provisioning remains its existing connection/sync workflow.
- Fixed ads rebuild routing: rejected/expired campaign snapshots always use the campaign collector, preserving budgets rather than rebuilding from SKU totals. Critic's high-priority finding fixed; regression covers partial, expired and invalidated snapshots.
- Removed guessed CDN-photo URLs for missing provider images. Verified one missing product with a real Content API query: HTTP 200, empty cards. This is not evidence that every one of the remaining 108 missing photos is unrecoverable.
- Real read checkpoint measurement: seller-info first call **13,105.2 ms**, repeat **1.1 ms**, HTTP 200 both, **one actual provider request**. This measures this request's DB reuse, not whole-page speed or a 10x collection benchmark.
- Latest targeted backend run: **377 passed** (report leases/checkpoints, source lifecycle/freshness, costs/import, dated tax/COGS, report construction, photos). Strengthened ads regression subsequently rechecked: **3 passed**. Latest combined frontend: **52 passed** across nine focused files, including products autoload/replaced-query cancellation. WB extension: **44 passed**. Final TypeScript/Vite build and backend wheel succeeded. Built extension remains version 0.1.4 with the verified production price endpoint; installation/production E2E not verified.
- Expanded backend run was **485 passed / 23 failed**, all failures in legacy `test_wb_repricer_bff.py`; an intermediate isolated rerun was **124 passed / 17 failed**. Inspection found old financial/SPP expectations, unisolated shared-catalog/file fixtures, missing financial schema/explicit empty-day metadata and stale catalogue expectations. These legacy fixtures/assertions were updated to current contracts, without changing runtime finance rules or adding skips/xfails. Independent critic checked the bounded changes and found no material weakening: authorization, periods, actual expense signs and unknown-profit guards retained. Final combined rerun recorded below when finished; do not infer an all-project-suite green result from these selected files.
- Current local canonical costs, legacy SKU cost overrides and saved cost Excel rows are all empty. Real final-margin acceptance still needs the user's actual cost data, not synthetic template examples. Existing-Chrome UI/extension acceptance remains unavailable to attached tools; no cookie/profile copying or browser security changes were attempted.

## Confirmed financial decision

The user explicitly selected deduction of 22% of WB sales for **all existing history**. This is the requested management calculation, not `22/122` and not an amount added on top. The existing local WB organization's dated policy starts at 1970-01-01; other organizations were not changed. At sales of 1,000,000 RUB the deduction is 220,000 RUB, before other expenses. Historical cost versions are not overwritten by this decision.

## Implemented scope

| Area | Implementation / verification |
| --- | --- |
| Product selection | Removed initial two-row selection; bulk actions follow user selection. Source/tests checked, no fresh Chrome visual acceptance. |
| Costs | Table editor, authorized legacy-to-canonical membership, exact cabinet/nmID mapping, historical versions, input errors and keyboard handling. XLSX preview/apply, duplicates/unknown IDs, empty cells, idempotency and conflict protection tested. Shared canonical cost/photo facts used by legacy screens. |
| Import template | `outputs/wb-20261005/wb-cost-template.xlsx`, also served from frontend downloads. Two sheets; parsed and rendered for inspection. Example costs are not real data and were NOT imported into the user's database. |
| Data loading | DB-first reads, shared/coalesced frontend cache, per-consumer cancellation, bounded timeouts, on-open statistics jobs, missing-date interval planning, progressive DB refresh, strict partial-day coverage and final table refresh even for reused completed jobs. Local SQLite JSON-date decoding fixed to avoid unnecessary provider reloads. |
| Funnel | Selectable labelled metrics/charts, separate units, actual daily values, last three completed days versus previous three, absolute/percentage changes and percentage points. Missing values and zero baseline distinguished. Cache requires complete sources and matching revision. |
| ABC / P&L | Canonical dated tax/COGS resolution, signed returns, real storage/ad expenses, unknown costs preserved as unknown profit. Late-settled operations reconciled at their actual business dates. Requested banners/prefixes removed, basket count neutral and signed deltas coloured. |
| Advertising | Background campaign collector supplies real budgets, balances, daily rows and spend documents. Budget cache is organization/credential scoped; per-campaign checkpoints survive partial collection. 401/403/429 stop further budget requests; retry does not treat partial report as complete. Report/job saves now fail rather than acknowledge an unsuccessful DB write. |
| Navigation/layout | Removed WB numeric sidebar badges, simulator UI/route redirect, requested promotion question marks, stock cluster column, 1C expenses UI (records/integration retained). Removed only four dropdowns in advertising expenses. RNP/week tables use available height. Photos use catalog DB facts. Browser layout verification remains pending. |
| Extension | Existing price extension built and tested (44 tests). Built manifest and background destination agree with frontend production rewrite: `https://api.elfprint-system.ru/api/v1/wb/browser-prices`. Existing extension does NOT collect general impressions; UI does not falsely claim it does. No installed-Chrome or production E2E acceptance. |

## Real data evidence (local DB, no business mutations)

- Existing saved WB credential verified by a successful seller-info read; no replacement credential requested or printed.
- Catalog: 3,522 products; 3,414 saved photo URLs. Missing provider photos were not fabricated.
- Confirmed canonical saved costs in this local organization: **0**. Therefore final profit/margin are not presented as known. A real cost workbook or the location of the already populated database is required to finish financial acceptance.
- Financial report: 893 product rows; tax resolved for all 893, including delayed business dates; 670 rows have nonzero storage.
- Funnel: seven daily points plus six comparison days. All four required source ranges are covered for 2026-09-28 through 2026-10-04.
- Advertising: 143 campaign report rows, all 143 with a saved `budgetTotalKopecks`; 1,864 daily rows. Collector also checkpointed 697 campaign budgets from the campaign inventory. Do not confuse the earlier 414 SKU aggregate rows with campaign count.
- Fresh local Celery digest job `810497d6-398b-4bfd-9095-0520ea6bb6b8` completed; sourceComplete and cache revision checks passed. This proves worker-to-DB/report construction, not browser rendering.
- Business changes to WB prices/budgets were blocked by the local read-only network guard. Local 5177 frontend and 58017 backend remain running, with dedicated Redis 59999 and a report-only worker. No deployment or GitHub push was performed from this ZIP checkout.

## Measurements

These are actual observed cold source collection / cached server reads, **not a comparable before/after benchmark or a 10x claim**. First useful browser paint and request-count baseline were not measured.

| Operation | Measured time |
| --- | ---: |
| Initial goods/content collection | 113.55 s |
| Initial period/finance/basket aggregates | 514.58 s |
| Seven-day detailed funnel collection | 992.19 s |
| SKU advertising aggregates | 387.79 s |
| Campaign statistics and budgets | 548.02 s |
| DB-only catalog read | ~81 ms |
| DB-only ABC build | ~1,176 ms |
| DB-only funnel build | ~456 ms |
| P&L build / cached repeat | ~616 ms / 12 ms |
| Completed digest cache plus freshness checks | ~116 ms |

## Test evidence

- Focused backend gates cover costs/import, dated tax/COGS, source coverage, partial data, report lifecycle, advertising budgets, HTTP 429 and local read-only permissions. **Final run: 424 passed**, including strict report-save failure handling and SQLite/PostgreSQL cache-date regressions.
- Source-cache PostgreSQL tests: 14 passed using temporary random databases on the already running local Unix-socket cluster; those test databases were removed by fixtures. The first disposable-server attempt failed to start; it was not treated as a code success.
- Latest focused frontend run: 27 passed (cost editor 15, read cache 3, source sync 5, funnel 4). TypeScript and Vite production build passed. Large-chunk warnings remain.
- WB extension: 44 passed; manifest/syntax checks and build passed.
- Backend wheel packaging succeeded using isolated declared build dependencies. No runtime dependency changes were required.
- Separate earlier `test_sprint_d_reports.py` run retained four failing legacy HTTP fixtures assuming inline/live report construction instead of the background/cache contract. This is not an all-suite-green claim.
- Independent read-only critic found a completed/reused job UI refresh bug; fixed and covered by a regression test.

## Still open — do not mark the full task complete

1. Real acceptance in the user's **existing Chrome**: edits/import persist after reload, responsive layouts/photos and source-to-interface rendering. No attached browser-control tool is available; no Safari/new window, security-setting change, copied profile or cookie extraction was used. A manual verification request is pending.
2. Actual cost data needed for verified final profit/margin. Do not populate the user's products from synthetic workbook examples.
3. Cooperative worker stop/resume, exact-request cross-worker deduplication and old-view fencing are now implemented and tested (6 October). Real browser acceptance remains pending. Different-request rate coordination across multiple workers, explicit cross-screen queue priority and expired HTTP-checkpoint cleanup are still open; local concurrency is one.
4. Source freshness/late-correction policy is now implemented for the report path. Multi-cabinet legacy cache isolation is not fully migrated. Current canonical cost mapping rejects multiple connected WB accounts rather than mixing them; raw HTTP checkpoints are scoped to credentials. Do not claim every historical legacy cache has cabinet isolation.
5. The remaining 108 missing catalog photo URLs were not independently recovered from another verified source. General impressions remain unsupported by the used API/price-only extension path; advertising impressions are separate.
6. Installed production extension, authenticated production backend persistence and deployment have not been checked end-to-end. Local functionality must not be described as published.
