# WB repricer financial verification — 6 October 2026

Status update, 7 October: the owner explicitly accepted the calculated figures ("по цифрам все верно"). Scoped implementation and existing-Chrome source-to-rendered-page acceptance are verified. Exact equality to the earlier screenshots is not claimed; see the dated addendum below.

## Scope

Only the WB product-list page is renamed «Репрайсер»; `/wb/repricer` is preserved. Its financial projection and collection job are isolated. Existing report calculations, prices, strategies, product costs and tax policy are unchanged. No deployment or extension changes.

Full original finance operations are persisted separately from legacy aggregates. Collection uses 10,000-record cursor pages and durable checkpoints, deduplicates operation IDs within the connection/organization, and completes only on the terminal response. Interrupted collection resumes; a new completed refresh starts at cursor zero. Cached source windows, not days with transactions, determine coverage. Newer completed posting-day observations replace older observations, including an empty replacement. Missing windows are explicit and incomplete settlement is not final.

The page separates merchandise operations, compensation and corrections; uses Moscow operation dates for trade and services, and posting dates for storage/acceptance/deductions. Commission uses the seller-price basis, not buyer payment. Buyout values are net of returns. WB payout is independent of cost/tax readiness. Unknown payable operation classifications prevent a confirmed profit. Original buyer payments remain separate.

The dedicated background job does not call legacy report sync or rebuild reports. Its key is connection-scoped, POST/GET retain completed status, connection changes record failures, and successfully persisted pages update the heartbeat. Financial polling is bounded at 720 five-second waits; ordinary statistics retain their existing limit.

## Actual source and page-summary function reconciliation

September full collection: 131,308 operations, 14 pages including terminal response, complete source window. Two previously complete weekly cursor chains were recovered from credential-scoped HTTP checkpoints. No fragment-only window was promoted to complete.

These are actual database-to-page-summary function results, NOT visually verified Chrome results. RUB, positive expense magnitudes:

| Metric | 29 Sep–5 Oct calculated | Reference | 1–30 Sep calculated | Reference |
|---|---:|---:|---:|---:|
| Gross sales | 6,481,226.65 | 6,480,375.42 | 27,947,601.91 | 27,945,523.01 |
| Gross units | 2,650 | 2,650 | 12,305 | 12,306 |
| Returns | 296,035.86 | 296,035.86 | 1,372,056.38 | 1,372,056.38 |
| Return units | 129 | 129 | 627 | 627 |
| Net revenue | 6,185,190.79 | 6,184,339.56 | 26,575,545.53 | 26,573,466.63 |
| Net units | 2,521 | 2,521 | 11,678 | 11,679 |
| Commission plus acquiring | 2,717,663.34 | 2,716,812.11 | 11,868,593.83 | 11,853,734.17 |
| Delivery | 669,967.62 | 669,967.62 | 3,140,418.77 | 3,140,418.76 |
| Storage | 12,686.41 | 12,686.41 | 48,469.93 | 48,469.93 |
| Acceptance | 19,170.00 | 19,170.00 | 85,010.00 | 85,010.00 |
| Fines | 15,939.00 | 15,939.00 | 80,483.64 | 80,483.64 |
| Deductions incl. WB promotion | 232,067.00 | 232,067.00 | 602,298.00 | 602,298.00 |
| Compensation | 851.23 | 851.23 | 2,078.90 | 2,078.90 |
| WB payout before costs/tax | 2,518,548.65 | 2,518,548.65 | 10,765,027.80 | 10,765,131.03 |

Additional arbitrary period **22 September–4 October**: net revenue 11,815,427.81, net units 4,920, returns 251 units, payout 4,710,695.89. Returning to the weekly period reproduced its original result exactly. No external reference was supplied for this arbitrary period.

Remaining revenue residuals are already present when directly summing `retailPriceWithDisc × quantity` for merchandise operations in the raw API response. Compensation entries themselves have seller price zero and are excluded from merchandise sums. Equal compensation/residual amounts therefore do NOT establish causation. A raw month has 12,305 merchandise units after the Moscow sale-date filter, versus the reference 12,306. The delivery kopeck discrepancy is present in the raw API amount sum, not caused by float rounding. Commission operation inclusion and late corrections still need exact reconciliation. No correction constants or forced subtraction were introduced.

