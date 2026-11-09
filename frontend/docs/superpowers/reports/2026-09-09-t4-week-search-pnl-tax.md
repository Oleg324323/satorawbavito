# Week segment search and P&L tax rendering

Independent frontend slice, no backend formula or activation changes.

## Actual runtime evidence

- Mounted P&L cached report with three identified synthetic rows proves the Tax
  column displays supplied `taxKopecks`: null → `нет данных`,0 → `0 ₽`,123400 →
  `1 234 ₽`. Exact financial cache query and backend table binding asserted.
  This tests display of supplied values, not tax calculation or canonical profit.
- Mounted Week search promised SKU/product/segment but did not include backend
  productStatus. Actual RED: SKU/name controls pass, searching `новинка` finds0
  instead of1. React rows now expose the supplied status; generic search appends
  it only for Week, preserving existing visible-text/dataset search for all rows.
  No inferred segment, manager rule or Growth/Below/Margin threshold was added.
- Main final combined P&L1+Week1+Stock filter1: **3PASS7.19s**, naturalexit0, all
  browserfinally cleanup. Requests locally fulfilled/aborted; no real actions.
- TypeScript `tsc -b --noEmit`:0; diff check0. Generated HTML decoded bytes match
  source exactly, hash `06f901b39aa6c7f03728bf80e31a355a96fcaf5376402c8378e9d9464d3d6bc6`.

## Existing broad-test debt

After actual P&L proof, only obsolete “Налоговая база” expectation changed to
“Налог”, with an added scoped P&L header assertion. Its first check failed on CSS
uppercase, corrected using the same case normalization as surrounding checks.
One selected broad-case rerun now passes P&L and fails on the next Ads scenario:
expected “Все SKU”, actual “Все товары”. Result1FAIL/27unselected; no skipped test
introduced in source. All later scenarios/interactions remain untouched and are
not claimed proven. Last whole-suite result remains405PASS4FAIL; no inferred new
whole total or new build claim.

Main critical pass verified actual backend field provenance, preservation of
existing search text and non-Week behavior, unknown segment omission, exact
identified tax cells and no formula computation. Final independent-agent review
was unavailable after its usage-limit error; this is not called independent
approval. Root final integration review remains required.

Remaining: Ads toolbar compatibility and old broad tests, Week chip policy/
manager data, complete report-state/race parity, deployment canary. No provider,
schema, production, actual export/printing, flags, push or legacy retirement.
