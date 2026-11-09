# Ads active search verification

## Verified scope

The active React Ads toolbar has a search input, not the standalone prototype's
SKU selector. Three synthetic backend campaign rows verify SKU search (two rows),
campaign search (one row), no match (zero), and clearing (three). Exactly one
cache GET uses the supplied dates, campaign grouping and operational source.
All browser traffic is fulfilled or aborted locally; browser closes in finally.

Final exact test-only run: **1 passed, 3.48s, exit 0**. TypeScript
`tsc -b --noEmit`: exit 0. No runtime or generated snapshot changes accompany
this test. Removing the active search filtering would fail the two-row assertion;
this is characterization coverage, not a newly fixed runtime defect.

## Rejected hypothesis and remaining debt

An earlier hypothesis targeted the generic prototype SKU selector's label lookup.
Although a pure helper test reproduced that lookup difference, mounted React
inspection disproved its relevance: the active toolbar has no such select and
the old Ads demo renderer deliberately has no table target. The speculative
helper change and its new test were removed; demo rendering was not revived.
An attempted standalone row test also failed and was removed because it assumed
an intentionally disabled renderer was active. These are diagnostic failures,
not evidence of successful standalone parity.

The old giant source test still fails at its obsolete Ads selector expectation.
Its subsequent assertions and the whole frontend suite have not been rerun.
Last whole-suite evidence remains 405 passed / 4 failed, with later scoped fixes
documented separately; no inferred whole-suite green or build claim.

Main-agent critical pass checked actual row identity, positive/negative search
controls, exact request scope, network containment and browser cleanup. No
independent final review is claimed; integration review remains required.
