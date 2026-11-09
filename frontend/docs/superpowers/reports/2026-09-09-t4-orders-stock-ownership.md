# T4 Orders request ownership and Stock filter evidence

## Orders

Actual React browser evidence exposed a second, legacy hydration of the same Avito
screen: wrong historical date, omitted page, duplicate returns status read and an
unexpected production-SKU read. Every request was intercepted locally. The legacy
function also owned fallback DOM writes, although React already owned the screen.

The HTML source now returns before any state/helper/read/render operation only
when the requested source is Avito and the exact React-owned root is present.
Standalone Avito and WB current/archive behavior are retained. The generated
snapshot is produced by the existing generator, never manually patched.
Pure VM tests extract the actual function and use local helper stubs:
main5PASS/1FAIL before guard →6PASS after. No actual export or DOM renderer runs.

An HTTP503 previously fell through to extension onboarding because its error
component was nested inside the nonempty-row branch. Loading/error now use the
existing data-state UI outside that branch. Successful zero-row onboarding is
deliberately unchanged: its product policy was not inferred from this bug.
Provider error payload is not echoed. Actual page verification: three cases pass
for populated/logout, empty and503; exactly one Orders GET and the two expected
auxiliary GETs, no other API requests. Close/external/print/export actions are not
invoked. Four obsolete provider/debug-copy clauses are removed from source tests;
route/loader/mapping/no-production-sync checks remain.

Limits: no reverse navigation/in-flight legacy cancellation proof, no complete
account/auth isolation, no command/export/print parity. The ownership guard fences
entry, not a legacy request that started before React acquired the screen.

## Stock

Actual three-row React test reaches a real RED at the risk chip: rows have no
metadata consumed by the existing legacy filter. Its visible Russian chip label
also fails the old OOS-only branch. Missing days default to999, incorrectly
classifying unknown evidence as excess.

React rows now expose their finite days/KTR, actual warehouse and decision fields.
No managers, promotions, source tags or extra warehouse ranking are invented.
The current risk label maps to the existing OOS rule; missing/empty days remain
unknown instead of999. Existing7-day/60-day and top-warehouse rules are unchanged.
Pure exact-function tests:8PASS/3FAIL→11PASS; with Orders guard:17PASS. Critic
found a zero-stock/null-days omission: added actual available-units evidence and
13PASS/1FAIL→14PASS; unknown remains unknown. Final Stock browser plus14pure cases:
15PASS. The first otherwise-passing browser run exposed two missing local brand
SVG fixtures; added only those exact GET paths, retaining strict API denials.
TypeScript and Vitebuild both exit0 after final runtime edits. Broad405PASS/4FAIL:
three old source tests and one reportLoading logout readiness race (heading already
visible before auth transition). Same exact session-text assertion now polls;
focused post-fix check:4PASS/0FAIL, naturalexit and browsercleanup after direct
T1/T2/T3 admission. No whole-suite retry:405/4 remains the actual broad snapshot,
not rewritten as an inferred406/3. No full green claim.
The browser test must retain exact SKU sets for risk/excess/top-warehouse,
combined search, empty/reset and no refetch. Positive promotion and manager
parity are expressly not established by source rows lacking that evidence.
Boundary caveat: legacy frontend <=7/>=60 differs from backend decisions <7/>60.
This metadata fix retains those existing boundaries; exact boundary alignment is
a separate identified follow-up, not covered by the away-from-boundary fixtures.

## Resource queue

Root assigned one sequential heavy slot after direct T1/T2/T3 acknowledgements.
First bounded actual-page batch:7PASS/1StockRED, naturalexit and browser cleanup,
then explicit release to every owner. No parallel build/PG was started. Follow-on
heavy gates require separate owner admission; small pure VM checks do not start
browser/build/database/provider work.

No production, backend/schema, flags, live credentials, actual print/export,
provider calls, push or deployment changes are part of this slice.
