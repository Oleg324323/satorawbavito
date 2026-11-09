# T4 independent frontend execution

User explicitly requested continuing all independent T2–T4 tasks instead of
waiting for T1 as a whole. T2 and T3 were resumed directly in their existing
owner worktrees. T4 remains frontend owner; no competing schema/backend edits.

## Coordination, not independent acceptance

- T2 reported starting strict raw JSON receipt validation, with remaining pure
  command contracts/test debt available. Job/receipt DB consumers require T1.
- T3 reported proceeding with Orders0067 provenance consumer acceptance and
  strict immutable-run decoder. Production P1 storage still requires T1.
- Root coordinator was informed of the new user directive, not asked for a
  per-slice merge or activation. Exact final commits/tests remain owner outputs.

These are attributed owner reports, not T4 verification of backend work.

## Avito overview actual-page proof

New avitoOverviewBrowser.test.ts mounts actual React/generated shell with synthetic
auth and all requests intercepted. Two scenarios start with a populated item,
open/close its actual detail modal, then choose the seven-day period. Held second
response proves old item absence and seven loading KPI placeholders. Empty and
503 responses have distinct visible states. Logout clears all seven KPI values
and makes no new request. Exactly two overview and two chats GETs; other requests
aborted (image/font content locally stubbed).

Browser Date fixed to2026-09-09T12:00Z and Europe/Moscow timezone, with real timers
unaffected; exact expected range2026-09-03..09 asserted, not only duration.
Removed obsolete loading-copy/date-input implementation assertions, retaining
backend loader and no-mock guards. Current overview UI uses preset tabs.
Initial2PASS; final fixed-clock2PASS. Critic found no important defects; its exact
calendar limitation was addressed with the pinned clock/range check.
This is not reversed response-order, real server auth or provider acceptance.

## Secondary renderer ownership proof

New secondaryReportProtectionBrowser.test.ts exposes the actual bridge only in
an in-memory Vite test transform; no runtime export/file modification. A controlled
legacy renderer attempts writes to all five protected table bodies, week, and an
unprotected root. Tests assert protected DOM node identity/content, week snapshot
capture without replacement, unprotected writes, PNL filter suppression, no double
installation/publication, and restoration of native innerHTML descriptor/filter
after ordinary completion and thrown exception. Normal post-call writes still work.

Guard-drop mutation actually fails both cases by replacing all five protected
rows; normal runtime passes both. Removed only the old exact four-element Set
source spelling; repricer-stats is also protected. Whole React lifecycle, unusual
nonconfigurable browser descriptor fallback and alternative DOM mutation APIs are
outside this bridge test's proof.

## Threshold-copy source audit

Kept the complete forbidden-copy scan and positive checks. Two stale exact
spellings now allow their observed equivalent threshold wording: encoded less-than
or plain-language below for OOS; capital/lowercase logistics/DRR threshold text with
optional profile suffix. No application text or business thresholds changed.
Actual first focused run exposed the second stale assertion; both corrected
only after inspecting the exact HTML (OOS10652/10657, logistics/DRR9173).
Focused targeted case passes; unselected cases in this filtered run are not
called suite skips or acceptance. Full-suite evidence recorded separately.

## Verification checkpoint

Combined new overview/secondary browser cases:4PASS. First broad checkpoint:
366PASS/12FAIL/0pending versus358/16, exact four old failed IDs closed and no new
failed IDs; naturalexit1. This predates the strategy invalid-example update and
the separate in-progress Ads/Stock period test. TypeScript remains running;
no final combined counts inferred. No app production build needed for this
test-only batch. No production, providers, migrations, credentials, flags, exports,
printing, application DB or CodeRabbit actions. Only synthetic local browser data.

Remaining independent candidates: RNP current-cache behavior, real listings/detail
flows, and replacing obsolete standalone mock fixtures with
actual React behavior. Do not restore legacy mock data to make tests pass.

Final checkpoint for this batch (before the separate RNP cache test):
**373PASS/7FAIL/0pending**, naturalexit1, versus358/16 before the batch. Exact
failed-ID comparison closes nine old cases, with zero new failed IDs. The moved
Reviews case is replaced by one passing explicit-fixture case, not a skipped test.
Final `tsc -b --noEmit` and `git diff --check` exit0. No full-success claim: remaining
three Avito source cases, RNP copy case and three broad static report/digest cases
remain unwaived. Independent read-only batch critique found no unjustified guard
weakening; no CodeRabbit.

## Ads/Stock period scope and Week evidence

Actual mounted Ads and Stock tests now pass. A changed own period plus unrelated
RNP event makes no GET; the own event sends exactly one new request with the
Aug8–14 range and campaign/SKU grouping. Separate owner-scoped in-memory guard
mutations each produce exactly one intended unrelated-request failure; the other
report remains passing. Fixed browser Date avoids default-date clamping drift.
This is a synthetic event-bridge test, not the calendar UI or backend scope proof.

Week source assertions retain the debug type/helper and actual cache failure
evidence. Removed obsolete two-stage job/report metadata expectations. Existing
committed reportLoadingBrowser cases assert visible actual cache503/error/session
states, so no obsolete request flow is restored. Active-surface tests retain
request/period guards while relying on real route tests for absence of unrelated
product/worker requests instead of removed provider variables/component internals.

## Legacy Reviews explicit fixture

Moved the legacy drawer/settings case to legacyReviewsBrowser.test.ts with all
meaningful original assertions preserved: brand, rows/queue, medium risk reason,
manual approval guard and two disabled buttons, low-risk scheduled guard/send
button enabled, Escape and three settings labels. Runtime HTML/REVIEWS remain
unchanged. The test first proves empty tbody/queue/empty-state, then injects two
typed synthetic rows into browser memory and invokes the original renderer.
No approval/send/save/generate/export is performed. One focused case passed;
this proves only legacy presentation parity, not authorization/durable delivery.

## Independent T2 verification

T4 separately ran the four receipt JSON/decoded/override/assignment tests in the
T2 owner worktree under env-i, PYTHONDONTWRITEBYTECODE, disabled plugin autoload,
explicit offline plugin and the T1 all-network-denied sandbox. Actual228PASS0.27s,
naturalexit0, pytest cacheprovider disabled. This confirms the reported pure
group, not the later DST change, DB consumers or whole T2 completion.

Then separately inspected exact baee618 buyer-price UTC elapsed-time diff and ran
test_wb_repricing_price_inputs.py under the same offline sandbox:38PASS0.22s,
naturalexit0. Confirms the scoped DST/fold/future/overflow cases; no active price
application or broader integration was run.

## Local strategy form test

Read-only critic checked actual backend contracts and T2 committed counterpart:
repricer_sprint_c StrategyVersionCreateRequest capPct/revenueMaxStepPct allow7;
control_plane RepricerTypedSettings maxPriceStepPctPerHour allows7. Six percent
is an execution default, not a universal ceiling. Main independently inspected
both actual schema definitions and HTML validation/save implementation.

The failing test is only local STRATEGIES presentation-state create/edit, not
backend create/update or price application. Replaced invalid example7 with-1,
preserving name, basket-order, save/edit and bulk-card assertions. Targeted
browser case passes. Added interception blocking every request except the exact
local file navigation. Runtime bounds unchanged; no acceptance of0..50 as a
universal domain policy, no attempt to change execution guards.