The existing WB token returned HTTP 200 for both detailed collection and the official report-list endpoint. This establishes finance access, not equivalence between every dashboard definition or correctness of all token categories. Do not rotate the token as an alleged fix.

## Verification

- 350 focused backend tests passed before the final callback placement adjustment; final related rerun: 268 passed. Includes raw cursor completion/dedup/conflicts, resume after 429, empty replacement, account/job separation, partial coverage, settlement/legacy regression and job lifecycle.
- 68 frontend period/cache tests passed; final collection/polling tests: 11 passed.
- TypeScript and Vite build passed. Large-chunk/Browserslist warnings remain; no unrelated bundling changes.
- Backend wheel build passed using isolated build dependencies. The environment lacks an executable `build` package and local setuptools, so the initial non-isolated commands failed; `pip wheel --no-deps` completed without installing application dependencies.
- Independent read-only critic identified job isolation, completion, credential-key and heartbeat problems; fixes and regression tests added.
- API health and existing UI URL return HTTP 200. Local read-only API and report worker restarted with latest code; existing SQLite preserved.
- Existing-Chrome DOM/visual/date switching acceptance is unverified: no attachable browser tool; no Safari/new window/security-setting changes.
- No whole-run comparable before/after speed claim. Page-summary function timings were approximately 4–6 seconds for the week and 12 seconds for September; these are not browser loading or provider collection timings.

At the end of 6 October, exact reconciliation and source-to-rendered-page acceptance remained open. The dated follow-up below supersedes browser-acceptance status, not the historical numerical residuals.

## 7 October follow-up — accepted figures and existing Chrome

The owner enabled AppleEvents JavaScript for the already-open Chrome tab. No Safari, new window or new browser profile was used. AppleEvents evaluates JavaScript in an isolated world; application globals were inspected through a transient main-world script publishing only selected non-secret metric/job fields. Background-tab animation frames delay React rendering; inspecting empty isolated-world globals was not evidence of a missing application runtime.

Remaining scoped fixes: persistent selected-period financial job progress/error/retry in the existing summary, including its collapsed state; progress from obsolete navigation cannot publish; financial timeout text now matches its 60-minute bound. Returning from an empty later page goes through the full first-page lifecycle, including financial preparation. No accepted formula, cost, tax, strategy or price was changed in this follow-up.

Final focused frontend run: 79 tests passed; TypeScript and production Vite build passed (existing chunk-size/Browserslist warnings). Backend related run: 268 tests passed. Independent critic identified the later-page lifecycle omission, which was repaired.

Existing Chrome rendered 150 real rows, zero automatically selected checkboxes and two-letter ABC values. For custom 22 September–4 October the DOM shows revenue 11,815,427.81 RUB and conditional-example margin -1,824,698 RUB. Switching to 14 completed days (23 September–6 October, since the clock crossed midnight) renders revenue 11,762,486.29 RUB; 30 completed days (7 September–6 October) renders 24,537,265.09 RUB. Restoring the custom range reproduced 11,815,427.81 RUB. Partial non-financial source coverage is explicitly listed, not hidden.

Observed SKU HTTP request times: new 14-day snapshot 11.35 seconds; new 30-day snapshot 13.97 seconds, repeated 30-day read 0.15 seconds; returning to the custom range 12.78 seconds. These are request timings, not a comparable provider collection before/after benchmark; no 10x speed claim. Scope remains this page only; no deployment or live interrupted-network/429 injection in the owner's Chrome.

Both reference periods also confirmed in the actual Chrome DOM: 29 September–5 October revenue 6,185,190.79 RUB, net units 2,521, conditional-example margin -858,993 RUB; 1–30 September revenue 26,575,545.53 RUB, net units 11,678, conditional-example margin -4,423,992 RUB. Each renders 150 real product rows. Historical reference residuals remain recorded above; the owner's acceptance does not make them disappear. The existing tab is restored to 22 September–4 October.
