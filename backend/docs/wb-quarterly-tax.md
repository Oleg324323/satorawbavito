# WB quarterly tax

The profile's quarterly tax settings are organization-scoped WB settings in
`wb_repricer_algorithm_settings.settings_payload.wbQuarterlyTax`. No schema
migration, source-data rewrite, marketplace call or authentication change is
needed. The shared economics service and Avito policy selection are unchanged.

## Compatibility and dates

GET `/api/v1/wb-repricer/quarterly-tax?year=YYYY` requires `settings:read`.
It displays all four initial values from the stored `taxPct`, including zero;
missing configuration remains null. GET does not write or activate a policy.
PUT at the same path requires `settings:write`, a calendar year, four nullable
percentages and the read revision. Rates use the existing basis-point precision
(0–100%, two decimal places). Empty is not zero.

Only edited quarters activate overrides for that complete calendar quarter.
Unedited quarters retain the existing effective-dated tax history, including
missing/unconfirmed states. Saving the initial equal rates cannot retroactively
confirm or replace historical policies. Saved year entries are never implicitly
copied to another year. Within a configured quarter, the profile tax is the WB
organization rate, as in the existing owner-rate planning path.

Factual WB calculations resolve the quarter from the existing finance operation
business date in Europe/Moscow, including delayed operations in exact settlement
reports. They preserve net taxable revenue, signed corrections and HALF_EVEN
rounding of accumulated revenue per rate. They do not apply one period-end rate
to a multi-quarter period. Without a sufficient daily revenue split, the existing
explicit missing-tax result is retained. Planning uses its existing effective
date, or today's Moscow date when there is no historical planning date.

## Data flow and concurrency

`wb_quarterly_tax.wb_policies_for_points` overlays only WB tax fields on the
existing policies. Legacy finance, canonical ABC/P&L, reports/comparisons and
exports consume these resolved amounts. Current price-planning settings use the
same quarter resolver; COGS, expenses, rounding and price-write guards do not change.

The settings write compares the persisted JSON document atomically and returns
409 on conflicting edits; other settings and other organizations are preserved.
Legacy settings writers cannot overwrite the quarterly subdocument. There is no
successful memory/file substitute for a failed quarterly-settings database write.

Tax-dependent derived caches include the tax revision; source observations and
collection coverage stay intact. One settings snapshot spans report rows, totals
and comparisons. Superseded jobs are rejected before publishing; cached report
envelopes retain the captured quarterly revision, so a concurrent save cannot
make old results appear current. Rebuilding uses the existing database-first
report pipeline. Saving tax settings itself neither enqueues collection nor
downloads WB data. Reopen an affected report to load its recalculated results.

Regression coverage: `tests/test_wb_quarterly_tax.py`, dated tax/report tests,
canonical ABC/P&L tests and the profile/export frontend tests. Tests use disposable
SQLite and synthetic source data; PostgreSQL JSONB comparison is compile-checked,
not a substitute for a PostgreSQL integration run.
