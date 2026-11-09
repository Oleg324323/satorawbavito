# T4 — Clear unavailable Repricer statistics aggregates

Follow-up to `311458a`, independently identified during its critique.
Loading/error previously replaced table rows but retained earlier KPI values,
trend classes and the filter-summary count. Those values could belong to a
previous query/session and must not look current.

`clearLiveRepricerStatsAggregates` now clears only the Repricer statistics
root: KPI values become `—`, delta text/classes and filter-summary text are
removed. Both loading and error renderers invoke it. Successful responses still
populate aggregates normally; the prior generation/disposal guard prevents late
responses from restoring them. No zero values or inferred readiness are used
for the unavailable state.

## Verification

- Expanded real-bridge browser fixture includes all four KPI slots, seeded
  stale values/deltas/classes, summary and rows. Actual RED: 0 passed / 6 failed
  (`/tmp/satorna-t4-stats-kpi-red.json`), with old values observed on loading
  and after logout.
- Focused GREEN: all six browser scenarios passed. Includes current error,
  initial loading, populated response, logout, stale success/error, pagination
  and exact query/page sequence. All requests intercepted; synthetic GET only.
- Final full suite: 288 passed / the same 21 failed / 0 pending, versus prior
  287 / 21 / 0. Exact failed identities match; no new failures. Report
  `/tmp/satorna-t4-stats-kpi-full.json`. These failures remain unwaived debt.
- TypeScript `tsc -b --noEmit` and Vite build exit 0; existing large-chunk
  warning remains. `git diff --check` exits 0.
- Independent read-only critic: scoped PASS, no important new defects. No
  CodeRabbit used. Full React lifecycle remains outside this fixture's proof.

This closes the KPI/summary limitation of the prior response-isolation report.
It still does not prove the entire React routing/auth lifecycle, account
authorization, production behavior or architecture completion. No source HTML,
generated snapshot, backend, schema, flags, provider or production changes.
