# Generic filtering and retained Digest renderer verification

This batch changes tests only. No production demo loader is revived, no new
manager/stock/aggregation policy or source readiness is inferred.

## Generic filters

The prior test waited for automatically generated stock rows; the runtime
explicitly disables that old renderer. The test now first proves no generated
stock rows, then supplies twelve synthetic DOM rows with exact SKU, manager code,
warehouse, stock coverage and KTR fields. The real controls, listeners, filter
functions and empty-state/reset behavior remain under test. Search must return
exactly two rows and manager selection exactly six, avoiding vacuous every-row
assertions. The twelve-row reset remains checked.

The first run exposed test startup before delayed listener binding: OOS showed
all twelve rows. The fixture now waits for actual search/chip binding metadata,
not a larger arbitrary delay. The second run exposed missing operational tags
in the synthetic P&L rows, correctly filtered out by its default source chip;
the fixture now explicitly supplies that source tag. Neither diagnostic failure
was fixed by weakening the runtime filter or suppressing assertions.

## Digest

The four old local period buttons no longer exist. This legacy-renderer test
now states that explicitly and selects retained synthetic scenarios through the
existing renderer entry point. Empty, 1/7/14/30-day point counts, grouped labels,
tooltip contents and narrow-viewport overflow checks remain. It does not claim
that these retired controls are present in the active React page.

Five additional pure tests exercise the actual aggregation and sum helpers:
empty, one, fourteen, fifteen and thirty days, all three metrics, unchanged input,
and the last partial bucket. An in-memory mutation dropping incomplete buckets
produced two instead of three buckets for fifteen days and was rejected. Source
files were not mutated. This characterizes the retained prototype algorithm,
not canonical aggregation policy.

## Final evidence

- Selected legacy generic-filter/Digest: **2 passed, 12.91s**, natural exit 0;
  other 26 tests unselected by this command, no source skip added.
- Pure aggregation5 + existing stock boundaries25: **30 passed, 0.72s**.
- TypeScript `tsc -b --noEmit`: exit 0; `git diff --check`: exit 0.

HTTP(S) requests are aborted, service workers blocked, browser closes in finally.
No print/export button is clicked. Gates ran in the explicitly admitted serial
T4 interval. Main critical pass checked identity counts, source tags, listener
readiness, untouched runtime and retained later assertions. No independent final
approval or full-suite green is claimed. A new whole frontend run remains needed.
