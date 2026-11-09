# WB financial realization source migration

Scope: WB financial reads only. Preserve the current dirty worktree, financial
formulas, tax/cost history, price execution, operational analytics and Avito.

## Source mapping (before implementation)

| Consumer | Definition / date basis | Existing source | Target / action |
| --- | --- | --- | --- |
| Repricer analytical widgets | Seller-price merchandise revenue; saleDt; posting-date storage/deductions | Finance detailed raw cache | Retain projection and formulas; strengthen source receipt and required-field coverage. No strategy/price changes. |
| Statistics | Existing buyer/seller monetary bases and posting selection | Finance aggregate cache, raw fallback | Keep metric bases; make Finance source provenance explicit, no Analytics substitution. |
| ABC / RNP | Product finance plus independently sourced funnel/ads | Finance detailed projections | Reuse ledger, retain report-specific adjustments; do not replace funnel or campaign spend. |
| P&L | Existing seller settlement profit, dated costs/tax and signed product fees | Finance raw projection | Retain formulas and unallocated charges; validate raw-source completeness. |
| Week-over-Week | Merchandise buyouts on saleDt; fees on rrDate; existing storage allocation | Finance raw projection | Retain both period definitions and unknown-vs-zero rules. |
| Sales Funnel | Analytics visits/carts/orders/cohort buyouts | Analytics v3 | No changes; financial realization is not an equivalent source. |
| Advertising | Campaign performance, attributed orders, DRR/ROMI, budget/balance | Promotion API | No changes; settlement deductions remain separate evidence. |
| Legacy report-source snapshot | Financial rows and report reconciliation | Retired Statistics v5 realization; first Finance list row | Reuse the existing Finance collector; paginate/deduplicate the report list and reconcile all reports. |
| Legacy snapshot's operational cost detail | Measurement penalties, warehouse-storage and acceptance-operation reports | Separate Analytics reports | Not a proven equivalent to the complete settlement ledger or its posting dates. Retained; active financial tab projections already use Finance charges. No new allocation introduced. |

## Rules

- Official schema: https://dev.wildberries.ru/api/swagger/yaml/ru/13-finances.yaml
- `daily`/`weekly` are alternative representations, not additive sources.
- `reportType=1`/`2` distinguish main/buyout reports, not daily/weekly.
- Exact-report retrieval must be scoped to reportId, and must not certify date
  coverage for unrelated reports or write into the daily raw cache.
- `retailAmount`, seller price times quantity, `forPay`, and `bankPaymentSum`
  retain their distinct meanings. No profit/tax formula change is authorized.
- Preserve account/credential isolation, cursor checkpoints, request budgeting,
  and read-only report queues. Fetch missing compatible coverage only.
- Keep report summary money by currency; never add different currencies as RUB.

## Baseline

Before edits: targeted financial-source/projection suite: 141 passed, 1 failed.
The pre-existing failure is
`test_wb_pnl_expenses.py::test_pnl_needs_raw_attribution_but_abc_may_reuse_aggregate_coverage`.
No live WB reconciliation or production verification is implied by fixtures.

## Implemented

- The existing collector now supports the official exact-report detail method
  as well as existing daily/date collection. `--report-id` and `--period` extend
  the existing read-only collection CLI, not page requests or price execution.
- All 42 selected detail fields were checked against the current official
  schema (96 available properties). Added currency, report dates, WB
  remuneration/VAT and reimbursed-logistics evidence. Newly retained evidence
  is not automatically added to expenses or substituted for another money base.
- The remaining retired Statistics financial reader now calls the shared
  Finance collector. Its commission is the reported `ppvzSalesCommission`, not
  a guessed amount calculated from the seller's discount percentage.
- Report-list reconciliation paginates and includes every distinct report,
  both types and month-split periods. It preserves nullable amounts and groups
  money by currency. Unsupported/missing currencies cannot become RUB totals.
- Exact-report and weekly raw caches/checkpoints are isolated from daily
  coverage. Report IDs, row IDs and cursors are not converted through floats.
  Wrong report IDs, conflicting duplicates and stalled pagination fail closed.
- Raw receipts record requested and actually available fields, currency,
  operation count and source version. Database-only coverage reads these small
  metadata fields rather than hydrating a whole ledger. Revenue-only data,
  missing fees, null fees and false boolean amounts cannot certify full costs.
  Optional absent `bonusTypeName` does not cause an endless reload.
- Cashback/loyalty amounts on return documents are normalized with the document
  direction exactly once in both existing projections. Profit, tax, commission
  reconciliation and cost-history formulas otherwise remain intact.
