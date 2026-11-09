# WB Week-over-Week: confirmed saved economics, 2026-10-25

## Cause

For 12–18 October 2026, financial operations were saved and complete, but the
local database had no canonical WB account/product bindings, cost versions, or
confirmed tax. The saved legacy inputs (720 ₽ hoodie, 440 ₽ longsleeve, 380 ₽
T-shirt, 22% tax) were planning inputs, not actual dated financial costs.
P&L correctly returned null; Week-over-Week reused those null values.

The user explicitly authorized using the currently entered values as actual.

## Action

- Verified seller identity with one read-only WB seller-info request. No guessed
  account identity and no credential modification.
- Reused the existing product-identity backfill, restricted to nmIds present in
  the selected saved financial report. Confirmed 799 mapped products, without
  importing hardcoded type defaults or replacing existing canonical histories.
- Actual cost versions start at 2026-10-12 00:00 Europe/Moscow. Earlier costs
  remain unknown; later manual cost versions take precedence as usual.
- Confirmed the unchanged 22% rate for WB Q4 2026 through the existing WB-only
  tax schedule. Other quarters, shared/Avito economics and expense inputs were
  not changed. Normal unchanged profile saves still do not confirm tax.
- Used the existing revision-based P&L/cache recalculation. No new profit formula,
  provider report download, price write, shared schedule or production deployment.
- Created an SQLite backup before changing the local economics.

## Results for 12–18 October

| nmId | Dated COGS, ₽ | Tax, ₽ | Product profit, ₽ | Margin |
| --- | ---: | ---: | ---: | ---: |
| 867213853 | 36,000.00 | 35,741.74 | 43,877.10 | 27.01% |
| 322463083 | 23,760.00 | 24,606.56 | 19,296.05 | 17.25% |
| 534815098 | 20,880.00 | 20,523.90 | 17,895.20 | 19.18% |
| 1025784485 | 14,960.00 | 10,534.77 | -5,140.11 | -10.73% |

These are product-level results with the existing attached-expense definition,
not final company profit. Account-level unallocated storage/advertising remain
separate. No allocation was invented. Of 801 P&L rows, 799 have a numeric result;
remaining missing mapping/tax evidence is not replaced by zero. The P&L page's
existing optional-1C confirmation gate is unchanged.

## Verification

- 141 targeted backend tests passed: confirmation/idempotence, preservation of
  later edits, missing/zero, tenant boundaries, tax confirmation, both periods,
  dated costs, returns and report reuse.
- Five frontend tests passed, including selected profit/margin with an unknown comparison.
- Real Chrome checked actual Week-over-Week DOM and API: nmId 867213853 shows
  27.01% and 43,877 ₽ (table's existing whole-ruble formatting); API contains
  4,387,710 kopecks. No page errors. P&L checked separately, not claimed final.
- Compared backup/current persisted goods, runtime state and non-tax settings:
  unchanged. All six existing finance caches retain identical operation and
  aggregate data; normal background refresh metadata can change.
- Financial values were reused from the saved WB ledger. This was not a fresh
  financial-report reconciliation against WB.

Before 12 October, no factual cost was fabricated. Therefore profit/margin
comparison deltas and the summary comparison can remain blank even when the
selected-period product profit is known.
