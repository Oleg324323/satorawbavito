# Local WB P&L: calculated profit visibility — 25 October 2026

## Root cause

On `/wb/reports/pnl`, source `financial`, the existing P&L builder calculated **799 of 801 row results** for 12–18 October 2026. A network-denied, SQLite query-only diagnostic reproduced the response mapper changing all 799 known profits to `null` because optional 1C expenses were unavailable. This was a display/API-mapping gate, not missing product costs or a new arithmetic error.

The operational 1C mode must retain its gate. Financial WB mode must preserve known WB results, without claiming a final company profit.

## Narrow correction

- Added an explicit financial-only mapping mode, preserving supplied profits, margins and overhead values. Finance permission masking and operational behavior remain unchanged.
- Propagated that mode through the direct response, background assembly, saved loading previews and scheduled snapshot publication. Operational/financial preview caches are separate.
- Advanced only the P&L derived payload version from v20 to v21. No raw financial data, settings or complete database was cleared.
- The source banner now describes the actual unresolved subset, rather than implying that all product results lack inputs.
- The financial workbench displays the known-row sum as **“Прибыль WB — известная часть”**, explicitly incomplete. It does not fabricate a whole-report profit/margin or zero for unknown rows. Tooltips exclude unavailable 1C expenses from the claimed result.

No formula, cost, tax, product mapping, source collection, marketplace price, Avito, extension, schema, production or Git publishing changes were made in this task. Unrelated working-tree changes were retained.

## Real local verification

Used headless system Chrome against the ordinary local application, with fresh in-memory authentication. No credentials, tokens, auth-state or traces were saved. Checked first entry and full-page reload, search, DOM profit/margin cells and the actual dated API response.

- **799/801 known profits** remained available in both responses, including losses. Unknown values stayed `null`.
- The known subset sums to **314,416.22 RUB**. This is not a final company-profit figure: two results and unconfirmed external operating expenses are excluded.
- nmId **1025784485**: API `netProfitKopecks=-514011`, `marginPct=-10.73`; displayed **−5,140.11 RUB / −10.7%**.
- nmId **867213853**: API `netProfitKopecks=4387710`, `marginPct=27.01`; displayed **43,877.10 RUB / 27%**. Existing UI rounding is unchanged.
- Remaining unknown results: the account-level unallocated-expense row and unmapped product **99866376**, with no supplied tax result. No tax rate or zero was invented for them.
- Warm page entry/reload: **2.31 / 2.19 seconds** in this local run. No browser JavaScript errors. This is not a production or empty-cache benchmark.
- Local API, Redis and report worker restarted and ready; marketplace price-application flags remain disabled.

Evidence, Git-ignored: `output/playwright/2026-10-25-pnl-profit-visibility/results.json`, `first-open.png`, `repeat-open.png`. The independent query-only diagnostic is `output/playwright/pnl-fresh-diagnostic.py`; its original baseline uses the default operational mapper intentionally.

## Checks and limitations

- **130 backend tests passed**: new P&L regressions, finance-permission/cache isolation and existing legacy report tax tests.
- **206 backend tests passed**: cache/job lifecycle, report loading, follow-up and P&L expense tests.
- **22 frontend tests passed**: new partial-profit display, default source, expenses, table export, unknown values, late export cancellation and account-level charges.
- TypeScript project check, direct Vite production build and `git diff --check` passed. Existing build size/Browserslist warnings remain.
- Two additional legacy isolated browser scenarios (`pnlTaxBrowser.test.ts` and `financeDisabledSourceBrowser.test.ts`) did not complete their mocked UI setup. The normal launch first lacked the expected downloaded Chromium; a local test-only system-Chrome override reached the browser but timed out waiting for fixture content. These scenarios were not repaired outside the requested P&L task, and a fully green frontend/project-wide suite is not claimed. The real localhost P&L scenario and the separate 16-case export browser suite succeeded.

This verifies preservation/display of existing calculations, not a new live WB API reconciliation or a full audit of every P&L formula. No deployment was performed.
