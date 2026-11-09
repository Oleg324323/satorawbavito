# Avito statistics redesign — 2026-10-01

## Final publication check — 2026-10-01

This section supersedes the earlier incomplete photo-collection status below. Extension 0.2.5 was verified in the user's authorized Chrome session: all 721 listing photographs and thumbnails were saved, decoded successfully, and a repeat run skipped all 721 without recollection. Batch extraction handles Avito Pro CSS thumbnails and pagination readiness, with four bounded upload workers and tab-replacement recovery. Temporary diagnostic tooling was removed. Local databases and credentials are excluded from Git.

Latest repricer UI corrections reserve 66px for the 66px photo plus a 12px text gap, wrap long titles, and remove the active-price-sum card. Only active and blocked listing counts remain. These layout changes have code/SSR regression coverage, not a new browser screenshot assertion.

Fresh publication checks: 87 scoped backend tests passed with an isolated in-memory SQLite schema; 28 frontend tests passed; TypeScript and Vite production build passed; 67 extension tests and extension build passed; diff whitespace checks passed. The initial backend run used the unavailable default PostgreSQL instance and failed four DB-dependent cases; the isolated rerun passed all cases. This is scoped verification, not a claim that the entire repository test suite or production database migration was run.

## Repricer / persistent listing-photo follow-up, 19:53

Implemented repricer-only navigation (old listings URLs redirect), requested metric columns and active/price-sum/blocked KPIs. Photos use uncropped66px previews, lazy authenticated thumbnail loads; no images in the inventory JSON. Trend windows use completed Moscow days and cached per-account batched metrics; CRorders=orders/views, contact cost=listing spend/contacts, undefined denominators stay unknown. Actual saved721rows; real comparison windows cover505rows, not fabricated zero trends for the rest.

New tenant/account/item-scoped photo table stores normalized image bytes and thumbnails. Extension0.2.3 explicitly collects only missing photos, persists each successful upload and resumes from the database, not a volatile local “done” flag. Repeated complete collection test performs exactly1manifest request/0Avito requests. Exact listing URL/OG URL/CDN validation; no matching by title. Core repricer refresh no longer scrapes public item pages. Context from current owner DB:721listings,17known image URLs,0new cached photographs; real extension run remains a user step, not verified by synthetic tests.

Manual critic pass (personal skill files unavailable): checked auth/tenant isolation, first-write idempotency, bounded uploads, unknown metrics, request credentials never reaching CDN, failed-upload progress, and all requested table columns. Code-only verification per user:67backend tests,51extension tests,13frontend SSR/pure tests, TypeScript and production build passed. Browser visual verification intentionally not run. API58017 restarted readonly, health/UI200; photo endpoint rejects unauthenticated request401. No deployment or price changes.

## Follow-up: presets and quarter data, 18:27

The user reported stale calendar fields after selecting 7 days and an empty quarter after HTTP 429. Preset and manual Apply now call the current module dispatcher directly, update applied/draft dates together using one tested patch, and remount date controls when the applied range changes. Added the React event-owner marker and derive request sequence from current store state rather than a captured render. No browser claim: the regression executes the actual dispatcher in a Node VM and checks 3m → 7 → 30 → 7 date state.

Backend can derive a complete subrange from a wider saved daily snapshot before OAuth. It verifies organization scoping, exact credential/account cache key, and daily coverage; it never substitutes partial-month totals for a quarter. Readonly live quarter fetch returned HTTP 200 with 92 real day groups and persisted the July 2–October 1 snapshot. Subsequent 92/7/30/12-day reads were checked with OAuth explicitly disabled. Scoped backend tests now 49 passed; calculation/SSR/dispatcher tests 9 passed. Browser verification remains explicitly declined by the user.

Final result: **blocked (visual verification only)**. Implementation and code checks are complete; the user explicitly requested code-only verification and no browser. No browser or screenshot automation was run for this redesign.

## Reference and scope

User screenshots: 17.36.40 reference analytics/funnel; 17.38.49 unclear legacy chart; 17.39.35 info badge; 17.41.06 search; 17.41.37 old metric/date ordering.

Implemented the reference hierarchy using existing Satorna colors and typography: date controls above metric cards; 7-day, 30-day and calendar-three-month presets; eight selectable metric cards with real sparklines and direction indicators; labeled daily chart, exact-value table, conversion funnel. Removed search, info badges and the old synthetic hourly comparison curves.

Comparison contract: the final three completed Moscow-calendar days ending at the selected range end versus the preceding three completed days. Today is excluded from comparisons, not from displayed period totals. Missing values remain unknown. Zero-baseline growth is explicit; conversion changes use percentage points and aggregate contacts/views. Expenses follow the requested up-green/down-red convention with an explanatory caption.

No unsupported revenue/profit/ROI values were copied from the reference. Rubles and conversion percentages are plotted separately from counts. Period-specific totals exclude any extra comparison-history days fetched by the backend.

## Evidence

- Backend: `../.venv/bin/python -m pytest tests/test_avito_stats.py tests/test_avito_overview.py tests/test_local_avito_readonly_guard.py -q` — 48 passed.
- Frontend: `npx vitest run src/features/vella-parity/avitoStatsMetrics.test.ts` — 8 passed, pure calculations and React server rendering; no browser.
- `npx tsc -b` — passed.
- `npx vite build` — passed; existing large-bundle and outdated Browserslist warnings remain.
- `git diff --check` — passed.
- Read-only provider check returned 30 real daily points for September 2–October 1 and saved the daily snapshot locally. No synthetic timeline fallback remains.
- Local UI and API health both return HTTP 200 after restarting the read-only backend.
- Existing browser-regression source updated for the new page contract but **not executed**.

## Critic pass and limitations

Manual code review checked period boundaries, division by zero, weighted conversion, missing metrics, cache isolation/fallback, removed legacy code, readable series labels and responsive CSS. TypeScript identified orphaned legacy helpers; those were removed and the full build re-run successfully.

The separately referenced personal `elite-product-designer` and `preflight-critic` skill files are absent at their configured paths; a local code-only critic pass was used instead.

No rendered layout, exact visual similarity, interaction behavior in a browser, responsive screenshot, keyboard navigation or physical display contrast has been verified. No visual score is claimed. No deployment or publication was performed.
