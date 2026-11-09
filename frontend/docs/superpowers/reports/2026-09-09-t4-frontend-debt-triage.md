# T4 — frontend debt: first verified correction and remaining gates

This is a work ledger, not approval of baseline failures. No runtime frontend
code changed in this slice; no missing backend API is invented.

## Verified ABC auth test correction

`abcAuthHelpSource.test.ts` required a literal standalone
`if (state.authExpired) return null`. The real KPI guard also checks loading,
missing report and missing rows. The literal assertion failed despite the
auth guard being present. Reproduced before change: 1 failed / 1 passed.

Replaced only that brittle assertion with real `AbcKpiStripIsland` rendering:
loaded rows first render visible KPIs, then the same rows with expired auth
render nothing. Other existing panel/help checks remain. The new test does not
claim to exercise the entire authentication flow or every report block.

Mutation check: temporarily remove only the KPI auth guard; new test fails.
Restore the guard; 65 focused tests pass. Production file has no final diff.
Independent critic: scoped PASS. Direct `tsc -b --noEmit`: exit 0, without
regenerating the HTML snapshot.

Fresh full Vitest: **266 passed / 27 failed / 0 pending**, versus previous
264 passed / 28 failed / 0 pending. Exact failure-ID comparison: **0 new,
1 resolved**. Full suite exit 1, not green. JSON reports:
`/tmp/satorna-t4-clock-after.json`, `/tmp/satorna-t4-auth-after.json`;
mutation evidence `/tmp/satorna-t4-abc-auth-mutation.json`.

## Remaining failures — investigation queue

These are classified by what their failing assertion measures, not by presumed
acceptability. Source assertions can mask real missing behavior. None below is
waived, deleted or skipped.

| Group | Requirement to preserve | Next verification / dependency |
|---|---|---|
| Avito live wiring (8) | Repricer settings, explicit period apply, overview/listings/stats, details, Orders and Notifications | Trace current hooks and route islands; replace obsolete source snippets only with behavior evidence. Canonical switch waits for approved domain HTTP contracts. |
| Performance source (1) | Route-scoped loading, no unnecessary strategy refetch | Verify actual effect dependencies and request counts on navigation/account change. |
| Report help (1) | Understandable expense headers/formulas | Verify rendered help, not exact JSX formatting. Do not invent financial definitions. |
| Repricer stats source (1) | Backend stats selected by active route | Trace current route and load path; preserve T2 ownership of canonical commands. |
| RNP loading source (1) | Cache/results bound to selected period | Verify period changes, stale response suppression and exact cache key. |
| Stock/week state source (2) | Useful loading/empty/error states | Resolve old blanket ban on progress panels against actual displayed state; no blanket suppression of errors. |
| Static-source assertions (8) | Live report paths, active-tab ownership, no mock fallback, threshold wording, legacy boundaries | Inspect current extracted code and generated snapshot source; no reintroducing demo rows to satisfy old text. |
| Static HTML browser cases (5) | Report copy, filters, digest chart, strategy create/edit, Reviews UX | These load local `public/vella-production.html` via file URL, not authenticated React application. Establish prototype versus integrated-route expectations; do not infer production parity from either alone. |

The five browser cases currently fail on tax-base wording, absent stock rows,
missing digest chart items, strategy editor expectation and Reviews timeout.
They need individual behavioral diagnosis. No claim yet whether each is a stale
fixture or a real defect. Canonical Reviews/Orders/Production and runtime proxy,
session and readiness integration still wait for actual committed contracts.

## Avito notification toolbar correction

Independent review identified the obsolete `Обновить Avito` source assertion.
The actual toolbar already renders the shared `AvitoRefreshButton`, whose label
is `Обновить данные` and tooltip is `Обновить данные Авито`. Its existing callback
still invokes the Avito notification refresh bridge.

Replaced only the obsolete literal with a real toolbar SSR test under MemoryRouter
at `/avito/notifications`, checking the refresh control and read-all control.
Other backend/action/no-static-mock assertions remain. The island receives a named
export for direct rendering; runtime behavior and callbacks do not change.
Original selected checks: 1 failed / 1 passed. Mutation removing the actual refresh
component: the new rendering test fails; restored component has no behavioral diff.
This is toolbar-render evidence, not an executed read-all/refresh action, session
integration, provider call or durable receipt proof. No external action was invoked.

After restoring the component: selected notifications checks 2 passed (the `-t`
selection excludes unrelated cases, not committed skips). Typecheck: exit 0.
Independent scoped critic: PASS. Fresh full suite, including both safe marking
cleanup slices: **274 passed / 26 failed / 0 pending**, exit 1; versus the preceding
266/27 report, **0 new failure IDs, 1 resolved**. New cases account for the additional
passes. Report: `/tmp/satorna-t4-notifications-after.json`. The next SKU browser
fixture was created after this full run began and is verified separately.

## Expense header help — behavior instead of an obsolete label