- Only affected financial derived-cache versions were advanced (financial
  aggregate, page projection, digest, ABC, RNP, P&L/expenses and week comparison).
  No raw history was deleted, migrated or globally reloaded. Stock, Advertising
  and Sales Funnel payload versions and source paths were not changed.

## Field and date definitions

| Family | Financial evidence | Treatment retained |
| --- | --- | --- |
| Merchandise units | quantity + document + exact seller operation | Only qualifying sale/return movements; compensation is not sold units. |
| Buyer-payment realization | retailAmount | Signed sale/return amounts, not seller-price revenue. |
| Seller-price realization | retailPriceWithDisc × quantity | Existing saleDt-based projection, inclusive Moscow dates. |
| Merchandise payable | forPay | Keep separate from report payable and actual bank receipts. |
| Report money | retailAmountSum, forPaySum, bankPaymentSum and other list totals | Already summarized by WB; sum distinct reports only, without a second return subtraction. |
| Commission | ppvzSalesCommission; existing seller-revenue/payable reconciliation | Preserve legitimate consumer-specific bases. vw/vwNds are retained evidence, not interchangeable extra fees. |
| Logistics and reimbursement | deliveryService; rebillLogisticCost | Existing logistics definition unchanged; reimbursed transport is not added a second time. |
| Storage / acceptance / deductions | paidStorage / paidAcceptance / deduction | Existing rrDate rules and explicitly defined account-level allocation remain intact. |
| Penalties / adjustments / compensation | penalty / additionalPayment / classified operations and forPay | Preserve signed charges, reversals and separate compensation treatment. |
| Loyalty and payment services | cashbackAmount / cashbackDiscount / cashbackCommissionChange; acquiringFee / paymentSchedule | Document direction for loyalty/acquiring; preserve signed service charges and avoid counting credits twice. |

The exact-report method retains the full report's raw rows. Its existing
date-filtered aggregate remains a **date-based projection**, not a claim that
it equals the complete Excel report. Compare report identity/type and raw
report rows/list totals for Excel reconciliation; saleDt and rrDate projections
may intentionally differ. There is no new Excel-upload screen or automatic
real-cabinet reconciliation in this change.

Official references checked:

- https://dev.wildberries.ru/api/swagger/yaml/ru/13-finances.yaml
- https://seller.wildberries.ru/instructions/ru/ru/material/how-to-read-general-financial-report
- https://seller.wildberries.ru/instructions/ru/ru/material/how-to-read-fimancial-reports-detalization

The list and exact-ID methods require Personal/Service Finance access and are
limited to one request per seller per minute. Period detail also supports
Basic access under its different limits (Basic without a secret: two requests
per day, 12-hour interval). Existing credential-scoped queue budgets,
checkpoints and WB Retry/Reset-header handling remain in force; token types are
not inferred from undocumented JWT flags. A missing permission is not a reason
to silently use Statistics as financial evidence.

## Checks and release limitations

- Final combined backend run: **729 passed**, covering report contracts,
  financial projections, returns/corrections, both comparison periods,
  account isolation, missing vs zero, cache lifecycle, retry handling, taxes and
  loaded-table exports. After two additional regressions were added, the new
  source module's standalone run is **35 passed** (including independent
  SQLite setup with foreign keys; no test-order dependence). Final collector,
  projection, pagination and retry recheck after namespace isolation:
  **150 passed**.
- Focused frontend run: **45 passed** across six suites (financial adapters,
  exports, week columns, P&L fees, schemas and polling). TypeScript build check
  (`tsc -b --noEmit`) passed. No frontend implementation was changed here.
- Broader checks exposed existing failures outside the changed financial path:
  four Statistics advertising/rate expectations, one old approval fixture that
  lacks dispatch storage, and three daily-stitch expectations omitting
  `oldestFetchedAt`. These also reproduce with `repricer_bff.py` loaded from
  HEAD in an isolated test process; this is a component baseline comparison,
  not a claim that the entire dirty worktree was reset or cleanly audited.
- The additional frontend run has two failing pre-existing canonical-ABC UI
  expectations (five-minute refetch and removed CTR copy; 63 other cases pass
  in that suite). Fifteen browser-export cases could not launch because the
  expected Playwright Chromium executable is absent; they are **unverified**,
  not fifteen demonstrated application defects. No browser was installed.
- The original P&L coverage fixture failure was corrected by mocking the actual
  small-metadata receipt reader, not by weakening the financial coverage check.
- Final diff whitespace check passed. Existing Avito/extension and unrelated
  loading edits were preserved, not authored or reverted by this task.
- No live WB/Excel comparison, production performance verification, marketplace
  mutations, database clearing, production migration, push, deployment or
  service restart was performed. Existing local services still need an
  explicitly approved restart to load the backend changes.
