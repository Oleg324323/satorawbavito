# T4 — removal of unused marking workspace, first slice

Coordinator relayed the user's new scope: KIZ and the separate matcher are not
required. Remove only confirmed unused surfaces; preserve core WB/Avito operations,
Catalog resolution, return matching, historical evidence and real data.

Removed in this bounded slice:

- `/orders/kiz` ComingSoon route and unused route constant.
- Marking navigation item and its unused icon import.
- Prototype print-list marking field, four placeholder values and table column.

The orders and returns routes/navigation retain their previous role policy.
Product, quantity and WB sticker columns remain. No handlers, backend, schema,
provider, printing/export action, shared HTML or user data were changed.
The removed source is recoverable in Git; this is not deletion of stored records.
Search across frontend src/api/scripts found no remaining runtime consumers of
`orders.kiz`, `/orders/kiz` or `orders-kiz`; only regression assertions remain.

Verification: real SSR print-list test failed on the old marking column, then
passed after removal. Navigation test failed on the actual old marking item,
then passed; its initial incorrect owner-role assumption was corrected rather
than widening runtime permissions. Focused navigation/print-list/ABC set: 13 pass.
Direct `tsc -b --noEmit`: exit 0. Git diff check: exit 0. Full suite was not
rerun for this removal; the preceding test-only slice recorded 266 pass / 27 fail.
These tests do not prove actual printing, backend orders or hosted rendering.
Independent scoped review: PASS. The old URL now reaches the application's
existing generic wildcard ComingSoon page, not a new 404; no KIZ-specific route
remains. The generic unmatched-route behavior is outside this removal slice.

## Remaining at the first slice (updated by the follow-ups below)

- Settings prototype module label and Notifications prototype blocked-action copy.
- Repricer prototype navigation counters and future marking gate.
- Avito picking table: marking header maps to an always-empty cell; remove header,
  matching cell and adjust empty-state colspan together, preserving return badge,
  sticker designer and Avito identifiers.
- Shared Vella source HTML/generated snapshot: inspect generator ownership and
  regenerate from source, not a regex deletion from embedded shared markup.
- `vellaBackendContracts.ts`: `kiz_marking` and the combined quality source reference
  appear only in this frontend file in the searched app/contracts scope. Verify
  remaining hidden/card/penalty/return requirements before changing the registry.

No need for a new backend marking contract is inferred. Historical documentation
and source evidence are retained. This slice is not a claim that every marking
reference has been removed.

## Follow-up: independent copy and prototype navigation

Removed the marking module from the prototype production user's displayed module
list, the invented marking prerequisite from a prototype order notification,
and the two static repricer marking counters/navigation sections. The removed
sections contained no other entries. Account IDs, dangerous permissions, price
controls and other notification restrictions are unchanged. `blockedActions` is
descriptive UI content here, not a print execution policy.

Four new real projection/SSR cases reproduced failures before the change;
combined settings/notifications/repricer/navigation/print-list tests: 26 pass.
Independent scoped review: PASS, no important findings.
Direct typecheck and Git diff checks after this follow-up: exit 0.

Shared HTML dependency discovery: `ordersIsProblemStatus` includes the historical
`missing_honest_sign` state and affects problem/ready filtering. `honestSign` also
participates in manual/edit fields, drawer external-code display and the sticker
code fallback. It is not proven to be a backend execution gate. These are not
safe targets for blind string deletion or automatic status conversion. Preserve
historical fields/data and barcode fallback until a compatibility-tested removal
of exposed controls is implemented. No additional marking service is required.

## SKU future-gate removal with actual browser interaction

Removed only the decorative marking row from `WbRepricerSkuPage` Orders tab.
The regression builds the actual component into an in-memory test-only IIFE with
existing Vite/React dependencies (`write:false`, `configFile:false`, `envFile:false`),
mounts it in Playwright, and clicks the Orders tab. Browser requests are aborted;
no auth, print/export, provider or backend action is invoked. Fixture remains under
test sources and does not add an application route or endpoint.

