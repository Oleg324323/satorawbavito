# WB repricer: period loading and source reconciliation

## Scope and status

Work performed in `satornawb-main`, branch `codex/fix-funnel-read-20261016`.
Existing unrelated working-tree changes were preserved. This change is limited
to WB reporting/read presentation, required-source planning, and derived-cache
dependencies. No pricing strategy, price application, night median, Avito,
extension, authentication policy, cost/tax setting, or marketplace setting was
changed. No marketplace price request, deployment, push, production migration,
raw-history deletion, or whole-cache purge was performed.

The user authorized a local restart after successful checks. The normal local
launcher restarted API, frontend, Redis and the read-only report worker; readiness
passed. The local launcher keeps marketplace price writes disabled.

## Confirmed defects and repairs

1. **A missing reporting period hid the entire repricer.** The
   `products-period-empty` CSS/state path hid the table and the message intended
   to explain missing analytics. Missing analytics now leave the catalog and
   date-independent fields accessible, with a visible reporting-state notice.
   Completed background reads finalize the same state as the initial read.
2. **Financial quantities could fall back to operational sales.** The public
   page-finance projection now sets financial trade fields from financial
   operations only. Missing coverage produces nulls; an explicitly completed
   financial merchandise interval can establish zero trade for an omitted SKU.
   This does not establish zero baskets, expenses, or profit. Execution/planning
   inputs are not rewritten by this read-only presentation helper.
3. **The buyout card's trend used orders and only loaded rows.** The backend now
   supplies the previous equal-length period's financial net buyouts, with the
   same account/product/filter scope and its own coverage. The frontend compares
   quantities with quantities, independently of pagination. Missing/partial or
   stale comparison coverage and nonpositive financial bases do not produce a
   misleading percentage. A complete 0-to-0 comparison is handled explicitly.
4. **Missing baskets and returns looked like zero.** Nullable summary/adaptor
   fields and rendering retain unavailable values. Genuine completed zeros are
   retained. The unrelated whole-catalog basket trend was also withheld because
   its previous value came only from the current page's rows; per-product trends
   were left intact.
5. **Missing-date metadata contradicted coverage.** A missing source now returns
   every inclusive requested date. Partial coverage whose precise missing dates
   are not known carries `missingDatesKnown: false`, rather than certifying full
   coverage. The planner considers coverage and freshness, not timestamps alone.
6. **Finance waited behind operational collection.** Finance and missing
   operational-source collection start independently; saved-data reads are
   serialized and guarded by current request identity. Only missing/stale
   previous-period Finance follows selected-period Finance. It does not launch
   a previous-period operational/account sync.
7. **Irrelevant sources and repeated conflicts prolonged loading.** Campaign
   performance is not a prerequisite for financial advertising expenses.
   Repricer required sources are Finance, operational orders, and baskets.
   Complete fresh Finance is reused. On a conflicting running operational job,
   the frontend reads status rather than repeatedly POSTing another run. Existing
   queue deduplication, rate-limit and retry handling remain in use.
8. **Unrelated periods invalidated catalog snapshots.** Period-source revisions
   and financial projection revisions are narrowed to overlapping selected and
   comparison windows. Goods, prices, tariffs, account/settings, tax/cost and
   other global dependencies remain. Finance progress checkpoints do not
   invalidate confirmed data. Snapshot version 23 invalidates derived list
   snapshots only. A freshness transition can still invalidate a snapshot even
   when no source row has changed.
9. **Old values could appear beneath newly selected dates.** On date change,
   previous-period analytics are cleared while identity/current prices remain.
   Superseded responses cannot finalize a newer request or restore old values.

## Source contract

| UI metric | Source and field | Period/sign/coverage rule |
| --- | --- | --- |
| Public financial revenue | Finance detail `retailAmount` | Inclusive Moscow `rrDate`; qualifying merchandise sales minus returns; operation amount is not multiplied by quantity again |
| Buyout card, quantity/money | Finance `quantity`, `retailAmount`, `docTypeName`, `sellerOperName` | Net qualifying merchandise; same source for both equal-length periods; whole-cabinet residual remains explicit |
| Table gross sales quantity/money | Same Finance merchandise records | Gross sales before merchandise returns; not orders, payout or current price multiplied by units |
| Returns | Same Finance return classification | Return quantity separately; service-operation quantities excluded |
| Actual commission/settlement bridge | Existing Finance projection using realization, payable and acquiring amounts | Signed monetary basis retained; not a replacement for planning commission tariffs |
| Logistics/storage/acceptance/penalties/deductions | Finance `deliveryService`, `paidStorage`, `paidAcceptance`, `penalty`, `deduction` and existing validated operation classification | Existing posting/service-date and allocation rules preserved; not guessed from operational statistics |
| Compensation/other credits | Finance payable/credit operation classification, `additionalPayment` | Separate from merchandise; do not count twice or manufacture product attribution |
| Financial advertising expense | Existing Finance deduction classification, including `bonusTypeName` | Unknown classification remains a blocker; campaign performance is not a second expense source |
| Baskets/views/funnel orders/order amount | Sales Funnel `openCount`, `cartCount`, `orderCount`, `orderSum` | Requested operational period; not fields supplied by the financial report |
| Legacy operational orders without stored funnel | Existing `supplier.orders` path | Different definition from Sales Funnel; retained, not represented as a successful funnel reconciliation |
| Cost and tax | Existing configured, dated local inputs | Not supplied by WB Finance; missing configuration is not zero; business-date rules unchanged |

