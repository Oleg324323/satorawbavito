# WB repricer: campaign advertising and margin presentation

## Scope and explicit decision

The user changed the repricer's advertising definition from financial-report
`WB Продвижение` postings to campaign performance spend for the selected dates.
This replaces the advertising component in the card, expenses and economic
profit; it does not add both measures. Commission, acquiring, logistics,
storage, acceptance, penalties, other deductions, additional payments, tax,
dated costs and marketplace cash payouts were not redefined.

The change is a read-only repricer projection. Shared financial operations,
financial source caches, other WB tabs, Avito, extensions, strategies, price
submission and business settings are untouched. No migrations or data reset.

## Causes and repairs

1. The summary deliberately preferred financial advertising and replaced the
   campaign total with it, including a legitimate financial zero. The repricer
   now reads the selected-period `wb_ads_fullstats` observation independently.
   Financial advertising is retained separately for reconciliation, not used as
   a substitute when campaign data is absent.
2. Product expenses replace their existing advertising amount once. Cabinet
   totals use the campaign total, including any unattributed remainder without
   allocating it to arbitrary products. Financial postings are not added again.
3. Cash settlement fields retain their original definitions. Economic profit
   after campaign advertising is separately named. A new page-snapshot namespace
   prevents reuse of the old financial-advertising presentation; source data and
   other snapshot namespaces are not cleared.
4. The old product margin suppressed the primary number when the only blocker
   was unallocated cabinet expenses, although an attributed subtotal existed.
   The campaign-based subtotal is now shown with `до общих расходов` and the
   existing assumed-cost qualification. Missing cost, tax, payout or advertising
   still blocks the number. This subtotal does not certify final SKU profit.
5. The cabinet margin no longer requires per-SKU allocation when all product
   payouts, dated costs, taxes and observed residual expense totals are known.
   It subtracts the residual globally, preserving signed corrections. Missing
   product economics, unidentified trade income or incomplete coverage remains
   a blocker; no arbitrary allocation was introduced.

## Why ABC did not match

The inspected ABC report shows a preliminary product calculation rather than
the repricer's previously allocation-gated final settlement figure. It also
includes configured internal expenses of 5% of revenue, excluded from the
repricer's settlement policy. For 867213853 on 12–16 October, those internal
expenses are 5,193.32 RUB. ABC also still uses its financial advertising measure.
Its existing formula was not silently changed to force matching numbers. The
remaining definitions must not be described as identical.

## Source-backed local verification

Inclusive ranges; saved account-scoped source observations, **not a fresh live
WB or production reconciliation**. The audit used query-only SQLite, denied
outbound sockets and intercepted derived-cache writes in memory. No credentials
were printed or issued.

| Dates, October 2026 | Campaign spend, RUB | Repricer expenses, RUB | Cabinet economic profit, RUB |
| --- | ---: | ---: | ---: |
| 05–11 | 129,471.82 | 1,029,355.01 | -238,757.23 |
| 12–16 | 134,411.85 | 922,735.40 | unavailable |
| 11 | 20,546.67 | 177,806.50 | -48,378.44 |
| 14–18 | 80,752.80 observed, partial | unavailable | unavailable |

The 12–16 October cabinet result remains blocked by 17 unresolved product
economic contexts. Confirmed cost 800 RUB for 1025784485 was preserved, not
applied to other products. Its campaign-based attributed subtotal is -13,187.16
RUB over 21 net units; for 867213853 it is 28,642.37 RUB over 32 net units. These
exclude unallocated cabinet expenses. Existing assumed costs remain assumed.

## Checks

- 179 targeted backend tests passed, plus 4 existing SKU snapshot/cache tests.
- 54 focused frontend tests passed, including isolated Chrome rendering and
  late-response/date-switch regressions. A separate Chrome replay checked the
  four source-backed ranges above, including rendered advertising, expenses,
  available cabinet profit, zero and unavailable states.
- TypeScript and `git diff --check` passed.
- The first browser invocation lacked the optional downloaded headless binary;
  rerunning with the installed Chrome channel passed without installing tools.
- The old filter-only test fixture did not supply the newer financial coverage
  contract. It now explicitly isolates legacy filter arithmetic; new campaign
  tests cover period coverage and filtered totals separately.
- No provider requests, broad synchronization or new collection workflow were
  needed. These checks are not a production load test.

After authorization the local API and idle report worker were restarted with
the saved fixes. Both are alive, API health and frontend returned HTTP 200, and
the worker answered its readiness ping. Their new startup logs contained no
error lines. Redis and frontend processes were preserved. No production
deployment, marketplace price write or business-data reset was performed.
