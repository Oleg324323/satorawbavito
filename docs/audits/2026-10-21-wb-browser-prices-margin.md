# WB browser prices and margin audit — 21 October 2026

## Scope and evidence

WB only. No marketplace price/discount/strategy writes, production deployment,
token rotation, database clearing or data migration was performed. Existing
unrelated working-tree changes were preserved.

The working local database was inspected query-only, with outbound sockets
disabled in the audit process. Derived projections were held in process memory.
No database backup was made. Private account-data fixtures are ignored under
`.local-preview/audits/`, not committed fixtures.

The user's local repricer and WB product page were inspected. The WB page for
1025784485 displayed **1,444 RUB without WB Wallet**, separately from its wallet
price. This matched the stored browser observation and its backend projection
while fresh. This is one browser-price reconciliation, not a live WB API check
of every product or size.

Browser regressions exercised real application components with intercepted
requests; source-data replay used query-only database responses. This does not
certify authenticated production HTTP behavior, production latency, or a fresh
whole-catalog collection. The isolated browser initially reached the ordinary
login screen on the working server; no authentication bypass was installed.

## Findings and changes

1. **The open Products page stopped reading after source preparation finished.**
   A later extension batch could be stored correctly without updating the open
   table. A visible, idle page now rereads saved data at most once a minute.
   It does not call source preparation, refresh the catalog, or apply prices in
   the normal populated-catalog flow. Hidden pages and active text/price edits
   are skipped, overlapping reads are prevented, date/filter/page changes win
   over older responses, and navigation aborts the read. Failures retain rows
   and show the existing nonblocking error state.

2. **ABC current-price columns could remain frozen inside a 24-hour report.**
   The response boundary now overlays these two current-price fields by exact
   `nmId` from the existing browser-price-aware goods cache. One batched catalog
   read is used. Unallocated/grouped rows and legacy tenants without a browser
   connection are untouched. Expired/missing quotes become unavailable rather
   than retaining the old buyer price. Historical order SPP/date/history,
   financial revenue, profit, tax and classes are not overwritten.

3. **The account commission bridge could be reset to zero.**
   `project_realization_operations` previously started the raw commission total
   from a service-charge dictionary that contains no merchandise commission.
   This manufactured an unassigned commission residual in cabinet summaries.
   The total now equals signed product commission bridges plus the genuine
   unallocated trade bridge. Duplicate account operations use the same verified
   identity set as product operations. Positive/negative values, returns,
   duplicate records, and the summary residual are regression-tested.
   Financial projection and affected derived-report versions were advanced;
   raw financial operations are reused, not cleared or downloaded again.

4. **A known product subtotal was hidden together with final margin.**
   Final settlement profit remains unavailable when required inputs or cabinet
   attribution are unconfirmed. An additive, explicitly labelled subtotal is
   now calculated when payout, product charges, dated COGS and configured tax
   are known. Products displays `RUB/unit before shared expenses` only when
   unallocated cabinet expenses are the sole blocker. Example COGS remains
   labelled approximate. This subtotal is not final margin, is not promoted to
   net profit or ABC ranking, and does not subtract expenses twice.

## Actual coverage and remaining blockers

- The goods cache contained **3,704 products**. The extension had observations
  for **1,315 distinct product/representative-size pairs**, not the entire catalog.
  No invalid buyer/seller bounds or mismatched observation keys were found in
  these stored items. Other sizes were not thereby certified.
- Observations are deliberately short-lived: both the buyer observation and
  seller-price reference must be within **30 minutes**, match the exact product,
  size and seller price, and belong to the connected account/binding. At the
  initial audit, 1,315 buyer quotes were usable. Later query-only reads returned
  zero fresh quotes after the seller references expired. Stored does not mean
  fresh. The lifetime/security gates were not relaxed.
- A completed partial extension pass does not automatically repeat; the existing
  extension tests confirm that behavior. A new collection with fresh seller
  references is required to certify current coverage. The audit did not start
  an uncontrolled repeated browser sweep or evade WB access denials/rate limits.
- For **05–11 October**, cabinet amounts not attributed to products included
  storage **13,492.15 RUB**, WB promotion **134,923 RUB**, and other deductions
  **44 RUB**. These are not invented per-product charges.
- For **12–16 October**, unallocated storage was **11,134.80 RUB** and deductions
  **42,990 RUB**. The dated tax bridge also reported **17 missing SKU mappings**.
  Missing historical stock/cost/mapping evidence is not fixed by receiving a
  current buyer quote.
- Existing ABC/P&L and repricer views do not all use an identical final-profit
  confirmation policy. ABC can show product-level operating profit while the
  repricer withholds final allocated settlement margin. Do not treat those as
  a same-definition reconciliation. This audit did not silently unify financial
  policies or allocate shared charges to make the figures appear complete.

### Reproducible product subtotal

For 1025784485, using saved financial operations and existing dated economics:

| Selected period | Net units | Payout after attributed WB charges | COGS | Tax | Before unallocated cabinet charges |
| --- | ---: | ---: | ---: | ---: | ---: |
| 05–11.10.2026 | 52 | 37,775.32 | 41,600.00 | 14,937.18 | **−18,761.86 RUB** |
| 12–16.10.2026 | 21 | 13,347.30 | 16,800.00 | 6,739.11 | **−10,191.81 RUB** |

COGS in these examples has the existing `assumed / user-example` provenance.
The current **1,444 RUB** quote is not substituted into historical sales or
treated as the seller's final settlement revenue.

## Page/source matrix

Twenty-one WB paths were opened in an isolated browser. Configuration/queue
screens were structural checks with bounded synthetic responses; their real
business workflows were not exercised. The financial/analytical report builders
were separately run against saved source data for the exact selected dates.

| Page(s) | Price/margin interpretation and verification boundary |
| --- | --- |
| `/wb/repricer` | Actual component replay; current buyer price refresh, expiry, no concurrent refresh while editing, unchanged historical margin, labelled product subtotal. Both selected financial periods projected for the full catalog; first 150 rows replayed. |
| `/wb/repricer/stats` | Same buyer-price-aware backend row source. Historical average-price/traffic fields remain period metrics; no wholesale substitution of current quotes. |
| `/wb/repricer/sku/:articleId` | Drawer uses the mapped product's seller/buyer prices; historical timeline is not rewritten. Structural route check, not a live price-application test. |
| `/wb/algorithm`, `/wb/templates` | Rule/settings screens; no authoritative current quote to replace. No settings were saved. |
| `/wb/repricer/changelog`, `/wb/repricer/work-status` | Historical decisions/execution prices remain historical. No repricing or replay of approvals. |
| `/wb/liquidation`, `/wb/promotions` | Seller-price recommendations/limits retain their pricing basis. Structural checks only; no live operation or strategy application. |
| `/wb/reports`, `/wb/reports/monitor` | Funnel visits, carts, orders and buyouts are not current price observations. No financial-source or CTR changes made. |
| `/wb/reports/abc` | Saved builder: 3,718 rows, including historical products. Current price read overlay tested separately from dated SPP history. Browser data replay checked. |
| `/wb/reports/rnp` | Saved builder: 3,296 rows. Funnel/financial quantities are not recomputed from current buyer prices. |
| `/wb/reports/pnl` | Saved builder: 715 rows. Dated cost/tax blockers remain explicit; current SPP cannot resolve them. |
| `/wb/reports/ads` | Saved builder: 394 rows. Campaign economics are not inferred from current product quotes. |
| `/wb/reports/stock` | Saved builder: 1,683 rows. Stock quantities and seller-basis valuation remain separate from buyer quotes. |
| `/wb/reports/week-over-week` | Each comparison uses its own historical financial/funnel period; current browser prices do not replace historical buyout amounts. |
| `/wb/reports/sales` | Historical dynamics/plan-fact, not a current-price ledger. Structural check only. |
| `/wb/reports/rules`, `/wb/sources`, `/wb/reviews` | No current product-price calculation to substitute. Structural checks only; no review sending, rule changes or source-wide refresh. |

## Checks

- **430 backend tests passed**, including disposable-PostgreSQL extension
  admission/persistence, exact account/size matching, freshness and snapshots,
  signed financial bridge, settlement blocking, permission/cache isolation,
  and report job lifecycle. One outdated snapshot-test stub was updated to
  accept the existing period-scoped revision arguments.
- **132 focused frontend tests passed**, including a real Chromium component
  regression: a new saved buyer quote updates the open table, an absent/expired
  quote remains unavailable, historical margin does not change, no price POST
  is made, and focused inputs prevent background refresh. The final formatting
  and editing additions were rerun separately.
- **90 WB extension tests passed**, including manifest/syntax checks. No
  extension packaging, permissions, collector or installed files were changed.
- TypeScript and `git diff --check` passed.
- Browser-only `crypto.randomUUID` failures encountered on the synthetic
  nonsecure `http://satorna.test` origin disappeared on trusted `localhost`.
  These were audit-harness failures, not evidence of a production HTTPS defect.
- In the corrected localhost replay, Products displayed 150 source rows and
  ABC/RNP/P&L/Ads/Stock each displayed the first 50 rows without page exceptions.
  The other paths were structural/source-mapping checks, not certifications of
  every live business scenario. Report-job/budget/source-refresh calls from the
  replay were intercepted; none reached the working API or WB.

## Application status

Source changes and tests are saved locally. After successful checks and user
approval, the local API and read-only report worker were gracefully restarted.
API health and frontend returned HTTP 200; the report worker answered `pong`.
Redis and frontend processes were left running, with no startup error markers
in the newly appended API/worker logs. No production deployment was performed.
Full current-price coverage and final allocated margin remain unverified for
the reasons above.