Harness initialization failures (`process`/JSX development-runtime mismatch) were
diagnosed separately and are not counted as product RED. After matching the test
runtime, the test failed specifically on the existing KIZ row; after its removal,
27 focused cases passed, including the browser test. Independent review: scoped
PASS. Positive assertions retain the estimate/time/deadline rows, not proof of
their numeric business formula. Shared HTML fields/statuses remain unchanged.
Direct typecheck: exit 0. Full suite last recorded before this browser slice:
274 passed / 26 failed; no new full-suite result is inferred from focused checks.

## Avito picking column removal with loaded and filtered states

The actual Avito consumer renders the picking table only when browser-snapshot
metadata and rows are present. Empty initial SSR is not coverage of that branch;
the exploratory SSR-only test was discarded without changing runtime behavior.

The new browser fixture mounts the actual `AvitoOrdersIsland` with a synthetic
auth context and intercepts the three existing read-only legacy endpoints on a
reserved `.test` origin. The complete synthetic order response exercises the
real fetch/effect/render path; it is not a new canonical endpoint or live backend.
All non-GET, unexpected-path or other-origin requests are aborted and recorded.
Only the order-page HTML and existing GET fixtures are fulfilled; printing,
export, token rotation and return-sync actions are not clicked.

RED failed specifically on the loaded table's marking header. Removed that
header and the matching always-null cell; empty-state colspan changes 16 to 15.
The test verifies all header/row column counts and existing job/quantity/sticker/
barcode/Avito item positions, then types an unmatched search and verifies the
empty row's colspan and message. Return matching and sticker callbacks are unchanged.
The component gains only a named export for the browser fixture.

Fresh focused set: 12 passed, including both actual-browser regressions. Direct
typecheck and Git diff check: exit 0. No unexpected requests or page errors in the
Avito fixture. Current remaining non-test source references are in shared HTML /
generated snapshot and `vellaBackendContracts.ts`; their compatibility assessment
remains separate, and no stored field or historical status was erased.
Independent scoped review: PASS. These browser checks do not claim live backend
authorization, complete account isolation, hosted parity or successful printing.

## Shared HTML compatibility cut

Removed the shared picking table column, matching edit field, manual single/table
inputs and the option to assign the retired marking status to new orders. Existing
`missing_honest_sign` is explicitly preserved by normalization and offered only
when already selected on a historical row. It remains in the problem filter and
cannot silently become ready. Historical fields, sample records, search, drawer
code display, sticker fallback and WB/Avito status identities are retained.
The false promise of a separate marking service becomes an explicit historical
status warning. Settings prototype scope and desktop table description no longer
advertise the removed UI; other scopes/permissions are unchanged.

The browser regression loads the full source HTML on a reserved synthetic origin,
blocks every other request, and evaluates the real normalize/render/read-edit/
finalize functions. No save, print, export or provider action is invoked. Pure
sticker markup is checked for preserved historical code without opening a print
window or downloading a document. It checks 15-column alignment, unchanged old
status/code, absent new controls and retained normal sticker/barcode inputs.
RED reproduced the old selectable status and stale desktop copy; final test passes.
The generator was run intentionally after the source change. Independent critic
verified exact embedded HTML/hash parity and found no important issue.

Fresh full suite: **277 passed / 26 failed / 0 pending**, exit 1. Compared with
274/26: zero new or resolved failure IDs; three new browser cases explain passes.
Report `/tmp/satorna-t4-shared-marking-full.json`. The 26 failures remain unwaived.
Initial new-test typecheck failed because string-based page.evaluate returned
unknown; a typed result contract fixes that test-only issue. No application
type weakening, dependency or new HTTP endpoint was introduced.
Fresh direct `tsc -b --noEmit` and diff check after the correction: exit 0.