Official schemas were checked directly in this run:
[Finance](https://dev.wildberries.ru/api/swagger/yaml/ru/13-finances.yaml) and
[Analytics](https://dev.wildberries.ru/api/swagger/yaml/ru/11-analytics.yaml).
Finance detail is a documented read-only POST to
`/api/finance/v1/sales-reports/detailed`; funnel uses the documented read-only
POST `/api/analytics/v3/sales-funnel/products`.

## Fresh live API reconciliation

Read-only network guards permitted the two official reporting endpoints, not
marketplace mutations. Credentials were read from the existing local connection
without printing, changing or saving them elsewhere. The working source database
was opened query-only. Live audit reads were not persisted as new coverage.

### Financial API

Fetched the common 01–15 October 2026 interval with full required fields and the
documented maximum page size 100,000, following its cursor to terminal HTTP 204.
The nonempty page contained **45,366 unique operations**. The complete read took
about **130.79 seconds**, using the existing request budget/limiter. The five
requested windows were projected independently through the existing authoritative
realization mapper, not by adding overlapping report totals.

| Inclusive 2026 period | Live net realization, RUB | Live net units | Persisted/backend projection |
| --- | ---: | ---: | --- |
| 01 October | 546,085.96 | 393 | Exact match |
| 01–06 October | 2,982,341.47 | 2,061 | Exact match |
| 05–11 October | 2,920,253.55 | 1,859 | Exact match |
| 11–14 October | 1,587,246.68 | 986 | Exact match |
| 12–15 October | 1,654,260.09 | 1,038 | Exact match |

Representative product fields were compared for `867213853`, `1025784485` and
`486612890`: gross/return/net quantities, realization and gross realization,
commission, logistics and storage matched the existing raw-data projection for
all five windows. These checks are not a live reconciliation of every other
expense, allocation, profit, export or WB page.

For 05–11 October:

| nmId | Gross / returns / net units | Net realization, RUB | Gross realization, RUB |
| --- | --- | ---: | ---: |
| 867213853 | 55 / 0 / 55 | 170,220.90 | 170,220.90 |
| 1025784485 | 62 / 10 / 52 | 67,896.25 | 80,534.25 |
| 486612890 | 49 / 1 / 48 | 63,782.66 | 65,208.66 |

### Operational API

Five read-only funnel requests covered the same three products and five October
windows. The first took about 0.94s; subsequent calls about 22s each including
the existing WB interval. No per-product request loop or full-catalog operational
audit job was launched.

Where funnel coverage was already persisted (05–11 and 12–15 October), the
stored table's baskets, order counts and order amounts matched live funnel data.
For example, 05–11 October:

| nmId | Views | Baskets | Orders | Ordered amount, RUB |
| --- | ---: | ---: | ---: | ---: |
| 867213853 | 6,106 | 635 | 88 | 432,902.00 |
| 1025784485 | 5,989 | 773 | 159 | 328,176.00 |
| 486612890 | 2,663 | 491 | 93 | 191,974.00 |

**Remaining source distinction:** for 01 October, 01–06 October and 11–14
October, no complete stored baskets/funnel source existed at the time of the
query-only audit. The existing order fallback was `supplier.orders`, and its
counts differ from funnel counts. Examples for 01–06: 48 versus 80 for
867213853, 66 versus 177 for 1025784485, 38 versus 60 for 486612890. This is not
a financial-report discrepancy. The missing-source planner now requests the
operational source independently; a successful full backfill of those intervals
has not been claimed. Do not call these supplier-order figures verified funnel
orders or assume missing API access merely because the local cache is absent.

## Frontend verification and performance

The actual repricer component was compiled and exercised in headless installed
Chrome. Two explicitly separate modes were used:

- Fully intercepted synthetic sources for missing/partial/confirmed-zero
  prerequisites, out-of-order responses, presets 1/7/14/30, date restoration and
  pagination. No marketplace mutation can escape this browser.
- Query-only working-database backend responses for all six exact periods,
  rendered through the same component/adaptor: card revenue/net units/money,
  table visibility/row count, and representative financial/operational row cells.
  The real-data response fixture is ignored local audit output, not a committed
  account-data fixture. This is not an authenticated browser-to-server HTTP
  latency benchmark.

All six ranges retained catalog data and correct date-specific summaries:

| Period | Catalog / page | Reporting state | Cold DB builder / warm snapshot, seconds |
| --- | --- | --- | --- |
| 01 October | 3,556 / 150 | Financial data; baskets missing | 3.534 / 0.165 |
| 01–06 October | 3,556 / 150 | Financial data; baskets missing | 4.390 / 0.162 |
| 05–11 October | 3,556 / 150 | Financial and baskets available; prior Finance partial | 5.175 / 0.202 |
| 11–14 October | 3,556 / 150 | Financial data; baskets missing | 5.188 / 0.190 |
| 12–15 October | 3,556 / 150 | Financial and baskets available | 3.704 / 0.162 |
| 01–07 September | 3,556 / 150 | No history; financial values unavailable, not zero | 2.087 / 0.167 |

Builder measurements used the working source DB query-only with isolated
in-memory derived snapshots. Repeated reads hit snapshots; **zero WB network
calls** occurred during this matrix. Cold assembly still takes seconds. Earlier
working-server traces were 2.4–7.5s, but these are different environments/runs;
there is no defensible production before/after speedup percentage. Complete
coverage latency remains dependent on missing WB sources and their rate limits.
Actual post-restart cold requests were slower than the isolated warm benchmark:
11.39s and 5.76s while opening 12–15 October, and 6.43s for 05–11 October.
These traces are retained as a performance limitation; the 0.16–0.20s isolated
warm readings must not be advertised as ordinary browser or production latency.

Existing default advanced type filters excluded two products from one 150-row
fixture (shorts/balaclavas). The unfiltered test explicitly selects all types;
production filter defaults were not changed to force a row-count match.

After the authorized restart, ordinary local HTTP health and frontend returned
200; launcher verified API/Redis/worker readiness. The existing logged-in Chrome
tab visibly rendered the real 12–15 October table and the correct
1,654,260.09 RUB / 1,038 net-unit card with the financial comparison. This is an
additional live-site smoke check, not a claim that all six periods were exercised
through the user's authenticated Chrome session or that production was deployed.

## Checks

- **287 passed** targeted backend tests: period reporting, Finance projection
  and cache, realization presentation, Finance completion/authoritative source,
  report-job lifecycle, Finance limiter, runtime source planning.
- **76 passed** frontend checks across six files, including one integrated
  browser scenario with synthetic and working-DB rendering modes.
- TypeScript `tsc --noEmit` and `git diff --check` passed.
- A wider legacy repricer BFF run was interrupted after approximately 312s:
  129 passed and five failed before interruption. Four failures are old
  advertising-diagnostic expectations (CTR/impressions/DRR); one price-approval
  persistence fixture reports `REPRICER_DISPATCH_STORAGE_UNAVAILABLE`. They are
  outside this reporting repair. The complete same-dirty-baseline suite was not
  rerun, so this run alone does not prove all five predate the change. Do not
  label the entire backend suite green or weaken price persistence to pass it.
- Existing deprecation/Browserslist warnings remain; no dependency upgrade was
  bundled into this task. The ordinary local page also logs the existing React
  empty-image-`src` warning; its image markup was not changed here.

## Remaining limits / handoff

- Some operational histories and prior comparison Finance remain incomplete.
  The UI now preserves available data and exposes gaps rather than inventing
  totals. The previous 28 September–04 October Finance interval for 05–11
  October had 4/7 days, so no buyout percentage is certified there.
- Financial advertising classification, dated costs, allocation and tax may
  legitimately leave profit unavailable. Campaign coverage alone cannot repair
  these financial prerequisites. No estimated profit was substituted.
- There was no live September Finance request, full-catalog funnel audit,
  production traffic measurement, all-page browser/export reconciliation or
  deployment. Related financial correctness is covered by focused shared-path
  regressions, not by a claim that every page was exhaustively retested live.
- Main changed files: `wb_repricer_reporting.py`, repricer BFF router,
  source-cache/Finance revision code, required-source job planning,
  `productsSourceSync.ts`, `preparePeriod.ts`, `liveParityData.ts`, repricer
  state/rendering in `VellaHtmlParityPage.tsx`, and targeted tests.