The expense table already uses `Статья расходов` rather than the old `Статья ДДС`.
Original focused run: 2 pass / 1 fail on the obsolete source literal. The new
assertion renders the real nonempty expense table and requires all nine headers
to carry nonempty help tips, while retaining the other reports' formula checks.
The component gains only an export; no formula, handler or displayed copy changes.
Mutation replacing the first help header with plain th fails the new test; restored
focused set is 3 pass. Independent scoped critic: PASS; this is SSR, not tooltip
interaction proof. Direct typecheck and diff check exit 0.

Fresh full suite after shared-HTML compatibility: **278 pass / 25 fail / 0 pending**,
exit 1. Against 277/26: zero new IDs, exactly the expense help test resolved.
Reports `/tmp/satorna-t4-help-full.json`, `/tmp/satorna-t4-report-help-mutation.json`.
All remaining 25 failures are still unwaived.

## Source scanner scope — test references are not shipped components

The deprecated-component guard reported exactly five test files referencing
KpiStrip; no ordinary runtime file was an offender. Exclude `.test/.spec.ts(x)`
from this existing runtime-source scan instead of excluding only the scanner's
own test. Existing runtime search and parity-file exception remain unchanged.
Targeted RED1 → GREEN1. Temporary ordinary `sourceGuardMutation.ts` containing
ReportLayout makes the same guard fail again; removed that synthetic file and
ran the full suite. No production source change or permanent test skip.

Fresh full suite **279 pass /24 fail /0 pending**, exit1: against278/25, zero new
IDs, only the deprecated-component guard resolved. Typecheck exit0; independent
critic scoped PASS. JSON `/tmp/satorna-t4-source-guard-full.json`; before/after/
mutation reports share `/tmp/satorna-t4-source-guard-` prefix. Remaining24 failures
are unwaived. This scan is an architecture guard, not proof of rendered UI parity.

## ABC live bridge — real results instead of obsolete URL/loading copy

The old source test expected `/api/wb/reports/abc?`; the actual legacy bridge uses
the period-scoped latest-cache path, while enabled canonical uses the separate v2
adapter. Original selected test1FAIL. Replace obsolete URL/loading-copy checks with
actual installAbcLiveDataBridge execution under a fully synthetic fetch boundary:
one backend row only, empty response,500 error, then null session. Empty/error/auth
retain no rows; error clears report and finishes loading; null auth makes no request.
All three requests are GET/latest-cache with the selected dates. No job/provider call.

Keep all six independent no-demo source guards: the new bridge test does not prove
that a downstream renderer cannot fabricate fallback rows. Critic identified this
coverage gap and the guards were restored; scoped fix review PASS. Mutation inserting
a demo row in actual bridge error branch makes the test fail; runtime restored.

Fresh full run280PASS/23FAIL/0pending, versus279/24: zero new IDs, only ABC source
case resolved, exit1. The full run started before the six guards were restored;
final focused rerun with them passes1/1 and typecheck passes. Full report
`/tmp/satorna-t4-abc-source-full.json`, finalfocused `-reviewed.json`, mutation
`-mutation.json`. No runtime source diff; no full green or renderer parity claim.

## Avito schedule modal — open/cancel behavior

Actual settings panel uses `Настроить время`/openScheduleSettings, not the obsolete
worker/openSettings modal copy required by the source test. Remove only those three
obsolete assertions, retaining strategy-table/history/pending-cell assertions.
New real-browser case mounts the existing panel (export-only runtime change) with
two complete synthetic read responses. It opens the schedule dialog, reads60,
chooses30, cancels, reopens60 and closes. No save or approval is invoked; any
non-GET/nonfixture request is aborted and fails the test. Worker/apply flags false
in synthetic responses. This is not panel-in-page integration or backend parity.

Original selected source1FAIL; browser1PASS. Mutation disconnecting actual open
handler makes the modal assertion fail (3000ms timeout); restored selected2PASS.
Independent critic scoped PASS; TypeScript0 and Vite production build0 (existing
large-chunk/plugin-time warnings). Source HTML/generated payload unchanged.

Fresh full **282PASS/22FAIL/0pending**, exit1 versus280/23: zero new failure IDs,
only Avito strategy/settings source test resolved, plus new browser case. This
run also includes restored ABC no-demo guards from4fd9156. JSON evidence prefix
`/tmp/satorna-t4-avito-settings-`: before/browser/mutation/after/full. Remaining22
failures are unwaived; no skips or blanket snapshots introduced.

## RNP — actual stale-period defect

Unlike the obsolete call-string assertion, an actual mounted-browser test reproduced
old ready rows remaining visible during a new period request. Scoped state now hides
them immediately and fences obsolete setters by session/organization/period. The
test also checks exact GET parameters and logout clearing without another request.
Two source cases replaced by one browser case; six other RNP/Ads guards retained.
RED old runtime → GREEN new runtime; independent scoped review PASS.

Fresh full **282PASS/21FAIL/0pending**, exit1 versus282/22: no new failure IDs,
one obsolete RNP source failure resolved. Evidence /tmp/satorna-t4-rnp-period-full.json;
details in 2026-09-09-t4-rnp-period-isolation.md. Still no canonical RNP cutover or
full release approval. Out-of-order/polling/org-switch races are not browser-proven.
